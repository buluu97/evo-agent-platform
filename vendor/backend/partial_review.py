"""Identify current published work that an explicit reviewer may inspect.

These pure functions neither grant files nor change execution/acceptance states.
Ordinary consumers still require successful dependencies. A surviving snapshot
is evidence to review, never evidence that the producer completed its task.
"""
import copy
from datetime import datetime
import hashlib
import json


def _event_time_ms(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return int(parsed.timestamp() * 1000) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def budget_partial_receipt(run, task):
    if not isinstance(run, dict) or not isinstance(task, dict):
        return None
    error = task.get('error')
    if (task.get('status') not in ('failed', 'partial')
            or not isinstance(error, dict) or error.get('code') != 'budget'
            or any(task.get(key) for key in ('_pendingTool', '_uncertainCalls', '_reworkWait'))):
        return None
    hashes = _current_published_hashes(run, task)
    if hashes is None:
        return None
    return {'taskId': task['id'], 'revision': task['revision'], 'executionStatus': task['status'],
            'artifactHashes': hashes, 'error': copy.deepcopy(task['error']),
            'acceptance': 'unassessed', 'purpose': 'read_only_review_of_budget_limited_work'}


def _current_published_hashes(run, task):
    """Check actual registered UTF-8 bytes and their latest workspace observation."""
    if any(not isinstance(row.get('id'), str) or not row['id'] for row in (run, task)):
        return None
    revision = task.get('revision')
    if type(revision) is not int or revision < 0:
        return None
    outputs = task.get('outputs')
    if (not isinstance(outputs, list) or not outputs
            or any(not isinstance(item, str) or not item for item in outputs)
            or len(set(outputs)) != len(outputs)):
        return None
    artifacts, all_calls = run.get('artifacts', []), run.get('calls', [])
    if any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
           for rows in (artifacts, all_calls)):
        return None
    items = [item for item in artifacts if item.get('id') in outputs]
    if len(items) != len(outputs) or len({item.get('id') for item in items}) != len(outputs):
        return None
    owner = task.get('agentId')
    if not isinstance(owner, str) or not owner:
        return None
    owner_calls = [(index, call) for index, call in enumerate(all_calls) if call.get('agentId') == owner]
    if any(not isinstance(call.get('args'), dict) for _, call in owner_calls
           if call.get('tool') in ('file.write', 'file.patch', 'file.delete', 'file.read', 'code.run', 'code.test')):
        return None
    if any(call.get('tool') in ('code.run', 'code.test')
           and call.get('status') in ('running', 'pending', 'uncertain') for _, call in owner_calls):
        return None
    paths = set()
    hashes = {}
    for item in items:
        path = item.get('path')
        if (not isinstance(path, str) or not path or path in paths
                or item.get('runId') != run.get('id') or item.get('taskId') != task.get('id')
                or item.get('ownerAgentId', item.get('owner')) != owner
                or item.get('sourceRunId') or item.get('inheritedReadOnly')):
            return None
        paths.add(path)
        try:
            raw = item['content'].encode('utf-8')
        except (KeyError, AttributeError, UnicodeEncodeError):
            return None
        digest = hashlib.sha256(raw).hexdigest()
        if digest != item.get('sha256') or type(item.get('size')) is not int or item['size'] != len(raw):
            return None
        # Same-owner workspaces are shared within this run. A later task may
        # replace/delete the path; an old published snapshot is then not current.
        calls = [(index, call) for index, call in owner_calls
                 if call.get('tool') in ('file.write', 'file.patch', 'file.delete', 'file.read')
                 and call['args'].get('path') == path]
        if any(call.get('status') in ('running', 'pending', 'uncertain') for _, call in calls):
            return None
        successful = [(index, call) for index, call in calls if call.get('status') == 'success'
                      and isinstance(call.get('result'), dict) and call['result'].get('ok') is True]
        if not successful:
            return None
        latest_index, latest = successful[-1]
        if (latest.get('tool') == 'file.delete' or latest['result'].get('sha256') != digest
                or latest.get('taskId') != task.get('id')
                or type(latest.get('revision')) is not int or latest['revision'] != revision):
            return None
        # A failed/reconciled write may have taken effect without a trustworthy
        # result. Only a later successful observation may re-establish bytes.
        if any(index > latest_index and call.get('tool') in ('file.write', 'file.patch', 'file.delete')
               for index, call in calls):
            return None
        # Python/JS can modify any private workspace path, including on failure.
        # Require a later matching file observation before accepting that path.
        # The trusted browser harness only sees frozen, read-only source copies.
        for index, call in owner_calls:
            if index <= latest_index or call.get('tool') not in ('code.run', 'code.test'):
                continue
            result = call.get('result')
            readonly_browser = (call['args'].get('language') == 'browser'
                                and isinstance(result, dict) and result.get('runtime') == 'browser'
                                and call.get('status') in ('success', 'failed'))
            if not readonly_browser:
                return None
        hashes[item['id']] = digest
    # Refuse an older registration at the same path, even when its bytes match.
    for path in paths:
        publications = [item for item in artifacts
                        if item.get('runId') == run.get('id') and item.get('path') == path
                        and item.get('ownerAgentId', item.get('owner')) == owner
                        and not item.get('sourceRunId')]
        if not publications or publications[-1].get('id') not in outputs:
            return None
    return hashes


def declared_partial_receipt(run, task):
    """Expose only a real, voluntarily final-partial snapshot for read-only review.

    The final model response must be linked to the current successful model call,
    agree with the stored result, and be followed by the host's task_finished
    event. Neither a surviving file nor a model's prose can attest completion.
    Old records without the necessary provenance remain ineligible.
    """
    if not isinstance(run, dict) or not isinstance(task, dict):
        return None
    error = task.get('error')
    if (task.get('status') != 'partial' or (error is not None and error != {})
            or (error is not None and not isinstance(error, dict))
            or any(task.get(key) for key in ('_pendingTool', '_uncertainCalls', '_reworkWait', '_approvalId'))):
        return None
    hashes = _current_published_hashes(run, task)
    if hashes is None:
        return None
    owner, task_id, revision = task['agentId'], task['id'], task['revision']
    rows = [call for call in run['calls'] if call.get('taskId') == task_id]
    if (not rows or any(call.get('agentId') != owner for call in rows)
            or any(call.get('status') in ('running', 'pending', 'uncertain', 'unknown')
                   or any(call.get(key) is True for key in ('outcomeUnknown', 'uncertain', 'mayHaveCompleted'))
                   or (isinstance(call.get('result'), dict) and any(call['result'].get(key) is True
                       for key in ('outcomeUnknown', 'uncertain', 'mayHaveCompleted')))
                   for call in rows)):
        return None
    final_call = rows[-1]
    if (final_call.get('tool') != 'llm.chat' or final_call.get('status') != 'success'
            or type(final_call.get('revision')) is not int or final_call['revision'] != revision
            or not isinstance(final_call.get('id'), str) or not final_call['id']
            or sum(call.get('id') == final_call['id'] for call in run['calls']) != 1):
        return None
    transcript, events, approvals = (run.get('transcript'), run.get('events'), run.get('approvals', []))
    if any(not isinstance(values, list) or any(not isinstance(row, dict) for row in values)
           for values in (transcript, events, approvals)):
        return None
    if any(row.get('taskId') == task_id and row.get('status') == 'pending' for row in approvals):
        return None
    responses = [row for row in transcript if row.get('taskId') == task_id and row.get('kind') == 'model_response']
    if not responses:
        return None
    closing = responses[-1]
    if (closing.get('runId') != run['id'] or closing.get('agentId') != owner
            or closing.get('role') != 'agent' or closing.get('callId') != final_call['id']
            or ('revision' in closing and (type(closing['revision']) is not int or closing['revision'] != revision))):
        return None
    try:
        action = json.loads(closing.get('text', ''))
    except (TypeError, ValueError):
        return None
    if (not isinstance(action, dict) or action.get('type') != 'final' or action.get('status') != 'partial'
            or not isinstance(action.get('text'), str) or not action['text'].strip()
            or action['text'][:18000] != task.get('result')):
        return None
    lifecycle = [row for row in events if isinstance(row.get('payload'), dict)
                 and row['payload'].get('taskId') == task_id
                 and row.get('type') in ('task_accepted', 'task_assigned', 'task_retry', 'task_failed',
                                         'task_finished', 'review_rework', 'review_reject', 'effect_reconciliation_required')]
    if not lifecycle:
        return None
    finished = lifecycle[-1]
    payload = finished['payload']
    if (finished.get('type') != 'task_finished' or payload.get('status') != 'partial'
            or finished.get('agentId') != owner or finished.get('actor') != owner
            or not isinstance(finished.get('id'), str) or not finished['id']
            or ('revision' in payload and (type(payload['revision']) is not int or payload['revision'] != revision))):
        return None
    response_at, event_at = _event_time_ms(closing.get('time')), _event_time_ms(finished.get('time'))
    finished_at = task.get('finishedAt')
    if (response_at is None or event_at is None or response_at > event_at
            or type(finished_at) is not int or not response_at <= finished_at < event_at + 1000
            or (type(run.get('finishedAt')) is int and run['finishedAt'] < finished_at)):
        return None
    for artifact_id, digest in hashes.items():
        registrations = [row for row in events if row.get('type') == 'artifact_registered'
                         and isinstance(row.get('payload'), dict)
                         and row['payload'].get('artifactId') == artifact_id]
        if not registrations:
            return None
        registered = registrations[-1]
        registered_at = _event_time_ms(registered.get('time'))
        if (registered.get('agentId') != owner or registered.get('actor') != owner
                or registered['payload'].get('taskId') != task_id
                or registered['payload'].get('sha256') != digest
                or registered_at is None or registered_at > response_at):
            return None
    return {'taskId': task_id, 'revision': revision, 'executionStatus': 'partial',
            'artifactHashes': hashes, 'error': copy.deepcopy(error), 'acceptance': 'unassessed',
            'purpose': 'read_only_review_of_agent_declared_partial_work',
            'closure': {'modelCallId': final_call['id'], 'taskFinishedEventId': finished['id']}}


def review_dependency_exceptions(run, consumer, agents):
    """Return only explicit review dependencies with verifiable surviving bytes."""
    if not all(isinstance(value, dict) for value in (run, consumer, agents)):
        return {}
    owner = consumer.get('agentId')
    agent = agents.get(owner) if isinstance(owner, str) else None
    capabilities = consumer.get('requiredCapabilities')
    if (consumer.get('finalSummary') or not isinstance(capabilities, list) or 'review' not in capabilities
            or not isinstance(agent, dict) or not isinstance(agent.get('capabilities'), list)
            or 'review' not in agent['capabilities']):
        return {}
    steps, dependencies = run.get('steps'), consumer.get('dependsOn')
    if (not isinstance(steps, list) or any(not isinstance(task, dict) or not isinstance(task.get('id'), str) for task in steps)
            or not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies)):
        return {}
    tasks = {task['id']: task for task in steps}
    if len(tasks) != len(steps):
        return {}
    exceptions = {}
    for task_id in dependencies:
        producer = tasks.get(task_id)
        if not producer or producer.get('agentId') == owner:
            continue
        receipt = budget_partial_receipt(run, producer) or declared_partial_receipt(run, producer)
        if receipt is not None and producer.get('status') == 'partial':
            exceptions[task_id] = receipt
    return exceptions
