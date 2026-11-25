"""Pure helpers for batched investigation and coverage."""
from math import ceil
from src.services.validation import coverage


def batch_count(requirements, size):
    if size < 1:
        raise ValueError('Batch size must be positive')
    return ceil(len(requirements) / size)


def expected_decisions(batch, decisions):
    """Match only unique requested IDs. Preserve independent valid outcomes."""
    expected = {r['source_id'] for r in batch}
    grouped = {rid: [] for rid in expected}
    unexpected = []
    for d in decisions:
        rid = d['requirement_id']
        if rid in grouped:
            grouped[rid].append(d)
        else:
            unexpected.append(rid)
    return {rid: ds[0] for rid, ds in grouped.items() if len(ds) == 1}, unexpected


def store_evidence(name, data, issues, comments, links):
    """Ingest only actual successful tool results; partial failures remain gaps."""
    if name == 'get_batch_evidence':
        for package in data['results'].values():
            store_evidence('get_issue_evidence', package, issues, comments, links)
        return
    if name == 'get_issue_evidence':
        key = data['issue_key']
        if any(data[p]['issue_key'] != key for p in ('issue', 'comments', 'links')):
            raise ValueError('Inconsistent evidence package keys')
        for tool, part in [('get_issue','issue'), ('get_comments','comments'), ('get_linked_issues','links')]:
            store_evidence(tool, data[part], issues, comments, links)
        return
    if name == 'get_issue':
        if not isinstance(data.get('description'), str):
            raise ValueError('Invalid issue description')
        issues[data['issue_key']] = data
    elif name == 'get_comments':
        rows = data['comments']
        if len(rows) != data['comment_count'] or len({r['comment_id'] for r in rows}) != len(rows):
            raise ValueError('Incomplete comment retrieval')
        comments[data['issue_key']] = data
    elif name == 'get_linked_issues':
        if not isinstance(data['linked_issues'], list):
            raise ValueError('Invalid link response')
        links[data['issue_key']] = data


def coverage_by_requirement(requirements, issues, links, comments):
    results = {}
    for req in requirements:
        try:
            missing, visited = coverage(req['issue_key'], issues, links, comments)
        except (ValueError, KeyError, TypeError) as exc:
            missing, visited = [f'Coverage validation error: {exc}'], {req['issue_key']}
        results[req['source_id']] = {'missing': sorted(set(missing)), 'visited': sorted(visited)}
    return results


def evaluation_keys(batch, all_requirements, issues, links, comments):
    """Give each batch its forward closure plus supplemental and relevant neighbors."""
    cover = coverage_by_requirement(all_requirements, issues, links, comments)
    all_linked = {key for result in cover.values() for key in result['visited']}
    own = {key for r in batch for key in cover[r['source_id']]['visited']}
    available = set(issues) | set(comments) | set(links)
    supplemental = available - all_linked
    neighbors = {item['issue_key'] for key in own for item in links.get(key, {}).get('linked_issues', [])
                 if item.get('record_type') not in ('manifest', 'requirement')}
    return own | supplemental | (neighbors & available), cover
