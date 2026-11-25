"""Summarize observed events without promoting framework completion to acceptance."""
import json
from pathlib import Path

root=Path(__file__).resolve().parent
rounds=[]
for folder in sorted((root/'runs').glob('pilot-*')):
    summary=folder/'summary.json'
    if not summary.is_file(): continue
    record=json.loads(summary.read_text())
    events=[]
    for team in ('software','newsroom','fund'):
        events.extend(json.loads(line) for line in (folder/team/'events.jsonl').read_text().splitlines())
    tools=[r for r in events if r['kind']=='tool_result']
    rows=[]
    for case in record['cases']:
        own=[e for e in events if e.get('caseId')==case['caseId']]
        rows.append({k:case.get(k) for k in ('caseId','status','elapsedSeconds','error','qualityVerdict')} | {
            'modelRequests':sum(e['kind']=='model_start' for e in own),
            'modelResponses':sum(e['kind']=='model_result' for e in own),
            'toolResults':sum(e['kind']=='tool_result' for e in own),
            'fileWrites':sum(e['kind']=='tool_result' and e.get('operation') in ('file.write','file.patch') and e.get('result',{}).get('ok') is True for e in own)})
    rounds.append({'round':folder.name,'wallSeconds':record['elapsedSeconds'],'cases':rows,
        'modelRequests':sum(e['kind']=='model_start' for e in events),
        'modelResponses':sum(e['kind']=='model_result' for e in events),
        'modelErrors':sum(e['kind']=='model_error' for e in events),
        'toolResults':len(tools),'successfulFileWrites':sum(e.get('operation') in ('file.write','file.patch') and e.get('result',{}).get('ok') is True for e in tools),
        'browserOrCodeTests':sum(e.get('operation')=='code.test' for e in tools),
        'returnedModels':sorted({e.get('model') for e in events if e['kind']=='model_result' and e.get('model')}),
        'ledger':str(folder),'qualityConclusion':'No independent acceptance claimed; inspect case execution status and real artifacts.'})
result={'date':'2026-11-23','framework':'CrewAI 1.15.44','sourceCommit':json.loads((root/'provenance.json').read_text())['sourceCommit'],
        'rounds':rounds,'fairness':'No fresh matched EvoMAC baseline was run. No causal speedup estimate is supported. Failed cases stay in the six-case denominator.',
        'protocol':'Three concurrent teams, two sequential requests per team, original task/role text, course API only, operator-authorized simulated users. Orchestration and transport differ by round.',
        'installationExcluded':True}
(root/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False,indent=2))
