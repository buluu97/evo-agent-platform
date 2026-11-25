"""One clarification of a voluntary partial's completion scope, never acceptance.

Only actual current published bytes and the current successful model proposal
can make this feedback eligible. The caller records it durably before another
model turn. No task, source verdict, budget, review or tool is changed here.
"""
import copy
import hashlib
import json

from .partial_review import _current_published_hashes
from .generation_retry import trusted_generation_failure


def _closed_generation_failure(call, revision):
    """Distinguish unknown billing from an unresolved tool/model operation.

    A failed, reaped text-generation worker accepted no action. Its unknown
    usage is still kept in the ledger; this does not cancel its upstream work.
    """
    error = call.get('error')
    if (call.get('tool') != 'llm.chat' or call.get('status') != 'failed'
            or type(call.get('revision')) is not int or call['revision'] != revision
            or not isinstance(error, dict)):
        return False
    transport = error.get('transport')
    return bool(isinstance(transport, dict)
                and transport.get('generationRetryEligible') is True
                and transport.get('retryScope') == 'model_generation_only'
                and trusted_generation_failure(error.get('code'), error.get('phase'),
                                               transport.get('upstreamRetryable'), transport))


def completion_clarification_feedback(run, task, action, validation, *,
                                      final_only=False, stage_phase=None, now_ms=None):
    """Return bounded server feedback, or None when the original partial must close.

    A passed declared contract is format/evidence information, not a semantic
    verdict. In particular, model-written MET labels do not enter this gate.
    """
    if not all(isinstance(value, dict) for value in (run, task, action, validation)):
        return None
    revision = task.get('revision')
    if (type(revision) is not int or revision < 0 or task.get('status') != 'running'
            or run.get('status') not in ('running', 'waiting_human')
            or final_only or task.get('finalSummary')
            or action.get('type') != 'final' or action.get('status') != 'partial'
            or not isinstance(action.get('text'), str) or not action['text'].strip()
            or task.get('error') not in (None, {})
            or any(task.get(key) for key in ('_pendingTool', '_uncertainCalls', '_approvalId',
                                            '_reworkWait', '_recheckTargets'))):
        return None
    previous = task.get('_completionClarification')
    if previous is not None:
        if (not isinstance(previous, dict) or type(previous.get('revision')) is not int
                or previous['revision'] >= revision):
            return None
    budget = run.get('budget', {})
    if isinstance(budget, dict) and budget.get('profile') == 'time_only':
        deadline = budget.get('deadlineAt')
        if (type(deadline) is not int or type(now_ms) is not int or now_ms >= deadline):
            return None
    contract = task.get('outputContract')
    if (not isinstance(contract, dict) or contract.get('kind') not in ('artifact', 'text')
            or validation.get('kind') != contract['kind'] or validation.get('status') != 'passed'
            or validation.get('semanticQuality') != 'unassessed'):
        return None
    criteria = task.get('acceptanceCriteria')
    if (not isinstance(criteria, list) or not 1 <= len(criteria) <= 12
            or any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in criteria)):
        return None
    hashes = _current_published_hashes(run, task)
    matched = validation.get('matchedArtifactIds')
    if not hashes or not isinstance(matched, list):
        return None
    if contract['kind'] == 'artifact':
        if (not matched or any(not isinstance(item, str) or item not in hashes for item in matched)
                or len(set(matched)) != len(matched)):
            return None
    elif matched:
        return None  # text validation must not invent matched artifact evidence
    calls = [call for call in run.get('calls', []) if call.get('taskId') == task.get('id')]
    if not calls or any(call.get('agentId') != task.get('agentId') for call in calls):
        return None
    for call in calls:
        result = call.get('result') if isinstance(call.get('result'), dict) else {}
        error = call.get('error') if isinstance(call.get('error'), dict) else {}
        transport = call.get('transport') if isinstance(call.get('transport'), dict) else {}
        failed_transport = error.get('transport') if isinstance(error.get('transport'), dict) else {}
        if (call.get('status') in ('pending', 'running', 'uncertain', 'unknown')
                or any(row.get(key) is True for row in (call, result, error, transport, failed_transport)
                       for key in ('outcomeUnknown', 'uncertain', 'mayHaveCompleted'))):
            return None
        closed_generation = _closed_generation_failure(call, revision)
        if (call.get('tool') == 'llm.chat' and call.get('status') != 'success' and not closed_generation):
            return None
        if (any(row.get(key) is True for row in (call, result, error, transport, failed_transport)
                for key in ('usageUnknown', 'requestMayContinueServerSide')) and not closed_generation):
            return None
    approvals = run.get('approvals', [])
    if (not isinstance(approvals, list) or any(not isinstance(item, dict) for item in approvals)
            or any(item.get('taskId') == task.get('id') and item.get('status') == 'pending' for item in approvals)):
        return None
    model_call = calls[-1]
    if (model_call.get('tool') != 'llm.chat' or model_call.get('status') != 'success'
            or type(model_call.get('revision')) is not int or model_call['revision'] != revision
            or not isinstance(model_call.get('id'), str) or not model_call['id']
            or sum(call.get('id') == model_call['id'] for call in run['calls']) != 1):
        return None
    transcript = run.get('transcript')
    if not isinstance(transcript, list) or any(not isinstance(row, dict) for row in transcript):
        return None
    responses = [row for row in transcript if row.get('taskId') == task.get('id')
                 and row.get('kind') == 'model_response']
    if not responses:
        return None
    proposal = responses[-1]
    if (proposal.get('runId') != run.get('id') or proposal.get('agentId') != task.get('agentId')
            or proposal.get('role') != 'agent' or proposal.get('callId') != model_call['id']
            or ('revision' in proposal and (type(proposal['revision']) is not int
                                           or proposal['revision'] != revision))):
        return None
    try:
        raw = proposal['text']
        if not isinstance(raw, str) or json.loads(raw) != action:
            return None
        raw_bytes = raw.encode('utf-8')
    except (KeyError, TypeError, ValueError, UnicodeError):
        return None
    feedback = {
        'ok': False,
        'error': {'code': 'completion_scope_clarification',
                  'message': 'Clarify this task completion against its unchanged criteria; no completion or review verdict has been granted.'},
        'attempt': 1, 'maxAttemptsPerRevision': 1,
        'binding': {'runId': run['id'], 'taskId': task['id'], 'agentId': task['agentId'],
                    'revision': revision, 'modelCallId': model_call['id'],
                    'proposalSHA256': hashlib.sha256(raw_bytes).hexdigest(),
                    'proposalCharacters': len(raw), 'artifactHashes': hashes},
        'acceptanceCriteria': list(criteria),
        'outputContract': copy.deepcopy(contract),
        'outputValidation': copy.deepcopy(validation),
        'semanticQuality': 'unassessed',
        'observedStagePhase': stage_phase,
        'instruction': (
            'This is one completion-scope clarification, not acceptance or an instruction to claim success. '
            'The declared output format and current published-file integrity checks passed; this does not prove semantic quality. '
            'semanticQuality=unassessed means only that the server did not perform a semantic judgment; '
            'it is not an extra acceptance criterion or a requirement to wait for a downstream independent reviewer. '
            'Independent review is required for this task only when its original criteria/contract require it. '
            'Missing actual files or unperformed mandatory checks still require partial/failed. '
            'Reassess each original criterion using actual current output IDs and evidence: identify any specifically '
            'unfinished required work, separately from factual uncertainty that a complete scoped brief/review '
            'already discloses. A review can finish with supported/unsupported/uncertain findings; that is not a '
            'pass for its producer or for the final product. In your next final text state each criterion as '
            'met/not_met/unassessed with evidence and remaining limitations, then choose your justified status. '
            'You may keep partial or failed; do not invent certainty, delete criteria, or treat your own MET '
            'labels as independent verification. Any originally required explicit review must still target the '
            'actual producer/revision under the existing rules; this receipt creates no review. '
            'No new user answer, tool execution/replay, dependency promotion, permission, time or budget is granted. '
            'If permitted work is genuinely missing, preserve that gap or perform only actions still allowed '
            'by the current stage and original deadline; do not reacquire evidence merely to erase disclosed uncertainty.'),
    }
    # Keep full criteria and provenance, or decline the extra feedback. Never
    # silently truncate a requirement or inject an oversized essential message.
    if len(json.dumps(feedback, ensure_ascii=False, separators=(',', ':'))) > 12000:
        return None
    return feedback
