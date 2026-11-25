"""Describe actual missing review source coverage, without accepting or editing.

Only current explicit dependency snapshots and actually served browser files
are compared. A coverage gap says nothing about whether product behavior is
correct. Existing grants alone determine which import references may be shown.
"""
import hashlib
import re


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def review_source_gaps(run, task, call):
    if not all(isinstance(value, dict) for value in (run, task, call)):
        return None
    if (call.get('tool') != 'code.test' or call.get('status') != 'failed'
            or call.get('taskId') != task.get('id') or call.get('agentId') != task.get('agentId')
            or type(task.get('revision')) is not int or task['revision'] < 0
            or type(call.get('revision')) is not int or call['revision'] != task['revision']
            or not isinstance(call.get('id'), str)):
        return None
    result = call.get('result')
    if (not isinstance(result, dict) or result.get('runtime') != 'browser'
            or result.get('ok') is not False or result.get('exitCode') != 1
            or any(result.get(key) for key in ('outcomeUnknown', 'deadlineExceeded', 'cancelled', 'modelViewOnly'))):
        return None
    browser = result.get('browser')
    if not isinstance(browser, dict) or browser.get('executed') is not True:
        return None
    observed = browser.get('testedFiles')
    if (not isinstance(observed, list) or not observed
            or any(not isinstance(row, dict) or not _sha(row.get('sha256'))
                   or not isinstance(row.get('path'), str) or not row['path'] for row in observed)):
        return None
    hashes = {row['sha256'] for row in observed}
    snapshots = call.get('reviewTargets')
    if not isinstance(snapshots, dict):
        return None
    steps, artifacts, events = (run.get(key) for key in ('steps', 'artifacts', 'events'))
    if any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
           for rows in (steps, artifacts, events)):
        return None
    dependencies = task.get('dependsOn')
    if not isinstance(dependencies, list):
        return None
    gaps = []
    for target in steps:
        if target.get('id') not in dependencies or target.get('id') == task.get('id'):
            continue
        outputs = target.get('outputs')
        if (not isinstance(outputs, list) or not outputs or any(not isinstance(value, str) for value in outputs)
                or type(target.get('revision')) is not int or target['revision'] < 0
                or len(set(outputs)) != len(outputs)):
            continue
        items = [item for item in artifacts if item.get('id') in outputs]
        if len(items) != len(outputs):
            continue
        current = {'targetRevision': target['revision'],
                   'artifactHashes': {item['id']: item.get('sha256') for item in items}}
        snapshot = snapshots.get(target.get('id'))
        if (not isinstance(snapshot, dict) or type(snapshot.get('targetRevision')) is not int
                or snapshot != current):
            continue
        valid = True
        for item in items:
            content = item.get('content')
            if (item.get('runId') != run.get('id') or item.get('taskId') != target['id']
                    or item.get('sourceRunId') or item.get('inheritedReadOnly')
                    or not isinstance(item.get('name'), str) or not item['name']
                    or item['name'].startswith('.') or '/' in item['name'] or '\\' in item['name']
                    or not isinstance(content, str) or not _sha(item.get('sha256'))):
                valid = False; break
            try:
                digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
            except UnicodeEncodeError:
                valid = False; break
            if digest != item['sha256']:
                valid = False; break
        if not valid or not any(str(item.get('name', '')).lower().endswith(('.html', '.htm')) for item in items):
            continue
        missing = []
        for item in items:
            if not str(item.get('name', '')).lower().endswith(('.html', '.htm', '.css', '.js', '.mjs')) or item['sha256'] in hashes:
                continue
            row = {'artifactId': item['id'], 'expectedSha256': item['sha256']}
            granted = any(event.get('type') == 'artifact_granted'
                          and isinstance(event.get('payload'), dict)
                          and event['payload'].get('artifactId') == item['id']
                          and event['payload'].get('toAgent') == task.get('agentId') for event in events)
            if granted:
                row['authorizedImportPath'] = 'imports/' + item['id'] + '/' + item['name']
            missing.append(row)
        if missing:
            gaps.append({'targetTaskId': target['id'], 'targetRevision': target['revision'],
                         'notObserved': missing[:8], 'notObservedOmitted': max(0, len(missing)-8)})
    if not gaps:
        return None
    return {'callId': call['id'], 'currentTargetGaps': gaps[:3], 'targetsOmitted': max(0, len(gaps)-3),
            'meaning': 'These current producer source hashes were not observed among files served in this actual test. '
                       'The test does not cover that complete published version; this is not a product-defect finding. '
                       'Use already granted exact sources for independent review. No body, grant, test result or acceptance was created.'}
