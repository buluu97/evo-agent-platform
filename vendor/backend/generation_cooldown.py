"""Durable time-only generation cooldown; never replay a task/tool/effect."""
import copy

from .generation_retry import generation_retry_decision
from .provider import ProviderError
from .stage_control import _unresolved_owner_effects


def cooldown_delay(cycle):
    return min(30000,5000*2**min(cycle-1,3))


def _history_count(task):
    rows=task.get('generationCooldownHistory',[])
    if not isinstance(rows,list) or any(not isinstance(row,dict) or type(row.get('revision')) is not int for row in rows):return None
    return sum(row['revision']==task['revision'] for row in rows)


def cooldown_failure(run,task,call,now_ms,request_phase=None,final_only=False):
    """Reconstruct eligibility from the actual settled call, not a saved claim."""
    if (type(now_ms) is not int or now_ms<0 or run.get('mode')!='task' or type(task.get('revision')) is not int or not isinstance(call,dict)
            or call.get('tool')!='llm.chat' or call.get('status')!='failed'
            or call.get('taskId')!=task.get('id') or call.get('agentId')!=task.get('agentId')
            or type(call.get('revision')) is not int or call['revision']!=task['revision']):return None
    current=[item for item in run.get('calls',[]) if item.get('taskId')==task['id'] and item.get('tool')=='llm.chat']
    if (not current or current[-1] is not call or not isinstance(call.get('id'),str)
            or sum(item.get('id')==call['id'] for item in run.get('calls',[]))!=1):return None
    # Another task of this owner shares its workspace. Unknown/active tools
    # there cannot be certified absent merely because generation is read-only.
    if _unresolved_owner_effects(run,task):return None
    failure=call.get('error')
    if not isinstance(failure,dict) or not isinstance(failure.get('transport'),dict):return None
    error=ProviderError('Prior text generation returned no accepted action.',failure.get('code','provider'),False,
                        phase=failure.get('phase'),transport=copy.deepcopy(failure['transport']))
    if generation_retry_decision(error,run,task,now_ms,request_phase,final_only) not in ('retry','refresh_phase'):return None
    return error


def new_cooldown(run,task,call,now_ms,identifier,request_phase=None,final_only=False):
    if cooldown_failure(run,task,call,now_ms,request_phase,final_only) is None:return None
    previous=_history_count(task)
    if previous is None:return None
    cycle=previous+1;delay=cooldown_delay(cycle)
    return {'id':identifier,'runId':run['id'],'taskId':task['id'],'agentId':task['agentId'],
            'revision':task['revision'],'runDeadlineAt':run['budget']['deadlineAt'],
            'failedCallId':call['id'],'requestPhase':request_phase,'closingGeneration':bool(final_only),
            'createdAt':now_ms,'nextEligibleAt':min(now_ms+delay,run['budget']['deadlineAt']),
            'delayMs':delay,'cycle':cycle,'status':'waiting','scope':'model_generation_only',
            'usageUnknown':bool((call.get('error',{}).get('transport') or {}).get('usageUnknown'))}


def cooldown_binding(run,task,now_ms):
    row=task.get('generationCooldown')
    if (not isinstance(row,dict) or row.get('status')!='waiting' or row.get('scope')!='model_generation_only'
            or row.get('runId')!=run.get('id') or row.get('taskId')!=task.get('id')
            or row.get('agentId')!=task.get('agentId') or type(row.get('revision')) is not int
            or row['revision']!=task.get('revision') or type(row.get('runDeadlineAt')) is not int
            or row['runDeadlineAt']!=(run.get('budget') or {}).get('deadlineAt')
            or any(type(row.get(key)) is not int for key in ('createdAt','nextEligibleAt','cycle','delayMs'))
            or not isinstance(row.get('id'),str) or not row['id']
            or _history_count(task) is None or row['cycle']!=_history_count(task)+1
            or row['cycle']<1 or row['delayMs']!=cooldown_delay(row['cycle'])
            or not 0<=row['createdAt']<=row['nextEligibleAt']<=row['runDeadlineAt']
            or row['nextEligibleAt']!=min(row['createdAt']+row['delayMs'],row['runDeadlineAt'])):return None
    call=next((item for item in run.get('calls',[]) if item.get('id')==row.get('failedCallId')),None)
    error=cooldown_failure(run,task,call,now_ms,row.get('requestPhase'),row.get('closingGeneration') is True)
    return (row,call,error) if error is not None else None
