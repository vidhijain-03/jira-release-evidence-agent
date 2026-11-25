import json,re

def source_links(description):
    matches=re.findall(r'^Source Links:[ \t]*(.*)$',description,re.M)
    if len(matches)!=1: raise ValueError('Missing or ambiguous imported Source Links')
    return {x.strip() for x in matches[0].split(',') if x.strip()}

def target_from(description):
    matches=re.findall(r'^Target:[ \t]*(\{.*\})$',description,re.M)
    if len(matches)!=1: raise ValueError('Missing or ambiguous target')
    target=json.loads(matches[0])
    if not all(isinstance(target.get(k),str) and target[k] for k in ('release','commit','service','environment','prompt')): raise ValueError('Invalid target')
    return target

def overall(expected, decisions, complete=True):
    ids=[d['requirement_id'] for d in decisions]
    if any(d['decision']=='FAIL' and d['requirement_id'] in expected for d in decisions): return 'BLOCKED'
    if not complete or not expected or len(ids)!=len(set(ids)) or set(ids)!=set(expected): return 'NEEDS_REVIEW'
    return 'READY' if all(d['decision']=='PASS' for d in decisions) else 'NEEDS_REVIEW'

def coverage(root, issues, links, comments):
    """Verify original forward links exist as native links; avoid unrelated reverse-link graph explosion."""
    missing=[]; seen=set(); stack=[root]
    while stack:
        key=stack.pop()
        if key in seen: continue
        seen.add(key)
        if key not in issues: missing.append(f'get_issue {key}'); continue
        if key not in comments: missing.append(f'get_comments {key}')
        if key not in links: missing.append(f'get_linked_issues {key}'); continue
        by_source={x['source_id']:x['issue_key'] for x in links[key]['linked_issues']}
        for source in source_links(issues[key]['description']):
            if source not in by_source: missing.append(f'Missing native link {key} -> {source}')
            else: stack.append(by_source[source])
    return missing,seen

def explicit_scope_mismatches(body, target):
    """Check explicit scope fields; absent fields are not inferred or treated as matches."""
    mismatches=[]
    for field in ('release','commit','service','environment','prompt'):
        # Most supplied evidence uses these explicit field/value phrases.
        values=[v.rstrip('.') for v in re.findall(r'\b'+field+r'\s+([\w.-]+)',body,re.I)]
        # Only consider clearly structured scope values, not ordinary prose such as 'release management'.
        if field=='release': values=[v for v in values if re.fullmatch(r'R\d+(?:\.\d+)+',v)]
        elif field=='commit': values=[v for v in values if re.fullmatch(r'[0-9a-f]{7,40}',v,re.I)]
        elif field=='prompt': values=[v for v in values if re.fullmatch(r'p\d+',v,re.I)]
        elif field=='environment': values=[v for v in values if v in ('production','staging','development','test')]
        elif field=='service': values=[v for v in values if '-' in v]
        for v in values:
            v=v.rstrip('.')
            if v!=target[field]: mismatches.append(f'{field}: {v} != {target[field]}')
    return mismatches
