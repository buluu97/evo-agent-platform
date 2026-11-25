"""One completion observation after an actual partial-review admission.

No verdict, rework, grant or producer completion is inferred here. The same
original review action and scheduler retain authority over every consequence.
"""
import copy
import hashlib
import json

from .partial_review import review_dependency_exceptions
from .stage_control import _unresolved_owner_effects


def _encode(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def make_partial_review_admission(run,reviewer,agents,now_ms):
    """Called only when the scheduler actually assigns this partial exception."""
    if (not all(isinstance(v,dict) for v in (run,reviewer,agents))
            or run.get('mode')!='task' or run.get('status') not in ('running','waiting_human')
            or reviewer.get('status')!='assigned' or type(reviewer.get('revision')) is not int
            or reviewer['revision']<0 or type(now_ms) is not int or now_ms<0
            or not any(t is reviewer for t in run.get('steps',[]))):return None
    targets=review_dependency_exceptions(run,reviewer,agents)
    if not targets:return None
    row={'schemaVersion':1,'runId':run['id'],'reviewerTaskId':reviewer['id'],
         'reviewerAgentId':reviewer['agentId'],'reviewerRevision':reviewer['revision'],
         'admittedAt':now_ms,'targets':copy.deepcopy(targets)}
    row['bindingSha256']=hashlib.sha256(_encode(row).encode()).hexdigest()
    return row


def unresolved_partial_reviews(run,reviewer,agents):
    """Current original partial inputs with no explicit assessment by this task."""
    if not all(isinstance(v,dict) for v in (run,reviewer,agents)):return []
    row=reviewer.get('_partialReviewAdmission')
    if (not isinstance(row,dict) or type(row.get('schemaVersion')) is not int or row['schemaVersion']!=1
            or type(row.get('admittedAt')) is not int or row['admittedAt']<0
            or row.get('runId')!=run.get('id')
            or row.get('reviewerTaskId')!=reviewer.get('id')
            or row.get('reviewerAgentId')!=reviewer.get('agentId')
            or type(row.get('reviewerRevision')) is not int
            or row['reviewerRevision']!=reviewer.get('revision')
            or type(reviewer.get('revision')) is not int
            or not isinstance(row.get('targets'),dict) or not row['targets']
            or _unresolved_owner_effects(run,reviewer)):return []
    original={k:v for k,v in row.items() if k not in ('bindingSha256','admissionEventId')}
    try:digest=hashlib.sha256(_encode(original).encode()).hexdigest()
    except (TypeError,ValueError,UnicodeError):return []
    if digest!=row.get('bindingSha256'):return []
    events=run.get('events',[])
    admitted=next((e for e in events if isinstance(e,dict) and e.get('id')==row.get('admissionEventId')),None)
    if (not admitted or admitted.get('type')!='partial_review_admitted'
            or admitted.get('agentId')!=reviewer.get('agentId') or admitted.get('actor')!=reviewer.get('agentId')
            or admitted.get('payload')!={k:v for k,v in row.items() if k!='admissionEventId'}):return []
    current=review_dependency_exceptions(run,reviewer,agents);pending=[]
    for task_id,receipt in row['targets'].items():
        if (not isinstance(receipt,dict) or type(receipt.get('revision')) is not int
                or current.get(task_id)!=receipt):continue
        target=next((t for t in run['steps'] if t.get('id')==task_id),None)
        if not target:continue
        validation=target.get('validation')
        resolved=(isinstance(validation,dict) and validation.get('reviewerTaskId')==reviewer['id']
                  and type(validation.get('targetRevision')) is int
                  and validation['targetRevision']==receipt['revision']
                  and validation.get('artifactHashes')==receipt['artifactHashes']
                  and validation.get('verdict') in ('pass','rework','reject')
                  and bool(str(validation.get('feedback','')).strip())
                  and (validation['verdict']!='pass' or validation.get('verified') is True
                       or validation.get('kind')=='editorial_assessment' and validation.get('assessed') is True)
                  and any(e.get('type')=='review_'+validation['verdict']
                          and e.get('agentId')==reviewer['agentId'] and e.get('actor')==reviewer['agentId']
                          and e.get('payload')=={'taskId':task_id,'validation':validation} for e in events))
        if not resolved:
            pending.append({'taskId':task_id,'revision':receipt['revision'],
                            'artifactHashes':copy.deepcopy(receipt['artifactHashes']),
                            'producerStatus':target['status'],'error':copy.deepcopy(receipt.get('error'))})
    return pending


def partial_review_context(run,reviewer,agents):
    pending=unresolved_partial_reviews(run,reviewer,agents)
    if not pending:return None
    return {'admissionBindingSha256':reviewer['_partialReviewAdmission']['bindingSha256'],
            'targets':pending,'acceptance':'unassessed',
            'actionTemplate':{'type':'review','taskId':pending[0]['taskId'],'verdict':'','feedback':'','evidenceCallIds':[]},
            'verdictOptions':['pass','rework','reject'],
            'instruction':'These inputs were admitted only for independent review of published partial work. '
                'Read the actual authorized bytes. Submit an explicit top-level review action for the current producer: '
                '{"type":"review","taskId":"<actual target taskId>","verdict":"pass|rework|reject",'
                '"feedback":"<actual evidence and specific findings>","evidenceCallIds":[]}. '
                'Fill the blank actionTemplate verdict and feedback using real findings and actual evidence call IDs; '
                'the unfilled template is not a valid review. Choose only a verdict justified by your real evidence. '
                'A prose risk/QA note is not a review action. '
                'For material unmet producer criteria, request specific rework within the original allowance, or honestly '
                'finish partial/failed if it cannot be resolved. Editorial pass records an assessment; it never makes a '
                'partial producer done or unlocks ordinary consumers. No automatic verdict, test, permission or time is added.'}


def partial_review_completion_feedback(run,reviewer,action,agents):
    """Bind a done proposal to a real unresolved admission, without executing it."""
    if (not isinstance(action,dict) or action.get('type')!='final' or action.get('status','done')!='done'
            or not isinstance(action.get('text'),str) or not action['text'].strip()
            or run.get('status') not in ('running','waiting_human') or reviewer.get('status')!='running'):return None
    context=partial_review_context(run,reviewer,agents)
    if not context:return None
    calls=[c for c in run.get('calls',[]) if c.get('taskId')==reviewer.get('id')]
    if not calls:return None
    call=calls[-1]
    if (call.get('tool')!='llm.chat' or call.get('status')!='success'
            or call.get('agentId')!=reviewer.get('agentId')
            or type(call.get('revision')) is not int or call['revision']!=reviewer.get('revision')
            or sum(c.get('id')==call.get('id') for c in run['calls'])!=1):return None
    proposals=[m for m in run.get('transcript',[]) if m.get('kind')=='model_response'
               and m.get('taskId')==reviewer.get('id')]
    if not proposals:return None
    proposal=proposals[-1]
    if (proposal.get('callId')!=call.get('id') or proposal.get('runId')!=run.get('id')
            or proposal.get('agentId')!=reviewer.get('agentId') or proposal.get('role')!='agent'):return None
    try:
        if json.loads(proposal['text'])!=action:return None
    except (KeyError,TypeError,ValueError):return None
    return {'ok':False,'actionExecuted':False,'error':{'code':'partial_review_unresolved',
            'message':'A prose result cannot complete an admitted partial-input review without an explicit current review decision.'},
            'attempt':1,'maxAttemptsPerRevision':1,'binding':{
                'runId':run['id'],'reviewerTaskId':reviewer['id'],'reviewerAgentId':reviewer['agentId'],
                'reviewerRevision':reviewer['revision'],'modelCallId':call['id'],
                'admissionBindingSha256':context['admissionBindingSha256']},
            'partialDependencyReview':context,
            'instruction':'This done proposal was not accepted. '+context['instruction']}
