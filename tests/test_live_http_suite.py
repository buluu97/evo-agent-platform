import json,sys,threading,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'vendor'))
import requests
from http.server import ThreadingHTTPServer
from server import handler
from live.platform import Platform
from live.suite import SuiteManager
from benchmarks.mid_test_live import load_suite
import test_live_platform as base
from test_live_platform import wait_for

class IntegrationTests(unittest.TestCase):
    setUp=base.RuntimeTests.setUp
    tearDown=base.RuntimeTests.tearDown
    execute=base.RuntimeTests.execute
    start=base.RuntimeTests.start
    finish=base.RuntimeTests.finish
    def test_http_origin_header_and_filesystem_boundary(self):
        suite=SuiteManager(self.p,ROOT/'fixtures/mid_test');server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.p,suite));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}';s=requests.Session();s.trust_env=False
        try:
            self.assertEqual(s.get(base+'/api/health').status_code,200)
            self.assertEqual(s.get(base+'/api/state',headers={'Host':'attacker.example'}).status_code,403)
            self.assertEqual(s.post(base+'/api/start',json={'prompt':'test'}).status_code,403)
            self.assertEqual(s.post(base+'/api/start',headers={'X-Crew-Client':'1','Origin':'https://attacker.example'},json={'prompt':'test'}).status_code,403)
            self.assertEqual(s.get(base+'/config-deepseek/.env').status_code,404)
            self.assertNotIn('offline-test-key',s.get(base+'/api/state').text)
            r=s.post(base+'/api/start',headers={'X-Crew-Client':'1'},json={'prompt':'checklist evidence; independent review'});self.assertEqual(r.status_code,200)
            done=self.finish({'id':r.json()['runId']});self.assertEqual(done['productVerdict'],'pass')
        finally:server.shutdown();server.server_close();s.close()
    def test_original_suite_order_and_pair_continuity(self):
        observed=[]
        def execute(p,ctx,step,description,expected,tools):
            if step['id']=='planning':return json.dumps({'tasks':[{'id':'main','agentId':next(iter(ctx['agents'])),'title':'Recorded task','instructions':'Record protocol order','dependsOn':[]}]})
            observed.append((ctx['team']['meta']['template'],ctx['run']['title'],ctx['run']['previousRunId']))
            return 'Protocol test result; no model or product quality claim'
        self.p.executor=execute;suite=SuiteManager(self.p,ROOT/'fixtures/mid_test');suite.start(60,False)
        wait_for(lambda:suite.state.get('finishedAt'),12)
        expected=[(g['team'],t['text']) for g in load_suite(ROOT/'fixtures/mid_test') for t in g['tasks']]
        self.assertEqual([(t,p) for t,p,previous in observed],expected)
        self.assertEqual([bool(previous) for _,_,previous in observed],[False,True,False,True,False,True])
        self.assertEqual(suite.state['executionCompletionRate'],1);self.assertEqual(suite.state['productPassRate'],0)
        self.assertTrue((self.p.root/'suites'/(suite.state['id']+'.json')).exists())
    def test_restart_reconciliation_does_not_replay_external_effect(self):
        r=self.finish(self.start());r['status']='running';r['calls'].append({'id':'uncertain-push','tool':'git.push','status':'running','agentId':'release','taskId':'write'})
        self.p.team('software')['repo'].save_run(r,'group')
        other=Platform(self.p.root,self.p.config_root,executor=self.execute)
        try:
            state=other.snapshot('software')['run'];self.assertEqual(state['status'],'interrupted');self.assertEqual(state['uncertainCalls'],['uncertain-push'])
            with self.assertRaisesRegex(ValueError,'Reconcile'):other.control('software',r['id'],'resume')
            other.control('software',r['id'],'reconcile','Operator inspected remote and local commit; no retry requested')
            item=other.team('software')['repo'].get_run(r['id']);self.assertEqual(item['calls'][-1]['status'],'reconciled')
            other.control('software',r['id'],'cancel')
            item=other.team('software')['repo'].get_run(r['id']);self.assertEqual(item['status'],'cancelled');self.assertGreaterEqual(item['elapsedMs'],0)
            self.assertEqual(item['calls'][-1]['result']['machineVerified'],False)
        finally:
            for t in other.teams.values():t['repo'].close()
if __name__=='__main__':unittest.main()
