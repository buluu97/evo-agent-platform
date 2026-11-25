"""Collect actual model-created products and observed execution-window timings."""
from pathlib import Path
import json
import shutil
import hashlib
import zipfile

ROOT=Path(__file__).resolve().parent
RUN=ROOT/'runs/delivery-01'
DEST=ROOT/'deliverables'
DEST.mkdir(exist_ok=True)
reports={t:json.loads((RUN/t/'report.json').read_text()) for t in ('software','newsroom','fund')}
assert all(c['status']=='crew_returned' for r in reports.values() for c in r['cases'])
work=lambda t,a: RUN/t/'runtime/workspaces'/a/reports[t]['runId']
files={
 'software-1':[(work('software','release')/'handoff/dev/9af47ee2/app'/n,n) for n in ('index.html','app.js','styles.css','README.md')],
 'software-2':[(work('software','release')/'handoff/dev/2f2a1f14/app'/n,n) for n in ('index.html','app.js','styles.css','README.md')],
 'newsroom-1':[(work('newsroom','editor')/n,n) for n in ('article_hkust_hsbc_ai_institute.md','newsletter_hkust_hsbc_ai_institute.md','social_hkust_hsbc_ai_institute.md')],
 'newsroom-2':[(work('newsroom','editor')/n,n) for n in ('article_ai_writing_detectors.md','newsletter_ai_writing_detectors.md','social_ai_writing_detectors.md')],
 'fund-1':[(work('fund','marketing')/'marketing/proposal.md','proposal.md'),(work('fund','marketing')/'marketing/slides.html','investment_intro.html')],
 'fund-2':[(work('fund','marketing')/'marketing/semiconductor_outlook_2026.html','semiconductor_outlook_2026.html')],
}
all_windows=[];case_windows={};all_events=[];per_team=[]
for team in reports:
 ledger=RUN/team/'events.jsonl'
 events=[json.loads(l) for l in ledger.read_text().splitlines()]
 all_events+=events
 segments=[]
 for e in events:
  if e['kind']=='team_start':segments.append([])
  if segments:segments[-1].append(e)
 # Each segment is one actual worker lifetime. Gaps between worker lifetimes
 # are not counted as active execution. Interrupted silent tails may be absent.
 base=ledger.stat().st_birthtime
 for segment in segments:
  first,last=segment[0]['elapsedSeconds'],segment[-1]['elapsedSeconds']
  all_windows.append((base+first,base+last))
  active=None;began=None
  for e in segment:
   if e['kind']=='case_start': active=e['caseId'];began=e['elapsedSeconds']
   if e['kind']=='case_end' and active:
    case_windows.setdefault(active,[]).append((began,e['elapsedSeconds']));active=None
  if active:case_windows.setdefault(active,[]).append((began,last))
 per_team.append({'team':team,'workerSegments':[{'fromSeconds':s[0]['elapsedSeconds'],'toSeconds':s[-1]['elapsedSeconds'],'endEvent':s[-1]['kind']} for s in segments]})

def merged(intervals):
 result=[]
 for a,b in sorted(intervals):
  if result and a<=result[-1][1]:result[-1]=(result[-1][0],max(result[-1][1],b))
  else:result.append((a,b))
 return result
windows=merged(all_windows)
active=sum(b-a for a,b in windows)
wall=max(b for a,b in windows)-min(a for a,b in windows)
rows=[];manifest=[]
for team,r in reports.items():
 for c in r['cases']:
  case=c['caseId'];folder=DEST/case;folder.mkdir(exist_ok=True)
  selected=[]
  for source,name in files[case]:
   assert source.is_file() and not source.is_symlink(),source
   target=folder/name;shutil.copyfile(source,target)
   entry={'caseId':case,'source':str(source),'file':str(target),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
   selected.append(entry);manifest.append(entry)
  (folder/'agent-delivery-report.md').write_text(c['output'],encoding='utf-8')
  own=[e for e in all_events if e.get('caseId')==case]
  rows.append({'caseId':case,'workflowStatus':c['status'],'observedWorkerSeconds':round(sum(b-a for a,b in case_windows.get(case,[])),3),
   'modelRequests':sum(e['kind']=='model_start' for e in own),'apiAttempts':sum(e['kind']=='api_attempt' for e in own),
   'toolReceipts':sum(e['kind']=='tool_result' for e in own),'stageErrors':sum(e['kind']=='stage_attempt_error' for e in own),
   'files':selected,'officialAcceptance':'unassessed'})

checks={}
for p in (RUN/'independent-checks').glob('*.json'):
 r=json.loads(p.read_text());checks[p.stem]={'exitCode':r.get('exitCode'),'assertions':r.get('browser',{}).get('assertions',[]),'testedFiles':r.get('browser',{}).get('testedFiles',[])}
assert checks['software-2-date-range']['exitCode']==0 and checks['fund-2-slide-navigation']['exitCode']==0
result={'framework':'CrewAI 1.15.44','architecture':'Four fixed role stages via sequential Crews, host checkpoints/retries/publication and logged terminal artifact-action recovery; no CrewAI Flow.',
 'requestedModel':'t2_hkgai-v3_fp8_1m_e7','returnedModel':'t2_hkgai-v3_fp8_1m_e10',
 'timing':{'observedActiveWindowUnionSeconds':round(active,3),'wallSpanSeconds':round(wall,3),'outsideObservedWindowsSeconds':round(wall-active,3),
  'method':'Union of worker team_start-to-last-event windows. Includes model/tool waiting and retries inside those windows; excludes gaps between workers. Interrupted silent tails may be undercounted. Per-case values sum observed case windows across attempts.',
  'continuousBenchmark':False,'note':'Adaptive debugging/rework run with pauses and changing harness. Not a frozen-version performance comparison and not evidence of a 15-minute six-case pass.'},
 'cases':rows,'independentBrowserChecks':checks,'workerSegments':per_team,
 'qualityLimits':['Software is an operator-assumed single-browser localStorage pilot, not a shared multi-user service.','News products contain source and uncertainty notes; publication-channel link placeholders remain.','Fund proposal assumptions and unverified tax data are disclosed; semiconductor deck relies on a limited secondary-source base.','Six generated products and limited functional checks are not official independent acceptance of all six products.']}
(ROOT/'delivery-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
(DEST/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
(DEST/'README.md').write_text('# CrewAI Mid-Test outputs\n\nSix original requests were attempted with the course API. Software HTML products and fund HTML slides open locally with no build step. News and proposal files are Markdown. Software is a single-browser localStorage pilot.\n\nFiles are actual model-created bytes collected by the host; no business content was generated during collection. Agent reports retain limitations. See delivery-results.json for timing, failed attempts, source limits and limited functional verification. This was an adaptive debugging run, not a continuous frozen-version benchmark or an official six-product acceptance.\n',encoding='utf-8')
shutil.copyfile(ROOT/'delivery-results.json',DEST/'delivery-results.json')
shutil.copytree(RUN/'independent-checks',DEST/'independent-checks',dirs_exist_ok=True)
with zipfile.ZipFile(ROOT/'crewai-midtest-deliverables.zip','w',zipfile.ZIP_DEFLATED) as z:
 for p in DEST.rglob('*'):
  if p.is_file():z.write(p,p.relative_to(DEST))
print(json.dumps({'timing':result['timing'],'cases':[{k:c[k] for k in ('caseId','observedWorkerSeconds','modelRequests','apiAttempts')} for c in rows]},ensure_ascii=False,indent=2))
