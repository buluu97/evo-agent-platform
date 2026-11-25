"""Observe the real live-suite API; human answers stay explicit and separately logged."""
import argparse, hashlib, importlib.metadata, json, time
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]

def write(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=3600)
    parser.add_argument('--url', default='http://127.0.0.1:8100')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    session = requests.Session(); session.trust_env = False
    def get(path, **params):
        response = session.get(args.url + path, params=params, timeout=20)
        response.raise_for_status(); return response.json()
    health = get('/api/health')
    previous = get('/api/suite')
    if previous.get('status') == 'running':
        raise RuntimeError('An existing suite is running; refusing to replace it')
    paths = [ROOT/'server.py', ROOT/'deepseek_protocol.py', ROOT/'context_budget.py']
    for folder in ('live', 'vendor/backend', 'fixtures/mid_test'):
        paths += [p for p in (ROOT/folder).rglob('*') if p.suffix in ('.py', '.md', '.cjs')]
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
    write(args.output/'manifest.json', {'health':health,'sourceHashes':hashes,'crewai':importlib.metadata.version('crewai'),
          'protocol':'Original six cases in sequential scenario order; fresh teams; memory on; assistant acts as explicitly authorized simulated user; every actual answer recorded separately',
          'suiteWallBudgetSeconds':args.seconds,'perCaseWallCapSeconds':1800,'observerStartedAt':time.time()})
    response = session.post(args.url+'/api/suite/start',headers={'X-Crew-Client':'1'},json={'seconds':args.seconds,'memory':True},timeout=30)
    response.raise_for_status(); suite = response.json(); suite_id=suite['id']
    write(args.output/'suite.json',suite)
    print(json.dumps({'event':'started','suite':suite_id,'output':str(args.output)},ensure_ascii=False),flush=True)
    last=''; shown=set(); terminal=set()
    while True:
        suite=get('/api/suite')
        if suite.get('id')!=suite_id: raise RuntimeError('Suite was replaced externally; refusing to mix measurements')
        write(args.output/'suite.json',suite)
        current=suite.get('current')
        if current:
            run=get('/api/export',team=current['teamId'],run=current['runId'])
            write(args.output/(run['id']+'.json'),run)
            summary={'event':'progress','suite':suite_id,'case':next((c['id'] for c in suite['cases'] if c.get('runId')==run['id']),None),
                     'status':run['status'],'elapsedSeconds':round((run.get('finishedAt') or time.time())-run['startedAt'],1),
                     'modelCalls':run['modelCalls'],'steps':[{k:s.get(k) for k in ('id','agentId','status','title')} for s in run['steps']],
                     'artifacts':len([a for a in run['artifacts'] if not a.get('superseded')]),'productVerdict':run['productVerdict']}
            key=json.dumps([run['id'],run['status'],run['modelCalls'],[(s['id'],s['status']) for s in run['steps']]])
            if key!=last: print(json.dumps(summary,ensure_ascii=False),flush=True);last=key
            for question in run['pending']:
                if question['status']=='pending' and question['id'] not in shown:
                    shown.add(question['id'])
                    print(json.dumps({'event':'human_required','team':current['teamId'],'run':run['id'],'question':question},ensure_ascii=False),flush=True)
        for case in suite['cases']:
            rid=case.get('runId')
            if not rid or case['status']=='running' or rid in terminal: continue
            run=get('/api/export',team=case['teamId'],run=rid)
            write(args.output/(rid+'.json'),run)
            product=session.get(args.url+'/api/bundle',params={'team':case['teamId'],'run':rid},timeout=30)
            if product.ok: (args.output/(case['id']+'-products.zip')).write_bytes(product.content)
            terminal.add(rid)
            print(json.dumps({'event':'case_finished',**{k:case.get(k) for k in ('id','status','elapsedMs','productVerdict','executionComplete')}},ensure_ascii=False),flush=True)
        if suite['status']!='running':
            changed=[str(p.relative_to(ROOT)) for p in paths if hashlib.sha256(p.read_bytes()).hexdigest()!=hashes[str(p.relative_to(ROOT))]]
            write(args.output/'integrity.json',{'changedSourceFiles':changed,'observerFinishedAt':time.time()})
            print(json.dumps({'event':'finished','suite':suite_id,'status':suite['status'],'elapsedMs':suite.get('elapsedMs'),'sourceChanged':changed},ensure_ascii=False),flush=True)
            break
        time.sleep(5)

if __name__=='__main__':main()
