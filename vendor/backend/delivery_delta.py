"""Describe final-package changes and reuse current unchanged runtime evidence."""

from .delivery import validate_delivery
from .review_versions import WEB_EXTENSIONS


def latest_package(run, task):
    return next((item for item in reversed(run.get('deliveries', []))
                 if item.get('taskId') == task['id'] and item.get('taskRevision') == task.get('revision', 0)
                 and item.get('status') == 'packaged'), None)


def delta_context(run, task, sources):
    package = latest_package(run, task)
    if package is None:
        return None
    reviewed = {item['path']: item for source in sources for item in source['files']}
    files = []
    for item in package['files']:
        original = reviewed.get(item['path'])
        unchanged = bool(original and original['sha256'] == item['sha256'])
        files.append({'artifactId': item['artifactId'], 'path': item['path'], 'sha256': item['sha256'],
                      'change': 'unchanged' if unchanged else 'modified' if original else 'added'})
    return {'deliveryId': package['id'], 'entrypoint': package['entrypoint'], 'instructions': package['instructions'],
            'files': files, 'removedPaths': sorted(set(reviewed) - {item['path'] for item in files}),
            'instruction': 'An independent reviewer must assess new/changed files and the entrypoint/use instructions. '
                           'Current identical runtime evidence can be reused only for the same complete path layout. '
                           'Submit review with targetSnapshot from the current target; then explicitly final.'}


def reviewed_delta(run, reviewer, target, action, store, sources, snapshot):
    """No new executable verdict. Reuse only existing current browser evidence.

    A reviewer explicitly accepts the package manifest, reads every packaged
    non-runtime file, and binds the decision to the current delivery snapshot.
    A changed runtime or layout requires a new executable test.
    """
    package = latest_package(run, target)
    supplied = action.get('targetSnapshot')
    if (package is None or not isinstance(supplied, dict)
            or type(supplied.get('targetRevision')) is not int or supplied != snapshot
            or not isinstance(action.get('feedback'), str) or not action['feedback'].strip()):
        return None
    try:
        packaged = validate_delivery(package, run, store)
    except ValueError:
        return None
    runtime = {item['path']: item['sha256'] for item in packaged if item['path'].lower().endswith(WEB_EXTENSIONS)}
    if not runtime:
        return None
    matched = None
    for source in sources:
        original = {item['path']: item['sha256'] for item in source['files'] if item['path'].lower().endswith(WEB_EXTENSIONS)}
        bindings = [binding for binding in source.get('runtimeBindings', []) if binding['entrypoint'] == package['entrypoint']]
        if source.get('verifiedExecutableEvidence') and runtime == original and bindings:
            matched = source, bindings[0]
            break
    if matched is None:
        return None
    # Additional configuration can alter runtime behavior despite unchanged JS.
    documents = [item for item in packaged if not item['path'].lower().endswith(WEB_EXTENSIONS)]
    if any(not item['path'].lower().endswith(('.md', '.txt')) for item in documents):
        return None
    cited = action.get('evidenceCallIds', [])
    reads = []
    for item in documents:
        path = 'imports/' + item['id'] + '/' + item['name']
        call = next((call for call in run.get('calls', [])
                     if call['id'] in cited and call.get('taskId') == reviewer['id']
                     and call.get('agentId') == reviewer['agentId'] and call.get('revision') == reviewer['revision']
                     and call.get('tool') == 'file.read' and call.get('status') == 'success'
                     and call.get('reviewTargets', {}).get(target['id']) == snapshot
                     and call.get('args', {}).get('path') == path
                     and isinstance(call.get('result'), dict) and call['result'].get('ok') is True
                     and call['result'].get('mode', 'full') == 'full' and not call['result'].get('truncated')
                     and call['result'].get('sha256') == item['sha256']), None)
        if call is None or not store.can_access(item['id'], reviewer['agentId'], run['id']):
            return None
        reads.append(call['id'])
    source, binding = matched
    return {'kind': 'reviewed_delivery_delta', 'deliveryId': package['id'],
            'sourceTaskId': source['sourceTaskId'], 'sourceRevision': source['sourceRevision'],
            'runtimeEvidenceCallIds': [binding['callId']], 'documentReadCallIds': sorted(set(reads)),
            'reviewerTaskId': reviewer['id'], 'reviewerRevision': reviewer['revision'],
            'snapshot': snapshot, 'scope': 'unchanged_runtime_and_explicit_package_assessment'}
