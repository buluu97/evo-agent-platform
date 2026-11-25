"""Bounded observations of actual browser execution errors, never acceptance.

The host reports facts and a next diagnostic step. The model/reviewer decides
whether the test or product needs correction; no expected value is rewritten.
"""
import copy
import hashlib

from .browser_failure_recovery import _key, _manifest
from .browser_observation import failure_observation_view
from .browser_stagnation import _step, _valid_args

_READONLY = {'llm.chat', 'file.read', 'file.list', 'web.search', 'web.fetch', 'git.status'}
_FLAGS = ('outcomeUnknown', 'outcome_unknown', 'deadlineExceeded', 'timedOut',
          'cancelled', 'outputLimitExceeded', 'notExecuted', 'modelViewOnly', 'needsApproval')
_ERROR_CODES = {'uncertain', 'outcome_unknown', 'deadline_exceeded', 'cancelled', 'timeout'}
_ASSERT_FIELDS = ('action', 'pageId', 'selector', 'key', 'name', 'expected', 'actual',
                  'passed', 'expectedDifferentFrom')


def _digest(value):
    encoded = _key(value)
    return hashlib.sha256(encoded.encode()).hexdigest() if encoded is not None else None


def _unsafe(value):
    if not isinstance(value, dict):
        return False
    if (any(value.get(k) for k in _FLAGS) or value.get('actionExecuted') is False
            or value.get('code') in _ERROR_CODES):
        return True
    return any(_unsafe(value.get(k)) for k in ('result', 'error', 'transport', 'lateResult', 'browser'))


def _unknown(value):
    """Only unresolved effects persist; known nonexecution is a local boundary."""
    if not isinstance(value, dict):
        return False
    if (any(value.get(k) is not None and value.get(k) is not False
            for k in ('outcomeUnknown', 'outcome_unknown'))
            or value.get('code') in ('uncertain', 'outcome_unknown')):
        return True
    return any(_unknown(value.get(k)) for k in ('result', 'error', 'transport', 'lateResult', 'browser'))


def _passing_assertion_consistent(check, observed):
    """Check only the trusted runner's small declarative pass comparisons."""
    if 'actual' not in observed:
        return False
    action, actual = check['action'], observed['actual']
    if action == 'assert_text':
        return isinstance(actual, str) and (actual == check['text'] if check.get('exact', False)
                                           else check['text'] in actual)
    if action == 'assert_count':
        return type(actual) is int and actual == check['count']
    if action in ('assert_value', 'assert_attribute', 'assert_storage'):
        return isinstance(actual, str) and actual == check['value']
    if action == 'assert_visible':
        return actual is True
    if action == 'assert_storage_missing':
        return actual is None
    if action == 'assert_canvas_changed':
        previous = observed.get('expectedDifferentFrom')
        return isinstance(actual, str) and isinstance(previous, str) and actual != previous
    return False


def _scope(run, task):
    if not isinstance(run, dict) or not isinstance(task, dict):
        return False
    if (any(not isinstance(v, str) or not v or len(v) > 200
            for v in (run.get('id'), task.get('id'), task.get('agentId')))
            or type(task.get('revision')) is not int or task['revision'] < 0):
        return False
    calls, steps = run.get('calls'), run.get('steps')
    if (not isinstance(calls, list) or any(not isinstance(c, dict) for c in calls)
            or not isinstance(steps, list) or any(not isinstance(t, dict) for t in steps)):
        return False
    targets = [t for t in steps if t.get('id') == task['id']]
    if (len(targets) != 1 or targets[0].get('agentId') != task['agentId']
            or type(targets[0].get('revision')) is not int or targets[0]['revision'] != task['revision']):
        return False
    seen = set()
    for call in calls:
        identifier = call.get('id')
        if (not isinstance(identifier, str) or not identifier or len(identifier) > 200
                or identifier in seen or call.get('runId', run['id']) != run['id']):
            return False
        seen.add(identifier)
        if call.get('agentId') == task['agentId'] and call.get('tool') != 'llm.chat' and _unknown(call):
            return False
    from .stage_control import _unresolved_owner_effects
    return not _unresolved_owner_effects(run, task)


def _effective_args(args):
    if not _valid_args(args):
        return None
    result = copy.deepcopy(args)
    result.setdefault('timeout', 20)
    result.setdefault('resources', {})
    result.setdefault('storage', {})
    result['checks'] = [_step(check) for check in result['checks']]
    return result



def _review_current(run, call):
    snapshots = call.get('reviewTargets', {})
    if not isinstance(snapshots, dict):
        return False
    if not snapshots:
        return True
    artifacts = run.get('artifacts')
    if not isinstance(artifacts, list) or any(not isinstance(a, dict) for a in artifacts):
        return False
    for identifier, snapshot in snapshots.items():
        targets = [t for t in run['steps'] if t.get('id') == identifier]
        if len(targets) != 1:
            return False
        target = targets[0]
        outputs = target.get('outputs')
        if (type(target.get('revision')) is not int or not isinstance(outputs, list)
                or any(not isinstance(i, str) for i in outputs) or len(set(outputs)) != len(outputs)):
            return False
        items = [a for a in artifacts if a.get('id') in outputs]
        if (len(items) != len(outputs) or len({a['id'] for a in items}) != len(items)
                or any(a.get('runId') != run['id'] or a.get('taskId') != identifier
                       or a.get('ownerAgentId', a.get('owner')) != target.get('agentId') for a in items)):
            return False
        hashes = {a['id']: a.get('sha256') for a in items}
        if hashes and _manifest([{'path': key, 'sha256': sha} for key, sha in hashes.items()]) is None:
            return False
        if (not isinstance(snapshot, dict) or type(snapshot.get('targetRevision')) is not int
                or snapshot != {'targetRevision': target['revision'], 'artifactHashes': hashes}):
            return False
    return True


def _execution(run, task, call):
    """Validate a real settled prefix, including passes used only as boundaries."""
    if (call.get('tool') != 'code.test' or call.get('agentId') != task['agentId']
            or call.get('taskId') != task['id'] or type(call.get('revision')) is not int
            or call['revision'] != task['revision'] or _unsafe(call) or not _review_current(run, call)):
        return None
    args, result = _effective_args(call.get('args')), call.get('result')
    if (args is None or not isinstance(result, dict) or result.get('runtime') != 'browser'
            or result.get('test') is not True or type(result.get('exitCode')) is not int):
        return None
    success = call.get('status') == 'success'
    if (call.get('status') not in ('success', 'failed') or result.get('ok') is not success
            or result['exitCode'] != (0 if success else 1)):
        return None
    isolation = result.get('isolation')
    if (not isinstance(isolation, dict) or isolation.get('runId') != run['id']
            or isolation.get('ownerAgentId') != task['agentId']):
        return None
    report = result.get('browser')
    if (not isinstance(report, dict) or report.get('engine') != 'chromium'
            or report.get('executed') is not True or report.get('ok') is not success):
        return None
    manifest, served = _manifest(report.get('sourceManifest')), _manifest(report.get('testedFiles'))
    entry = report.get('entry')
    if (not manifest or not served or not isinstance(entry, dict)
            or entry.get('path') != args['path'] or manifest.get(entry.get('path')) != entry.get('sha256')
            or served.get(entry.get('path')) != entry.get('sha256')
            or any(manifest.get(p) != h for p, h in served.items())
            or set(manifest) != {args['path'], *args['resources'].values()}):
        return None
    steps, assertions = report.get('steps'), report.get('assertions')
    if (not isinstance(steps, list) or not 1 <= len(steps) <= len(args['checks'])
            or not isinstance(assertions, list) or any(not isinstance(s, dict) for s in steps + assertions)
            or type(report.get('assertionCount')) is not int
            or type(report.get('assertionsPassed')) is not int
            or report['assertionCount'] != len(assertions)
            or report['assertionsPassed'] != sum(a.get('passed') is True for a in assertions)):
        return None
    executed_assertions, exception_assertions = [], set()
    for index, observed in enumerate(steps):
        expected = args['checks'][index]
        if (observed.get('action') != expected['action']
                or observed.get('pageId', 'main') != expected['pageId']
                or any(observed.get(k) != expected.get(k) for k in ('selector', 'key'))
                or type(observed.get('ok')) is not bool
                or observed['ok'] is False and index != len(steps) - 1):
            return None
        if expected['action'] in ('storage_write_denied', 'storage_read_denied') and observed.get('enabled') is not expected['enabled']:
            return None
        if expected['action'].startswith('assert_'):
            field = {'assert_text':'text', 'assert_count':'count', 'assert_value':'value',
                     'assert_attribute':'value', 'assert_storage':'value'}.get(expected['action'])
            # Locator exceptions can occur after expected was set but before actual/passed.
            if field and observed.get('expected') != expected[field]:
                return None
            if expected['action'] == 'assert_visible' and observed.get('expected') is not True:
                return None
            if expected['action'] == 'assert_storage_missing' and observed.get('expected') is not None:
                return None
            if expected['action'] == 'assert_attribute' and observed.get('name') != expected['name']:
                return None
            if 'passed' in observed:
                if type(observed['passed']) is not bool or observed['ok'] is not observed['passed']:
                    return None
                if observed['passed'] and not _passing_assertion_consistent(expected, observed):
                    return None
                executed_assertions.append(observed)
            elif observed['ok']:
                return None
            else:
                # The runner records a locator/read exception before an
                # assertion comparison finishes: steps has no passed value,
                # while the catch-path assertion receipt adds passed=False.
                # Bind that exact pair; never invent an actual value or turn
                # an incomplete comparison into a normal assertion mismatch.
                if success or not isinstance(observed.get('error'), str) or not observed['error']:
                    return None
                exception_assertions.add(len(executed_assertions))
                executed_assertions.append({**observed, 'passed': False})
    if (len(executed_assertions) != len(assertions)
            or any(any(a.get(k) != s.get(k) for k in _ASSERT_FIELDS)
                   for a, s in zip(assertions, executed_assertions))
            or any(assertions[i].get('error') != executed_assertions[i]['error']
                   or ('actual' in assertions[i]) != ('actual' in executed_assertions[i])
                   for i in exception_assertions)):
        return None
    if steps[-1]['ok'] and len(steps) != len(args['checks']):
        return None
    pages = {'main'} | {s['pageId'] for s in args['checks'][:len(steps)] if s['action'] == 'new_page'}
    page_errors = report.get('pageErrors')
    if (not isinstance(page_errors, list) or any(not isinstance(e, dict)
            or not isinstance(e.get('message'), str) or not e['message']
            or e.get('pageId') not in pages for e in page_errors)):
        return None
    if success and (page_errors or not steps[-1]['ok']
                    or any(a.get('passed') is not True for a in assertions)
                    or report.get('error') or result.get('error')
                    or report.get('blockedRequests') or report.get('httpErrors')):
        return None
    observation = report.get('failureObservation')
    if observation is not None and (not isinstance(observation, dict)
            or observation.get('stepIndex') != len(steps) - 1
            or observation.get('pageId') != steps[-1].get('pageId', 'main')):
        return None
    kind = None
    failed_assertions = [i for i, a in enumerate(assertions) if a.get('passed') is False]
    if not success and (not failed_assertions or set(failed_assertions) == exception_assertions):
        if not steps[-1]['ok']:
            if not isinstance(steps[-1].get('error'), str) or not steps[-1]['error']:
                return None
            kind = 'step_execution_error'
        elif page_errors:
            kind = 'page_errors'
    identity = {'kind': kind, 'pageErrors': page_errors,
                'failedStep': {k: steps[-1].get(k) for k in ('action', 'pageId', 'selector', 'key', 'error')}
                              if not steps[-1]['ok'] else None}
    return {'args': args, 'manifest': manifest, 'served': served, 'report': report,
            'kind': kind, 'failureIdentity': identity, 'success': success}


def _file_view(mapping, max_items=6, budget=1150):
    rows, used = [], 0
    for path, sha in mapping.items():
        item = {'path': path, 'sha256': sha}
        size = len(_key(item))
        if len(rows) >= max_items or used + size > budget:
            break
        rows.append(item)
        used += size
    return rows, len(mapping) - len(rows)


def _view(run, task, call, value):
    if value['kind'] is None:
        return None
    report = value['report']
    files, omitted = _file_view(value['manifest'])
    served, served_omitted = _file_view(value['served'], budget=700)
    absent = {p:h for p,h in value['manifest'].items() if p not in value['served']}
    missing, missing_omitted = _file_view(absent, budget=700)
    result = {'runId': run['id'], 'taskId': task['id'], 'agentId': task['agentId'],
        'revision': task['revision'], 'callId': call['id'], 'kind': value['kind'],
        'diagnosisOnly': True, 'acceptanceCreated': False, 'actionExecuted': False,
        'actualTestStatus': 'failed', 'cause': 'unassessed',
        'testArgsSha256': _digest(value['args']), 'sourceManifestSha256': _digest(value['manifest']),
        'sourceManifest': files, 'sourceManifestOmitted': omitted,
        'assertions': {'recorded': report['assertionCount'], 'passed': report['assertionsPassed']},
        'pageErrors': [{'pageId': e['pageId'], 'message': e['message'][:300],
                        'messageTruncated': len(e['message']) > 300} for e in report['pageErrors'][:3]],
        'pageErrorsOmitted': max(0, len(report['pageErrors']) - 3),
        'executedFaultSteps': [{k: (v[:160] if isinstance(v, str) else v)
            for k, v in {'index': i, **step}.items() if k in ('index', 'action', 'pageId', 'enabled', 'key')}
            for i, step in enumerate(value['args']['checks'][:len(report['steps'])])
            if step['action'] in ('storage_write_denied', 'storage_read_denied')][:4],
        'assetObservation': {'observedServedFiles': served, 'observedServedFilesOmitted': served_omitted,
            'suppliedButNotObservedServed': missing, 'suppliedButNotObservedServedOmitted': missing_omitted,
            'meaning': 'Actually served files only, not JavaScript execution. Unserved supplied files are a coverage gap, not proof of a missing dependency; the test may have stopped before requesting them.'}}
    if value['kind'] == 'page_errors':
        result['recommendations'] = [
            'Assertions passed but the actual browser test failed on page errors. Inspect the recorded exception and relevant source/error handling; passing assertions do not erase it.',
            'If an injected fault was expected, check the required recovery behavior. Do not suppress the exception or weaken acceptance to make the test green.',
            'For a demonstrated product fault, the authorized reviewer should issue a precise rework request with this callId; otherwise report the unresolved limitation.']
    else:
        step = report['steps'][-1]
        result['failedStep'] = {'index': len(report['steps']) - 1, 'action': step['action'],
                               'pageId': step.get('pageId', 'main'), 'error': step['error'][:550],
                               'errorTruncated': len(step['error']) > 550}
        if 'selector' in step:
            result['failedStep'].update(selector=step['selector'][:400],
                                       selectorTruncated=len(step['selector']) > 400)
        if step['action'].startswith('assert_') and 'passed' not in step:
            result['failedStep']['comparisonCompleted'] = False
            result['failedStep']['actualRecorded'] = 'actual' in step
            for key in ('expected', 'actual'):
                if key in step:
                    observed_value = step[key]
                    result['failedStep'][key] = observed_value[:300] if isinstance(observed_value, str) else copy.deepcopy(observed_value)
                    if isinstance(observed_value, str) and len(observed_value) > 300:
                        result['failedStep'][key + 'Truncated'] = True
        result['failureObservation'] = failure_observation_view(report.get('failureObservation'))
        result['recommendations'] = [
            'The recorded step did not complete. Inspect its actual match count, visible state and the already authorized source before blaming product logic.',
            'For a locator error, check DOM structure, uniqueness, date/view and readiness; use a source-supported selector or setup, then rerun normally. Zero matches alone do not prove why it failed.',
            'An assertion interrupted before comparison has no established expected/actual mismatch. Check dynamic IDs, timestamps and post-reload view/setup against observed state; do not alter the expected value merely to force a pass.',
            'Use observed served files to check coverage only. Resource mappings do not load scripts. If the cause remains unclear, publish usable work with limitations or request precise rework; do not rewrite expected results.']
    if len(_key(result)) > 5500:
        result.pop('assetObservation')
        result['assetObservationOmittedForSize'] = True
    if len(_key(result)) > 5500:
        result.pop('failureObservation', None)
        result['failureObservationOmittedForSize'] = True
    return result if len(_key(result)) <= 5500 else None


def browser_execution_diagnostic(run, task, call):
    """Latest current browser failure, still current across read-only activity."""
    if not _scope(run, task) or not isinstance(call, dict):
        return None
    indices = [i for i,c in enumerate(run['calls']) if c.get('id') == call.get('id')]
    if len(indices) != 1 or run['calls'][indices[0]] != call:
        return None
    for later in run['calls'][indices[0]+1:]:
        if later.get('agentId') == task['agentId'] and later.get('tool') not in _READONLY:
            return None
    value = _execution(run, task, call)
    return _view(run, task, call, value) if value is not None else None


def current_browser_execution_diagnostic(run, task):
    """Do not lose a genuine current diagnosis just because its source was read."""
    if not _scope(run, task):
        return None
    for call in reversed(run['calls']):
        if call.get('agentId') != task['agentId'] or call.get('tool') in _READONLY:
            continue
        return browser_execution_diagnostic(run, task, call)
    return None


def browser_execution_failure_series(run, task, action):
    """Two actual matching execution failures; never counts rejected proposals."""
    if (not _scope(run, task) or not isinstance(action, dict) or action.get('type') != 'tool'
            or action.get('name') != 'code.test'):
        return None
    proposed = _effective_args(action.get('args'))
    if proposed is None:
        return None
    groups, source, latest = {}, None, None
    for call in run['calls']:
        if call.get('agentId') != task['agentId'] or call.get('tool') in _READONLY:
            continue
        value = _execution(run, task, call)
        if value is None:
            groups, source, latest = {}, None, None
            continue
        if value['manifest'] != source:
            groups = {}
        source = value['manifest']
        args_key = _key(value['args'])
        if value['success']:
            groups = {k:v for k,v in groups.items() if k[0] != args_key}
            latest = None
        elif value['kind']:
            key = (args_key, _key(value['failureIdentity']))
            groups.setdefault(key, []).append((call, value))
            latest = key
        else:
            # A different assertion result is not proof that a runtime error
            # was fixed, but it supersedes the current call's diagnostic.
            latest = None
    if latest is None or latest[0] != _key(proposed) or len(groups.get(latest, [])) < 2:
        return None
    rows = groups[latest]
    call, value = rows[-1]
    diagnostic = _view(run, task, call, value)
    if diagnostic is None:
        return None
    identity = {'runId': run['id'], 'taskId': task['id'], 'agentId': task['agentId'],
                'revision': task['revision'], 'latestCallId': call['id'], 'sourceManifest': source,
                'effectiveArgs': proposed, 'failure': value['failureIdentity']}
    result = {'runId': run['id'], 'taskId': task['id'], 'agentId': task['agentId'],
        'revision': task['revision'], 'kind': value['kind'], 'failureSignature': _digest(identity),
        'diagnosisOnly': True, 'acceptanceCreated': False,
        'observedFailures': {'count': len(rows), 'callIds': [c['id'] for c,v in rows[-4:]],
            'callIdsTruncated': len(rows) > 4, 'latestCallId': call['id']},
        'sourceManifest': diagnostic['sourceManifest'],
        'sourceManifestOmitted': diagnostic['sourceManifestOmitted'], 'diagnostic': diagnostic}
    if len(_key(result)) > 6900:
        diagnostic.pop('assetObservation', None)
        diagnostic['assetObservationOmittedForSize'] = True
    return result if len(_key(result)) <= 6900 else None
