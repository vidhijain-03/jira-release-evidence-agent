"""Repeatable Jira Cloud dataset importer. Run from the repository root."""
import argparse
import json
import logging
import os
import re
from collections import Counter
from pathlib import Path
from time import monotonic

from dotenv import load_dotenv
from jira import JIRA
from jira.exceptions import JIRAError

ROOT = Path(__file__).resolve().parent.parent
LOG = logging.getLogger(__name__)
load_dotenv(ROOT / '.env')


def save_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


def validate_dataset(data):
    records = data['records']
    ids = [r['source_id'] for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate source IDs')
    known = set(ids)
    for r in records:
        for field in ('source_id', 'kind', 'summary', 'release', 'recorded_status', 'description'):
            if not isinstance(r[field], str):
                raise ValueError(f'Invalid {field}: {r["source_id"]}')
        for field in ('comments', 'links'):
            if not isinstance(r[field], list) or not all(isinstance(x, str) for x in r[field]):
                raise ValueError(f'Invalid {field}: {r["source_id"]}')
        if set(r['links']) - known or r['source_id'] in r['links']:
            raise ValueError(f'Invalid links: {r["source_id"]}')
    manifests = [r for r in records if r['kind'] == 'manifest']
    if len(manifests) != 1:
        raise ValueError('Expected exactly one manifest')
    match = re.search(r'^Target:\s*(\{.*\})$', manifests[0]['description'], re.M)
    if not match or json.loads(match.group(1)) != data['target']:
        raise ValueError('Manifest target does not match dataset target')
    return records


def get_or_create_project(jira, key, name):
    try:
        return jira.project(key)
    except JIRAError as exc:
        if exc.status_code != 404:
            raise
    LOG.info('Project %s not found or inaccessible; attempting creation', key)
    jira.create_project(key=key, name=name,
        ptype=os.getenv('JIRA_PROJECT_TYPE', 'software'),
        template_name=os.getenv('JIRA_PROJECT_TEMPLATE', 'Kanban'))
    return jira.project(key)


def fields_for(record, project_key, dataset_label):
    labels = [dataset_label, f'rr-{record["kind"]}',
              f'release-{record["release"]}', f'source-{record["source_id"].lower()}']
    match = re.search(r':\s*([\w-]+)\s+R\d+\.\d+$', record['summary'])
    if match:
        labels.append(f'service-{match.group(1)}')
    description = (f'Source ID: {record["source_id"]}\nRecord Type: {record["kind"]}\n'
                   f'Release: {record["release"]}\nRecorded Status: {record["recorded_status"]}\n\n'
                   f'{record["description"]}\n\nSource Links: {", ".join(record["links"])}')
    return {'project': {'key': project_key}, 'issuetype': {'name': 'Task'},
            'summary': f'[{record["source_id"]}] {record["summary"]}',
            'description': description, 'labels': labels}


def existing_mapping(jira, key, label):
    mapping = {}
    jql = f'project = "{key}" AND labels = "{label}"'
    # Jira Cloud enhanced search handles token-based pagination.
    issues = jira.enhanced_search_issues(jql, maxResults=False, fields='labels')
    for issue in issues:
        sources = [x.removeprefix('source-').upper() for x in issue.fields.labels if x.startswith('source-')]
        if len(sources) != 1 or sources[0] in mapping:
            raise ValueError(f'Ambiguous source labels on {issue.key}')
        mapping[sources[0]] = issue.key
    return mapping


def sync_comments(jira, key, expected):
    # Use a multiset: identical source comments can legitimately occur twice.
    existing = Counter(c.body for c in jira.comments(key))
    added = 0
    for body in expected:
        if existing[body]:
            existing[body] -= 1
        else:
            jira.add_comment(key, body)
            added += 1
    return added


def sync_status(jira, key, desired):
    issue = jira.issue(key, fields='status')
    if issue.fields.status.name.casefold() == desired.casefold():
        return
    for transition in jira.transitions(key):
        if transition['to']['name'].casefold() == desired.casefold():
            jira.transition_issue(key, transition['id'])
            return
    raise RuntimeError(f'{key}: no direct transition to {desired}')


def sync_links(jira, records, mapping):
    pairs = set()
    for record in records:
        for target in record['links']:
            pairs.add(tuple(sorted((mapping[record['source_id']], mapping[target]))))
    added = 0
    for a, b in sorted(pairs):
        links = jira.issue(a, fields='issuelinks').fields.issuelinks
        if any(link.type.name == 'Relates' and
               any(getattr(getattr(link, side, None), 'key', None) == b
                   for side in ('inwardIssue', 'outwardIssue')) for link in links):
            continue
        jira.create_issue_link('Relates', a, b)
        added += 1
    return added, len(pairs)


def main():
    file = Path(r"D:\jira-release-evidence-agent\inputs\dataset.json")
    data = json.loads(file.read_text(encoding='utf-8'))
    records = validate_dataset(data)
    label = data['dataset_label']
    if os.getenv('JIRA_DATASET_LABEL', label) != label:
        raise ValueError('Configured dataset label does not match input')
    print(f'Validated {len(records)} records, {sum(len(r["comments"]) for r in records)} comments')
    out = ROOT / 'outputs/import'
    out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(), logging.FileHandler(out / 'import.log', encoding='utf-8')])
    start = monotonic()
    report = {'status': 'incomplete', 'records_expected': len(records), 'created': 0,
              'reused': 0, 'comments_added': 0, 'links_added': 0, 'status_errors': []}
    try:
        jira = JIRA(server=os.environ['JIRA_BASE_URL'].rstrip('/'),
                    basic_auth=(os.environ['JIRA_EMAIL'], os.environ['JIRA_API_TOKEN']),
                    options={'verify': os.getenv('JIRA_VERIFY_SSL', 'true').lower() == 'true'},
                    timeout=int(os.getenv('JIRA_TIMEOUT_SECONDS', '30')))
        jira.myself()
        key = os.environ['JIRA_PROJECT_KEY'].strip().upper()
        project = get_or_create_project(jira, key, os.environ['JIRA_PROJECT_NAME'])
        if project.key != key:
            raise RuntimeError('Unexpected project key')
        report['project_key'] = key
        mapping = existing_mapping(jira, key, label)
        if set(mapping) - {r['source_id'] for r in records}:
            raise ValueError('Unexpected source IDs already in dataset scope')
        # Preserve locally acknowledged creates if Jira search indexing is delayed.
        checkpoint = out / 'issue_mapping.json'
        if checkpoint.exists():
            saved = json.loads(checkpoint.read_text(encoding='utf-8'))
            for source, issue_key in saved.items():
                if source not in {r['source_id'] for r in records}:
                    continue
                if source in mapping:
                    if mapping[source] != issue_key:
                        raise ValueError(f'Conflicting checkpoint for {source}')
                    continue
                issue = jira.issue(issue_key, fields='labels,project')
                if issue.fields.project.key != key or not {label, f'source-{source.lower()}'}.issubset(issue.fields.labels):
                    raise ValueError(f'Invalid checkpoint issue {issue_key}')
                mapping[source] = issue_key
        for record in records:
            source = record['source_id']
            if source in mapping:
                report['reused'] += 1
                continue
            issue = jira.create_issue(fields=fields_for(record, key, label))
            mapping[source] = issue.key
            report['created'] += 1
            save_json(checkpoint, mapping)
            LOG.info('Created %s -> %s', source, issue.key)
        save_json(checkpoint, mapping)
        for record in records:
            issue_key = mapping[record['source_id']]
            report['comments_added'] += sync_comments(jira, issue_key, record['comments'])
            try:
                sync_status(jira, issue_key, record['recorded_status'])
            except (JIRAError, RuntimeError) as exc:
                report['status_errors'].append({'issue': issue_key, 'error': str(exc) if isinstance(exc, RuntimeError) else f'HTTP {exc.status_code}'})
                LOG.warning('Status sync failed for %s', issue_key)
        report['links_added'], report['links_expected'] = sync_links(jira, records, mapping)
        report['records_mapped'] = len(mapping)
        report['comments_expected'] = sum(len(r['comments']) for r in records)
        # Verify all expected comments and statuses by reading Jira again.
        report['verification_errors'] = []
        for record in records:
            issue_key = mapping[record['source_id']]
            actual = Counter(c.body for c in jira.comments(issue_key))
            if Counter(record['comments']) - actual:
                report['verification_errors'].append(f'{issue_key}: missing comments')
            issue = jira.issue(issue_key, fields='status,labels,description,summary,issuelinks')
            expected = fields_for(record, key, label)
            if not set(expected['labels']).issubset(issue.fields.labels) or issue.fields.description != expected['description'] or issue.fields.summary != expected['summary']:
                report['verification_errors'].append(f'{issue_key}: fields differ')
            if issue.fields.status.name.casefold() != record['recorded_status'].casefold():
                report['verification_errors'].append(f'{issue_key}: status differs')
            linked = {getattr(getattr(link, side, None), 'key', None) for link in issue.fields.issuelinks if link.type.name == 'Relates' for side in ('inwardIssue', 'outwardIssue')}
            if any(mapping[target] not in linked for target in record['links']):
                report['verification_errors'].append(f'{issue_key}: missing links')
        report['status'] = 'complete' if not report['verification_errors'] and not report['status_errors'] else 'needs_attention'
    except Exception as exc:
        report['error'] = f'HTTP {exc.status_code}' if isinstance(exc, JIRAError) else type(exc).__name__
        LOG.error('Import stopped: %s', report['error'])
        raise
    finally:
        report['elapsed_seconds'] = round(monotonic() - start, 2)
        save_json(out / 'import_report.json', report)
    print(json.dumps(report, indent=2))
    if report['status'] != 'complete':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
