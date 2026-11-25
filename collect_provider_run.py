"""Export verified published bytes and real timing without treating return as acceptance."""
import argparse,json,hashlib,shutil,zipfile
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('run',type=Path);a=p.parse_args();root=Path(__file__).resolve().parent;run=a.run.resolve()
out=root/(run.name+'-deliverables');out.mkdir(exist_ok=True)
summary=json.loads((run/'summary.json').read_text());cases=[]
for team in ('software','newsroom','fund'):
 report=json.loads((run/team/'report.json').read_text());rid=report['runId'];events=[json.loads(l) for l in (run/team/'events.jsonl').read_text().splitlines()]
 for c in report['cases']:
  cid=c['caseId'];folder=out/cid;folder.mkdir(exist_ok=True);own=[x for x in events if x.get('caseId')==cid]
  pubs={}
  for e in own:
   if e['kind']!='tool_result' or e.get('operation')!='publish' or not e.get('result',{}).get('ok'):continue
   for original in e.get('args',{}).get('paths',[]):
    for r in e['result'].get('receipts',[]):
     if not r['path'].endswith('/'+original):continue
     source=run/team/'runtime/workspaces'/r['receiver']/rid/r['path']
     if source.is_file() and not source.is_symlink() and hashlib.sha256(source.read_bytes()).hexdigest()==r['sha256']:
      pubs[(e['agent'],original)]=(source,r['sha256']);break
  files=[]
  for (owner,path),(source,digest) in pubs.items():
   target=folder/'published'/owner/path;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
   files.append({'file':str(target.relative_to(out)),'sha256':digest,'source':str(source)})
  written=[];writes={}
  for e in own:
   if e['kind']=='tool_result' and e.get('operation') in ('file.write','file.patch') and e.get('result',{}).get('ok'):
    writes[(e['agent'],e['args']['path'])]=e['result'].get('sha256')
  for (owner,path),digest in writes.items():
   source=run/team/'runtime/workspaces'/owner/rid/path
   if not digest or not source.is_file() or source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest()!=digest:continue
   if any(f['sha256']==digest for f in files):continue
   target=folder/'written-not-published'/owner/path;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
   written.append({'file':str(target.relative_to(out)),'sha256':digest,'source':str(source),'status':'actual successful file-write receipt; not a publication or acceptance claim'})
  tests=[e for e in own if e['kind']=='tool_result' and e.get('operation')=='code.test' and e.get('args',{}).get('language')=='browser']
  passed=[e for e in tests if e.get('result',{}).get('exitCode')==0]
  if passed:
   e=passed[-1];base=run/team/'runtime/workspaces'/e['agent']/rid;target=folder/'runnable';target.mkdir(exist_ok=True)
   mappings={Path(e['args']['path']).name:e['args']['path'],**e['args'].get('resources',{})}
   expected={x['path']:x['sha256'] for x in e['result'].get('browser',{}).get('testedFiles',[])}
   for url,source_path in mappings.items():
    source=base/source_path
    if source_path not in expected or hashlib.sha256(source.read_bytes()).hexdigest()!=expected[source_path]:raise ValueError('Tested source bytes changed')
    dst=target/url
    if '..' in Path(url).parts or Path(url).is_absolute():raise ValueError('Unsafe runtime URL')
    dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,dst)
   (folder/'passed-browser-receipt.json').write_text(json.dumps(e['result'],ensure_ascii=False,indent=2))
  (folder/'agent-report.md').write_text(c.get('output',c.get('error','No final report')),encoding='utf-8')
  cases.append({'caseId':cid,'executionStatus':c['status'],'seconds':c.get('elapsedSeconds'),'stagesCompleted':len(c.get('stages',[])),
   'modelRequests':sum(e['kind']=='model_start' for e in own),'apiAttempts':sum(e['kind']=='api_attempt' for e in own),
   'apiErrors':sum(e['kind']=='api_attempt_error' for e in own),'publishedFiles':files,'writtenFiles':written,'browserTests':len(tests),'passedBrowserTests':len(passed),
   'officialQualityVerdict':'unassessed'})
result={'run':run.name,'provider':'deepseek','requestedModel':report['modelRequested'],'returnedModels':sorted({e.get('model') for t in ('software','newsroom','fund') for e in [json.loads(l) for l in (run/t/'events.jsonl').read_text().splitlines()] if e['kind']=='model_result' and e.get('model')}),
 'wallSeconds':summary['elapsedSeconds'],'teamConcurrency':3,'withinTeamTasks':'sequential','cases':cases,
 'limitations':'Stage return is not product acceptance. Published bytes and exact-byte browser receipts are exported. This fresh run excludes initial protocol diagnosis; no prior HKGAI products were imported.'}
if (run/'repair-start.json').is_file():
 first=json.loads((run/'measured-results-first-pass.json').read_text())
 initial_critical=max(sum(c['seconds'] for c in first['cases'] if c['caseId'].startswith(t+'-')) for t in ('software','newsroom','fund'))
 startup=summary['elapsedSeconds']-initial_critical
 last=max(e['elapsedSeconds'] for t in ('software','newsroom','fund') for e in [json.loads(l) for l in (run/t/'events.jsonl').read_text().splitlines()] if e['kind']=='case_end')
 result['firstPassWallSeconds']=summary['elapsedSeconds'];result['wallSeconds']=round(last+startup,3)
 result['followupNote']='First pass had QA failures. Later targeted original-team followup added evidence; elapsed includes the diagnosis/rework interval. This is not an unattended first-pass success.'
 result['qaCorrection']='Original software QA expected a future-day booking in a range ending today. Original failed-run and final passed-run testedFiles hashes are identical. Product refresh defect was not established; additional independent range tests verify exclusion/inclusion, cancellation and reload on those exact bytes.'
(run/'measured-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));(out/'measured-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
if (run/'independent-checks').is_dir():shutil.copytree(run/'independent-checks',out/'independent-checks',dirs_exist_ok=True)
(out/'README.md').write_text('# DeepSeek Mid-Test outputs\n\nStart with software-1/runnable/index.html and software-2/runnable/index.html: these are exact tested runtime bytes. News articles are under each newsroom task published/editor directory. The actual fund proposal is fund-1/written-not-published/marketing/investment_proposal.html; it has a file-write receipt, not a team publication receipt. The semiconductor slides are fund-2/published/marketing/marketing/semiconductor_outlook_2026.html.\n\nOther written-not-published files can be intermediate work and are not accepted releases. Agent reports retain limitations. Read measured-results.json for timing and QA correction: first pass 9m10s; with targeted team followup about 13m20s, not unattended first-pass acceptance. Independent-checks contains real browser receipts. No complete official six-product acceptance or causal provider speedup is claimed.\n',encoding='utf-8')
with zipfile.ZipFile(root/(out.name+'.zip'),'w',zipfile.ZIP_DEFLATED) as z:
 for f in out.rglob('*'):
  if f.is_file():z.write(f,f.relative_to(out))
print(json.dumps({'wallSeconds':result['wallSeconds'],'cases':[{k:c[k] for k in ('caseId','executionStatus','seconds','passedBrowserTests')} for c in cases]},ensure_ascii=False,indent=2))
