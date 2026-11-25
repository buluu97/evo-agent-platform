"""Local ReAct execution loop and course-provider adapter."""
import json
import re
from urllib.parse import parse_qsl, urlsplit
import requests
from backend.sandbox import ToolError
from .runtime import compact_messages,check_budget,review_work,repetitive,proposed_action,model_checkpoint,review_directive,repeated_published_read
from deepseek_protocol import normalize_dsml

def normalize_gateway_reply(text):
    """Accept one complete standalone gateway JSON call after optional reasoning."""
    pattern = r'(?m)^ *gateway: *'
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        return text
    prefix = text[:matches[0].start()]
    if '```' in prefix or re.search(r'(?m)^ *(?:Action|Final Answer|Observation):', prefix):
        return text
    payload = text[matches[0].end():].strip()
    try:
        action = json.loads(payload)
    except ValueError:
        return text
    if not isinstance(action, dict) or set(action) != {'operation', 'arguments'}:
        return text
    return 'Action: gateway\nAction Input: ' + payload

def listing_page(url):
    if not isinstance(url,str):
        return None
    parts=urlsplit(url)
    query=dict(parse_qsl(parts.query))
    if not query.get('page','').isdigit():
        return None
    del query['page']
    return parts.netloc.lower(),parts.path,tuple(sorted(query.items()))

def delivery_gate(calls,step,available_operations):
    """Return the single delivery phase justified by actual tool receipts."""
    declared=set(step.get('outputPaths') or [])
    if not declared:
        # Older/ad-hoc tasks may not have a declared contract. Keep their
        # existing behavior instead of creating an impossible empty publish.
        return {'phase':'legacy','missing':[],'allowed':set(available_operations)|{'artifact.publish'}}
    edits=[(i,c) for i,c in enumerate(calls) if c['tool'] in ('file.write','file.patch') and c['status']=='success']
    written={c['args'].get('path') for _,c in edits}
    missing=sorted(declared-written)
    if missing:
        return {'phase':'build','missing':missing,'allowed':{'file.write','file.patch'}}
    executable=any(str(p).lower().endswith(('.html','.js','.ts','.py','.tsx','.jsx')) for p in declared)
    testable=executable and bool({'code.test','code.run'} & set(available_operations))
    if testable:
        last_edit=max((i for i,_ in edits),default=-1)
        tests=[(i,c) for i,c in enumerate(calls) if c['tool'] in ('code.test','code.run')]
        after=[(i,c) for i,c in tests if i>last_edit]
        if not after:
            failed_before=[i for i,c in tests if c['status']=='failed' or c.get('result',{}).get('ok') is False]
            return {'phase':'test','missing':[],'allowed':{'code.test','code.run'},
                    'repairProgress':bool(failed_before and last_edit>max(failed_before))}
        _,latest=after[-1]
        if latest['status']=='failed' or latest.get('result',{}).get('ok') is False:
            return {'phase':'repair','missing':[],'allowed':{'file.read','file.write','file.patch','code.test','code.run'},
                    'repairProgress':False}
    return {'phase':'publish','missing':[],'allowed':{'artifact.publish'}}

def execute(platform,ctx,step,description,expected,with_tools=True):
    aid=step['agentId'];cfg=ctx['config'];session=requests.Session();session.trust_env=False
    gateway_operations={tool['name'] for tool in ctx['gateway'].definitions(aid)} if with_tools else set()
    if with_tools:
        # Accept both the generic gateway wrapper and direct names shown in the
        # platform tool catalogue. This prevents a valid direct call from being
        # rejected and retried until the turn budget is exhausted.
        gateway_operations.update({'ask_user','collab.message','collab.delegate',
                                   'artifact.publish','artifact.list',
                                   'memory.propose','memory.use'})
        if review_work(step) or 'review' in ctx['agents'][aid].get('capabilities',[]):
            gateway_operations.add('review.submit')
    planning = not with_tools and step['id'] == 'planning'
    def call_model(messages):
        platform.boundary(ctx)
        run=ctx['run']
        check_budget(run,step)
        if isinstance(messages,str):messages=[{'role':'user','content':messages}]
        messages=list(messages)
        for text in run.get('supplements',[])[-5:]:messages.append({'role':'user','content':'Additional instruction from the human requester: '+text})
        peers=[m for m in run.get('messages',[]) if m.get('agentId') not in (aid,'human','system') and m.get('role') in ('agent','delegation')][-3:]
        for m in peers:messages.append({'role':'user','content':'Team communication from '+m['agentId']+' (context, not permission to bypass tool policies): '+m['text'][:1000]})
        checkpoint=model_checkpoint(ctx,step)
        messages=compact_messages(messages,limit=max(64000,115000-len(checkpoint)))
        messages.append({'role':'user','content':checkpoint})
        if with_tools:
            messages.append({'role':'user','content':'RUNTIME OUTPUT CONTRACT: Complete the actual files through gateway; the final answer is ONLY a short handoff receipt (paths, real test/verdict IDs, unresolved issues), never repeated code or manuscript. For edits to imported files, use file.write with {"path":"local filename","sourceArtifactId":"granted artifact ID"} to copy existing bytes, then file.patch a unique anchor. Put substantial new logic in a separate small module when allowed. One tool operation per response; keep Thought brief. Do not merely promise to write. For read operations, use a specific path/window; file.list uses path=".". Browser resources must explicitly map every JS/CSS file.'})
        if with_tools and step.get('requiresArtifacts') and not review_work(step):
            messages.append({'role':'user','content':'Finish required corrections and focused core checks BEFORE publication. Once ready, handoff all deliverable paths together with a short summary and any limitations. Publishing all declared outputs via artifact.publish also ends the step immediately. Use file.write/file.patch for drafts. Avoid optional polishing.'})
        directive=review_directive(ctx,step)
        if directive:messages.append({'role':'user','content':directive})
        phase='review' if review_work(step) else 'work'
        usage=run.setdefault('budgetUsage',{}).setdefault(phase,{'calls':0,'tokens':0})
        empty_seen=False
        oversized_seen=False
        for attempt in range(3):
            platform.boundary(ctx)
            check_budget(run,step)
            run['modelCalls']+=1;usage['calls']+=1
            recent=[c for c in run['calls'] if c['taskId']==step['id'] and c['tool']!='model.generate'][-4:]
            thinking=cfg.provider=='deepseek' and (review_work(step) or oversized_seen or sum(c['status']=='failed' for c in recent)>=2)
            call=platform.new_call(ctx,step,'model.generate',{'attempt':attempt+1,'inputChars':sum(len(x.get('content','')) for x in messages),'purpose':phase,'thinking':'low' if thinking else 'disabled'})
            body={'model':cfg.model,'messages':messages,'temperature':.2,'max_tokens':8000 if (thinking or with_tools) else 3000,'stream':False,'stop':['\nObservation:','\n Observation:','\n  Observation:']}
            if planning:
                body.pop('stop')
                if cfg.provider == 'deepseek' or getattr(cfg, 'json_mode', 'default') == 'json_object':
                    body['response_format'] = {'type': 'json_object'}
            if cfg.provider=='deepseek':
                body['thinking']={'type':'enabled' if thinking else 'disabled'}
                if thinking:body['reasoning_effort']='low'
                if empty_seen:body.pop('stop',None)
            elif cfg.thinking_mode=='disabled':body['chat_template_kwargs']={'enable_thinking':False}
            try:
                read_timeout=75 if thinking else 45
                if run.get('deadlineEnforced', True):read_timeout=min(read_timeout,max(1,platform._step_remaining(run,step)))
                response=session.post(cfg.base_url+'/chat/completions',json=body,headers={'Authorization':'Bearer '+cfg.api_key},timeout=(8,read_timeout),allow_redirects=False)
                response.raise_for_status();value=response.json();choice=value['choices'][0];raw=choice['message'].get('content')
                spent=int(value.get('usage',{}).get('total_tokens',0));run['tokens']+=spent;usage['tokens']+=spent;run['returnedModel']=value.get('model')
                platform.end_call(ctx,call,{'model':value.get('model'),'usage':value.get('usage',{}),'finishReason':choice.get('finish_reason'),'rawContent':raw},True)
                if not isinstance(raw,str) or not raw.strip():
                    empty_seen=True;raise ValueError('Model returned empty content')
                if planning:
                    if choice.get('finish_reason') == 'length':
                        raise ValueError('Task graph was truncated. Shorten task descriptions.')
                    json.loads(raw)
                    platform.boundary(ctx)
                    return raw
                if repetitive(raw):raise ValueError('Repetitive response did not execute an action; return one real gateway operation now')
                if with_tools:raw=normalize_gateway_reply(raw)
                if choice.get('finish_reason')=='length' and not proposed_action(raw):
                    oversized_seen=True
                    raise ValueError('Incomplete oversized response. No operation executed. For imported source, file.write with path and sourceArtifactId copies the existing bytes; then patch one unique anchor. For new code write one small module per call (aim below 3000 characters).')
                content=normalize_dsml(raw,gateway_operations=gateway_operations,allow_handoff=with_tools and bool(step.get('requiresArtifacts')) and not review_work(step),allow_review=with_tools and review_work(step)) if cfg.provider=='deepseek' else raw
                if '\nObservation:' in content and 'Action:' in content.split('\nObservation:')[0] and 'Final Answer:' not in content.split('\nObservation:')[0]:content=content.split('\nObservation:',1)[0]
                platform.boundary(ctx)
                return content
            except Exception as exc:
                status=getattr(getattr(exc,'response',None),'status_code',None)
                if call['status']=='running':platform.end_call(ctx,call,{'error':cfg.scrub(str(exc)),'httpStatus':status},False)
                else:platform.event(ctx,'model_output_rejected',callId=call['id'],reason=str(exc))
                if attempt==2 or status in (400,401,402,403):raise
                platform.boundary(ctx)
                retry_hint = (' Return one complete JSON object with a tasks array.' if planning else
                              ' Return ONE complete compact Thought/Action with valid JSON, or a short truthful Final Answer receipt. Never invent observations.')
                messages=compact_messages(messages+[{'role':'user','content':str(exc)[:500]+retry_hint}],limit=115000)
        raise RuntimeError('Model retries exhausted')

    role = ctx['agents'][aid]
    system = f"You are {role['name']}. {role['rolePrompt']}"
    if with_tools:
        system += (
            '\nReturn one Action: <tool name> followed on the next line by '
            'Action Input: <JSON object>, or Final Answer: <short truthful result>. '
            'Only the runtime supplies Observation. Available tools:\n'
            'gateway: {"operation":"...", "arguments":{...}}\n'
            + platform.tool_description(ctx, aid)
        )
        if step.get('requiresArtifacts') and not review_work(step):
            system += '\nhandoff: {"paths":["..."],"summary":"..."}, publish completed files.'
        if review_work(step):
            system += '\nverdict: {"verdict":"pass or fail","summary":"evidence"}, submit independent review.'
        description += '\nArtifact acceptance criteria: ' + expected
        expected = 'A truthful handoff receipt of at most 180 words with paths, evidence and unresolved issues.'
    messages = [
        {'role':'system','content':system},
        {'role':'user','content':description + '\nExpected output: ' + expected},
    ]
    try:
        if not with_tools:
            return call_model(messages)
        listings={}
        unchanged_patches=set()
        action_history={}
        previous_rejection=None
        research_started=False
        normal_turns=36
        max_turns=44

        def task_calls():
            return [c for c in ctx['run']['calls'] if c['taskId']==step['id'] and
                    c['agentId']==aid and c['tool']!='model.generate']

        def delivery_instruction(turn):
            if turn<28 or not step.get('requiresArtifacts') or review_work(step):return ''
            state=delivery_gate(task_calls(),step,gateway_operations)
            remaining=max_turns-turn if turn>=normal_turns else normal_turns-turn
            if state['phase']=='build':
                return f'DELIVERY PHASE ({remaining} turns remain): create only the missing declared outputs now: '+', '.join(state['missing'])+'. Stop optional research and polishing.'
            if state['phase']=='test':
                return f'DELIVERY PHASE ({remaining} turns remain): run one focused code.test/code.run now. Reads, cosmetic edits, and new features are blocked until this check runs.'
            if state['phase']=='repair':
                return f'DELIVERY PHASE ({remaining} turns remain): the latest focused test failed. Fix only that observed defect, then rerun the test. Optional polishing is blocked.'
            if state['phase']=='legacy':
                return f'DELIVERY PHASE ({remaining} turns remain): publish the actual requested files now. This legacy task has no declared outputPaths, so handoff must name the real completed paths.'
            return f'DELIVERY PHASE ({remaining} turns remain): the deliverable has sufficient current evidence and will now be published automatically.'

        def extension_allowed():
            state=delivery_gate(task_calls(),step,gateway_operations)
            return state['phase']=='test' and state.get('repairProgress',False)

        for turn in range(max_turns):
            if turn==normal_turns:
                if not extension_allowed():
                    raise RuntimeError('Agent reached its 36-turn limit without a final result; no recent non-repetitive delivery progress justified an extension')
                messages.append({'role':'user','content':'A final 8-turn extension was granted because recent unique file edits show delivery progress. Use it only for the remaining focused check and immediate publication. No new research, features, or cosmetic refinement.'})
            closing=turn>=30 and research_started and step.get('requiresArtifacts') and not review_work(step)
            if closing:
                remaining=max_turns-turn if turn>=normal_turns else normal_turns-turn
                messages.append({'role':'user','content':f'{remaining} turns remain. Source collection is closed. Write the requested deliverable now using actual sources and dated citations; mark every unsupported claim as unverified or omit it. Finish with handoff.'})
            delivery=delivery_instruction(turn)
            if delivery:messages.append({'role':'user','content':delivery})
            if turn>=28 and step.get('requiresArtifacts') and not review_work(step):
                state=delivery_gate(task_calls(),step,gateway_operations)
                if state['phase']=='publish':
                    paths=list(step.get('outputPaths') or [])
                    if paths:
                        result=platform.tool(ctx,step,'artifact.publish',{'paths':paths})
                        return json.dumps({'handoff':result,'summary':'Runtime published the completed declared outputs after delivery checks.','productAcceptance':'unassessed'},ensure_ascii=False)
            reply = call_model(messages).strip()
            # Parse only the first protocol directive. Text inside a JSON string is data.
            directive = re.search(r'(?m)^ *(Action|Final Answer):[ \t]*', reply)
            if directive and directive[1] == 'Final Answer':
                return reply[directive.end():].strip()
            rejected=None;op=None;params={}
            try:
                if not directive:
                    raise ValueError('Expected Action or Final Answer')
                name, payload = reply[directive.end():].split('\n', 1)
                header, separator, raw = payload.strip().partition(':')
                if not separator or header != 'Action Input':
                    raise ValueError('Expected Action Input: followed by a JSON object')
                args = json.loads(raw)
                if not isinstance(args, dict):
                    raise ValueError('Tool input must be a JSON object')
                name = name.strip()
                if name == 'gateway' and args.get('operation') == 'handoff':
                    name = 'handoff'
                    args = args.get('arguments')
                    if not isinstance(args, dict):
                        raise ValueError('handoff requires arguments with paths and a short summary')
                if name == 'gateway':
                    op = args['operation']
                    params = args.get('arguments', {})
                    if not isinstance(op, str) or not isinstance(params, dict):
                        raise ValueError('Invalid gateway operation or arguments')
                elif name in gateway_operations:
                    op, params = name, args
                elif name == 'handoff' and step.get('requiresArtifacts') and not review_work(step):
                    op = 'artifact.publish'
                    params = {'paths': args['paths']}
                elif name == 'verdict' and review_work(step):
                    op = 'review.submit'
                    params = {'targetTaskIds':step.get('reviewTargets') or [step.get('reviewOf')],
                              'verdict':args['verdict'], 'summary':args['summary']}
                else:
                    rejected=name
                    raise ValueError(f'Unavailable direct tool: {name}. For a listed platform operation, use Action: gateway with Action Input: {{"operation":"<listed operation>","arguments":{{...}}}}.')
                platform.boundary(ctx)
                if turn>=28 and step.get('requiresArtifacts') and not review_work(step):
                    state=delivery_gate(task_calls(),step,gateway_operations)
                    if op not in state['allowed']:
                        required=', '.join(sorted(state['allowed']))
                        raise ValueError(f'Delivery phase {state["phase"]} blocks {op}. Next operation must be one of: {required}.')
                fingerprint=json.dumps([op,params],sort_keys=True,ensure_ascii=False,separators=(',',':'))
                prior=action_history.get(fingerprint)
                # file.patch has a SHA-aware no-op guard below; allow it to run so
                # a changed file version is not mistaken for a duplicate action.
                guarded=op in {'artifact.publish','file.write','collab.message','memory.propose','memory.use','review.submit'}
                if prior and guarded:
                    prior['repeats']+=1
                    if prior['result'].get('ok'):
                        result={**prior['result'],'reusedAction':True,
                                'runtimeHint':'This identical successful action was not executed again. Continue to the next required state.'}
                        if op=='artifact.publish' and step.get('requiresArtifacts') and not review_work(step):
                            declared=set(step.get('outputPaths') or [])
                            published={a['path'] for a in result.get('artifacts',[]) if a.get('sha256')}
                            if published and (not declared or declared<=published):
                                return json.dumps({'handoff':result,'summary':'Publication was already completed.',
                                                   'productAcceptance':'unassessed'},ensure_ascii=False)
                    elif prior['repeats']>=2:
                        raise RuntimeError('Repeated identical failed action was stopped: '+str(prior['result'].get('error','unknown tool failure')))
                    else:
                        result={'ok':False,'error':'This exact action already failed and was not executed again. Change the arguments or choose the next required operation. Previous error: '+str(prior['result'].get('error','unknown'))}
                else:
                    result=None
                page=None
                if result is None and op in ('web.fetch','web.search'):
                    research_started=True
                    if closing:
                        raise ValueError('Research time is reserved for writing and handoff. Use the evidence already collected.')
                    if op=='web.fetch':
                        page=listing_page(params.get('url'))
                        if page and listings.get(page,0)>=3:
                            raise ValueError('Already inspected three pages of this listing. Follow a specific article URL from a receipt, use a different primary source, or write up the evidence and gaps.')
                if result is None:result = platform.tool(ctx, step, op, params)
                if guarded and fingerprint not in action_history:
                    action_history[fingerprint]={'result':result,'repeats':0}
                if page and result.get('ok'):
                    listings[page]=listings.get(page,0)+1
                    if listings[page]>=2:
                        result={**result,'researchHint':'Identify the new citable finding and its URL. If this list adds no evidence, open a specific article or switch sources now.'}
                if result.get('ok') and op == 'artifact.publish' and step.get('requiresArtifacts') and not review_work(step):
                    declared=set(step.get('outputPaths') or [])
                    published={a['path'] for a in result.get('artifacts',[]) if a.get('sha256')}
                    if declared and not declared <= published:
                        result={**result,'handoffHint':'When ready, publish all declared outputPaths together: '+', '.join(sorted(declared))}
                    elif published and (declared or name == 'handoff'):
                        return json.dumps({'handoff':result,'summary':str(args.get('summary',''))[:1500],
                                           'productAcceptance':'unassessed'},ensure_ascii=False)
                if result.get('ok') and op == 'review.submit' and (
                    name == 'verdict' or review_directive(ctx,step).startswith('REVIEW RECORDS COMMITTED')
                ):
                    return json.dumps(result,ensure_ascii=False)
            except (KeyError, TypeError, ValueError, ToolError) as exc:
                result = {'ok':False,'error':cfg.scrub(str(exc))}
            if rejected is not None and rejected == previous_rejection:
                raise RuntimeError('Repeated rejected tool call. ' + result['error'])
            previous_rejection=rejected
            messages.append({'role':'assistant','content':reply + '\nObservation: ' + json.dumps(result,ensure_ascii=False)})
            if result.get('unchanged') and op == 'file.patch':
                signature=(params['path'],result['sha256'],params['old'])
                if signature in unchanged_patches:
                    messages.append({'role':'user','content':
                        'Repeated identical patch on the same file version. Stop retrying this edit. '
                        'If a correction remains, make a genuinely different replacement. Otherwise '
                        'finish outstanding checks and hand off the final files, noting any unresolved items.'})
                unchanged_patches.add(signature)
            if result.get('ok') and op == 'file.read' and repeated_published_read(ctx['run'],step,params,result):
                messages.append({'role':'user','content':
                    'This exact location has already been read at the current published SHA256. '
                    'Use the existing receipt. If the assignment requires a functional check still missing, '
                    'run that specific check now using an available test tool, or report the limitation. '
                    'If all requested deliverables and checks are complete, call handoff now with all final paths. '
                    'Continue editing only for an identified defect; an unchanged hash is not a fix.'})
        raise RuntimeError('Agent reached its extended 44-turn limit without a final result')
    finally:
        session.close()
