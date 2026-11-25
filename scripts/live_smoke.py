"""Explicit operator-authored real-model smoke through the HTTP application."""
import argparse,json,time
from pathlib import Path
import requests
p=argparse.ArgumentParser();p.add_argument('--url',default='http://127.0.0.1:8100');p.add_argument('--output',type=Path);a=p.parse_args()
s=requests.Session();s.trust_env=False;s.headers['X-Crew-Client']='1'
def post(path,data):
 r=s.post(a.url+path,json=data,timeout=10);r.raise_for_status();return r.json()
prompt='连通性验收任务：请团队自主拆解并执行。让开发者用 Markdown 写三条检查清单到 checklist.md，内容自选，不需要额外澄清；实际发布给另一位有审核能力的成员，审核者必须独立读取当前文件并用 review.submit 提交结论。只需要这个小文件，不执行代码、不操作 Git。不要把工具提案或文字自述当作工具已执行。'
started=time.time();run=post('/api/start',{'team':'software','channel':'group','prompt':prompt,'participants':['lead','dev','qa'],'seconds':180,'memory':True})
while time.time()-started<200:
 r=s.get(a.url+'/api/state',params={'team':'software'},timeout=10);r.raise_for_status();state=r.json();item=state['run']
 if item['id']!=run['runId']:raise RuntimeError('Run changed during smoke')
 if item['status'] not in ('queued','planning','running','waiting_human','pausing','paused','cancelling'):
  out={'runId':item['id'],'status':item['status'],'productVerdict':item['productVerdict'],'modelCalls':item['modelCalls'],'artifacts':item['artifacts'],'reviews':item['reviews'],'elapsedSeconds':round(time.time()-started,3),'error':item.get('error')}
  folder=Path(__file__).resolve().parents[1]/'evidence';folder.mkdir(exist_ok=True);(a.output or folder/'live-deepseek-smoke.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False));break
 pending=[x for x in item['pending'] if x['status']=='pending']
 if pending:
  print(json.dumps({'waitingForRealHuman':pending},ensure_ascii=False),flush=True)
  # Do not synthesize replies; the workbench exposes these actual requests.
 time.sleep(.5)
else:raise SystemExit('Smoke observation window expired; inspect actual run in UI')
