"""Bounded shared investigation and batched independent release decisions."""
import asyncio
import json
import logging
import os
import time
from collections import Counter

from langgraph.graph import StateGraph, START, END
from langchain_core.messages import SystemMessage, HumanMessage
from langsmith import traceable
from src.agent.state import AgentState
from src.agent.prompts import USER_QUERY_UNDERSTANDING_PROMPT, INVESTIGATION_POLICY, BATCH_DECISION_POLICY
from src.agent.mcp_client import decode
from src.schemas.request import QueryUnderstanding
from src.schemas.response import BatchDecision
from src.services.validation import source_links, target_from, overall, explicit_scope_mismatches
from src.services.batching import expected_decisions, store_evidence, coverage_by_requirement, evaluation_keys

logger = logging.getLogger(__name__)

# Only the evidence rules; the tool-selection section is irrelevant (and costs tokens) at decision time.
EVIDENCE_RULES = INVESTIGATION_POLICY.split('\n\nTOOL EFFICIENCY')[0]


def slim_issue(i):
    return {k: i.get(k) for k in ('issue_key', 'source_id', 'record_type', 'summary', 'status', 'description')}


def slim_comments(c):
    keys = ('comment_id', 'recorded_timestamp', 'recorded_actor', 'recorded_role', 'body')
    return {'issue_key': c['issue_key'], 'comments': [{k: x.get(k) for k in keys} for x in c['comments']]}


def slim_links(l):
    keys = ('issue_key', 'source_id', 'record_type', 'summary')
    return {'issue_key': l['issue_key'], 'linked_issues': [{k: x.get(k) for k in keys} for x in l['linked_issues']]}


def slim_issue_ref(r):
    return {k: r.get(k) for k in ('issue_key', 'source_id', 'summary')}


def retry_after(exc):
    """Seconds to wait if exc is a provider 429 (uses Retry-After header), else None."""
    if getattr(exc, 'status_code', None) != 429:
        return None
    try:
        return min(float(exc.response.headers.get('retry-after', 10)), 60.0) + 0.5
    except Exception:
        return 10.0


class Runner:
    """One isolated instance per API request. Query understanding executes once."""

    def __init__(self, session, llm, tools):
        self.session = session
        self.llm = llm
        self.trace = []
        self.tokens = {}
        self.calls = 0
        self.model_calls = 0
        self.cache_hits = 0
        self.partial_decisions = []
        self.target = None
        self.expected = set()
        self.evidence_cache = {}
        self.enable_cache = os.getenv('AGENT_ENABLE_CACHE', 'true').lower() == 'true'
        self.max_calls = int(os.getenv('AGENT_MAX_TOOL_CALLS', '200'))
        self.max_model_calls = int(os.getenv('AGENT_MAX_LLM_CALLS', '20'))
        self.max_rounds = int(os.getenv('AGENT_MAX_INVESTIGATION_ROUNDS', '10'))
        self.batch_size = int(os.getenv('AGENT_REQUIREMENT_BATCH_SIZE', '2'))
        self.max_batch_issues = min(10, max(1, int(os.getenv('AGENT_MAX_BATCH_ISSUES', '10'))))
        self.timeout = float(os.getenv('AGENT_TIMEOUT_SECONDS', '900'))
        if min(self.max_calls, self.max_model_calls, self.max_rounds, self.batch_size, self.timeout) <= 0:
            raise ValueError('Execution limits and batch size must be positive')
        self.started = time.monotonic()
        self.tools = [{'type': 'function', 'function': {'name': t.name, 'description': t.description or '', 'parameters': t.inputSchema}} for t in tools]
        self.names = {t.name for t in tools}

    def remaining(self):
        return max(0.01, self.timeout - (time.monotonic() - self.started))

    def budget(self):
        return self.calls < self.max_calls and time.monotonic() - self.started < self.timeout

    @traceable(name='Jira MCP tool', run_type='tool',
               process_inputs=lambda inputs: {k: inputs[k] for k in ('name', 'args', 'phase') if k in inputs})
    async def call(self, name, args, phase):
        if name not in self.names:
            raise RuntimeError('Unknown tool')
        cache_key = name + ':' + json.dumps(args, sort_keys=True)
        start = time.monotonic()
        entry = {'kind': 'tool', 'phase': phase, 'tool': name, 'arguments': args, 'cache_hit': False}
        try:
            if self.enable_cache and cache_key in self.evidence_cache:
                self.cache_hits += 1
                entry['cache_hit'] = True
                result = self.evidence_cache[cache_key]
            else:
                if not self.budget():
                    raise RuntimeError('Tool or execution budget exhausted')
                self.calls += 1
                # Composite batches may require multiple Jira REST requests. Respect the overall deadline.
                tool_timeout = 180 if name == 'get_batch_evidence' else 60
                result = decode(await asyncio.wait_for(self.session.call_tool(name, args), timeout=min(tool_timeout, self.remaining())))
                if not isinstance(result, dict):
                    raise ValueError('Invalid tool result')
                if self.enable_cache and result.get('complete', True) and not result.get('possibly_truncated', False):
                    self.evidence_cache[cache_key] = result
            entry['result'] = result
            return result
        except (Exception, asyncio.CancelledError) as exc:
            entry['error'] = type(exc).__name__
            raise
        finally:
            entry['elapsed_seconds'] = round(time.monotonic() - start, 3)
            self.trace.append(entry)

    async def model(self, runnable, messages, phase):
        if time.monotonic() - self.started >= self.timeout or self.model_calls >= self.max_model_calls:
            raise RuntimeError('Model or execution budget exhausted')
        self.model_calls += 1
        start = time.monotonic()
        entry = {'kind': 'model', 'phase': phase, 'call_number': self.model_calls}
        try:
            response = await asyncio.wait_for(runnable.ainvoke(messages), timeout=self.remaining())
            raw = response['raw'] if isinstance(response, dict) else response
            usage = getattr(raw, 'usage_metadata', None) or {}
            entry['usage'] = usage
            for key in ('input_tokens', 'output_tokens', 'total_tokens'):
                if isinstance(usage.get(key), int):
                    self.tokens[key] = self.tokens.get(key, 0) + usage[key]
            if isinstance(response, dict):
                entry['structured_output_valid'] = not response.get('parsing_error') and response.get('parsed') is not None
            else:
                entry['requested_tools'] = getattr(raw, 'tool_calls', [])
            return response
        except (Exception, asyncio.CancelledError) as exc:
            entry['error'] = type(exc).__name__
            raise
        finally:
            entry['elapsed_seconds'] = round(time.monotonic() - start, 3)
            self.trace.append(entry)

    def metrics(self):
        return {'tool_calls': self.calls, 'cache_hits': self.cache_hits,
                'llm_calls': self.model_calls,
                'llm_calls_by_phase': dict(Counter(e['phase'] for e in self.trace if e['kind'] == 'model')),
                'model_usage': self.tokens}

    async def understand(self,s):
        try:
            response=await self.model(self.llm.with_structured_output(QueryUnderstanding,method='function_calling',include_raw=True),[SystemMessage(content=USER_QUERY_UNDERSTANDING_PROMPT),HumanMessage(content=s['user_query'])],'query')
            if response['parsing_error'] or response['parsed'] is None: raise ValueError('Query parsing failed')
            q=response['parsed'].model_dump(mode='json')
            if q['intent']!='release_assessment': raise ValueError('This endpoint supports full release assessment only')
            if q['release'] and q['release']!=s['target_release']: raise ValueError('Query and configured release conflict')
            return {'query':q}
        except Exception as e: return {'error':str(e)}
    async def discover(self,s):
        try:
            result=await self.call('search_issues',{'release':s['target_release'],'record_type':'manifest','max_results':5},'manifest')
            if result['possibly_truncated'] or len(result['issues'])!=1: raise ValueError('Expected one manifest')
            key=result['issues'][0]['issue_key']; manifest=await self.call('get_issue',{'issue_key':key},'manifest')
            target=target_from(manifest['description'])
            if target['release']!=s['target_release']: raise ValueError('Manifest release mismatch')
            for qkey,tkey in [('service','service'),('environment','environment'),('commit','commit'),('prompt_version','prompt')]:
                if s['query'].get(qkey) and s['query'][qkey]!=target[tkey]: raise ValueError(f'Query conflicts with manifest: {qkey}')
            linked=await self.call('get_linked_issues',{'issue_key':key},'manifest')
            expected=source_links(manifest['description']); reqs=[x for x in linked['linked_issues'] if x['source_id'] in expected and x['record_type']=='requirement']
            if not expected or len(reqs)!=len(expected) or {x['source_id'] for x in reqs}!=expected: raise ValueError('Manifest requirement links incomplete or ambiguous')
            self.target=target; self.expected=expected
            return {'manifest':manifest,'target':target,'requirements':sorted(reqs,key=lambda x:x['source_id']),'index':0,'decisions':[]}
        except Exception as e: return {'error':str(e)}
    async def gather(self, s):
        """Deterministic evidence retrieval: requirement roots plus their Source Links closure. No LLM calls."""
        issues, comments, links = {}, {}, {}
        seen, frontier = set(), [r['issue_key'] for r in s['requirements']]
        while frontier:
            keys = [k for k in dict.fromkeys(frontier) if k not in seen]
            frontier = []
            for i in range(0, len(keys), self.max_batch_issues):
                chunk = keys[i:i + self.max_batch_issues]
                seen.update(chunk)
                try:
                    data = await self.call('get_batch_evidence', {'issue_keys': chunk}, 'gather')
                    store_evidence('get_batch_evidence', data, issues, comments, links)
                except Exception as exc:
                    # Left as a coverage gap -> INSUFFICIENT_EVIDENCE, never PASS.
                    logger.warning('Evidence retrieval failed for %s: %s', chunk, type(exc).__name__)
            for k in keys:
                if k in issues and k in links:
                    by_source = {x['source_id']: x['issue_key'] for x in links[k]['linked_issues']}
                    try:
                        for src in source_links(issues[k]['description']):
                            if src in by_source:
                                frontier.append(by_source[src])
                    except ValueError:
                        pass  # coverage() reports this as a gap
        cover = coverage_by_requirement(s['requirements'], issues, links, comments)
        self.trace.append({'kind': 'coverage', 'gaps': {r: v['missing'] for r, v in cover.items() if v['missing']}})
        return {'issues': issues, 'comments': comments, 'links': links, 'rounds': 1, 'investigation_finished': True}

    def validate_decision(self, s, req, d, missing, visited, allowed_keys):
        rid, root = req['source_id'], req['issue_key']
        def unknown(reason):
            return {'requirement_id': rid, 'decision': 'INSUFFICIENT_EVIDENCE', 'reason': reason,
                    'supporting_evidence': [], 'rejected_evidence': [], 'outstanding_verification': missing or [reason]}
        try:
            if d is None:
                raise ValueError('Missing or duplicate structured decision for this requirement')
            if root not in s['issues']:
                raise ValueError('Requirement description not retrieved')
            desc = s['issues'][root]['description']
            if target_from(desc) != s['target'] or 'Mandatory: yes' not in desc:
                raise ValueError('Requirement target or mandatory flag invalid')
            if d['requirement_id']!=rid: raise ValueError('Incorrect requirement ID')
            if d['decision'] in ('PASS','FAIL') and not d['supporting_evidence']: raise ValueError('No supporting references')
            if (missing or not s['investigation_finished']) and d['decision']=='PASS': raise ValueError('Incomplete investigation')
            if d['decision']=='PASS' and d['outstanding_verification']: raise ValueError('Outstanding verification prevents PASS')
            for ref in d['supporting_evidence']+d['rejected_evidence']:
                if ref['issue_key'] not in allowed_keys: raise ValueError('Reference was not supplied to this evaluation batch')
                matches=[c for c in s['comments'].get(ref['issue_key'],{}).get('comments',[]) if c['comment_id']==ref['comment_id'] and ref['quote'] in c['body']]
                if len(matches)!=1: raise ValueError('Reference or exact quote not retrieved')
                ref['url']=matches[0]['url']; ref['source_id']=s['issues'].get(ref['issue_key'],{}).get('source_id')
                ref['recorded_actor']=matches[0]['recorded_actor']; ref['recorded_role']=matches[0]['recorded_role']; ref['recorded_timestamp']=matches[0]['recorded_timestamp']
                if ref in d['supporting_evidence'] and d['decision'] in ('PASS','FAIL'):
                    mismatches=explicit_scope_mismatches(matches[0]['body'],s['target'])
                    if 'prompt' not in desc.split('Acceptance criterion:',1)[-1].split('Source Links:',1)[0].lower(): mismatches=[m for m in mismatches if not m.startswith('prompt:')]
                    if mismatches: raise ValueError('Supporting evidence has conflicting explicit scope: '+', '.join(mismatches))
            if d['decision'] in ('PASS','FAIL') and not any(r.get('recorded_role') in ('QA Lead','Security Owner') for r in d['supporting_evidence']): raise ValueError('No authoritative QA or security evidence cited')
            if missing: d['outstanding_verification']+=missing
        except Exception as exc:
            d = unknown(f'Assessment could not be validated: {type(exc).__name__}: {exc}')
        d['requirement_key'] = root
        d['requirement_name'] = req['summary']
        d['mandatory'] = True
        d['investigated_issue_keys'] = sorted(visited)
        return d

    async def decide(self, s):
        batch = s['requirements'][s['index']:s['index'] + self.batch_size]
        allowed_keys, cover = evaluation_keys(batch, s['requirements'], s['issues'], s['links'], s['comments'])
        context = {'target': s['target'], 'manifest_policy': s['manifest']['description'], 'requirements': [slim_issue_ref(r) for r in batch],
                   'issues': {k: slim_issue(v) for k, v in s['issues'].items() if k in allowed_keys},
                   'comments': {k: slim_comments(v) for k, v in s['comments'].items() if k in allowed_keys},
                   'links': {k: slim_links(v) for k, v in s['links'].items() if k in allowed_keys},
                   'coverage': {r['source_id']: cover[r['source_id']] for r in batch},
                   'investigation_finished': s['investigation_finished']}
        candidates = {}
        error = None
        runnable = self.llm.with_structured_output(BatchDecision, method='function_calling', include_raw=True)
        messages = [SystemMessage(content=EVIDENCE_RULES + '\n' + BATCH_DECISION_POLICY),
                    HumanMessage(content=json.dumps(context, ensure_ascii=False, separators=(',', ':')))]
        try:
            for attempt in (1, 2):
                try:
                    response = await self.model(runnable, messages, 'batch_evaluation')
                    break
                except Exception as exc:
                    wait = retry_after(exc)
                    if attempt == 2 or wait is None or wait >= self.remaining():
                        raise
                    logger.warning('Rate limited; waiting %.1fs before retrying batch', wait)
                    await asyncio.sleep(wait)
            if response['parsing_error'] or response['parsed'] is None:
                raise ValueError('Batch decision parsing failed')
            candidates, unexpected = expected_decisions(batch, response['parsed'].model_dump()['decisions'])
            if unexpected:
                self.trace.append({'kind': 'validation', 'unexpected_requirement_ids': unexpected})
        except Exception as exc:
            logger.warning('Batch evaluation failed: %s %s', type(exc).__name__, str(getattr(exc, 'body', None) or exc)[:500])
            error = f'{type(exc).__name__}: batch evaluation unavailable'
            self.trace.append({'kind': 'validation', 'error': error})
        validated = []
        for req in batch:
            rid = req['source_id']
            d = self.validate_decision(s, req, candidates.get(rid), cover[rid]['missing'], cover[rid]['visited'], allowed_keys)
            if error:
                d['outstanding_verification'].append(error)
            validated.append(d)
        self.partial_decisions = s['decisions'] + validated
        return {'decisions': self.partial_decisions, 'index': s['index'] + len(batch)}

    def report(self, s):
        decisions = s.get('decisions', [])
        expected = {r['source_id'] for r in s.get('requirements', [])}
        return {'report': {'target': s.get('target'), 'project_key': s['project_key'],
            'dataset_label': s['dataset_label'], 'requested_release': s['target_release'],
            'manifest_key': s.get('manifest', {}).get('issue_key'),
            'overall_decision': overall(expected, decisions, not s.get('error')),
            'requirements': decisions, 'error': s.get('error'),
            **self.metrics(), 'investigation_rounds': s.get('rounds', 0),
            'investigation_finished': s.get('investigation_finished', False),
            'elapsed_seconds': round(time.monotonic() - self.started, 3)}}

    def build(self):
        g = StateGraph(AgentState)
        for name in ['understand', 'discover', 'gather', 'decide', 'report']:
            g.add_node(name, getattr(self, name))
        g.add_edge(START, 'understand')
        g.add_conditional_edges('understand', lambda s: 'report' if s.get('error') else 'discover', {'report': 'report', 'discover': 'discover'})
        g.add_conditional_edges('discover', lambda s: 'report' if s.get('error') else 'gather', {'report': 'report', 'gather': 'gather'})
        g.add_edge('gather', 'decide')
        g.add_conditional_edges('decide', lambda s: 'decide' if s['index'] < len(s['requirements']) else 'report', {'decide': 'decide', 'report': 'report'})
        g.add_edge('report', END)
        return g.compile()
