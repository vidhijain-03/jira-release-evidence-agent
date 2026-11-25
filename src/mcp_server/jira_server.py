import os, sys, logging, re
from typing import Literal
from mcp.server.fastmcp import FastMCP
from src.jira.jira_client import get_jira_client
from src.jira.jql_builder import build_jql

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
mcp = FastMCP('jira-mcp')

def scoped(key, fields='summary,description,status'):
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*-\d+', key):
        raise ValueError('Invalid issue key')
    if key.rsplit('-',1)[0] != os.environ['JIRA_PROJECT_KEY']:
        raise PermissionError('Outside project')
    issue = get_jira_client().issue(key, fields=','.join(sorted(set(fields.split(',')) | {'project','labels'})))
    if issue.fields.project.key != os.environ['JIRA_PROJECT_KEY'] or os.environ['JIRA_DATASET_LABEL'] not in issue.fields.labels:
        raise PermissionError('Outside assessment dataset')
    return issue

def metadata(issue):
    labels = issue.fields.labels
    def one(prefix):
        values = [x[len(prefix):] for x in labels if x.startswith(prefix)]
        return values[0] if len(values)==1 else None
    return {'issue_key':issue.key,'source_id':(one('source-') or '').upper(), 'record_type':one('rr-'), 'summary':issue.fields.summary,'status':issue.fields.status.name,'labels':labels,'url':issue.permalink()}

@mcp.tool()
def search_issues(release: str | None = None, record_type: Literal['manifest','requirement','work','evidence'] | None = None, source_id: str | None = None, service: str | None = None, search_text: str | None = None, max_results: int = 20) -> dict:
    """Search scoped Jira metadata. Service labels are incomplete: never use them to exclude linked evidence. Refine truncated searches. Status is not proof."""
    if not 1 <= max_results <= 50: raise ValueError('max_results must be 1..50')
    jql = build_jql(os.environ['JIRA_PROJECT_KEY'],os.environ['JIRA_DATASET_LABEL'],release,record_type,source_id,service,search_text)
    issues = get_jira_client().enhanced_search_issues(jql,maxResults=max_results+1,fields='summary,status,labels,project')
    return {'jql':jql,'possibly_truncated':len(issues)>max_results,'issues':[metadata(i) for i in issues[:max_results]]}

@mcp.tool()
def get_issue(issue_key: str) -> dict:
    """Read a scoped Jira ticket, including original description and source links. Use for acceptance criteria and evidence context."""
    i = scoped(issue_key)
    return {**metadata(i),'description':i.fields.description or ''}

@mcp.tool()
def get_comments(issue_key: str) -> dict:
    """Read all recorded comments on a scoped issue. Use recorded actor, role and timestamp, not native import metadata."""
    i = scoped(issue_key)
    # Explicit REST pagination avoids relying on the library's comment-list defaults.
    jira = get_jira_client()
    start=0; rows=[]; total=None
    while True:
        page = jira._get_json(f'issue/{i.key}/comment',params={'startAt':start,'maxResults':100})
        batch=page['comments']; total=page['total']
        for c in batch:
            body=c['body']
            if not isinstance(body,str): raise ValueError('Expected REST v2 plain-text comment body')
            match=re.match(r'^Recorded event ([^|\r\n]+)\s*\|\s*actor ([^|\r\n]+)\s*\|\s*role ([^\r\n]+)\r?\n(.*)$',body,re.S)
            rows.append({'comment_id':str(c['id']),'recorded_timestamp':match[1].strip() if match else None,'recorded_actor':match[2].strip() if match else None,'recorded_role':match[3].strip() if match else None,'body':body,'url':f'{i.permalink()}?focusedCommentId={c["id"]}'})
        start+=len(batch)
        if start>=total: break
        if not batch or start>=1000: raise RuntimeError('Incomplete comment pagination')
    if len({c['comment_id'] for c in rows})!=total: raise RuntimeError('Comment count changed during retrieval')
    return {'issue_key':i.key,'comment_count':len(rows),'comments':rows}

@mcp.tool()
def get_linked_issues(issue_key: str) -> dict:
    """Read native Jira relationships in both directions. Follow relevant dependencies; links alone do not prove applicability."""
    i=scoped(issue_key,'summary,issuelinks'); rows=[]; excluded=0; seen=set()
    for link in i.fields.issuelinks:
        other=getattr(link,'outwardIssue',None) or getattr(link,'inwardIssue',None)
        if not other or other.key in seen: continue
        seen.add(other.key)
        try: detail=scoped(other.key,'summary,status')
        except PermissionError: excluded+=1; continue
        rows.append(metadata(detail))
    return {'issue_key':i.key,'linked_issues':rows,'excluded_count':excluded}

@mcp.tool()
def get_issue_evidence(issue_key: str) -> dict:
    """Read a complete scoped evidence package: issue details, every comment and native links. Use for requirements, linked verification records and dependencies."""
    issue = get_issue(issue_key)
    return {'issue_key': issue['issue_key'], 'issue': issue,
            'comments': get_comments(issue['issue_key']),
            'links': get_linked_issues(issue['issue_key'])}

@mcp.tool()
def get_batch_evidence(issue_keys: list[str]) -> dict:
    """Retrieve full evidence packages for multiple known Jira issue keys in one MCP invocation (at most 10, or the configured lower limit). Prefer this for grouped requirements and linked records. Inspect returned links before selecting further evidence. Partial failures are explicitly reported. Does not automatically traverse dependencies."""
    limit = min(10, max(1, int(os.getenv('AGENT_MAX_BATCH_ISSUES', '10'))))
    if not 1 <= len(issue_keys) <= limit:
        raise ValueError(f'Provide between 1 and {limit} issue keys')
    keys = list(dict.fromkeys(issue_keys))
    results, errors = {}, {}
    for key in keys:
        try:
            results[key] = get_issue_evidence(key)
        except Exception as exc:
            errors[key] = {'error': type(exc).__name__, 'message': 'Evidence retrieval failed'}
    return {'requested_count': len(keys), 'retrieved_count': len(results),
            'complete': not errors, 'results': results, 'errors': errors}

if __name__=='__main__': mcp.run(transport='stdio')
