"""Success-side learning: propose candidate memories for repeatable patterns.

The existing LearningService writes an episodic record on failure. On success
the model is responsible for proposing a reflection, but R10–R13 showed that
rarely happens, so the memory pool stays empty of positive strategies.

This module fills the gap with three narrowly scoped behaviours:

* ``task_signature`` derives a stable short hash from a task's agent, title
  and output contract. It is used to recognise "the same kind of task" across
  runs without inspecting task bodies the model could falsify.
* ``propose_success_memory`` is idempotent: given a signature, it either
  returns the existing candidate for this agent, or writes a new one.
* ``record_success_outcome`` records a success witness and, after two
  independent task ids, asks :class:`MemoryService` to auto-confirm with an
  explicit ``result:<id>`` receipt.

All calls go through the regular ``MemoryService`` guards: tool permissions,
owner-scoped storage, candidate/verified/rejected states, confidence math.
Nothing is written to shared visibility; sharing still needs a human approval.
"""
import hashlib
import json


def _contract_signature(contract):
    if not isinstance(contract, dict):
        return 'none'
    kind = contract.get('kind') or contract.get('type') or 'text'
    extras = [kind]
    for key in ('candidateDelivery', 'minArtifacts', 'allowedExtensions'):
        if key in contract:
            extras.append(key + '=' + json.dumps(contract[key], sort_keys=True,
                                                 ensure_ascii=False))
    return '|'.join(extras)


def task_signature(task):
    """Short deterministic signature used to identify 'this type of task'."""
    if not isinstance(task, dict):
        return None
    agent = task.get('agentId')
    title = (task.get('title') or '').strip().lower()[:160]
    contract = _contract_signature(task.get('outputContract'))
    if not isinstance(agent, str) or not title:
        return None
    raw = (agent + '\0' + title + '\0' + contract).encode('utf-8')
    return 'sig-' + hashlib.sha256(raw).hexdigest()[:24]


def _lesson_from(task):
    title = (task.get('title') or '').strip()
    goal = (task.get('goal') or '').strip()
    contract = task.get('outputContract') or {}
    kind = contract.get('kind') or contract.get('type') or 'text'
    summary = title or goal or 'task'
    return ('Prior success pattern: when asked to "' + summary[:140]
            + '" with a ' + kind + ' output, the completed call chain from '
            'this run has been accepted. Use this as a starting plan, not '
            'a guarantee; validate the current requirements before reuse.')


def propose_success_memory(memory_service, run, task, calls, channel):
    """Idempotent candidate proposal for a successful task.

    Returns ``(memory, created)``: ``memory`` is the current memory dict (new
    or existing), ``created`` is True only for a fresh insertion. Returns
    ``(None, False)`` when the task does not have enough shape to sign.
    """
    signature = task_signature(task)
    if signature is None:
        return None, False
    agent_id = task.get('agentId')
    if not isinstance(agent_id, str) or not agent_id:
        return None, False
    existing = memory_service.find_by_signature(agent_id, signature, channel)
    if existing is not None:
        return existing, False
    call_ids = [call['id'] for call in calls if isinstance(call, dict)
                and isinstance(call.get('id'), str)]
    lesson = _lesson_from(task)
    trigger = (task.get('title') or task.get('goal') or '').strip()[:800] or 'task'
    conditions = ('outputContract=' + _contract_signature(task.get('outputContract'))
                  + '; agent=' + agent_id)
    evidence = [{'type': 'call', 'id': call_id} for call_id in call_ids[:32]]
    memory = memory_service.propose(
        agent_id, lesson, trigger, task['id'], run['id'],
        evidence or [{'type': 'tool', 'id': 'successful-run:' + run['id']}],
        conditions=conditions, kind='procedural', channel_id=channel,
        signature=signature)
    return memory, True


def record_success_outcome(memory_service, memory, task, run, *, threshold=2):
    """Register one more success witness; may auto-confirm the memory."""
    if not isinstance(memory, dict) or 'id' not in memory:
        return {'applied': False, 'reason': 'invalid_memory'}
    agent_id = task.get('agentId')
    if not isinstance(agent_id, str) or not agent_id:
        return {'applied': False, 'reason': 'missing_agent'}
    try:
        return memory_service.record_success_streak(
            memory['id'], agent_id, task['id'], run['id'], threshold=threshold)
    except (ValueError, KeyError) as exc:
        return {'applied': False, 'reason': 'service_rejected',
                'message': str(exc)[:300]}
