"""Current reviewed-source handoff and strictly unchanged-bundle packaging.

This is not a new review verdict. A packaging task may retain the evidence for
one explicitly depended-on, currently reviewed producer only when every file,
including documentation/configuration, keeps its original path and bytes.
Ordinary packaging permissions and unassessed deliveries remain unchanged.
"""
import copy

from .delivery import _snapshot, _current_source, validate_delivery


def _ancestors(run, task):
    tasks = {item.get('id'): item for item in run.get('steps', [])}
    found, pending = set(), list(task.get('dependsOn', []))
    while pending:
        identifier = pending.pop()
        if identifier in found:
            continue
        if identifier == task.get('id') or identifier not in tasks:
            return set()
        found.add(identifier)
        pending.extend(tasks[identifier].get('dependsOn', []))
    return found


def _current_bundle(run, task, store):
    ids = task.get('outputs')
    if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
        return None
    rows = [a for a in run.get('artifacts', []) if a.get('id') in ids]
    if {a.get('id') for a in rows} != set(ids):
        return None
    # Re-publishing the same logical path may retain old IDs in outputs. Only
    # the current immutable snapshot can be packaged; validate it below.
    latest = {a.get('path'): a for a in rows}
    if len({p.casefold() for p in latest if isinstance(p, str)}) != len(latest):
        return None
    bundle = []
    for item in latest.values():
        if item.get('sourceRunId') or item.get('inheritedReadOnly'):
            return None
        try:
            snapshot = _snapshot(run, store, item['id'])
            source = _current_source(run, snapshot)
        except (KeyError, TypeError, ValueError):
            return None
        if (source is not task or snapshot.get('ownerAgentId') != task.get('agentId')
                or snapshot.get('sourceRunId') or snapshot.get('inheritedReadOnly')):
            return None
        bundle.append(snapshot)
    return sorted(bundle, key=lambda a: a['path'])


def _browser_bindings(run, source, reviewer, store, bundle, snapshot, browser_evidence):
    """Prove the reviewed runtime used this exact authorized package layout.

    The browser harness serves its entry at the root basename. Consequently only
    a root entry can prove an identical package URL layout here. A relocated or
    privately copied test source is deliberately not enough for this new proof.
    """
    validation = source.get('validation') or {}
    imports = {'imports/' + a['id'] + '/' + a['name']: a for a in bundle}
    bindings = []
    for call in run.get('calls', []):
        result = call.get('result') or {}
        if (call.get('id') not in validation.get('evidenceCallIds', [])
                or call.get('taskId') != reviewer['id'] or call.get('agentId') != reviewer['agentId']
                or type(call.get('revision')) is not int or call['revision'] != reviewer.get('revision')
                or call.get('tool') != 'code.test' or call.get('status') != 'success'
                or result.get('ok') is not True or type(result.get('exitCode')) is not int or result['exitCode'] != 0
                or any(result.get(k) for k in ('outcomeUnknown', 'deadlineExceeded', 'cancelled', 'modelViewOnly',
                    'timedOut', 'outputLimitExceeded', 'notExecuted', 'error'))
                or result.get('actionExecuted') is False
                or call.get('reviewTargets', {}).get(source['id']) != snapshot
                or not browser_evidence(run, source, call)):
            continue
        args = call.get('args') or {}
        entry = imports.get(args.get('path'))
        resources = args.get('resources', {})
        if not entry or '/' in entry['path'] or not isinstance(resources, dict):
            continue
        mapping = {entry['path']: args['path'], **resources}
        if len(mapping) != len(resources) + 1:
            continue
        valid = True
        mapped = []
        for virtual, imported in mapping.items():
            artifact = imports.get(imported) if isinstance(imported, str) else None
            if (not artifact or virtual != artifact['path']
                    or not store.can_access(artifact['id'], reviewer['agentId'], run['id'])):
                valid = False; break
            mapped.append({'path': imported, 'sha256': artifact['sha256']})
        report = result['browser']
        if (report.get('ok') is not True or report.get('error')
                or any(report.get(k) for k in ('timedOut', 'outputLimitExceeded', 'notExecuted', 'cancelled'))
                or report.get('actionExecuted') is False):
            continue
        expected_entry = {'path': args['path'], 'sha256': entry['sha256']}
        manifest = report.get('sourceManifest')
        # Manifest equality binds every runtime resource; no extra same-SHA
        # private source, virtual alias, foreign file, or unrecorded mapping.
        if (not valid or report.get('entry') != expected_entry or not isinstance(manifest, list)
                or len(manifest) != len(mapped)
                or any(not isinstance(row, dict) or set(row) != {'path', 'sha256'} for row in manifest)
                or sorted(manifest, key=lambda row: row['path']) != sorted(mapped, key=lambda row: row['path'])):
            continue
        observed = report.get('testedFiles')
        if (not isinstance(observed, list) or expected_entry not in observed
                or any(row not in mapped for row in observed)):
            continue
        web = [a for a in bundle if a['path'].lower().endswith(('.html', '.htm', '.css', '.js', '.mjs'))]
        if any({'path': 'imports/' + a['id'] + '/' + a['name'], 'sha256': a['sha256']} not in observed for a in web):
            continue
        bindings.append({'callId': call['id'], 'reviewerRevision': reviewer['revision'],
                         'entrypoint': entry['path'],
                         'resources': [{'path': virtual, 'sourceArtifactId': imports[imported]['id'],
                                        'sha256': imports[imported]['sha256']}
                                       for virtual, imported in sorted(mapping.items())]})
    return bindings


def reviewed_sources(run, task, store, agents, snapshot_for, valid_review, dependency_validation, browser_evidence):
    """Only actual current, explicitly granted ancestor sources; no new grants."""
    if ((task.get('outputContract') or {}).get('kind') != 'delivery'
            or task.get('finalSummary') or run.get('mode') != 'task'
            or type(task.get('revision')) is not int or task['revision'] < 0
            or task.get('agentId') not in run.get('participants', [])
            or dependency_validation(run, task).get('status') != 'passed'):
        return []
    ancestors = _ancestors(run, task)
    result = []
    for source in run.get('steps', []):
        if (source.get('id') not in ancestors or source.get('status') != 'done'
                or type(source.get('revision')) is not int or source['revision'] < 0
                or source.get('agentId') not in run.get('participants', [])):
            continue
        snapshot = snapshot_for(run, source)
        expected = {'status': source['status'], **snapshot}
        if (task.get('dependencySnapshots') or {}).get(source['id']) != expected:
            continue
        validation = source.get('validation') or {}
        reviewer = next((t for t in run.get('steps', [])
                         if t.get('id') == validation.get('reviewerTaskId')), None)
        if (not reviewer or reviewer.get('status') != 'done'
                or type(reviewer.get('revision')) is not int or reviewer['revision'] < 0
                or reviewer.get('agentId') not in run.get('participants', [])
                or source['id'] not in reviewer.get('dependsOn', [])
                or (reviewer.get('dependencySnapshots') or {}).get(source['id']) != expected
                or dependency_validation(run, reviewer).get('status') != 'passed'
                or reviewer.get('agentId') in (source.get('agentId'), task.get('agentId'))
                or 'review' not in agents.get(reviewer.get('agentId'), {}).get('capabilities', [])
                or validation.get('verdict') != 'pass'
                or any(validation.get(k) != v for k, v in snapshot.items())
                or dependency_validation(run, source).get('status') == 'stale'):
            continue
        executable_verified = valid_review(run, source)
        editorial_assessed = (validation.get('kind') == 'editorial_assessment'
                              and validation.get('assessed') is True)
        if not executable_verified and not editorial_assessed:
            continue
        bundle = _current_bundle(run, source, store)
        if not bundle or any(not store.can_access(a['id'], task['agentId'], run['id']) for a in bundle):
            continue
        result.append({'sourceTaskId': source['id'], 'sourceRevision': source.get('revision', 0),
                       'reviewerTaskId': reviewer['id'], 'reviewKind': validation.get('kind'),
                       'verifiedExecutableEvidence': executable_verified,
                       'evidenceCallIds': list(validation.get('evidenceCallIds', [])),
                       'runtimeBindings': _browser_bindings(run, source, reviewer, store, bundle, snapshot, browser_evidence)
                           if executable_verified else [],
                       'reviewSnapshot': copy.deepcopy(snapshot),
                       'files': [{k: a[k] for k in ('id', 'path', 'sha256', 'size')} for a in bundle]})
    return result


def handoff_context(sources):
    if not sources:
        return None
    return {'sources': [{k: copy.deepcopy(v) for k, v in s.items() if k not in ('reviewSnapshot', 'runtimeBindings')} for s in sources],
            'instruction': 'These are actual authorized current reviewed source IDs. Prefer package_delivery '
                'with the original IDs and original relative paths; no copying/republication is required. '
                'Direct packaging of those IDs plus necessary new instructions/documentation remains available '
                'under the original task contract; the new descriptions have unassessed semantic quality. '
                'Select the real entrypoint and all needed assets. An editorial assessment is not executable QA. '
                'Changes to runtime code, resources, configuration or layout need appropriate independent review. '
                'The optional unchanged-copy proof has narrower rules: identical entire bundle including documents, '
                'root HTML entry tested from original imports with identical URL layout, and run instructions '
                'copied exactly from the original README.md/README.txt. That proof is not a new review.'}


def unchanged_bundle_binding(run, task, delivery, store, sources):
    """Derive proof only for a real current package of one unchanged bundle.

    Identical path names are deliberate: relocating a whole directory can alter
    absolute URLs, currentScript/location-dependent behavior and run commands.
    Byte equality alone is insufficient to approve such a change.
    """
    if ((task.get('outputContract') or {}).get('kind') != 'delivery'
            or delivery.get('status') != 'packaged'
            or delivery.get('taskId') != task.get('id')
            or delivery.get('taskRevision') != task.get('revision', 0)):
        return None
    try:
        packaged = validate_delivery(delivery, run, store, current=True)
    except (KeyError, TypeError, ValueError):
        return None
    own = _current_bundle(run, task, store)
    if (not own or {a['id'] for a in own} != {a['id'] for a in packaged}
            or any(not store.can_access(a['id'], task['agentId'], run['id']) for a in packaged)):
        return None
    destination = {a['path']: a for a in packaged}
    for source in sources:
        if not source['verifiedExecutableEvidence'] or source.get('reviewKind') != 'browser_test':
            continue
        original = {a['path']: a for a in source['files']}
        if set(original) != set(destination):
            continue
        if any(any(original[path][key] != destination[path][key] for key in ('sha256', 'size'))
               for path in original):
            continue
        if not delivery['entrypoint'].lower().endswith(('.html', '.htm')):
            continue
        instruction_source = next((a for a in source['files']
            if a['path'].rsplit('/', 1)[-1].lower() in ('readme.md', 'readme.txt')
            and store.get(a['id']).get('content', '').strip() == delivery.get('instructions')), None)
        if not instruction_source:
            continue
        runtime = next((binding for binding in source['runtimeBindings']
                        if binding['entrypoint'] == delivery['entrypoint']), None)
        if not runtime:
            continue
        return {'kind': 'unchanged_reviewed_bundle', 'scope': 'packaging_integrity_only',
                'newReviewCreated': False, 'semanticQuality': 'unassessed',
                'sourceTaskId': source['sourceTaskId'], 'sourceRevision': source['sourceRevision'],
                'sourceReviewSnapshot': copy.deepcopy(source['reviewSnapshot']),
                'reviewerTaskId': source['reviewerTaskId'], 'evidenceCallIds': [runtime['callId']],
                'runtimeBinding': copy.deepcopy(runtime),
                'instructionSourceArtifactId': instruction_source['id'],
                'packageTaskId': task['id'], 'packageRevision': task.get('revision', 0),
                'entrypoint': delivery['entrypoint'],
                'files': [{'artifactId': destination[path]['id'], 'sourceArtifactId': original[path]['id'],
                           'path': path, 'sha256': original[path]['sha256'], 'size': original[path]['size']}
                          for path in sorted(original)]}
    return None


def current_packaging_binding(run, task, store, sources):
    """A stored server receipt must still equal a fresh current-source check."""
    for delivery in reversed(run.get('deliveries', [])):
        recorded = delivery.get('reviewedSourceBinding')
        if not isinstance(recorded, dict):
            continue
        actual = unchanged_bundle_binding(run, task, delivery, store, sources)
        if actual is not None and actual == recorded:
            return {'deliveryId': delivery['id'], **copy.deepcopy(actual)}
    return None
