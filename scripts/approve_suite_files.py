"""Operator-only approval assistant for exact private-file writes in one pinned suite.

This does not answer clarifications, approve deletion/push, or change app policy.
Every approval is submitted through the normal HITL API and logged separately.
"""
import argparse,hashlib,json,subprocess,sys,time
from pathlib import Path
import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'vendor'))
from backend.sandbox import confined_path,read_text

parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--target',type=Path);args=parser.parse_args()
expected=json.loads((args.target or args.output/'suite.json').read_text()).get('id')
session=requests.Session();session.trust_env=False
base='http://127.0.0.1:8100';seen=set()
while True:
    if args.target:current=json.loads(args.target.read_text())
    else:
        response=session.get(base+'/api/suite',timeout=20);response.raise_for_status();suite=response.json()
        if suite['id']!=expected:raise RuntimeError('Suite changed; stopping approval assistant')
        if suite['status']!='running':break
        current=suite.get('current')
    if current:
        response=session.get(base+'/api/export',params={'team':current['teamId'],'run':current['runId']},timeout=20);response.raise_for_status();run=response.json()
        if args.target and run['status'] not in ('queued','planning','running','waiting_human','pausing','paused','cancelling'):break
        for pending in run['pending']:
            if pending['status']!='pending' or pending['kind']!='approval' or pending['id'] in seen:continue
            operation=pending['args'].get('operation');bound=pending['args'].get('arguments',{})
            if operation not in ('file.write','file.patch'):continue
            workspace=ROOT/'.platform/teams'/current['teamId']/'tools/workspaces'/pending['agentId']/run['id']
            try:
                path=bound['path']
                if path.startswith('imports/'):raise ValueError('Imported snapshots are read-only')
                confined_path(workspace,path)
                raw=read_text(workspace,path)
                actual=hashlib.sha256(raw.encode('utf-8')).hexdigest()
                if actual!=bound.get('expectedSha256'):raise ValueError('Expected hash does not match current private file')
            except Exception as error:
                seen.add(pending['id']);print(json.dumps({'notApproved':pending['id'],'reason':str(error)}),flush=True);continue
            decision={'answer':True,'assumptions':[],
                      'reason':'Standing operator authorization for necessary reversible changes to this agent-owned isolated task file. Exact arguments and current expected SHA256 verified by the operator helper. Permission is not a semantic correctness verdict. No clarification, deletion, imported-file write or remote push is authorized by this helper.'}
            command=[sys.executable,str(ROOT/'scripts/answer_live_question.py'),'--output',str(args.output),'--question',pending['id']]
            if args.target:command+=['--target',str(args.target)]
            completed=subprocess.run(command,input=json.dumps(decision),text=True,capture_output=True,timeout=30)
            print(json.dumps({'privateFileApproval':pending['id'],'operation':operation,'path':path,'exitCode':completed.returncode,'receipt':completed.stdout.strip(),'error':completed.stderr[-500:]},ensure_ascii=False),flush=True)
            seen.add(pending['id'])
    time.sleep(2)
