"""Bounded recovery for observed browser execution failures, never completion.

Two host-recorded diagnostic replies allow correction. A third unchanged
proposal requests a real final response under the existing deadline. Neither
model text nor an isolated event can manufacture that closing checkpoint.
"""
import copy
import json
import re

from .browser_execution_diagnostics import browser_execution_failure_series
from .browser_failure_recovery import _key


_KIND = 'browser_execution_diagnostic'
_EVENT = 'browser_failure_recovery'
_CODE = 'repeated_browser_execution_failure'
_MAX_FEEDBACK = 10000


def _scope(run, task, agents):
    if not all(isinstance(v, dict) for v in (run, task, agents)):
        return False
    if run.get('status') not in ('running', 'waiting_human') or task.get('status') != 'running':
        return False
    if (not isinstance(task.get('agentId'), str) or not task['agentId']
            or type(task.get('revision')) is not int or task['revision'] < 0):
        return False
    agent = agents.get(task.get('agentId'))
    steps = run.get('steps')
    if (not isinstance(agent, dict) or not isinstance(steps, list)
            or not isinstance(agent.get('capabilities', []), list)):
        return False
    matches = [t for t in steps if isinstance(t, dict) and t.get('id') == task.get('id')]
    return len(matches) == 1 and matches[0] == task


def _review_targets(run, task, series, agents):
    """Metadata of declared current targets; no inferred access or test coverage."""
    if 'review' not in agents[task['agentId']].get('capabilities', []):
        return []
    deps, declared = task.get('dependsOn', []), task.get('reviewOf')
    if not isinstance(deps, list) or any(not isinstance(x, str) for x in deps):
        return []
    if declared is None or declared == []:
        declared = deps
    if not isinstance(declared, list) or any(not isinstance(x, str) for x in declared):
        return []
    call = next(c for c in run['calls'] if c['id'] == series['observedFailures']['latestCallId'])
    snapshots = call.get('reviewTargets')
    if not isinstance(snapshots, dict):
        return []
    artifacts = run.get('artifacts', [])
    if not isinstance(artifacts, list) or any(not isinstance(a, dict) for a in artifacts):
        return []
    rows = []
    for identifier in dict.fromkeys(declared):
        targets = [t for t in run['steps'] if isinstance(t, dict) and t.get('id') == identifier]
        if len(targets) != 1 or identifier not in deps:
            continue
        target = targets[0]
        if (identifier == task['id'] or target.get('agentId') == task['agentId']
                or not isinstance(target.get('agentId'), str)
                or target.get('status') not in ('done', 'partial')
                or type(target.get('revision')) is not int or target['revision'] < 0
                or target.get('_planning') or target.get('kind') == 'planning' or target.get('finalSummary')):
            continue
        outputs = target.get('outputs', [])
        if not isinstance(outputs, list) or any(not isinstance(x, str) for x in outputs):
            continue
        selected = [a for a in artifacts if a.get('id') in outputs]
        if (len(selected) != len(set(outputs)) or len({a.get('id') for a in selected}) != len(selected)
                or any(a.get('runId') != run['id'] or a.get('taskId') != identifier
                       or a.get('ownerAgentId', a.get('owner')) != target['agentId']
                       or not isinstance(a.get('sha256'), str)
                       or not re.fullmatch('[0-9a-f]{64}', a['sha256']) for a in selected)):
            continue
        snapshot = snapshots.get(identifier)
        expected = {'targetRevision': target['revision'], 'artifactHashes': {a['id']: a['sha256'] for a in selected}}
        if (not isinstance(snapshot, dict) or type(snapshot.get('targetRevision')) is not int
                or snapshot != expected):
            continue
        rows.append({'taskId': identifier, 'agentId': target['agentId'], 'revision': target['revision']})
    # IDs must stay exact, so bound the number of metadata rows rather than
    # truncating an identifier into an unusable or different review target.
    bounded = []
    for row in rows:
        if len(bounded) == 8 or len(_key(bounded + [row])) > 900:
            break
        bounded.append(row)
    return bounded


def _receipts(run, task, series):
    """Count paired host receipts, each following its own real model proposal."""
    events, transcript = run.get('events', []), run.get('transcript', [])
    if (not isinstance(events, list) or not isinstance(transcript, list)
            or any(not isinstance(x, dict) for x in events + transcript)):
        return []
    actor, task_id, revision = task['agentId'], task['id'], task['revision']
    def unique_id(row, collection):
        identifier = row.get('id')
        return isinstance(identifier, str) and bool(identifier) and sum(r.get('id') == identifier for r in collection) == 1
    def same_scope(row):
        return row.get('runId') == run['id'] and row.get('taskId') == task_id and row.get('agentId') == actor
    def model_call(row):
        matches = [c for c in run['calls'] if c.get('id') == row.get('callId')]
        if len(matches) != 1:
            return None
        call = matches[0]
        if (call.get('runId', run['id']) != run['id'] or call.get('taskId') != task_id
                or call.get('agentId') != actor or call.get('tool') != 'llm.chat'
                or call.get('status') != 'success' or type(call.get('revision')) is not int
                or call['revision'] != revision):
            return None
        return call
    accepted, used_calls, event_position, response = [], set(), -1, None
    for row in transcript:
        if not same_scope(row):
            continue
        if row.get('kind') == 'model_response':
            response = None
            if row.get('role') != 'agent' or not unique_id(row, transcript):
                continue
            call = model_call(row)
            if call is None:
                continue
            try:
                proposal = json.loads(row.get('text', ''))
            except (TypeError, ValueError):
                continue
            observed = browser_execution_failure_series(run, task, proposal)
            if observed and observed['failureSignature'] == series['failureSignature']:
                response = call
            continue
        if (row.get('kind') != _EVENT or row.get('role') != 'system' or response is None
                or response['id'] in used_calls or not unique_id(row, transcript)
                or row.get('callId', response['id']) != response['id']):
            continue
        try:
            payload = json.loads(row.get('text', ''))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        recovery = {'kind': _KIND, 'attempt': len(accepted) + 1,
                    'priorFeedbacks': len(accepted), 'requireFinal': len(accepted) >= 2}
        actual = payload.get('recovery')
        if (not isinstance(actual, dict) or type(actual.get('attempt')) is not int
                or type(actual.get('priorFeedbacks')) is not int or type(actual.get('requireFinal')) is not bool
                or actual != recovery or payload.get('ok') is not False
                or payload.get('notExecuted') is not True or payload.get('actionExecuted') is not False
                or not isinstance(payload.get('error'), dict) or payload['error'].get('code') != _CODE
                or any(payload.get(k) != series[k] for k in
                       ('runId', 'taskId', 'agentId', 'revision', 'kind', 'failureSignature',
                        'diagnosisOnly', 'acceptanceCreated', 'observedFailures', 'diagnostic'))
                or type(payload.get('revision')) is not int):
            continue
        expected = {'taskId': task_id, 'observedCallIds': series['observedFailures']['callIds'],
                    'failureSignature': series['failureSignature'], 'recovery': recovery}
        event = next(((i, e) for i, e in enumerate(events) if i > event_position
                      and e.get('type') == _EVENT and e.get('actor') == actor and e.get('agentId') == actor
                      and e.get('runId', run['id']) == run['id'] and e.get('payload') == expected
                      and unique_id(e, events)), None)
        if event is None:
            continue
        event_position = event[0]
        used_calls.add(response['id'])
        accepted.append(payload)
        if len(accepted) == 3:
            break
    return accepted


def _feedback(run, task, series, agents, prior):
    result = copy.deepcopy(series)
    closing = prior >= 2
    result.update(ok=False, notExecuted=True, actionExecuted=False,
                  error={'code': _CODE, 'retryable': False,
                         'message': 'The same actual browser execution failure remains unresolved; this proposal was not executed.'},
                  recovery={'kind': _KIND, 'attempt': min(prior + 1, 3),
                            'priorFeedbacks': min(prior, 2), 'requireFinal': closing})
    targets = _review_targets(run, task, series, agents)
    result['reviewTargets'] = targets
    if closing:
        result['instruction'] = (
            'Two diagnostic replies were already ignored for this exact failure and source version. '
            'Your next response must be an explicit final with truthful status and text: identify existing artifact/delivery IDs, '
            'the observed failed call IDs, usable scope and unresolved gaps. If requirements remain unmet, report partial. '
            'No further action is authorized by this checkpoint; it creates no publication, acceptance or completion. '
            'Keep the original deadline, permissions and output contract.')
    else:
        common = ('Inspect the recorded failure and already authorized source before another test. '
                  'Correct a selector/setup only when supported by actual DOM/source evidence; do not weaken acceptance, '
                  'suppress an unhandled exception, or treat unserved files as proven missing dependencies. ')
        if targets:
            route = ('The listed IDs/revisions are current declared review targets, not proof that this test covers all of them. '
                     'If the evidence demonstrates a product defect in a listed target, issue an actual review with verdict rework, '
                     'that top-level taskId, the failed call ID and precise reproduction/correction criteria; normal review gates still apply. '
                     'For a test/setup defect, correct your own test/setup first. ')
        else:
            route = ('Fix your own selector, resource mapping or runtime/source error when the evidence supports it. '
                     'No eligible independent review target is established here; do not invent a target or wait for downstream QA. ')
        result['instruction'] = common + route + (
            'If a useful product exists but the failure cannot be resolved, explicitly publish usable files under the existing '
            'contract/candidate rules and then give a truthful partial final with limitations. Otherwise report partial without '
            'claiming unpublished work. Do not delegate a duplicate task, repeat this unchanged test or await dependent QA. '
            'This diagnosis grants no source access, creates no review result, and extends no budget.')
    encoded = _key(result)
    return result if encoded is not None and len(encoded) <= _MAX_FEEDBACK else None


def browser_diagnostic_recovery(run, task, action, agents):
    """Return one bounded host observation; never mutate or execute anything."""
    if not _scope(run, task, agents):
        return None
    series = browser_execution_failure_series(run, task, action)
    if series is None:
        return None
    return _feedback(run, task, series, agents, len(_receipts(run, task, series)))


def current_browser_diagnostic_checkpoint(run, task, agents):
    """Revalidate a real closing receipt; a stored marker alone is not authority."""
    if not _scope(run, task, agents):
        return None
    marker = task.get('_browserDiagnosticClosing')
    if not isinstance(marker, dict) or not isinstance(marker.get('proposal'), dict):
        return None
    series = browser_execution_failure_series(run, task, marker['proposal'])
    if series is None or series['failureSignature'] != marker.get('failureSignature'):
        return None
    receipts = _receipts(run, task, series)
    if len(receipts) != 3 or receipts[-1]['recovery']['requireFinal'] is not True:
        return None
    return _feedback(run, task, series, agents, 2)
