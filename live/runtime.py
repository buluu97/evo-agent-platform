"""Bound model context and receipts without changing stored execution evidence."""
import copy
import hashlib
import json
import re
from collections import Counter


class WorkBudgetExhausted(RuntimeError):
    """Stop production while preserving the independent acceptance reserve."""


def review_work(step):
    return bool(step.get('reviewOf') or step.get('acceptance'))


def check_budget(run, step):
    limits=run['limits']
    if run['modelCalls']>=limits['calls']:raise RuntimeError('Model call budget exhausted')
    if run['tokens']>=limits['tokens']:raise RuntimeError('Token budget exhausted')
    if not review_work(step):
        usage=run.get('budgetUsage',{}).get('work',{})
        if usage.get('calls',0)>=limits['calls']-limits.get('reviewReserveCalls',0):
            raise WorkBudgetExhausted('Production call allowance reached; independent acceptance reserve preserved')
        if usage.get('tokens',0)>=limits['tokens']-limits.get('reviewReserveTokens',0):
            raise WorkBudgetExhausted('Production token allowance reached; independent acceptance reserve preserved')


def repetitive(text):
    lines=[re.sub(r'[^a-z0-9 ]','',s.lower()).strip() for s in text.splitlines()]
    lines=[s for s in lines if len(s)>3]
    counts=Counter(lines)
    return len(lines)>35 and sum(n for _,n in counts.most_common(5))>len(lines)*.65


def proposed_action(text):
    """Parse only a top-level, complete ReAct proposal; never a quoted example."""
    match=re.search(r'(?m)^Action:\s*gateway\s*\nAction Input:\s*',text)
    if not match or 'Final Answer:' in text[:match.start()] or '```' in text[:match.start()]:return None
    try:value,_=json.JSONDecoder().raw_decode(text[match.end():].lstrip())
    except (ValueError,TypeError):return None
    if not isinstance(value,dict) or not isinstance(value.get('operation'),str) or not isinstance(value.get('arguments'),dict):return None
    return value


def compact_messages(messages, limit=48000):
    """Keep task/permissions and the newest observations; shrink old write bodies."""
    prepared=[dict(m) for m in messages]
    seen=set()
    for m in reversed(prepared):
        body=str(m.get('content',''));action=proposed_action(body)
        if action and action['operation']=='file.read' and '\nObservation:' in body:
            try:
                value,_=json.JSONDecoder().raw_decode(body.rpartition('\nObservation:')[2].lstrip())
                if value.get('ok') and value.get('sha256'):
                    key=json.dumps([action['arguments'],value['sha256']],sort_keys=True)
                    if key in seen:m['content']='[Duplicate older read omitted; the same current file/window and hash are retained in the later real observation.]'
                    else:seen.add(key)
            except (ValueError,AttributeError,TypeError):pass
    # Current linked source files are necessary context, not redundant history.
    # Do not strip HTML/JS bodies while they still fit in the request budget.
    if sum(len(str(m.get('content',''))) for m in prepared)<=limit:return prepared
    copied=[]
    for index,original in enumerate(prepared):
        m=dict(original);body=str(m.get('content',''))
        if index>=2 and index<len(messages)-2 and m.get('role')=='assistant':
            action=proposed_action(body)
            if action and action['operation'] in ('file.write','file.patch','code.run','code.test'):
                args=action['arguments'];value=args.get('content',args.get('new',''))
                if len(str(value))>1200 or action['operation'] in ('code.run','code.test'):
                    args={k:v for k,v in args.items() if k not in ('content','old','new','code','checks','resources','storage')}
                    args['historicalBody']={'characters':len(str(value)),'sha256':hashlib.sha256(str(value).encode()).hexdigest(),'instruction':'Historical proposal abbreviated. Consult its real receipt/current file; this is not an executable action.'}
                    # CrewAI appends the actual Observation to the SAME assistant
                    # message as the Action. Never drop its successful receipt.
                    observation=body.rpartition('\nObservation:')[2]
                    body='Historical operation arguments abbreviated: '+json.dumps({'operation':action['operation'],'arguments':args},ensure_ascii=False)
                    if observation:body+='\nObservation:'+observation
        m['content']=body;copied.append(m)
    if sum(len(m['content']) for m in copied)<=limit:return copied
    # CrewAI sends system/task messages separately. Preserve those contracts and
    # retain the freshest actual observations, instead of a large stale prefix.
    head=[]
    for m in copied[:2]:
        item=dict(m)
        if len(item['content'])>13000:
            item['content']=item['content'][:9500]+'\n[Earlier context abbreviated; actual files remain authoritative.]\n'+item['content'][-3000:]
        head.append(item)
    note={'role':'user','content':'Older intermediate turns were removed. Current task/permissions above and recent real observations below are authoritative. Read a specific file window only when missing; never reprint entire files as a handoff.'}
    remaining=limit-sum(len(m['content']) for m in head)-len(note['content'])
    tail=[]
    for item in reversed(copied[2:]):
        if len(item['content'])>remaining:
            if not tail and remaining>300:
                item=dict(item);item['content']='[Observation preview; reread missing lines if needed.]\n'+item['content'][-remaining+100:];tail.insert(0,item)
            break
        tail.insert(0,item);remaining-=len(item['content'])
    return head+[note]+tail


def receipt_view(operation,result):
    """Model-facing view only. The full unmodified receipt stays in the ledger."""
    view=copy.deepcopy(result)
    if operation in ('file.write','file.patch','file.delete'):
        view.pop('content',None)
    if operation=='web.fetch':
        if len(view.get('content',''))>7000:view['content']=view['content'][:7000];view['truncated']=True
        view['links']=view.get('links',[])[:6]
    if operation in ('code.run','code.test'):
        browser=view.get('browser')
        if isinstance(browser,dict):
            # stdout duplicates the complete browser JSON in the raw receipt.
            view.pop('stdout',None)
            steps=browser.get('steps',[])
            compact={k:v for k,v in browser.items() if k not in ('steps','assertions','console','pages')}
            compact['stepCount']=len(steps)
            compact['failedSteps']=[s for s in steps if s.get('ok') is False or s.get('passed') is False][:3]
            assertions=browser.get('assertions',[])
            compact['assertions']=assertions[:24] if isinstance(assertions,list) else assertions
            compact['console']=browser.get('console',[])[:4]
            compact['pages']=[{k:v for k,v in p.items() if k in ('pageId','entryPath','errors')} for p in browser.get('pages',[])]
            view['browser']=compact
        for key in ('stdout','stderr'):
            if len(view.get(key,''))>6000:view[key]=view[key][:2000]+'\n[Full output retained in call receipt]\n'+view[key][-3800:]
    if operation=='collab.delegate':
        view['result']=str(view.get('result',''))[:1800]
    return view


def repeated_published_read(run, step, args, receipt):
    """Detect an identical read of the producer's current published version."""
    digest = receipt.get('sha256')
    if not digest or not step.get('requiresArtifacts') or review_work(step):
        return False
    path = args.get('path')
    current = any(a['taskId'] == step['id'] and a['ownerAgentId'] == step['agentId']
                  and a['path'] == path and a['sha256'] == digest
                  and not a.get('superseded') and not a.get('carriedForward')
                  for a in run['artifacts'])
    if not current:
        return False
    matches = 0
    for call in reversed(run['calls']):
        if call['taskId'] != step['id'] or call['agentId'] != step['agentId']:
            continue
        if call['tool'] in ('code.run', 'code.test'):
            break  # A test result can justify inspecting this location again.
        if call['tool'] == 'file.read' and call['status'] == 'success' and call['args'] == args:
            if call.get('result', {}).get('sha256') == digest:
                matches += 1
                if matches == 2:
                    return True
    return False


def model_checkpoint(ctx,step):
    """Durable facts survive scratchpad compaction; no model claims become facts."""
    run=ctx['run'];aid=step['agentId'];calls=[c for c in run['calls'] if c['agentId']==aid]
    files={}
    for c in calls:
        if c['tool'] in ('file.write','file.patch') and c['status']=='success':
            files[c['args']['path']]={k:c.get('result',{}).get(k) for k in ('sha256','bytes','size') if c.get('result',{}).get(k) is not None}
    tests=[]
    for c in calls:
        if c['tool'] not in ('code.test','code.run') or c['status']=='running':continue
        v=c.get('result',{});b=v.get('browser',{})
        assertions=[{k:x.get(k) for k in ('action','selector','text','count','value')} for x in c['args'].get('checks',[]) if x.get('action','').startswith('assert_')]
        tests.append({'callId':c['id'],'ok':v.get('ok',False),'testedFiles':b.get('testedFiles',[]),'assertions':assertions[:12],
                      'error':str(v.get('error',''))[:600],'output':str(v.get('stdout',''))[:400] if not b else '',
                      'failureObservation':b.get('failureObservation') if not v.get('ok') else None})
    reads=[{'path':c['args'].get('path'),'sha256':c.get('result',{}).get('sha256'),'mode':c.get('result',{}).get('mode'),'truncated':c.get('result',{}).get('truncated')} for c in calls if c['tool']=='file.read' and c['status']=='success']
    state={'ownFilesAlreadyWrittenFromActualReceipts':files,'recentActualTests':tests[-4:],'recentActualReads':reads[-6:],
           'humanDecisions':[{'question':p['title'][:180],'answer':p['answer']} for p in run['pending'] if p['status']=='answered' and p['kind']=='clarification'][-3:],
           'reviewsAlreadySubmitted':[{'target':r['targetTaskId'],'revision':r['revision'],'verdict':r['verdict'],'artifacts':r['artifacts']} for r in run['reviews'] if r['reviewer']==aid][-6:]}
    if review_work(step):
        targets=step.get('reviewTargets') or [step.get('reviewOf')]
        artifacts=[a for a in run['artifacts'] if a['taskId'] in targets and not a.get('superseded') and not a.get('carriedForward')]
        read_hashes={c.get('result',{}).get('sha256') for c in calls if c['tool']=='file.read' and c['status']=='success'}
        tested_hashes={f['sha256'] for c in calls if c['tool'] in ('code.test','code.run') and c['status']=='success' and c.get('result',{}).get('exitCode')==0 for f in c.get('result',{}).get('browser',{}).get('testedFiles',[])}
        unread=[{'target':a['taskId'],'path':'imports/'+a['id']+'/'+a['name'],'sha256':a['sha256']} for a in artifacts if a['sha256'] not in read_hashes]
        untested=[a['name'] for a in artifacts if a['name'].endswith(('.html','.js')) and a['sha256'] not in tested_hashes]
        state['reviewGateStatus']={'targets':targets,'unreadArtifacts':unread,'executableFilesWithoutSuccessfulHashBoundTest':untested,'minimumEvidenceReady':bool(artifacts) and not unread and not untested,
                                 'instruction':'After verifying the ORIGINAL goal and confirmed requirements, submit review.submit NOW. If essential behavior/evidence fails, submit fail with concrete defects. Do not keep re-reading unchanged files or explore optional, unrequested policies instead of giving a verdict.'}
    text=json.dumps(state,ensure_ascii=False,separators=(',',':'))
    if len(text)>8500:
        state['recentActualTests']=tests[-2:];state['recentActualReads']=reads[-3:]
        for t in state['recentActualTests']:
            t['failureObservation']=None;t['assertions']=t['assertions'][:5]
        text=json.dumps(state,ensure_ascii=False,separators=(',',':'))
    from backend.browser_spec import _ACTIONS
    shapes={k:sorted(v) for k,v in _ACTIONS.items()}
    guide='\nBROWSER PROTOCOL: Each check is {"action":NAME, ...EXACT_REQUIRED_FIELDS}. Required fields by action: '+json.dumps(shapes,separators=(',',':'))+'. Only optional fields: pageId; exact:boolean on assert_text; key on storage read/write denial; value on respond_dialog. No contains/match/expected fields. assert_text is case-sensitive substring unless exact=true. assert_storage compares the COMPLETE stored string exactly; never use it for unknown dynamic IDs/timestamps or a placeholder. Test persistence via actual UI after reload instead. Seed storage only using a known correct application schema. Use select for <select>, fill for input/textarea, and unique CSS selectors (e.g. table tr:last-child button, not button:last-of-type across many rows). Each successful submit may clear required fields: refill them for the next record. Arm respond_dialog BEFORE a click opening a native dialog. Max60 checks, >=1 real assertion, all JS/CSS resources explicitly mapped.'
    if not set(ctx['agents'][aid]['tools']) & {'code.run','code.test'}:guide=''
    return 'AUTHORITATIVE EXECUTION CHECKPOINT (actual receipts only; not a success/acceptance claim): '+text+'\nDo not rewrite existing files just because their old Action was compacted. Use existing files and fix only observed defects. Reuse successful tests of unchanged hashes. Correct test-input/selector errors without treating them as product defects.'+guide


def short_handoff(text,limit=1800):
    """The actual product is in files. Do not propagate repeated source bodies."""
    if len(text)<=limit:return text
    start=text.find('```')
    if 0<=start<limit:return text[:start]+'\n[Source/content omitted here. Read the published artifact files for the complete product.]'
    return text[:limit]+'\n[Long narrative omitted; inspect actual artifact files and receipts.]'


def review_directive(ctx,step):
    """Prioritize remaining protocol obligations without choosing the verdict."""
    if not review_work(step):return ''
    run=ctx['run'];aid=step['agentId'];targets=step.get('reviewTargets') or [step.get('reviewOf')]
    artifacts=[a for a in run['artifacts'] if a['taskId'] in targets and not a.get('superseded') and not a.get('carriedForward')]
    read_hashes={c.get('result',{}).get('sha256') for c in run['calls'] if c['agentId']==aid and c['tool']=='file.read' and c['status']=='success'}
    unread=[a for a in artifacts if a['sha256'] not in read_hashes]
    if unread:
        a=unread[0]
        return 'NEXT REQUIRED REVIEW ACTION: file.read '+json.dumps({'path':'imports/'+a['id']+'/'+a['name']})+'. This current artifact has not been read. Other already-read source hashes are in the checkpoint; do not reread them without a specific missing detail.'
    completed=[]
    for tid in targets:
        target=next((s for s in run['steps'] if s['id']==tid),None)
        ids=sorted(a['id'] for a in artifacts if a['taskId']==tid)
        if target and any(r['targetTaskId']==tid and r['revision']==target['revision'] and r['artifacts']==ids for r in run['reviews']):completed.append(tid)
    remaining=[t for t in targets if t not in completed]
    if not remaining:return 'REVIEW RECORDS COMMITTED for the current versions. Return a short Final Answer receipt now; do not run extra tests or write a recursively reviewed review report.'
    return 'REVIEW DECISION DUE for targetTaskIds='+json.dumps(remaining)+'. Use the actual evidence collected, assess the ORIGINAL requested behavior and confirmed requirements, and finish with the verdict TOOL using your REAL pass or fail verdict and concrete evidence (it commits the actual review.submit record and ends the step). Protocol readiness is NOT a product pass. Perform only targeted checks still essential to that decision (including source verification for research); do not re-read unchanged files or repeat already-successful checks. Optional unrequested policies are not blockers. If an essential criterion cannot be verified, use verdict with fail and identify the actual gap instead of looping indefinitely.'
