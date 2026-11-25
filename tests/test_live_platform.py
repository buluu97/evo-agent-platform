"""Meaningful runtime boundary and persistence checks without a model service."""
import json,sys,tempfile,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'vendor'))
from live.platform import Platform,ACTIVE,MAX_INLINE_WRITE_CHARS
from backend.sandbox import write_text,ToolError

def wait_for(fn,seconds=6):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=fn()
        if value:return value
        time.sleep(.02)
    raise AssertionError('Condition did not complete')

PLAN={'tasks':[{'id':'write','agentId':'dev','title':'Write checklist','instructions':'Write checklist and publish','dependsOn':[],'requiresArtifacts':True,'expectedOutput':'checklist.md'},
               {'id':'check','agentId':'qa','title':'Independent review','instructions':'Read the exact artifact and review','dependsOn':['write'],'reviewRequest':'independent review','reviewOf':'write','requiresArtifacts':False}]}
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);cfg=self.base/'config';cfg.mkdir();(cfg/'.env').write_text('LLM_PROVIDER=deepseek\nLLM_BASE_URL=https://api.deepseek.com/v1\nLLM_MODEL=deepseek-flash\nLLM_API_KEY=offline-test-key\n')
        self.p=Platform(self.base/'data',cfg,executor=self.execute);self.flags={}
    def tearDown(self):
        for ctx in list(self.p.contexts.values()):
            if ctx['run']['status'] in ACTIVE:
                try:self.p.control(ctx['run']['teamId'],ctx['run']['id'],'cancel')
                except ValueError:pass
            if ctx.get('thread'):ctx['thread'].join(2)
        for t in self.p.teams.values():t['repo'].close()
        self.temp.cleanup()
    def execute(self,p,ctx,s,description,expected,tools):
        if s['id']=='planning':
            if self.flags.get('author_only'):return json.dumps({'tasks':[{'id':'write','agentId':'qa','title':'Risk note','instructions':'Write the risk note','dependsOn':[],'requiresArtifacts':True,'expectedOutput':'risk.md'}]})
            return json.dumps(PLAN)
        if s['id']=='reply':return 'PRIVATE_REPLY_246'
        if s['id']=='write':
            if self.flags.get('fail'):raise RuntimeError('Source retrieval failed; no result was created')
            if self.flags.get('ask'):p.tool(ctx,s,'ask_user',{'question':'Which checklist title?'})
            p.tool(ctx,s,'file.write',{'path':'checklist.md','content':'# Checklist\n- Verify actual evidence\n'})
            if self.flags.get('overwrite'):p.tool(ctx,s,'file.write',{'path':'checklist.md','content':'replacement'})
            p.tool(ctx,s,'artifact.publish',{'paths':['checklist.md'],'to':['qa']})
            if self.flags.get('candidate'):p.tool(ctx,s,'memory.propose',{'lesson':'Check actual checklist evidence before accepting','trigger':'checklist evidence'})
            if self.flags.get('use'):
                for mid in s.get('recalled',[]):
                    m=ctx['team']['repo'].get_memory(mid,'dev')
                    if m and m['status']=='candidate':p.tool(ctx,s,'memory.use',{'id':mid,'reason':'Use checklist evidence rule'})
            return 'Created actual checklist.md'
        if s['id']=='check':
            a=next(a for a in ctx['run']['artifacts'] if a['taskId']=='write' and not a.get('superseded'))
            path='imports/'+a['id']+'/'+a['name'];p.tool(ctx,s,'file.read',{'path':path})
            p.tool(ctx,s,'review.submit',{'targetTaskId':'write','verdict':'pass','summary':'Read the exact checklist and checked the requested line'})
            if self.flags.get('supersede'):
                producer=next(x for x in ctx['run']['steps'] if x['id']=='write')
                write_text(ctx['gateway'].workspace('dev',ctx['run']['id']),'checklist.md','changed version '+str(producer['revision']),overwrite=True)
                p.tool(ctx,producer,'artifact.publish',{'paths':['checklist.md'],'to':['qa']})
            return 'Reviewed current checklist'
        raise AssertionError('Unexpected step')
    def start(self,channel='group',**kwargs):return self.p.start('software',channel,'checklist evidence; independent review',seconds=30,**kwargs)
    def finish(self,r):return wait_for(lambda:(x if (x:=self.p.team('software')['repo'].get_run(r['id']))['status'] not in ACTIVE else None))
    def test_human_reply_wait_does_not_consume_the_deadline(self):
        run={'deadline':time.time()-5,'humanWaitMs':0,'humanWaitSince':time.time()-30,'cancelRequested':False,'pauseRequested':False}
        self.p.boundary({'run':run})
        run['humanWaitSince']=None;run['humanWaitMs']=10000
        self.p.boundary({'run':run})
        run['humanWaitMs']=0
        with self.assertRaisesRegex(TimeoutError,'not counted'):self.p.boundary({'run':run})
        run['deadlineEnforced']=False
        self.p.boundary({'run':run})
    def test_planner_graph_validation(self):
        for plan in ({'tasks':[]},{'tasks':[{'id':'a','agentId':'missing','instructions':'x'}]}, {'tasks':[{'id':'a','agentId':'dev','instructions':'x','dependsOn':['a']}]}):
            with self.assertRaises(ValueError):self.p.validate_plan(plan,self.p.team('software')['state']['agents'])
    def test_oversized_inline_write_is_rejected_with_split_guidance(self):
        r=self.finish(self.start());ctx=self.p.contexts[r['id']];s=r['steps'][0]
        with self.assertRaisesRegex(ValueError,'Do not inline a whole document'):
            self.p.tool(ctx,s,'file.write',{'path':'deck.html','content':'x'*(MAX_INLINE_WRITE_CHARS+1)})
        self.assertTrue(self.p.tool(ctx,s,'file.write',{'path':'deck.html','content':'x'*MAX_INLINE_WRITE_CHARS})['ok'])
    def test_unsolicited_review_is_replanned_and_delivery_completes(self):
        original=self.p.executor
        plans=[]
        def execute(p,ctx,step,*args):
            if step['id']=='planning':
                plans.append(step['id'])
                return json.dumps(PLAN if len(plans)==1 else {'tasks':PLAN['tasks'][:1]})
            return original(p,ctx,step,*args)
        self.p.executor=execute
        run=self.finish(self.p.start('software','group','Write a checklist',seconds=30))
        self.assertEqual(len(plans),2)
        self.assertEqual([s['id'] for s in run['steps']],['write'])
        self.assertEqual((run['status'],run['productVerdict']),('done','unassessed'))
        self.assertFalse(run['reviews'])
    def test_review_of_can_go_to_the_most_suitable_participant(self):
        agents=self.p.team('software')['state']['agents']
        self.assertNotIn('review', agents['release']['capabilities'])
        plan={'tasks':[
            {'id':'write','agentId':'dev','instructions':'Write the note','dependsOn':[]},
            {'id':'check','agentId':'release','instructions':'Check the note','dependsOn':['write'],'reviewRequest':'independent review','reviewOf':'write'}]}
        steps=self.p.validate_plan(plan, agents)
        self.assertEqual(steps[1]['agentId'], 'release')
        self.assertEqual(steps[1]['reviewOf'], 'write')
        r=self.finish(self.start());ctx=self.p.contexts[r['id']]
        ctx['run']['steps'].append(steps[1])
        with self.assertRaises(ValueError) as caught:
            self.p.review_record(ctx,'release','write',{'verdict':'pass','summary':'checked'})
        self.assertNotIn('review-capable', str(caught.exception))
        with self.assertRaisesRegex(ValueError, 'assigned to you'):
            self.p.review_record(ctx,'lead','write',{'verdict':'pass','summary':'not assigned'})
    def test_end_to_end_review_and_persistence(self):
        r=self.finish(self.start());self.assertEqual((r['status'],r['productVerdict']),('done','pass'))
        self.assertEqual(len(r['reviews']),1);self.assertTrue(r['calls']);self.assertTrue(self.p.team('software')['memory'].list('dev'))
        snapshot=self.p.snapshot('software');self.assertNotIn('offline-test-key',json.dumps(snapshot))
        self.assertEqual(snapshot['evaluation']['productPassRate'],1)
    def test_failure_is_retained_as_owner_scoped_experience(self):
        self.flags['fail']=True;r=self.finish(self.start())
        self.assertEqual(r['steps'][0]['status'],'failed');self.assertEqual(r['steps'][1]['status'],'blocked')
        memories=self.p.team('software')['memory'].list('dev')
        episode=next(m for m in memories if m['kind']=='episodic')
        self.assertFalse(episode['success']);self.assertIn('Source retrieval failed',episode['reason'])
        self.assertEqual(episode['channelId'],'group');self.assertEqual(episode['sourceRunId'],r['id'])
    def test_real_human_clarification_and_cancel(self):
        self.flags['ask']=True;r=self.start();snap=wait_for(lambda:(s if (s:=self.p.snapshot('software'))['run']['status']=='waiting_human' else None))
        p=snap['run']['pending'][0];self.p.answer('software',r['id'],p['id'],'Actual human title')
        self.assertEqual(self.finish(r)['status'],'done')
        r=self.start();wait_for(lambda:self.p.snapshot('software')['run']['status']=='waiting_human');self.p.control('software',r['id'],'cancel');self.assertEqual(self.finish(r)['status'],'cancelled')
    def test_approval_hash_rejects_stale_write(self):
        self.flags['overwrite']=True;r=self.start();snap=wait_for(lambda:(s if (s:=self.p.snapshot('software'))['run']['pending'] and s['run']['pending'][-1]['status']=='pending' else None))
        pending=snap['run']['pending'][0];self.assertEqual(pending['kind'],'approval');self.assertIn('expectedSha256',pending['args']['arguments'])
        ctx=self.p.contexts[r['id']];base=ctx['gateway'].workspace('dev',r['id']);write_text(base,'checklist.md','external concurrent change',overwrite=True)
        self.p.answer('software',r['id'],pending['id'],True);done=self.finish(r);self.assertNotEqual(done['status'],'done');self.assertEqual((base/'checklist.md').read_text(),'external concurrent change')
    def test_independent_snapshot_is_immutable_and_imports_read_only(self):
        r=self.finish(self.start());ctx=self.p.contexts[r['id']];a=r['artifacts'][0]
        write_text(ctx['gateway'].workspace('dev',r['id']),'checklist.md','private later change',overwrite=True)
        self.assertIn('Verify actual evidence',self.p.artifact('software',r['id'],a['id'])['content'])
        with self.assertRaises(ToolError):ctx['gateway'].execute('qa',r['id'],'file.write',{'path':'imports/'+a['id']+'/'+a['name'],'content':'tamper'})
        with self.assertRaises(ToolError):ctx['gateway'].execute('lead',r['id'],'file.read',{'path':'imports/'+a['id']+'/'+a['name']})
    def test_superseded_review_cannot_accept_new_revision(self):
        self.flags['supersede']=True;r=self.finish(self.start());self.assertEqual(r['status'],'partial');self.assertEqual(r['productVerdict'],'unassessed')
    def test_missing_independent_reviewer_does_not_block_delivery(self):
        self.flags['author_only']=True;r=self.finish(self.start())
        self.assertEqual((r['status'],r['productVerdict']),('done','unassessed'))
        self.assertTrue(r['artifacts']);self.assertFalse(r['reviews'])
        self.assertFalse(any(s.get('acceptance') for s in r['steps']))
    def test_private_channel_does_not_leak_to_group_or_memory(self):
        r=self.finish(self.start(channel='dev',mode='chat'));self.assertIn('PRIVATE_REPLY_246',json.dumps(self.p.snapshot('software','dev')))
        self.assertNotIn('PRIVATE_REPLY_246',json.dumps(self.p.snapshot('software','group')['messages']))
        self.assertFalse(self.p.team('software')['memory'].recall('dev','PRIVATE_REPLY_246','group'))
    def test_candidates_can_be_tried_without_becoming_verified(self):
        self.flags['candidate']=True;self.finish(self.start());self.flags['candidate']=False;self.flags['use']=True;r=self.finish(self.start())
        self.assertEqual(r['status'],'done');m=next(m for m in self.p.team('software')['memory'].list('dev') if m['kind']=='procedural')
        self.assertEqual(m['status'],'candidate');self.assertFalse(m['verified']);self.assertTrue(m.get('candidateTrials'))
    def test_subset_roster_can_load_existing_artifact_index(self):
        self.finish(self.start());r=self.p.start('software','dev','Only private reply',mode='chat',seconds=30);self.assertEqual(self.finish(r)['status'],'done')
    def test_code_pass_needs_real_current_test_receipt(self):
        r=self.finish(self.start());ctx=self.p.contexts[r['id']];producer=r['steps'][0];reviewer=r['steps'][1]
        self.p.tool(ctx,producer,'file.write',{'path':'app.html','content':'<h1>App</h1>'});self.p.tool(ctx,producer,'artifact.publish',{'paths':['app.html'],'to':['qa']})
        a=ctx['run']['artifacts'][-1];self.p.tool(ctx,reviewer,'file.read',{'path':'imports/'+a['id']+'/'+a['name']})
        with self.assertRaisesRegex(ValueError,'test evidence'):self.p.tool(ctx,reviewer,'review.submit',{'targetTaskId':'write','verdict':'pass','summary':'Unexecuted prose test must fail'})
    def test_code_pass_accepts_matching_producer_python_test(self):
        r=self.finish(self.start());ctx=self.p.contexts[r['id']];producer=r['steps'][0];reviewer=r['steps'][1]
        wrote=self.p.tool(ctx,producer,'file.write',{'path':'calc.py','content':'print(42)\n'})
        ctx['run']['calls'].append({'agentId':producer['agentId'],'tool':'code.test','status':'success','args':{'path':'calc.py'},'result':{'exitCode':0,'sha256':wrote['sha256']}})
        self.p.tool(ctx,producer,'artifact.publish',{'paths':['calc.py'],'to':['qa']})
        a=ctx['run']['artifacts'][-1]
        self.p.tool(ctx,reviewer,'file.read',{'path':'imports/'+a['id']+'/'+a['name']})
        review=self.p.tool(ctx,reviewer,'review.submit',{'targetTaskId':'write','verdict':'pass','summary':'Read current artifact; prior successful code.test matches published hash.'})
        self.assertTrue(review.get('verdict')=='pass' or ctx['run']['reviews'][-1]['verdict']=='pass')
    def test_local_git_commit_is_real(self):
        r=self.finish(self.start());ctx=self.p.contexts[r['id']];s={'id':'git','agentId':'release'}
        self.p.tool(ctx,s,'file.write',{'path':'release-note.md','content':'Actual local commit\n'})
        result=self.p.tool(ctx,s,'git.commit',{'files':['release-note.md'],'message':'Verify isolated local commit'})
        self.assertTrue(result['ok']);self.assertRegex(result['pushTarget']['commit'],r'^[a-f0-9]{40}$')
    def test_pause_is_observed_at_a_safe_boundary(self):
        self.flags['ask']=True;r=self.start();s=wait_for(lambda:(s if (s:=self.p.snapshot('software'))['run']['status']=='waiting_human' else None));self.p.control('software',r['id'],'pause')
        wait_for(lambda:self.p.snapshot('software')['run']['status']=='paused');self.p.answer('software',r['id'],s['run']['pending'][0]['id'],'Actual reply while paused')
        self.p.control('software',r['id'],'resume');self.assertEqual(self.finish(r)['status'],'done')
    def test_agent_can_dynamically_delegate(self):
        original=self.p.executor
        def executor(p,ctx,s,d,e,tools):
            if s['id']=='planning':return json.dumps({'tasks':[{'id':'manager','agentId':'lead','title':'Coordinate','instructions':'Delegate appropriate work','dependsOn':[]}]})
            if s['id']=='manager':
                result=p.tool(ctx,s,'collab.delegate',{'agentId':'dev','title':'Specialist action','instructions':'Execute delegated work'})
                self.assertEqual(result['status'],'done');return 'Delegated and collected a specialist result'
            return 'Actual delegated specialist response'
        self.p.executor=executor;r=self.finish(self.start());self.assertEqual(r['status'],'done');self.assertEqual(len(r['steps']),2);self.assertEqual(r['steps'][1]['parentId'],'manager')
    def test_bundle_preserves_relative_runtime_layout(self):
        import io,zipfile
        r=self.finish(self.start());ctx=self.p.contexts[r['id']];s={'id':'write','agentId':'dev','revision':0}
        self.p.tool(ctx,s,'file.write',{'path':'app/index.html','content':'<script src="app.js"></script>'});self.p.tool(ctx,s,'file.write',{'path':'app/app.js','content':'console.log("runtime")'})
        self.p.tool(ctx,s,'artifact.publish',{'paths':['app/index.html','app/app.js'],'to':['qa']})
        with zipfile.ZipFile(io.BytesIO(self.p.bundle('software',r['id']))) as z:self.assertIn('dev/app/index.html',z.namelist());self.assertIn('dev/app/app.js',z.namelist())

class SuiteBudgetTests(unittest.TestCase):
    def test_each_task_keeps_the_full_limit_and_overtime_does_not_cancel(self):
        from live.suite import SuiteManager
        temp=tempfile.TemporaryDirectory();root=Path(temp.name)
        class Recorder:
            def __init__(self):
                self.root=root;self.starts=[];self.cancelled=[];self.config=type('C',(),{'scrub':staticmethod(lambda text:text)})()
            def create_team(self,name,template):return {'id':'team-'+template}
            def update_agent(self,*args,**kwargs):pass
            def start(self,team,channel,prompt,participants=None,mode='task',memory=True,seconds=900,enforce_deadline=True):
                self.starts.append({'seconds':seconds,'enforce':enforce_deadline})
                return {'id':'run-%d'%len(self.starts)}
            def team(self,team):
                done={'status':'done','productVerdict':'unassessed','elapsedMs':10,'steps':[{'status':'done'}],'cancelRequested':False}
                return {'repo':type('R',(),{'get_run':staticmethod(lambda rid:done)})()}
            def control(self,*args):self.cancelled.append(args)
        recorder=Recorder();suite=SuiteManager(recorder,ROOT/'fixtures'/'mid_test')
        suite.start(3600)
        wait_for(lambda:suite.state['status']=='finished',seconds=8)
        self.assertEqual(len(recorder.starts),6)
        self.assertTrue(all(item=={'seconds':3600,'enforce':False} for item in recorder.starts))
        self.assertEqual(recorder.cancelled,[])
        self.assertTrue(all(case['status']=='done' for case in suite.state['cases']))
        temp.cleanup()

    def test_suite_auto_answers_visible_hitl_requests_with_defaults(self):
        from live.suite import SuiteManager
        temp=tempfile.TemporaryDirectory();root=Path(temp.name)
        class Recorder:
            def __init__(self):
                self.root=root;self.runs={};self.answers=[];self.config=type('C',(),{'scrub':staticmethod(lambda text:text)})()
            def create_team(self,name,template):return {'id':'team-'+template}
            def update_agent(self,*args,**kwargs):pass
            def start(self,team,channel,prompt,participants=None,mode='task',memory=True,seconds=900,enforce_deadline=True):
                rid='run-%d'%(len(self.runs)+1);kind='approval' if len(self.runs)%2==0 else 'clarification'
                self.runs[rid]={'status':'waiting_human','productVerdict':'unassessed','elapsedMs':10,
                    'steps':[{'status':'running'}],'cancelRequested':False,'pending':[{'id':'q-'+rid,'kind':kind,
                    'choices':[] if kind=='approval' else ['Use the first option','Use another option'],
                    'status':'pending','createdAt':time.time()}]}
                return {'id':rid}
            def team(self,team):
                runs=self.runs
                return {'repo':type('R',(),{'get_run':staticmethod(lambda rid:runs[rid])})()}
            def answer(self,team,rid,qid,answer,source='human'):
                run=self.runs[rid];pending=run['pending'][0];pending.update(status='answered',answer=answer,answeredBy=source)
                run.update(status='done',steps=[{'status':'done'}]);self.answers.append((answer,source))
            def control(self,*args):pass
        recorder=Recorder();suite=SuiteManager(recorder,ROOT/'fixtures'/'mid_test');suite.auto_answer_delay=0
        suite.start(60,False,True)
        wait_for(lambda:suite.state['status']=='finished',seconds=8)
        self.assertEqual(recorder.answers,[(True,'suite_auto'),('Use the first option','suite_auto')]*3)
        self.assertTrue(suite.state['autoHuman'])
        self.assertIn('remain visible',suite.state['protocol'])
        temp.cleanup()
if __name__=='__main__':unittest.main()
