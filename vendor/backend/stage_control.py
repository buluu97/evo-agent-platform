"""Fixed wall-clock work/synthesis/handoff checkpoints inside one run deadline.

This is scheduling policy, not business acceptance. It neither performs tools
nor grants private data, and never turns a partial result into success.
"""
import copy
import hashlib
import json

from .runtime import ProtocolError

_TERMINAL={'done','partial','failed','skipped','rejected','cancelled'}
_STAGE_BINDING=('runId','taskId','agentId','revision','runDeadlineAt','allocatedAt','synthesisAt','handoffAt','expectedHandoffBy')
_LOCAL_TOOLS={'file.read','file.write','file.patch','code.test'}
_HANDOFF_ACTIONS=('publish_artifact','publish_artifacts','package_delivery','review','final')
_INSTRUCTION=('Before synthesisAt collect necessary evidence and begin notes. In synthesis use actual retained sources '
    'to write/patch/test/publish the deliverable; no new web acquisition or delegation. From handoffAt complete locally: '
    'file.read/write/patch and code.test, plus publish_artifact(s), package_delivery, review and final remain allowed '
    'under original ACL/approval/contract checks. No web, delegation, deletion, Git or code.run. '
    'expectedHandoffBy is a soft scheduling target, not a second run deadline. Finish the handoff promptly '
    'with current output IDs, evidence and gaps. Only the original run deadline is hard. Unmet contracts stay partial/failed; '
    'no additional time, calls/turn caps, permissions or automatic acceptance. A necessary-source exception may admit only '
    'web.fetch of a URL actually recorded in this run human text or this task current-revision successful links, '
    'or URLs literally returned by this task current-revision file.read, with a concrete top-level evidenceGap. This is not free search or permission to invent content.')


def _critical_path(run,task):
    tasks={row['id']:row for row in run.get('steps',[]) if isinstance(row,dict) and isinstance(row.get('id'),str)
           and not row.get('_planning') and row.get('kind')!='planning' and row.get('status') not in _TERMINAL}
    children={key:set() for key in tasks}
    for row in tasks.values():
        for dependency in row.get('dependsOn',[]):
            if dependency in tasks:children[dependency].add(row['id'])
        # A delegated child must leave its waiting parent time to consume output.
        parent=row.get('parentId')
        if parent in tasks:children[row['id']].add(parent)
    def length(task_id,seen):
        if task_id in seen:return 1
        return 1+max((length(child,seen|{task_id}) for child in children.get(task_id,())),default=0)
    return length(task['id'],set())


def stage_control(run,task,now_ms):
    """Pin each revision; only approved rework reallocates within the run wall."""
    if ((run.get('budget') or {}).get('profile')!='time_only' or run.get('mode')!='task'
            or task.get('_planning') or task.get('kind')=='planning'):return None
    deadline=run['budget'].get('deadlineAt')
    if type(deadline) is not int:raise ProtocolError('阶段调度缺少固定运行截止时间。')
    if type(task.get('revision',0)) is not int or task.get('revision',0)<0:
        raise ProtocolError('阶段调度任务修订无效。')
    saved=task.get('stageControl')
    if saved is None:
        revision=task.get('revision',0)
        if type(revision) is not int or revision<0:raise ProtocolError('阶段调度任务修订无效。')
        if revision:
            permit=task.get('_stageReworkAllocation') or {}
            matching=next((row for row in task.get('validationHistory',[]) if row.get('verdict')=='rework'
                           and row.get('reviewerTaskId')==permit.get('reviewerTaskId')
                           and row.get('targetRevision')==revision-1
                           and row.get('artifactHashes')==permit.get('artifactHashes')),None)
            if (permit.get('runId')!=run['id'] or permit.get('taskId')!=task['id']
                    or type(permit.get('forRevision')) is not int or permit['forRevision']!=revision or matching is None):
                raise ProtocolError('新修订缺少真实服务端返工授权；未重新分配阶段时间。')
        stages=_critical_path(run,task);remaining=max(0,deadline-now_ms)
        share=remaining//stages
        close_window=min(120000,max(15000,share//10))
        saved={'schemaVersion':1,'runId':run['id'],'taskId':task['id'],'agentId':task.get('agentId'),'revision':revision,
               'allocatedAt':now_ms,'runDeadlineAt':deadline,'criticalStages':stages,
               'synthesisAt':now_ms+min(share*65//100,max(0,share-close_window)),
               'handoffAt':now_ms+max(0,share-close_window),
               'expectedHandoffBy':now_ms+share,
               'policy':'fixed_wall_checkpoints_within_original_deadline',
               'boundarySemantics':'Next action only; in-flight effects are not cancelled or replayed.',
               'instruction':_INSTRUCTION}
        task['stageControl']=saved
    if (not isinstance(saved,dict) or saved.get('runId')!=run['id'] or saved.get('taskId')!=task['id']
            or saved.get('agentId')!=task.get('agentId')
            or type(saved.get('revision')) is not int or saved['revision']!=task.get('revision',0)
            or saved.get('runDeadlineAt')!=deadline
            or any(type(saved.get(key)) is not int for key in ('allocatedAt','synthesisAt','handoffAt','expectedHandoffBy','runDeadlineAt'))
            or not saved['allocatedAt']<=saved['synthesisAt']<=saved['handoffAt']<=saved['expectedHandoffBy']<=saved['runDeadlineAt']):
        raise ProtocolError('阶段调度保存记录无效；未重新分配或延长时间。')
    view=copy.deepcopy(saved)
    # A restored checkpoint keeps its original timestamps but uses the current
    # admission policy, rather than obsolete prose from a prior source version.
    view['instruction']=_INSTRUCTION
    view['phase']='handoff_final' if now_ms>=saved['handoffAt'] else 'synthesis' if now_ms>=saved['synthesisAt'] else 'work'
    view['remainingUntilHandoffMs']=max(0,saved['expectedHandoffBy']-now_ms)
    view['handoffTargetIsSoft']=True
    view['handoffTargetOverdueMs']=max(0,now_ms-saved['expectedHandoffBy'])
    view['handoffAllowedActions']=list(_HANDOFF_ACTIONS)
    view['handoffAllowedTools']=sorted(_LOCAL_TOOLS)
    recoveries=task.get('synthesisRecoveries',[])
    if not isinstance(recoveries,list):raise ProtocolError('本地合成恢复记录损坏；未重新分配时间。')
    current=[row for row in recoveries if isinstance(row,dict) and type(row.get('revision')) is int and row['revision']==task.get('revision',0)]
    if len(current)>1:raise ProtocolError('同一修订存在重复合成恢复票据；未延长时间。')
    if current:
        recovery=current[0]
        if (recovery.get('runId')!=run['id'] or recovery.get('taskId')!=task['id']
                or recovery.get('agentId')!=task.get('agentId') or recovery.get('originalStage')!={key:saved[key] for key in _STAGE_BINDING}
                or recovery.get('originalStageSha256')!=_stage_hash(saved)
                or any(type(recovery.get(key)) is not int for key in ('grantedAt','expiresAt','finalOnlyAt'))
                or type(recovery.get('finalizationWindowMs')) is not int or not 30000<=recovery['finalizationWindowMs']<=120000
                or recovery['finalOnlyAt']!=deadline-recovery['finalizationWindowMs']
                or not saved['handoffAt']<=recovery['grantedAt']<recovery['expiresAt']<=min(recovery['grantedAt']+90000,recovery['finalOnlyAt'])
                or not recovery['finalOnlyAt']<deadline):
            raise ProtocolError('本地合成恢复绑定无效；未扩大动作准入。')
        view['localSynthesisRecovery']=copy.deepcopy(recovery)
        if (recovery['grantedAt']<=now_ms<recovery['expiresAt'] and now_ms<recovery['finalOnlyAt']
                and not _unresolved_owner_effects(run,task)):
            view['originalPhase']=view['phase'];view['phase']='local_synthesis_recovery'
            view['instruction']=('A legacy server-bound, at most 90-second recovery marker follows a real failed work generation. '
                'It has the same local completion actions as ordinary handoff: file.read/write/patch, code.test, '
                'publish_artifact(s), package_delivery, review and final under existing ACL/approval/contract checks. '
                'No new web, delegation, deletion, Git or code.run. The original stage timestamps and global wall deadline are unchanged. '
                'The marker grants no extra actions or time; in-flight effects are never cancelled or replayed. Expiry returns to ordinary handoff. '
                'No output, test, permission or acceptance is automatically created.')
    return view


def _stage_hash(saved):
    return hashlib.sha256(json.dumps({key:saved[key] for key in _STAGE_BINDING},sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _unresolved_owner_effects(run,task):
    """The owner's same-run tasks share a workspace; do not ignore their effects."""
    owner=task.get('agentId')
    owned=[row for row in run.get('steps',[]) if isinstance(row,dict) and row.get('agentId')==owner]
    if any(row.get('_pendingTool') or row.get('_uncertainCalls') for row in owned):return True
    owned_ids={row.get('id') for row in owned}|{task['id']}
    known={'success','failed','denied','not_executed','reconciled'}
    def unknown(record):
        if not isinstance(record,dict):return False
        if any(key in record and record[key] is not None and record[key] is not False
               for key in ('outcomeUnknown','outcome_unknown')):return True
        if record.get('code') in ('uncertain','outcome_unknown'):return True
        return any(unknown(record.get(key)) for key in ('result','error','transport','lateResult'))
    for call in run.get('calls',[]):
        if not isinstance(call,dict) or call.get('tool')=='llm.chat':continue
        if call.get('agentId')==owner or call.get('taskId') in owned_ids:
            if call.get('status') not in known or unknown(call):return True
    return False


def allocate_synthesis_recovery(run,task,call,now_ms,finalization_window_ms=120000):
    """One durable admission window, only for a proven lost whole synthesis phase."""
    from .generation_retry import trusted_generation_failure
    budget=run.get('budget') or {};revision=task.get('revision',0)
    if (budget.get('profile')!='time_only' or run.get('mode')!='task' or run.get('status')!='running'
            or task.get('status') not in ('running','assigned','retrying') or type(revision) is not int
            or type(now_ms) is not int or type(finalization_window_ms) is not int or not 30000<=finalization_window_ms<=120000
            or type(budget.get('deadlineAt')) is not int or now_ms>=budget['deadlineAt']-finalization_window_ms
            or task.get('_pendingTool') or task.get('_uncertainCalls') or task.get('_approvalId') or not isinstance(call,dict)
            or not isinstance(task.get('stageControl'),dict)):
        return None
    if _unresolved_owner_effects(run,task):return None
    records=task.get('synthesisRecoveries',[])
    if not isinstance(records,list) or any(not isinstance(row,dict) or type(row.get('revision')) is not int for row in records):return None
    if any(row['revision']==revision for row in records):return None
    control=stage_control(run,task,now_ms);saved=task.get('stageControl')
    if not control or control['phase']!='handoff_final':return None
    error=call.get('error');error=error if isinstance(error,dict) else {};transport=error.get('transport')
    if (call.get('tool')!='llm.chat' or call.get('status')!='failed' or call.get('taskId')!=task['id']
            or call.get('agentId')!=task.get('agentId') or type(call.get('revision')) is not int or call['revision']!=revision
            or not isinstance(call.get('id'),str) or sum(row.get('id')==call['id'] for row in run.get('calls',[]) if isinstance(row,dict))!=1
            or call.get('requestStagePhase')!='work' or not isinstance(call.get('requestStageControl'),dict)
            or any(call['requestStageControl'].get(key)!=saved[key] for key in _STAGE_BINDING)
            or any(type(call['requestStageControl'].get(key)) is not int for key in _STAGE_BINDING if key not in ('runId','taskId','agentId'))
            or any(type(call.get(key)) is not int for key in ('requestStartedAt','requestFinishedAt','durationMs'))
            or not saved['allocatedAt']<=call['requestStartedAt']<saved['synthesisAt']<=saved['handoffAt']<=call['requestFinishedAt']<=now_ms
            or call['durationMs']!=call['requestFinishedAt']-call['requestStartedAt']
            or not isinstance(transport,dict) or transport.get('generationRetryEligible') is not True
            or transport.get('retryScope')!='model_generation_only'
            or not trusted_generation_failure(error.get('code'),error.get('phase'),transport.get('upstreamRetryable'),transport)):
        return None
    same_calls=[row for row in run.get('calls',[]) if isinstance(row,dict) and row.get('taskId')==task['id']]
    if not same_calls or same_calls[-1].get('id')!=call['id']:return None
    recovery={'schemaVersion':1,'runId':run['id'],'taskId':task['id'],'agentId':task.get('agentId'),'revision':revision,
              'failedCallId':call['id'],'grantedAt':now_ms,'expiresAt':min(now_ms+90000,budget['deadlineAt']-finalization_window_ms),
              'finalOnlyAt':budget['deadlineAt']-finalization_window_ms,'finalizationWindowMs':finalization_window_ms,
              'originalStage':{key:saved[key] for key in _STAGE_BINDING},
              'originalStageSha256':_stage_hash(saved),'semantics':'next_action_admission_only_no_effect_replay',
              'acceptance':'unassessed'}
    task.setdefault('synthesisRecoveries',[]).append(recovery)
    return copy.deepcopy(recovery)


def allocate_review_rework(run,target,reviewer,validation):
    """Called only inside the server's already authorized review-rework branch."""
    if (run.get('budget') or {}).get('profile')!='time_only':return
    previous=target.pop('stageControl',None)
    if previous is not None:
        target.setdefault('stageControlHistory',[]).append({'revision':target['revision'],
            'control':copy.deepcopy(previous),'reviewerTaskId':reviewer['id'],
            'reviewBinding':copy.deepcopy(validation)})
    target['_stageReworkAllocation']={'runId':run['id'],'taskId':target['id'],'forRevision':target['revision']+1,
        'reviewerTaskId':reviewer['id'],'artifactHashes':copy.deepcopy(validation['artifactHashes'])}
    target.pop('_stagePhase',None)


def allocate_review_recheck(run,reviewer,target,snapshot,binding):
    """Only the successful exact new-revision handoff opens a new QA phase."""
    if (run.get('budget') or {}).get('profile')!='time_only':return
    if target.get('status')!='done':return
    if (binding.get('runId')!=run['id'] or binding.get('taskId')!=target['id']
            or binding.get('reviewerTaskId')!=reviewer['id'] or binding.get('revision')!=target.get('revision')
            or type(target.get('revision')) is not int or snapshot.get('targetRevision')!=target['revision']
            or not any(row.get('verdict')=='rework' and row.get('reviewerTaskId')==reviewer['id']
                       and type(row.get('targetRevision')) is int and row['targetRevision']==target['revision']-1
                       for row in target.get('validationHistory',[]))):
        raise ProtocolError('独立重验缺少真实返工交接绑定；未延长阶段。')
    previous=reviewer.pop('stageControl',None)
    proof={'producerTaskId':target['id'],'producerSnapshot':copy.deepcopy(snapshot),'reworkBinding':copy.deepcopy(binding)}
    if previous is not None:
        reviewer.setdefault('stageControlHistory',[]).append({'revision':reviewer.get('revision',0),
            'reason':'review_recheck','control':copy.deepcopy(previous),'handoffProof':proof})
    reviewer['_stageRecheckAllocation']=proof
    reviewer.pop('_stagePhase',None);reviewer.pop('_timeFinalRevision',None)


def stage_action_feedback(control,action):
    """Typed refusal before tool reservation. Current file work remains allowed."""
    if not control or control['phase']=='work':return None
    kind=action.get('type')
    forbidden=(kind=='delegate' or kind=='tool' and action.get('name') in ('web.search','web.fetch'))
    if control['phase'] in ('handoff_final','local_synthesis_recovery'):
        forbidden=not (kind in _HANDOFF_ACTIONS or kind=='tool' and action.get('name') in _LOCAL_TOOLS)
    if not forbidden:return None
    local=control['phase']=='local_synthesis_recovery'
    feedback={'ok':False,'actionExecuted':False,'error':{'code':'stage_local_synthesis_only' if local else 'stage_acquisition_closed' if control['phase']=='synthesis' else 'stage_handoff_required',
            'retryable':False,'message':'已到固定阶段交接检查点；请利用已有真实材料形成成果并交棒，缺证据须如实说明。'},
            'stageControl':control,'instruction':('No action was executed. The legacy recovery marker permits the same local completion as ordinary handoff: '
                'file.read/write/patch, code.test, publish_artifact(s), package_delivery, review and final under existing checks. '
                'It adds no actions/time, web access, delegation, deletion, Git, code.run or acceptance.' if local else
                'No tool or delegation was executed. Write/patch/test and publish actual retained evidence '
                'during synthesis and local handoff. Handoff permits file.read/write/patch, code.test, publish_artifact(s), package_delivery, '
                'review and final under original checks; no web, delegation, deletion, Git or code.run. '
                'expectedHandoffBy is a soft target inside the unchanged hard run deadline. '
                'An action is never automatically performed or accepted. Missing facts are not permission to invent them.')}
    if kind=='tool' and action.get('name')=='web.fetch':
        args=action.get('args');url=args.get('url') if isinstance(args,dict) else None
        if not isinstance(url,str) or not 1<=len(url)<=2048 or '\0' in url:
            url='<exact URL already observed through an allowed source>'
        feedback['necessarySourceRetry']={
            'guidanceOnly':True,'knownUrlStatus':'not_assessed_by_this_feedback',
            'example':{'type':'tool','name':'web.fetch','args':{'url':url},
                       'evidenceGap':''},
            'instruction':('If this fetch is necessary, explicitly propose a NEW action with a concrete top-level evidenceGap '
                '(8–600 stripped characters), not inside args; args must contain only url. Fill the deliberately blank example gap '
                'with the actual missing evidence; the example as given is not eligible. '
                'The exact URL must have real same-run human or this-task/current-revision successful tool-link/read proof. '
                'This hint does not establish that proof or admit a request. The server still checks current phase, '
                'original ACL/SSRF, unresolved effects, permanent-failure guards, actual visible source bodies and the unchanged '
                'global deadline/final-only gate. No search, extra permissions/time, automatic execution or source content is added. '
                'Otherwise use actual retained evidence, state the gap and publish the partial deliverable.')}
    return feedback


def failure_references(run,task,max_items=4):
    """Actual same-task failures, not an inferred product defect or oracle."""
    rows=[]
    for call in run.get('calls',[]):
        if call.get('taskId')!=task['id'] or call.get('tool')=='llm.chat' or call.get('status') not in ('failed','denied','unknown'):continue
        result=call.get('result');result=result if isinstance(result,dict) else {}
        error=result.get('error');error=error if isinstance(error,dict) else {'message':str(error or '')[:320]}
        rows.append({'callId':call.get('id'),'taskId':task['id'],'agentId':call.get('agentId'),
                     'revision':call.get('revision'),'tool':call.get('tool'),'status':call['status'],
                     'error':{key:(value[:320] if isinstance(value,str) else value) for key,value in error.items()
                              if key in ('code','message','retryable','phase')},
                     'reviewTargets':copy.deepcopy(call.get('reviewTargets')),
                     'diagnosis':'Observed tool/test failure; product cause and semantic quality unassessed.'})
    return {'records':rows[-max_items:],'total':len(rows),'truncated':len(rows)>max_items}


def latest_test_diagnostic(run,task):
    from .browser_review_diagnostics import review_source_gaps
    from .browser_stagnation import browser_assertion_semantics
    calls=[call for call in run.get('calls',[]) if call.get('taskId')==task['id'] and call.get('tool')!='llm.chat']
    if not calls or calls[-1].get('tool')!='code.test' or calls[-1].get('status')!='failed':return None
    call=calls[-1];result=call.get('result');result=result if isinstance(result,dict) else {}
    browser=result.get('browser');browser=browser if isinstance(browser,dict) else {}
    row=failure_references({'calls':[call]},task,1)['records'][0]
    sources=browser.get('sourceManifest',[]);checks=browser.get('assertions',[])
    row['testedSourceHashes']=[{key:value for key,value in source.items() if key in ('path','sha256')}
                              for source in (sources[:8] if isinstance(sources,list) else []) if isinstance(source,dict)]
    row['failedAssertions']=[{key:(value[:240] if isinstance(value,str) else value) for key,value in check.items()
                              if key in ('action','pageId','selector','expected','actual','text','passed','error')}
                             for check in (checks if isinstance(checks,list) else []) if isinstance(check,dict) and check.get('passed') is False][:2]
    row['instruction']='A real failed test is not proof the product is wrong. Check tested source hashes, selector/date/setup '
    row['instruction']+='and actual versus expected before changing code. Correct an invalid test or request precise rework '
    row['instruction']+='with this callId; do not replace granted assets or repeat the identical failed test.'
    gaps=review_source_gaps(run,task,call)
    if gaps:row['reviewSourceCoverage']=gaps
    semantics=browser_assertion_semantics(run,task,call)
    if semantics:row['assertionSemantics']=semantics
    return row
