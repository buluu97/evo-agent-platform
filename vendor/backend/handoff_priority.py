"""Advisory handoff evidence; never publishes, accepts, or changes a budget."""
import copy
import json
from pathlib import PurePosixPath
from .artifacts import _TEXT_TYPES

PREFIX='交接优先提醒：'


def _sha(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def _path(value):
    return (isinstance(value,str) and 0<len(value)<=240 and '\\' not in value
            and not value.startswith('/') and all(part not in ('','.','..') for part in value.split('/'))
            and not value.startswith('imports/'))


def handoff_priority(run,task,stage,qa_evidence,current_hashes,now_ms,finalization_ms,candidate=None):
    """Only current owned writes and actually served, host-validated browser bytes.

    ``qa_evidence`` is supplied by the existing host browser-progress verifier;
    current_hashes comes from confined owner/run reads, never model assertions.
    Counts and passing checks do not establish semantic coverage of criteria.
    """
    from .stage_control import _unresolved_owner_effects
    budget=run.get('budget') or {};revision=task.get('revision')
    if (budget.get('profile')!='time_only' or run.get('mode')!='task' or run.get('status')!='running'
            or task.get('status')!='running' or task.get('finalSummary') or type(revision) is not int
            or revision<0 or type(now_ms) is not int or type(finalization_ms) is not int or finalization_ms<0
            or type(budget.get('deadlineAt')) is not int or now_ms>=budget['deadlineAt']-finalization_ms
            or _unresolved_owner_effects(run,task) or not isinstance(stage,dict)):
        return None
    if (stage.get('runId')!=run.get('id') or stage.get('taskId')!=task.get('id')
            or stage.get('agentId')!=task.get('agentId') or type(stage.get('revision')) is not int
            or stage['revision']!=revision or stage.get('runDeadlineAt')!=budget['deadlineAt']
            or any(type(stage.get(k)) is not int for k in ('allocatedAt','synthesisAt','handoffAt','expectedHandoffBy'))
            or not stage['allocatedAt']<=stage['synthesisAt']<=stage['handoffAt']<=stage['expectedHandoffBy']<=budget['deadlineAt']
            or now_ms<stage['expectedHandoffBy']):
        return None
    contract=task.get('outputContract') or {}
    extensions=contract.get('extensions',[])
    if (contract.get('kind')!='artifact' or not isinstance(extensions,list) or not extensions
            or not all(isinstance(e,str) and e.startswith('.') for e in extensions)
            or not isinstance(qa_evidence,dict) or qa_evidence.get('currentTaskRevision')!=revision
            or type(qa_evidence.get('currentTaskRevision')) is not int):return None
    calls=run.get('calls',[]);writes={}
    for call in calls:
        if call.get('agentId')!=task.get('agentId'):continue
        args=call.get('args') or {};result=call.get('result') or {};name=call.get('tool')
        if not isinstance(args,dict) or not isinstance(result,dict):continue
        path=args.get('path')
        if name in ('file.write','file.patch','file.delete') and _path(path):
            writes.pop(path,None)
            if (name!='file.delete' and call.get('taskId')==task.get('id')
                    and type(call.get('revision')) is int and call['revision']==revision
                    and call.get('status')=='success' and result.get('ok') is True and _sha(result.get('sha256'))):
                writes[path]={'path':path,'sha256':result['sha256'],'writeCallId':call.get('id')}
    validated={(r.get('path'),r.get('sha256')) for r in qa_evidence.get('sources',[]) if isinstance(r,dict)}
    browser_ids=set(qa_evidence.get('successfulBrowserCallIds',[]));tested={}
    for call in calls:
        if (call.get('id') not in browser_ids or call.get('agentId')!=task.get('agentId')
                or call.get('taskId')!=task.get('id') or type(call.get('revision')) is not int
                or call['revision']!=revision or call.get('status')!='success' or call.get('tool')!='code.test'):continue
        result=call.get('result') or {};args=call.get('args') or {};browser=result.get('browser') or {}
        if (args.get('language')!='browser' or result.get('ok') is not True or result.get('test') is not True
                or type(result.get('exitCode')) is not int or result['exitCode']!=0
                or browser.get('executed') is not True or browser.get('ok') is not True):continue
        for ref in browser.get('testedFiles',[]):
            if isinstance(ref,dict) and (ref.get('path'),ref.get('sha256')) in validated:
                tested[(ref['path'],ref['sha256'])]=call['id']
    published={(a.get('path'),a.get('sha256')) for a in run.get('artifacts',[]) if isinstance(a,dict)
               and a.get('id') in task.get('outputs',[]) and a.get('runId')==run.get('id')
               and a.get('taskId')==task.get('id') and a.get('ownerAgentId',a.get('owner'))==task.get('agentId')
               and not a.get('inheritedReadOnly')}
    rows=[];published_rows=[];contract_match=False
    for path,row in sorted(writes.items()):
        digest=row['sha256']
        if (PurePosixPath(path).suffix.lower() not in _TEXT_TYPES or current_hashes.get(path)!=digest
                or (path,digest) not in tested):continue
        if PurePosixPath(path).suffix.lower() in extensions:contract_match=True
        if (path,digest) in published:
            published_rows.append({**row,'browserCallId':tested[(path,digest)]})
            continue
        rows.append({**row,'browserCallId':tested[(path,digest)]})
    if not rows and contract_match and published_rows and isinstance(candidate,dict):
        runtime = [item for item in candidate.get('files',[]) if item.get('path','').lower().endswith(('.html','.htm','.js','.mjs','.css'))]
        if (candidate.get('status')=='candidate' and candidate.get('runId')==run['id']
                and candidate.get('taskId')==task['id'] and candidate.get('taskRevision')==revision
                and runtime and all((item['path'],item['sha256']) in published
                                    and (item['path'],item['sha256']) in tested
                                    and current_hashes.get(item['path'])==item['sha256'] for item in runtime)):
            return {'schemaVersion':1,'advisoryOnly':True,'runId':run['id'],'taskId':task['id'],
                    'agentId':task['agentId'],'revision':revision,'deadlineAt':budget['deadlineAt'],
                    'currentOwnedUnpublishedTestedFiles':[], 'currentPublishedTestedFiles':published_rows[:12],
                    'candidateDeliveryId':candidate['id'],'semanticCoverage':'unassessed',
                    'instruction':'The soft handoff target has passed and the current candidate runtime already has successful browser observations. '
                        'Compare the original criteria with existing coverage. Complete only specific missing checks or repairs, '
                        'then explicitly final with done/partial/failed so the planned independent reviewer can inspect this candidate. '
                        'Repeated passing tests do not replace independent review. No publication, review, completion or budget was created.'}
    if not rows or not contract_match:return None
    selected=rows[:12]
    priority={'schemaVersion':1,'advisoryOnly':True,'runId':run['id'],'taskId':task['id'],
        'agentId':task['agentId'],'revision':revision,'softTargetAt':stage['expectedHandoffBy'],
        'overdueMs':now_ms-stage['expectedHandoffBy'],'remainingMs':budget['deadlineAt']-now_ms,
        'deadlineAt':budget['deadlineAt'],'currentOwnedUnpublishedTestedFiles':selected,
        'omittedEligibleFiles':len(rows)-len(selected),'semanticCoverage':'unassessed',
        'validatedSourceIndexOmitted':qa_evidence.get('omittedSources',0),
        'bundleCompleteness':'not_proven; these are bounded candidates, not a complete resource inventory',
        'criteriaReference':'task.acceptanceCriteria','contractReference':'task.outputContract',
        'publicationProposal':{'type':'publish_artifacts','paths':[row['path'] for row in selected]},
        'instruction':'The soft handoff target has passed. These current owned bytes have actual passing browser observations, '
            'not proof that the business criteria are met. Prioritize publishing usable current files with the explicit top-level '
            'publication action, then honestly finalize this producer so the planned independent QA can inspect them. '
            'Contract extensions specify required output types, not a ban on accompanying tested JS/CSS assets. '
            'The bounded source index can omit resources; identify and explicitly include any other required current dependencies. '
            'Do not keep merely rearranging passed checks; any genuinely missing test or repair remains allowed. '
            'Check original criteria and output contract; disclose gaps. This reminder does not publish, grant, approve, accept, '
            'stop testing, change status, or add time/calls/turns.'}
    # Oversized path lists are explicit subsets, never an unbounded second ledger.
    while len(json.dumps(priority,ensure_ascii=False))>4200 and len(selected)>1:
        selected.pop();priority['omittedEligibleFiles']+=1
        priority['publicationProposal']['paths']=[row['path'] for row in selected]
    return priority


def priority_tail(priority):
    """One compact current reminder, derived only from the canonical receipt."""
    if priority.get('candidateDeliveryId'):
        return {'runId':priority['runId'],'taskId':priority['taskId'],'revision':priority['revision'],
                'candidateDeliveryId':priority['candidateDeliveryId'],
                'instruction':priority['instruction']}
    return {'runId':priority['runId'],'taskId':priority['taskId'],'revision':priority['revision'],
        'currentFiles':[{'path':r['path'],'sha256':r['sha256']} for r in priority['currentOwnedUnpublishedTestedFiles']],
        'publicationProposal':copy.deepcopy(priority['publicationProposal']),
        'instruction':'Read progressReceipt.handoffPriority. The soft handoff target is overdue: prioritize an explicit publication '
            'of usable current files and honest producer final so planned independent QA can start. Original criteria remain '
            'unassessed; new necessary tests/repairs are allowed. No automatic publication, approval, acceptance or extra time.'}


def protected_priority_indices(messages):
    """Protect exact canonical-derived advice, never a user prefix by itself."""
    try:
        context=json.loads(messages[1]['content']);priority=context['progressReceipt']['handoffPriority']
        if priority.get('advisoryOnly') is not True or priority.get('taskId')!=context['task']['id']:return []
        expected=priority_tail(priority)
    except (IndexError,KeyError,TypeError,ValueError,AttributeError):return []
    found=[]
    for index,message in enumerate(messages[2:],2):
        if message.get('role')!='user' or not message.get('content','').startswith(PREFIX):continue
        try:value=json.loads(message['content'][len(PREFIX):])
        except (TypeError,ValueError):continue
        if value==expected:found.append(index)
    return found[-1:]
