"""Periodic, deterministic observations; never execute, retry, accept or stop work.

The host calls watchdog_tick under its state lock and saves the returned state.
A notice may be queued during an active call, but current_watchdog_notice only
releases it at a safe boundary. The host marks _watchdogDeliveredNoticeId after
delivery. No model text, new call ID or repeated publication is useful progress.
"""
import copy
import hashlib
import json
import re


_TERMINAL = {'done', 'partial', 'failed', 'cancelled', 'rejected', 'skipped'}
_HEX = re.compile(r'^[0-9a-f]{64}$')
_FLAGS = ('outcomeUnknown', 'outcome_unknown', 'deadlineExceeded', 'timedOut',
          'cancelled', 'outputLimitExceeded', 'notExecuted', 'modelViewOnly', 'needsApproval')
_MAX_EVIDENCE = 4096
_IDENTIFIER = re.compile(r'\b(?:artifact|call|evt)-[0-9a-f]{16,64}\b')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _sha(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _content_key(value):
    if isinstance(value, str):
        return _IDENTIFIER.sub('<record-id>', value)
    if isinstance(value, (list, tuple)):
        return [_content_key(x) for x in value]
    if isinstance(value, dict):
        return {k: _content_key(v) for k, v in value.items() if k not in ('callId', 'artifactId', 'eventId')}
    return value


def _binding(run, task):
    return {'runId': run['id'], 'taskId': task['id'], 'agentId': task.get('agentId'),
            'revision': task.get('revision', 0)}


def _valid(run, task):
    return (isinstance(run, dict) and isinstance(task, dict)
        and all(isinstance(x, str) and x for x in (run.get('id'), task.get('id'), task.get('agentId')))
        and type(task.get('revision')) is int and task['revision'] >= 0
        and sum(t.get('id') == task['id'] and t.get('agentId') == task['agentId']
                and t.get('revision') == task['revision'] for t in run.get('steps', []) if isinstance(t, dict)) == 1)


def _unknown(value):
    if not isinstance(value, dict):
        return False
    return (value.get('outcomeUnknown') is True or value.get('outcome_unknown') is True
        or value.get('code') in ('uncertain', 'outcome_unknown')
        or any(_unknown(value.get(k)) for k in ('result', 'error', 'transport')))


def _settled(call):
    result = call.get('result')
    return (call.get('status') in ('success', 'failed', 'denied', 'not_executed', 'reconciled')
        and not _unknown(call) and not (isinstance(result, dict) and result.get('needsApproval')))


def _owned_calls(run, task):
    return [c for c in run.get('calls', []) if isinstance(c, dict)
        and c.get('runId', run['id']) == run['id'] and c.get('agentId') == task['agentId']
        and c.get('taskId') == task['id'] and type(c.get('revision')) is int
        and c['revision'] == task['revision']]


def _wait_state(run, task, calls):
    # Unknown effects outrank every ordinary wait; this module never reconciles.
    owner_tools = [c for c in run.get('calls', []) if isinstance(c, dict)
        and c.get('agentId') == task['agentId'] and c.get('tool') != 'llm.chat']
    if (task.get('_uncertainCalls') or (task.get('_pendingTool') or {}).get('phase') == 'uncertain'
            or any(c.get('status') in ('unknown', 'uncertain') or _unknown(c) for c in owner_tools)):
        return 'unknown_effect'
    if run.get('status') == 'interrupted':
        return 'interrupted'
    if run.get('userPaused') or run.get('status') == 'paused':
        return 'paused'
    if (task.get('status') == 'waiting_human' or any(a.get('status') == 'pending'
            and a.get('taskId') == task['id'] for a in run.get('approvals', []) if isinstance(a, dict))
            or (task.get('_pendingTool') or {}).get('phase') == 'approval'):
        return 'approval'
    if task.get('status') in ('pending', 'waiting_children') or task.get('_reworkWait'):
        return 'dependency' if task.get('dependsOn') or task.get('childIds') or task.get('_reworkWait') else 'scheduler'
    active = [c for c in calls if c.get('status') in ('running', 'pending', 'reserved')]
    if active:
        return 'active_tool' if any(c.get('tool') != 'llm.chat' for c in active) else 'active_model'
    return 'safe_boundary'


def _files(rows):
    return sorted({(r.get('path', ''), r['sha256']) for r in rows
        if isinstance(r, dict) and isinstance(r.get('path'), str) and r['path'] and _sha(r.get('sha256'))})


def _failure(result):
    browser = result.get('browser')
    if isinstance(browser, dict) and browser.get('executed') is True:
        failed = [s for s in browser.get('steps', []) if isinstance(s, dict) and s.get('ok') is False]
        # Expected strings and unexecuted suffixes are not new observed failure.
        return {'source': _files(browser.get('sourceManifest', [])),
            'steps': [{k: s.get(k) for k in ('action', 'pageId', 'selector', 'actual', 'error')} for s in failed],
            'pageErrors': browser.get('pageErrors', [])}
    error = result.get('error')
    if isinstance(error, dict):
        return {k: error.get(k) for k in ('code', 'message')}
    return str(error or '')[:2000]


def _tool_evidence(call):
    """Content witnesses, not identities/counters or unexecuted proposals."""
    result = call.get('result'); name = call.get('tool'); args = call.get('args') or {}
    if not isinstance(result, dict) or name == 'llm.chat' or not _settled(call):
        return None
    if any(result.get(k) for k in _FLAGS) or result.get('actionExecuted') is False:
        return None
    if call['status'] == 'failed' and result.get('ok') is False:
        return ('observed_failure', name, _failure(result))
    if call['status'] != 'success' or result.get('ok') is not True:
        return None
    if name in ('file.write', 'file.patch', 'file.read') and _sha(result.get('sha256')):
        window = {k: result.get(k) for k in ('startLine', 'endLine', 'startColumn', 'endColumn', 'mode')}
        if name == 'file.read' and isinstance(result.get('matches'), list):
            window['matches'] = [{k: x.get(k) for k in ('startLine', 'endLine', 'startColumn', 'endColumn')}
                                 for x in result['matches'] if isinstance(x, dict)]
        return (name, result.get('path', args.get('path')), result['sha256'], window if name == 'file.read' else None)
    if name == 'web.fetch' and isinstance(result.get('sourceSnapshot'), dict):
        source = result['sourceSnapshot']
        if _sha(source.get('sha256')):
            return (name, source.get('url'), source['sha256'])
    if name == 'web.search' and isinstance(result.get('results'), list):
        return (name, sorted({str(x.get('url')) for x in result['results'] if isinstance(x, dict) and x.get('url')}))
    if name in ('code.test', 'code.run'):
        browser = result.get('browser')
        if isinstance(browser, dict):
            if browser.get('executed') is not True or browser.get('ok') is not True:
                return None
            return (name, _files(browser.get('testedFiles', [])),
                    [{k: a.get(k) for k in ('action', 'pageId', 'selector', 'expected', 'actual', 'passed')}
                     for a in browser.get('assertions', []) if isinstance(a, dict)])
        if type(result.get('exitCode')) is int:
            return (name, result.get('runtime'), result['exitCode'],
                    _hash(str(result.get('stdout', ''))), _hash(str(result.get('stderr', ''))))
    return None


def assess_task_progress(run, task):
    """Read-only snapshot; an observed change is not proof of product quality."""
    if not _valid(run, task):
        return None
    calls = _owned_calls(run, task)
    evidence = set()
    for call in calls:
        witness = _tool_evidence(call)
        if witness is not None:
            result = call.get('result') or {}; browser = result.get('browser')
            if call.get('tool') in ('code.test', 'code.run') and call.get('status') == 'success' and isinstance(browser, dict):
                # Rearranging or combining already observed passing assertions
                # is not new coverage. Each actual source/check pair is unique.
                for assertion in witness[2]:
                    evidence.add(_hash(_content_key((witness[0], witness[1], assertion))))
            else:
                evidence.add(_hash(_content_key(witness)))
    artifacts = [a for a in run.get('artifacts', []) if isinstance(a, dict)
        and a.get('runId') == run['id'] and a.get('taskId') == task['id']
        and a.get('ownerAgentId', a.get('owner')) == task['agentId']
        and a.get('id') in task.get('outputs', []) and not a.get('carriedForward')]
    for path, sha in _files(artifacts):
        evidence.add(_hash(('published_content', path, sha)))
    for delivery in run.get('deliveries', []):
        if (isinstance(delivery, dict) and delivery.get('runId') == run['id']
                and delivery.get('taskId') == task['id'] and delivery.get('agentId') == task['agentId']
                and delivery.get('taskRevision') == task['revision']
                and delivery.get('status') in ('candidate', 'packaged') and _files(delivery.get('files', []))):
            evidence.add(_hash(('published_package', delivery['status'], delivery.get('entrypoint'),
                               _files(delivery['files']), delivery.get('instructions'))))
    for other in run.get('steps', []):
        if not isinstance(other, dict):
            continue
        if other.get('id') in task.get('childIds', []) + task.get('dependsOn', []) and other.get('status') in _TERMINAL:
            evidence.add(_hash(('related_task_terminal', other.get('agentId'), other['status'],
                               _hash(_content_key(other.get('result', ''))))))
        validation = other.get('validation')
        if not isinstance(validation, dict) or validation.get('reviewerTaskId') != task['id']:
            continue
        if (type(validation.get('reviewerRevision')) is not int or type(validation.get('targetRevision')) is not int
                or validation['reviewerRevision'] != task['revision'] or validation['targetRevision'] != other.get('revision')
                or validation.get('verdict') not in ('pass', 'reject', 'rework')):
            continue
        actual = {a['id']: a['sha256'] for a in run.get('artifacts', []) if isinstance(a, dict)
                  and a.get('id') in other.get('outputs', []) and _sha(a.get('sha256'))
                  and a.get('runId') == run['id'] and a.get('taskId') == other.get('id')
                  and a.get('ownerAgentId', a.get('owner')) == other.get('agentId')}
        if actual and len(actual) == len(set(other.get('outputs', []))) and validation.get('artifactHashes') == actual:
            evidence.add(_hash(('current_review_state', validation.get('verdict'), validation.get('verified'),
                               other.get('revision'), sorted(actual.values()))))
    settled = {c.get('id') for c in calls if isinstance(c.get('id'), str) and _settled(c)}
    return {**_binding(run, task), 'waitState': _wait_state(run, task, calls),
            'evidence': sorted(evidence)[:_MAX_EVIDENCE], 'evidenceSignature': _hash(sorted(evidence)),
            'settledCount': len(settled), 'evidenceSaturated': len(evidence) > _MAX_EVIDENCE}


def watchdog_tick(run, previous_state, now_ms, interval_seconds=90):
    """At most one notice per unchanged evidence epoch; no wall-time stop."""
    interval = max(60, min(120, int(interval_seconds))) * 1000
    if type(now_ms) is not int or not isinstance(run, dict) or not isinstance(run.get('id'), str):
        raise ValueError('watchdog requires a current run and integer clock milliseconds')
    previous = previous_state if isinstance(previous_state, dict) and previous_state.get('version') == 1 and previous_state.get('runId') == run['id'] else {}
    terminal = run.get('status') in _TERMINAL
    if terminal:
        return {'due': False, 'terminal': True, 'state': previous, 'assessments': [], 'notices': []}
    if type(previous.get('nextCheckAt')) is int and now_ms < previous['nextCheckAt']:
        return {'due': False, 'terminal': False, 'state': previous, 'assessments': [], 'notices': []}
    state = {'version': 1, 'runId': run['id'], 'lastCheckAt': now_ms,
             'nextCheckAt': now_ms + interval, 'intervalMs': interval, 'tasks': {}}
    assessments = []; notices = []
    for task in run.get('steps', []):
        if not isinstance(task, dict) or task.get('status') in _TERMINAL:
            continue
        snap = assess_task_progress(run, task)
        if snap is None:
            continue
        old = (previous.get('tasks') or {}).get(task['id'], {})
        if not isinstance(old, dict):
            old = {}
        same = (all(old.get(k) == snap[k] for k in ('runId', 'taskId', 'agentId', 'revision'))
                and type(old.get('lastProgressAt')) is int and type(old.get('settledAtProgress')) is int)
        progressed = not same or old.get('evidenceSignature') != snap['evidenceSignature']
        row = {**snap, 'lastProgressAt': now_ms if progressed else old['lastProgressAt'],
               'settledAtProgress': snap['settledCount'] if progressed else old['settledAtProgress'],
               'notice': None if progressed else old.get('notice')}
        unchanged_ms = max(0, now_ms - row['lastProgressAt'])
        new_settled = max(0, snap['settledCount'] - row['settledAtProgress'])
        ordinary = snap['waitState'] in ('safe_boundary', 'active_model', 'active_tool')
        # Busy loops can be observed while their next call is running. A single
        # long call with no newly settled behavior remains an ordinary wait.
        eligible = (ordinary and not snap['evidenceSaturated'] and unchanged_ms >= 2 * interval
                    and new_settled >= 2)
        if eligible and row['notice'] is None:
            notice = {**_binding(run, task), 'evidenceSignature': snap['evidenceSignature'],
                'observedAt': now_ms, 'lastProgressAt': row['lastProgressAt'],
                'unchangedMs': unchanged_ms, 'settledWithoutNewEvidence': new_settled,
                'diagnosisOnly': True, 'acceptanceCreated': False, 'actionExecuted': False,
                'instruction': 'Recent settled activity added no new content or observed result. Inspect the existing actual failure/source receipts, correct an evidenced cause, use the existing review/rework path, or report the remaining gap honestly. Do not repeat unchanged work, invent success, extend the deadline, bypass approval, or replay an unknown effect. This observation does not stop or complete the task.'}
            notice['noticeId'] = 'watchdog-' + _hash((notice['runId'], notice['taskId'], notice['agentId'],
                notice['revision'], notice['evidenceSignature'], notice['lastProgressAt']))[:24]
            row['notice'] = notice
            notices.append(copy.deepcopy(notice))
        state['tasks'][task['id']] = row
        assessments.append({k: row[k] for k in ('taskId', 'agentId', 'revision', 'waitState', 'evidenceSignature', 'settledCount')})
    return {'due': True, 'terminal': False, 'state': state, 'assessments': assessments, 'notices': notices}


def current_watchdog_notice(run, task, state, now_ms):
    """Revalidate before the next model request; never interrupt active effects."""
    if (not isinstance(run, dict) or not isinstance(task, dict) or not isinstance(state, dict)
            or state.get('version') != 1 or state.get('runId') != run.get('id')
            or run.get('status') in _TERMINAL or task.get('status') in _TERMINAL):
        return None
    row = (state.get('tasks') or {}).get(task.get('id'))
    notice = row.get('notice') if isinstance(row, dict) else None
    if not isinstance(notice, dict) or type(now_ms) is not int or now_ms < notice.get('observedAt', now_ms + 1):
        return None
    if task.get('_watchdogDeliveredNoticeId') == notice.get('noticeId'):
        return None
    snap = assess_task_progress(run, task)
    if (snap is None or snap['waitState'] != 'safe_boundary' or snap['evidenceSaturated']
            or any(notice.get(k) != snap[k] for k in ('runId', 'taskId', 'agentId', 'revision', 'evidenceSignature'))):
        return None
    return copy.deepcopy(notice)
