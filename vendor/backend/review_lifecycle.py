"""Current, event-backed review receipts and bounded duplicate-decision recovery.

This is a metadata ledger, never an assessment, file grant or synthetic final.
The engine supplies its existing evidence and dependency checks.
"""
import copy
import re

from .runtime import fingerprint
from .review_versions import target_artifacts


def current_pass_receipt(run, reviewer, target, agents, dependency_validation, valid_code_review):
    """Recognize only this reviewer's explicit pass on these exact current bytes."""
    actor=reviewer.get('agentId');validation=target.get('validation')
    if (not any(t is reviewer for t in run.get('steps',[]))
            or not any(t is target for t in run.get('steps',[]))
            or 'review' not in agents.get(actor,{}).get('capabilities',[])
            or target.get('agentId')==actor or target.get('status') not in ('done','partial')
            or type(reviewer.get('revision')) is not int or type(target.get('revision')) is not int
            or not isinstance(validation,dict) or validation.get('verdict')!='pass'
            or validation.get('reviewerTaskId')!=reviewer.get('id')
            or type(validation.get('reviewerRevision')) is not int
            or validation['reviewerRevision']!=reviewer['revision']
            or type(validation.get('targetRevision')) is not int
            or validation['targetRevision']!=target['revision']
            or not isinstance(validation.get('feedback'),str) or not validation['feedback'].strip()
            or dependency_validation.get('status')=='stale'):return None
    outputs=[item['id'] for item in target_artifacts(run, target)];hashes={}
    for artifact_id in outputs:
        artifacts=[a for a in run.get('artifacts',[]) if a.get('id')==artifact_id]
        if len(artifacts)!=1:return None
        a=artifacts[0];digest=a.get('sha256')
        if (a.get('runId')!=run.get('id') or ((target.get('outputContract') or {}).get('kind') != 'delivery' and a.get('taskId')!=target['id'])
                or ((target.get('outputContract') or {}).get('kind') != 'delivery' and a.get('ownerAgentId',a.get('owner'))!=target.get('agentId'))
                or not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest)):return None
        hashes[artifact_id]=digest
    if validation.get('artifactHashes')!=hashes:return None
    if validation.get('kind')=='editorial_assessment':
        if validation.get('assessed') is not True or validation.get('verified') is not False:return None
    elif not valid_code_review:return None
    events=[e for e in run.get('events',[]) if e.get('type')=='review_pass'
            and e.get('agentId')==actor and e.get('actor')==actor
            and e.get('payload')=={'taskId':target['id'],'validation':validation}]
    if not events or not isinstance(events[0].get('id'),str):return None
    return {'eventId':events[0]['id'],'taskId':target['id'],
            'reviewerTaskId':reviewer['id'],'reviewerRevision':reviewer['revision'],
            'targetRevision':target['revision'],'artifactHashes':hashes,
            'verdict':'pass','kind':validation['kind'],'verified':validation['verified']}


def review_context(run,reviewer,agents,receipt_for,can_access):
    """Declared targets first; otherwise independent direct task dependencies only."""
    actor=reviewer.get('agentId')
    if ('review' not in agents.get(actor,{}).get('capabilities',[])
            or not any(t is reviewer for t in run.get('steps',[]))
            or type(reviewer.get('revision')) is not int):return None
    declared=reviewer.get('reviewOf') or reviewer.get('dependsOn',[])
    if not isinstance(declared,list) or any(not isinstance(x,str) for x in declared):return None
    steps={t['id']:t for t in run.get('steps',[])};rows=[];inputs={}
    for task_id in dict.fromkeys(declared):
        target=steps.get(task_id)
        if (not target or target is reviewer or target.get('agentId')==actor or target.get('_planning')
                or target.get('kind')=='planning' or target.get('finalSummary')):continue
        receipt=receipt_for(target)
        rows.append({'taskId':task_id,'agentId':target.get('agentId'),'title':target.get('title','')[:160],
                     'revision':target.get('revision'),'status':target.get('status'),
                     'artifactHashes':{a['id']:a.get('sha256') for a in target_artifacts(run, target)},
                     'passReceipt':({key:receipt[key] for key in ('eventId','kind','verified')} if receipt else None)})
        # Explain provenance dependencies which were never direct reviewer inputs.
        # Metadata exposes no private path/body and conveys no implicit grant.
        for source_id in target.get('dependsOn',[]):
            source=steps.get(source_id)
            if not source or source_id in declared or source.get('agentId')==actor:continue
            for a in run.get('artifacts',[]):
                if (a.get('id') not in source.get('outputs',[]) or a.get('runId')!=run.get('id')
                        or a.get('taskId')!=source_id
                        or a.get('ownerAgentId',a.get('owner'))!=source.get('agentId')):continue
                accessible=can_access(a['id'],actor,run['id'])
                item={'artifactId':a['id'],'sourceTaskId':source_id,'ownerAgentId':source['agentId'],
                      'name':str(a.get('name',''))[:120],'sha256':a.get('sha256'),
                      'directDependency':source_id in reviewer.get('dependsOn',[]),
                      'accessible':accessible}
                if accessible:item['importPath']='imports/'+a['id']+'/'+a['name']
                inputs[a['id']]=item
    if not rows:return None
    body={'runId':run['id'],'reviewerTaskId':reviewer['id'],'reviewerAgentId':actor,
          'reviewerRevision':reviewer['revision'],'targets':rows}
    complete=[row['taskId'] for row in rows if row['passReceipt']]
    pending=[row['taskId'] for row in rows if not row['passReceipt']]
    return {**body,'bindingSha256':fingerprint({'scope':body,'inputs':inputs}),
            'completedTargetIds':complete,'remainingTargetIds':pending,
            'reviewInputs':list(inputs.values())[:16],'reviewInputsOmitted':max(0,len(inputs)-16),
            'instruction':'Use recorded passes; review remainingTargetIds, satisfy task.outputContract and explicitly final. '
                'Editorial pass is not executable proof or producer completion. Inputs are metadata only: inaccessible bytes need explicit owner sharing; '
                'do not guess paths. No grant, completion or extra time is implied.'}


def repeat_binding(context,progress_signature):
    return {'scopeSha256':context['bindingSha256'],'workSha256':progress_signature}


def report_work_signature(files, artifacts):
    """Report work is changed bytes or a first publication, not new IDs.

    Artifact IDs stay intact in provenance, permissions and review validation.
    This derived progress key only ignores repeated publication of the same
    report bytes at the same path. A different path or content remains work.
    """
    publications = sorted({fingerprint({'path':a.get('path') or a.get('name'),
                                        'sha256':a.get('sha256')})
                           for a in artifacts})
    return fingerprint({'files':files,'publications':publications})


def review_model_view(context,repeat=None):
    """Derived status only; full identities remain in host scope and evidence.

    Task/owner titles, snapshots and output contracts are already indexed in
    canonical task/dependency/artifact context. Repeating those fields here can
    displace the actual just-returned review validation at small context limits.
    """
    view={key:copy.deepcopy(context[key]) for key in ('completedTargetIds','remainingTargetIds')}
    passes=[{'taskId':row['taskId'],'revision':row['revision'],**row['passReceipt']}
            for row in context['targets'] if row['passReceipt']]
    if passes:view['recordedPasses']=passes
    if context['reviewInputs']:
        view['reviewInputs']=copy.deepcopy(context['reviewInputs'])
        if context['reviewInputsOmitted']:view['reviewInputsOmitted']=context['reviewInputsOmitted']
    if repeat:view['duplicateRecovery']={key:copy.deepcopy(value) for key,value in repeat.items()
                                        if key not in ('binding','remainingTargetIds')}
    view['instruction']='Recorded passes do not complete this task. Review remaining targets, satisfy task.outputContract, then final. Input metadata grants no access; request explicit owner sharing for missing bytes.'
    return view


def current_repeat_state(reviewer,context):
    state=reviewer.get('_reviewRepeat')
    if (not context or not isinstance(state,dict)
            or state.get('binding')!=repeat_binding(context,reviewer.get('_reviewWorkSignature'))):return None
    return state


def record_duplicate(reviewer,context):
    old=current_repeat_state(reviewer,context)
    count=(old.get('duplicatesWithoutProgress',0) if old else 0)+1
    limit=2 if not context['remainingTargetIds'] else 3
    state={'binding':repeat_binding(context,reviewer.get('_reviewWorkSignature')),
           'duplicatesWithoutProgress':count,'finalRequired':count>=limit,
           'duplicateLimit':limit,'remainingTargetIds':list(context['remainingTargetIds'])}
    reviewer['_reviewRepeat']=state
    return copy.deepcopy(state)
