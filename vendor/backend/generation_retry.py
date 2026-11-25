"""Narrow text-generation retry policy; never a task/tool/effect replay policy."""
_CODES={'network','timeout','transport','transport_incomplete','provider_busy','provider_http'}
_PHASES={'wait_headers','read_body','wait_slot'}


def trusted_generation_failure(code,phase,upstream_retryable,transport):
    """Only host-validated, completed worker error envelopes can opt in."""
    status=transport.get('httpStatus')
    if status is not None and (type(status) is not int or
            not (status in (408,429,500,502,503,504) or code!='provider_http' and 200<=status<300)):return False
    if code=='provider_http' and status is None:return False
    return bool(code in _CODES and (phase in _PHASES or code=='transport_incomplete' and phase=='decompress_body') and upstream_retryable is True
                and transport.get('workerTerminated') is True and transport.get('workerReaped') is True
                and transport.get('modelResultAccepted') is False)


def generation_retry_decision(error,run,task,now_ms,request_phase=None,final_only=False):
    """Recheck original time/stage and effect safety before each new generation."""
    budget=run.get('budget') or {};transport=getattr(error,'transport',None)
    if (budget.get('profile')!='time_only' or run.get('status') not in ('running','waiting_human','waiting_children','paused')
            or type(budget.get('deadlineAt')) is not int or now_ms>=budget['deadlineAt']
            or not isinstance(transport,dict) or transport.get('generationRetryEligible') is not True
            or transport.get('retryScope')!='model_generation_only'
            or not trusted_generation_failure(getattr(error,'code',None),getattr(error,'phase',None),
                                             transport.get('upstreamRetryable'),transport)
            or task.get('_pendingTool') or task.get('_uncertainCalls')):return 'deny'
    stage=task.get('stageControl')
    if stage is not None:
        if (not isinstance(stage,dict) or stage.get('runId')!=run['id'] or stage.get('taskId')!=task['id']
                or not isinstance(stage.get('agentId'),str) or stage['agentId']!=task.get('agentId')
                or type(task.get('revision',0)) is not int or type(stage.get('revision')) is not int or stage['revision']!=task.get('revision',0)
                or type(stage.get('runDeadlineAt')) is not int or stage['runDeadlineAt']!=budget['deadlineAt']
                or any(type(stage.get(key)) is not int for key in ('allocatedAt','synthesisAt','handoffAt','expectedHandoffBy'))
                or not stage['allocatedAt']<=stage['synthesisAt']<=stage['handoffAt']<=stage['expectedHandoffBy']<=stage['runDeadlineAt']):return 'deny'
        # Global final_only can precede the per-task phase. It is already the
        # closing operation, so crossing synthesis must not reopen/lose it.
        if final_only:return 'retry'
        current_phase='handoff_final' if now_ms>=stage['handoffAt'] else 'synthesis' if now_ms>=stage['synthesisAt'] else 'work'
        if task.get('synthesisRecoveries'):
            from .stage_control import stage_control
            try:current_phase=stage_control(run,task,now_ms)['phase']
            except (ValueError,TypeError,KeyError):return 'deny'
        if request_phase is not None and current_phase!=request_phase:return 'refresh_phase'
        if request_phase is None and current_phase=='handoff_final':return 'deny'
    return 'retry'


def generation_retry_allowed(error,run,task,now_ms,request_phase=None,final_only=False):
    return generation_retry_decision(error,run,task,now_ms,request_phase,final_only)=='retry'
