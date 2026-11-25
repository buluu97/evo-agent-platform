"""Explain a rejected target using current metadata, never select or authorize it."""
import re


def review_target_feedback(run,reviewer,requested_task_id,agents):
    if not all(isinstance(v,dict) for v in (run,reviewer,agents)):return None
    actor=reviewer.get('agentId');steps=run.get('steps');artifacts=run.get('artifacts')
    participants=run.get('participants');capabilities=agents.get(actor,{}).get('capabilities') if isinstance(actor,str) else None
    if (not isinstance(actor,str) or not isinstance(participants,list) or actor not in participants
            or not isinstance(capabilities,list) or 'review' not in capabilities
            or run.get('mode')!='task' or run.get('status') not in ('running','waiting_human')
            or not isinstance(run.get('id'),str) or not run['id']
            or not isinstance(steps,list) or any(not isinstance(t,dict) for t in steps)
            or not any(t is reviewer for t in steps)
            or len({t.get('id') for t in steps if isinstance(t.get('id'),str)})!=len(steps)
            or not isinstance(artifacts,list)):return None
    requested=next((t for t in steps if t['id']==requested_task_id),None)
    if requested is not None and requested['id']!=reviewer.get('id') and requested.get('status') in ('done','partial'):
        return None  # A later explicit proposal uses the original review path.
    reason=('self_target' if requested is reviewer else 'target_not_delivered' if requested is not None else 'missing_target')
    declared=reviewer.get('reviewOf')
    if declared is not None and (not isinstance(declared,list) or any(not isinstance(x,str) for x in declared)):return None
    declared=set(declared) if declared else None
    dependencies=reviewer.get('dependsOn',[]);targets=[]
    for target in steps:
        revision=target.get('revision');owner=target.get('agentId')
        if (target is reviewer or target.get('_planning') or target.get('kind')=='planning'
                or target.get('status') not in ('done','partial') or type(revision) is not int or revision<0
                or not isinstance(owner,str) or owner not in run['participants']
                or target.get('runId',run['id'])!=run['id'] or declared is not None and target['id'] not in declared):continue
        outputs=target.get('outputs',[]);refs=[]
        if not isinstance(outputs,list) or any(not isinstance(x,str) for x in outputs):continue
        for artifact in artifacts:
            if (not isinstance(artifact,dict) or artifact.get('id') not in outputs
                    or artifact.get('runId')!=run['id'] or artifact.get('taskId')!=target['id']
                    or artifact.get('ownerAgentId',artifact.get('owner'))!=owner
                    or not isinstance(artifact.get('sha256'),str)
                    or not re.fullmatch('[0-9a-f]{64}',artifact['sha256'])):continue
            refs.append({'id':artifact['id'],'name':str(artifact.get('name',''))[:120],'sha256':artifact['sha256']})
        targets.append({'taskId':target['id'],'title':str(target.get('title',''))[:200],
                        'agentId':owner,'status':target['status'],'revision':revision,
                        'directDependency':target['id'] in dependencies,'differentOwner':owner!=actor,
                        'artifactRefs':refs[:3],'artifactRefsTotal':len(refs),'artifactRefsOmitted':max(0,len(refs)-3)})
    targets.sort(key=lambda t:(not t['differentOwner'],not t['directDependency'],t['taskId']))
    return {'ok':False,'notExecuted':True,'actionExecuted':False,
            'error':{'code':'protocol','reason':reason,'retryable':False,
                     'message':'审查对象必须是其他已交付子任务；此请求未执行审查、返工或版本变更。'},
            'runId':run['id'],'reviewerTaskId':reviewer['id'],
            'requestedTaskId':requested_task_id if isinstance(requested_task_id,str) and len(requested_task_id)<=80 else None,
            'requestedStatus':requested.get('status') if requested is not None else 'not_found',
            'candidateTargets':targets[:8],'candidateTargetsTotal':len(targets),'candidateTargetsOmitted':max(0,len(targets)-8),
            'actionTemplate':{'type':'review','taskId':'','verdict':'','feedback':'','evidenceCallIds':[]},
            'instruction':'Your reviewerTaskId identifies this review task, not the product to assess. Explicitly choose the actual '
                'intended delivered producer taskId; fill the blank template with pass/rework/reject, real findings and only '
                'your actual evidence call IDs. Candidate entries are current metadata only, not fetched bodies, authorization '
                'or acceptance. DifferentOwner=false is not independent evidence. No target or verdict was selected. '
                'A pending final-summary task is not an existing draft. If the intended product is not yet delivered, report '
                'the real missing-work limitation rather than invent a review or mark it passed. Any new explicit action still '
                'undergoes original ACL, current revision/hash, evidence, rework allowance and unchanged deadline/final-only checks.'}
