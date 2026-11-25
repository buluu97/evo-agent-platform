"""Check review proposals against current immutable producer bytes."""

from hashlib import sha256

from .sandbox import ToolError, read_text


WEB_EXTENSIONS = ('.html', '.htm', '.js', '.mjs', '.css')


def target_artifacts(run, target):
    ids = set(target.get('outputs', []))
    if (target.get('outputContract') or {}).get('kind') == 'delivery':
        for delivery in reversed(run.get('deliveries', [])):
            if (delivery.get('taskId') == target['id'] and delivery.get('taskRevision') == target.get('revision', 0)
                    and delivery.get('status') == 'packaged'):
                ids = {row['artifactId'] for row in delivery['files']}
                break
    return [item for item in run.get('artifacts', []) if item['id'] in ids]


def version_feedback(run, reviewer, target, action, snapshot):
    """Stale rejection evidence cannot consume a producer's next revision."""
    current = snapshot(run, target)
    supplied = action.get('targetSnapshot')
    stale = supplied is not None and (not isinstance(supplied, dict)
        or type(supplied.get('targetRevision')) is not int or supplied != current)
    current_ids = set(current['artifactHashes'])
    if action.get('verdict') == 'pass' and not stale:
        return None
    previous_ids = {item['id'] for item in run.get('artifacts', [])
                    if item.get('taskId') == target['id'] and item['id'] not in current_ids}
    feedback = str(action.get('feedback', ''))
    stale_ids = [artifact_id for artifact_id in previous_ids if artifact_id in feedback]
    calls = {call['id']: call for call in run.get('calls', [])}
    cited = action.get('evidenceCallIds', [])
    if isinstance(cited, str):
        cited = [cited]
    has_current_observation = False
    for call_id in cited:
        call = calls.get(call_id, {})
        bound = call.get('reviewTargets', {}).get(target['id'])
        result = call.get('result')
        if (bound == current and call.get('taskId') == reviewer['id']
                and call.get('agentId') == reviewer['agentId'] and call.get('revision') == reviewer.get('revision')
                and call.get('status') in ('success', 'failed') and isinstance(result, dict)
                and (call.get('tool') == 'code.test' or call.get('tool') == 'file.read'
                     and result.get('sha256') in current['artifactHashes'].values())):
            has_current_observation = True
        if bound is not None and bound != current:
            stale = True
        if (call.get('tool') == 'file.read' and isinstance(call.get('args'), dict)
                and any(f'imports/{artifact_id}/' in str(call['args'].get('path', '')) for artifact_id in previous_ids)):
            stale = True
    if supplied == current and has_current_observation:
        stale_ids = []
    if not stale and not stale_ids:
        return None
    return {'ok': False, 'actionExecuted': False,
            'error': {'code': 'stale_review', 'message':
                      'This rejection cites a superseded producer version. Read the current artifacts and identify an unresolved finding before proposing a new decision.'},
            'currentTarget': {'taskId': target['id'], **current}, 'supersededArtifactIds': sorted(stale_ids),
            'instruction': 'The producer revision and rework allowance were preserved. Use targetSnapshot with the current revision and hashes. Earlier findings are history until checked against current bytes.'}


def browser_source_feedback(run, reviewer, args, store, gateway, agents):
    """Reject a mismatched QA source before launching the browser container."""
    if args.get('language') != 'browser' or 'review' not in agents.get(reviewer.get('agentId'), {}).get('capabilities', []):
        return None
    targets = set(reviewer.get('reviewOf') or reviewer.get('dependsOn', []))
    expected = [item for task in run.get('steps', []) if task['id'] in targets and task.get('agentId') != reviewer['agentId']
                for item in target_artifacts(run, task) if item.get('path', '').lower().endswith(WEB_EXTENSIONS)]
    if not expected or not hasattr(gateway, '_location'):
        return None
    resources = args.get('resources', {})
    if not isinstance(resources, dict) or not isinstance(args.get('path'), str):
        return None  # The tool's argument validator owns malformed requests.
    paths = [args['path'], *resources.values()]
    if any(not isinstance(path, str) for path in paths):
        return None
    observed = set()
    entry_hash = None
    for path in paths:
        try:
            base, relative = gateway._location(reviewer['agentId'], run['id'], path)
            content = read_text(base, relative)
        except (ToolError, OSError):
            return None  # Preserve the tool's specific unreadable-path diagnostic.
        digest = sha256(content.encode('utf-8')).hexdigest()
        observed.add(digest)
        if path == args['path']:entry_hash = digest
    matched_tasks = {item['taskId'] for item in expected if item['path'].lower().endswith(('.html', '.htm')) and item['sha256'] == entry_hash}
    if matched_tasks:
        expected = [item for item in expected if item['taskId'] in matched_tasks]
    missing = [item for item in expected if item['sha256'] not in observed]
    if not missing:
        return None
    return {'ok': False, 'actionExecuted': False,
            'error': {'code': 'review_source_mismatch', 'message':
                      'The proposed browser test omits current published runtime bytes. Test the delivered entry and assets before attributing failures to the producer.'},
            'missingSources': [{'artifactId': item['id'], 'sourcePath': item['path'], 'sha256': item['sha256'],
                                **({'authorizedImportPath': 'imports/' + item['id'] + '/' + item['name']}
                                   if store.can_access(item['id'], reviewer['agentId'], run['id']) else {})}
                               for item in missing],
            'instruction': 'Use the real entry path and resource URL mapping. Byte-identical local copies are eligible. This preflight created no browser receipt or product verdict.'}
