"""Submit one explicitly reviewed simulated-user answer and keep its full receipt."""
import argparse,json,sys,time
from pathlib import Path
import requests

parser=argparse.ArgumentParser()
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--question',required=True)
parser.add_argument('--target',type=Path)
args=parser.parse_args()
decision=json.load(sys.stdin)
session=requests.Session();session.trust_env=False
base='http://127.0.0.1:8100'
if args.target:
    target=json.loads(args.target.read_text())
    suite={'id':target['runId'],'current':target,'cases':[{'id':target['caseId'],'runId':target['runId']}]}
else:
    suite=session.get(base+'/api/suite',timeout=20).json()
    expected=json.loads((args.output/'suite.json').read_text())
    assert suite['id']==expected['id'],'Refusing to answer a different suite'
current=suite['current']
run=session.get(base+'/api/export',params={'team':current['teamId'],'run':current['runId']},timeout=20).json()
pending=next(p for p in run['pending'] if p['id']==args.question and p['status']=='pending')
record={'suiteId':suite['id'],'teamId':current['teamId'],'runId':run['id'],'caseId':next(c['id'] for c in suite['cases'] if c.get('runId')==run['id']),
        'question':pending,'answer':decision['answer'],'assumptions':decision.get('assumptions',[]),'reason':decision['reason'],
        'answeredBy':'Codex acting as the user-authorized simulated requester','preparedAt':time.time()}
with (args.output/'human-decisions.jsonl').open('a') as file:file.write(json.dumps({'state':'prepared',**record},ensure_ascii=False)+'\n')
response=session.post(base+'/api/answer',headers={'X-Crew-Client':'1'},json={'team':current['teamId'],'run':run['id'],'question':pending['id'],'answer':decision['answer']},timeout=20)
record.update(submittedAt=time.time(),httpStatus=response.status_code,response=response.json())
with (args.output/'human-decisions.jsonl').open('a') as file:file.write(json.dumps({'state':'submitted',**record},ensure_ascii=False)+'\n')
response.raise_for_status()
print(json.dumps({'answered':pending['id'],'case':record['caseId'],'waitSeconds':round(record['submittedAt']-pending['createdAt'],2)},ensure_ascii=False))
