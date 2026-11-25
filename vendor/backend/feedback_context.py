"""Bound current execution feedback to authoritative server transcript records.

No user-prefix trust, permissions, source retrieval or tool execution. Only the
latest still-current observation is projected. Full audit bytes stay unchanged.
"""
from dataclasses import dataclass
import hashlib
import json
from .generation_retry import trusted_generation_failure


_PREFIXES={'stage_action_blocked':'阶段动作结果：','browser_failure_recovery':'浏览器测试失败恢复：',
           'readonly_failure_recovery':'只读失败恢复：','generation_phase_refresh':'模型阶段恢复：',
           'protocol_feedback':'动作协议错误：','truncation_feedback':'输出截断修复：',
           'action_result':'动作的真实结果：','repeated_action_warning':'重复动作警告：',
           'browser_success_handoff':'Browser evidence handoff: '}
_MAX_FEEDBACK=16000
_MAX_PROPOSAL=8000


def _encode(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)


def _fail(message):
    from .runtime import ProtocolError
    raise ProtocolError(message)


@dataclass(frozen=True)
class FeedbackProof:
    run_id:str
    task_id:str
    actor:str
    revision:int
    transcript_id:str
    model_call_id:str
    payload_sha256:str
    messages:tuple
    proposal_sha256:str|None=None
    proposal_characters:int=0


def _current_review_result(run,task,payload,response):
    """Protect one actual success receipt, never grant authority from its prefix.

    This preserves a pass/reject decision (including verified=false), not a new
    review or proof of product quality. Unaccepted review actions still use the
    existing error path; rework has its separate wait path. No tool qualifies.
    """
    if not response or not any(t is task for t in run.get('steps',[])):return False
    call=next((c for c in run.get('calls',[]) if c.get('id')==response.get('callId')),None)
    if not call or call.get('runId',run['id'])!=run['id']:return False
    try:action=json.loads(response['text'])
    except (KeyError,TypeError,ValueError):return False
    validation=payload.get('validation')
    if (not isinstance(action,dict) or action.get('type')!='review' or action.get('verdict') not in ('pass','reject')
            or payload.get('ok') is not True or not isinstance(validation,dict)
            or validation.get('verdict')!=action['verdict'] or validation.get('reviewerTaskId')!=task['id']
            or type(validation.get('reviewerRevision')) is not int
            or validation['reviewerRevision']!=task['revision']
            or action['verdict']=='pass' and not (validation.get('verified') is True or validation.get('kind')=='editorial_assessment'
                    and validation.get('verified') is False and validation.get('assessed') is True)):return False
    matches=[t for t in run.get('steps',[]) if t.get('id')==action.get('taskId')]
    if len(matches)!=1:return False
    target=matches[0];owner=target.get('agentId')
    if (not isinstance(owner,str) or owner==task.get('agentId') or payload.get('targetAgentId')!=owner
            or payload.get('targetTaskId')!=target['id']
            or target.get('status') not in (('done','partial') if action['verdict']=='pass' else ('failed',))
            or payload.get('status')!=target['status']
            or type(target.get('revision')) is not int or type(validation.get('targetRevision')) is not int
            or validation['targetRevision']!=target['revision'] or target.get('validation')!=validation):return False
    hashes={}
    for artifact_id in target.get('outputs',[]):
        rows=[a for a in run.get('artifacts',[]) if a.get('id')==artifact_id]
        if len(rows)!=1:return False
        artifact=rows[0]
        if (artifact.get('runId')!=run['id'] or artifact.get('taskId')!=target['id']
                or artifact.get('ownerAgentId',artifact.get('owner'))!=owner
                or not isinstance(artifact.get('sha256'),str)):return False
        hashes[artifact_id]=artifact['sha256']
    if validation.get('artifactHashes')!=hashes:return False
    # Recorded upstream versions are part of the current review boundary too.
    # Missing legacy snapshots are not invented; a recorded mismatch is stale.
    visiting=set();checked=set()
    def current_inputs(item):
        if item['id'] in checked:return True
        if item['id'] in visiting:return False
        visiting.add(item['id'])
        snapshots=item.get('dependencySnapshots')
        if snapshots is not None:
            if not isinstance(snapshots,dict):return False
            for dependency_id,expected in snapshots.items():
                rows=[t for t in run.get('steps',[]) if t.get('id')==dependency_id]
                if len(rows)!=1 or not isinstance(expected,dict) or type(expected.get('targetRevision')) is not int:return False
                dependency=rows[0]
                if type(dependency.get('revision')) is not int:return False
                actual={'status':dependency.get('status'),'targetRevision':dependency['revision'],
                        'artifactHashes':{a['id']:a['sha256'] for a in run.get('artifacts',[])
                            if a.get('id') in dependency.get('outputs',[]) and a.get('sha256')}}
                if actual!=expected or not current_inputs(dependency):return False
        visiting.remove(item['id']);checked.add(item['id']);return True
    if not current_inputs(target):return False
    events=[e for e in run.get('events',[]) if e.get('type')=='review_'+action['verdict']
            and e.get('agentId')==task['agentId'] and e.get('actor')==task['agentId']
            and e.get('payload')=={'taskId':target['id'],'validation':validation}]
    event_id=payload.get('existingReviewEventId') if payload.get('duplicateReview') is True else payload.get('reviewEventId')
    if (not isinstance(event_id,str) or not event_id or not any(e.get('id')==event_id for e in events)
            or sum(e.get('id')==event_id for e in run.get('events',[]))!=1):return False
    if payload.get('duplicateReview') is True:
        return (action['verdict']=='pass' and payload.get('actionExecuted') is False and payload.get('newDecision') is False
                and any(e.get('id')==payload.get('existingReviewEventId') for e in events))
    return bool(validation.get('feedback')==str(action.get('feedback',''))[:3000])


def latest_feedback_proof(run,task):
    """Read actual ledger/transcript only; never infer authority from model text."""
    if not all(isinstance(v,dict) for v in (run,task)):return None
    revision=task.get('revision');actor=task.get('agentId');task_id=task.get('id')
    if (type(revision) is not int or revision<0 or not all(isinstance(x,str) and x for x in (run.get('id'),task_id,actor))):return None
    transcript=run.get('transcript');calls=run.get('calls')
    if not isinstance(transcript,list) or not isinstance(calls,list):return None
    calls=[c for c in calls if isinstance(c,dict)]
    scoped=[r for r in transcript if isinstance(r,dict) and r.get('runId')==run['id'] and r.get('taskId')==task_id and r.get('agentId')==actor]
    def call_for(call_id):
        matches=[c for c in calls if c.get('id')==call_id]
        if len(matches)!=1:return None
        c=matches[0]
        if (c.get('taskId')!=task_id or c.get('agentId')!=actor or c.get('tool')!='llm.chat'
                or type(c.get('revision')) is not int or c['revision']!=revision):return None
        return c
    def real_response(row):
        if row.get('kind')!='model_response' or row.get('role')!='agent':return False
        call=call_for(row.get('callId'))
        return bool(call and (call.get('status')=='success' or call.get('status')=='failed' and call.get('finishReason')=='length'))
    responses=[r for r in scoped if real_response(r)]
    latest_response=responses[-1] if responses else None
    for row in reversed(scoped):
        kind=row.get('kind')
        if kind not in _PREFIXES or row.get('role') not in ('system','tool'):continue
        if not isinstance(row.get('id'),str) or not row['id'] or sum(isinstance(r,dict) and r.get('id')==row['id'] for r in transcript)!=1:continue
        position=scoped.index(row)
        # Superseded historical bytes are not current execution constraints.
        # Decide this from actual same-revision model calls BEFORE decoding or
        # size-checking an old observation, which may be huge or malformed.
        if any(real_response(r) for r in scoped[position+1:]):return None
        try:payload=json.loads(row.get('text',''));canonical=_encode(payload);canonical.encode('utf-8')
        except (TypeError,ValueError,UnicodeError):
            _fail('真实服务端执行约束JSON无效；未静默丢弃后继续请求模型。')
        if not isinstance(payload,dict):continue
        error=payload.get('error')
        review_success=(kind=='action_result' and row.get('role')=='tool' and row.get('action')=='review' and payload.get('ok') is True
                        and _current_review_result(run,task,payload,latest_response))
        if kind=='repeated_action_warning':
            if (payload.get('runId')!=run['id'] or payload.get('taskId')!=task_id
                    or type(payload.get('revision')) is not int or payload['revision']!=revision
                    or type(payload.get('consecutiveIdenticalProposals')) is not int
                    or payload['consecutiveIdenticalProposals']!=2
                    or payload.get('thirdProposalStopsIfExistingGuardsPermit') is not True):continue
        elif not review_success and (payload.get('ok') is not False or not isinstance(error,dict)
                or not isinstance(error.get('code'),str)):continue
        if kind=='action_result' and row.get('action') not in ('final','tool','review','reject','accept','delegate','publish_artifact','publish_artifacts','package_delivery','ask_human'):continue
        if len(canonical)>_MAX_FEEDBACK:_fail('真实服务端执行约束超过16000字符上限；未截掉条件后发送模型。')
        if kind=='generation_phase_refresh':
            call=call_for(row.get('callId'))
            if (not call or call.get('status')!='failed' or payload.get('acceptedModelAction') is not False
                    or payload.get('failedCallId')!=call['id'] or error['code']!='generation_phase_refresh'):continue
            failure=call.get('error');failure=failure if isinstance(failure,dict) else {}
            transport=failure.get('transport');transport=transport if isinstance(transport,dict) else {}
            if (transport.get('generationRetryEligible') is not True or transport.get('retryScope')!='model_generation_only'
                    or not trusted_generation_failure(failure.get('code'),failure.get('phase'),transport.get('upstreamRetryable'),transport)):continue
            # A later real response supersedes this unsuccessful generation.
            proposal=None;proposal_sha=None;proposal_chars=0
        else:
            previous=[r for r in scoped[:position] if real_response(r)]
            if not previous:return None
            proposal=previous[-1]
            if proposal is not latest_response:return None
            call=call_for(proposal.get('callId'))
            if not call or (call.get('status')!='success' and not (kind=='truncation_feedback' and call.get('status')=='failed' and call.get('finishReason')=='length')):return None
            if row.get('callId') is not None and row['callId']!=call['id']:return None
            text=proposal.get('text')
            if not isinstance(text,str):return None
            proposal_sha=hashlib.sha256(text.encode('utf-8',errors='replace')).hexdigest();proposal_chars=len(text)
            if kind=='repeated_action_warning':
                if len(previous)<2:continue
                try:
                    recent=[json.loads(item['text']) for item in previous[-2:]]
                    signatures=[hashlib.sha256(_encode(item).encode()).hexdigest() for item in recent]
                except (KeyError,TypeError,ValueError,UnicodeError):continue
                if (any(not isinstance(item,dict) for item in recent)
                        or signatures!=[payload.get('proposalSha256')]*2):continue
        citation={'runId':run['id'],'taskId':task_id,'agentId':actor,'revision':revision,
                  'transcriptId':row['id'],'kind':kind,'modelCallId':call['id'],
                  'proposalSha256':proposal_sha,'proposalCharacters':proposal_chars,
                  'meaning':'Actual server observation and original model proposal only; no new execution, permission or acceptance.'}
        messages=[]
        if proposal is not None:
            text=proposal['text']
            if len(text)<=_MAX_PROPOSAL:messages.append(('assistant',text))
            else:
                reference={'modelCallId':call['id'],'jsonCharacters':len(text),'jsonSha256':proposal_sha,
                           'proposalBodyOmitted':True,'meaning':'Reference to the real oversized proposal, not a complete executable action.'}
                try:
                    action=json.loads(text)
                    if isinstance(action,dict):
                        reference['actionType']=action.get('type');reference['tool']=action.get('name')
                        if isinstance(action.get('args'),dict) and isinstance(action['args'].get('path'),str):reference['path']=action['args']['path'][:1000]
                except (TypeError,ValueError):pass
                messages.append(('user','原模型动作引用：'+_encode(reference)))
        else:citation['noAcceptedModelProposal']=True
        messages.append(('user','服务端执行约束来源：'+_encode(citation)))
        messages.append(('user',_PREFIXES[kind]+canonical))
        if review_success:
            # Successful decisions need their exact result, not another copy
            # of the already accepted proposal and verbose error provenance.
            # Full provenance remains bound in this typed proof and raw audit.
            messages=messages[-1:]
        return FeedbackProof(run['id'],task_id,actor,revision,row['id'],call['id'],
                             hashlib.sha256(canonical.encode()).hexdigest(),tuple(messages),proposal_sha,proposal_chars)
    return None


def protect_feedback_messages(messages,proof):
    """Restore the SAME authenticated observation, then protect its exact view.

    The proof is an internal typed object, never parsed from user/API JSON.
    Older audits and file/tool messages are not removed or relabelled as facts.
    """
    if proof is None:return list(messages),[]
    if not isinstance(proof,FeedbackProof):_fail('执行约束缺少内部服务端证明；未信任任意用户前缀。')
    if len(messages)<2:_fail('服务端约束缺少当前任务上下文绑定。')
    try:context=json.loads(messages[1]['content'])
    except (ValueError,TypeError,KeyError):_fail('服务端约束缺少当前任务上下文绑定。')
    if not isinstance(context,dict) or not isinstance(context.get('task'),dict) or context['task'].get('id')!=proof.task_id:
        _fail('服务端约束与当前任务不匹配；未跨任务注入。')
    restored=[{'role':role,'content':content} for role,content in proof.messages]
    if any(m['role'] not in ('user','assistant') or not isinstance(m['content'],str) for m in restored):_fail('服务端执行约束投影无效。')
    # Only THIS authenticated observation may be canonicalized. Its original
    # working JSON often has spaces while the bounded restored view does not.
    # Different repair attempts and unrelated user prefixes remain untouched.
    receipt=restored[-1]['content'];prefix=next((p for p in _PREFIXES.values() if receipt.startswith(p)),None)
    exact={(m['role'],m['content']) for m in restored if m['role']=='user'}
    remove=set();observations=[]
    for index,message in enumerate(messages):
        if (message.get('role'),message.get('content')) in exact:remove.add(index)
        text=message.get('content')
        if message.get('role')!='user' or not prefix or not isinstance(text,str) or not text.startswith(prefix):continue
        try:canonical=_encode(json.loads(text[len(prefix):]));digest=hashlib.sha256(canonical.encode()).hexdigest()
        except (TypeError,ValueError,UnicodeError):continue
        if digest==proof.payload_sha256:remove.add(index);observations.append(index)
    for index in observations:
        previous=index-1
        while previous>=0 and previous in remove:previous-=1
        if previous<0:continue
        message=messages[previous];text=message.get('content')
        if (message.get('role')=='assistant' and isinstance(text,str) and proof.proposal_sha256 is not None
                and len(text)==proof.proposal_characters
                and hashlib.sha256(text.encode('utf-8',errors='replace')).hexdigest()==proof.proposal_sha256):
            remove.add(previous)
    result=[m for index,m in enumerate(messages) if index not in remove]
    if restored and restored[0]['role']=='assistant' and result and result[-1]==restored[0]:restored=restored[1:]
    start=len(result);result.extend(restored)
    protected=list(range(start,len(result)))
    if start and proof.messages[0][0]=='assistant' and (not restored or restored[0]['role']!='assistant'):protected.insert(0,start-1)
    return result,protected
