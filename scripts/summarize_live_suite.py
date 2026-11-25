"""Derive non-overlapping timing evidence from real exported live-suite receipts."""
import argparse,json,time,statistics
from collections import Counter
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

def merge(intervals):
    out=[]
    for left,right in sorted((a,b) for a,b in intervals if a is not None and b is not None and b>=a):
        if out and left<=out[-1][1]:out[-1][1]=max(out[-1][1],right)
        else:out.append([left,right])
    return out

def length(intervals):return sum(b-a for a,b in merge(intervals))

def local_time(value):
    return datetime.fromtimestamp(value,ZoneInfo('Asia/Shanghai')).isoformat() if value is not None else None

def summarize(folder):
    suite=json.loads((folder/'suite.json').read_text());manifest=json.loads((folder/'manifest.json').read_text())
    decisions=[json.loads(line) for line in (folder/'human-decisions.jsonl').read_text().splitlines()] if (folder/'human-decisions.jsonl').exists() else []
    decisions=[d for d in decisions if d['state']=='submitted' and d.get('httpStatus')==200]
    rows=[];all_waits=[];all_models=[];all_clarifications=[];all_approvals=[]
    for case in suite['cases']:
        row={k:case.get(k) for k in ('id','team','status','productVerdict','executionComplete','elapsedMs','runId','teamId')}
        if case.get('runId') and (folder/(case['runId']+'.json')).exists():
            r=json.loads((folder/(case['runId']+'.json')).read_text());end=r.get('finishedAt') or time.time()
            waits=[(p['createdAt'],p.get('answeredAt',end)) for p in r['pending']]
            clarifications=[(p['createdAt'],p.get('answeredAt',end)) for p in r['pending'] if p['kind']=='clarification']
            approvals=[(p['createdAt'],p.get('answeredAt',end)) for p in r['pending'] if p['kind']=='approval']
            models=[(c['startedAt'],c.get('finishedAt',end)) for c in r['calls'] if c['tool']=='model.generate']
            tools=[(c['startedAt'],c.get('finishedAt',end)) for c in r['calls'] if c['tool'] not in ('model.generate','ask_user','collab.delegate')]
            wall=end-r['startedAt'];human=length(waits);model=length(models)
            read_keys=[json.dumps([c['args'],c.get('result',{}).get('sha256')],sort_keys=True) for c in r['calls'] if c['tool']=='file.read' and c['status']=='success']
            input_chars=[c['args'].get('inputChars',0) for c in r['calls'] if c['tool']=='model.generate']
            tool_without_wait=length(tools+waits)-human
            row.update(startedAt=r['startedAt'],finishedAt=r.get('finishedAt'),wallSeconds=round(wall,3),
                       simulatedUserWaitSeconds=round(human,3),modelRequestSeconds=round(model,3),
                       clarificationWaitSeconds=round(length(clarifications),3),approvalWaitSeconds=round(length(approvals),3),
                       wallExcludingHumanWaitSeconds=round(wall-human,3),
                       humanInteractionTimeline=[{'id':p['id'],'kind':p['kind'],'agentId':p['agentId'],'taskId':p['taskId'],'question':p['title'],
                           'requestedAtShanghai':local_time(p['createdAt']),'answeredAtShanghai':local_time(p.get('answeredAt')),
                           'waitSeconds':round(p.get('answeredAt',end)-p['createdAt'],3),'status':p['status'],'answer':p.get('answer'),
                           'assumptions':next((d['assumptions'] for d in decisions if d['question']['id']==p['id']),[])} for p in r['pending']],
                       toolExecutionExcludingHumanWaitSeconds=round(tool_without_wait,3),
                       otherHostSeconds=round(max(0,wall-length(waits+models+tools)),3),
                       modelCalls=r['modelCalls'],reportedTokens=r['tokens'],returnedModel=r.get('returnedModel'),
                       runtimeVersion=r.get('runtimeVersion'),thinkingPolicy=r.get('thinkingPolicy'),
                       modelModeCalls=dict(Counter(c['args'].get('thinking','not_logged') for c in r['calls'] if c['tool']=='model.generate')),
                       modelInputChars={'maximum':max(input_chars,default=0),'mean':round(statistics.mean(input_chars),1) if input_chars else 0},
                       identicalRepeatedReads=sum(n-1 for n in Counter(read_keys).values()),
                       unchangedPublicationsReused=sum(len(c.get('result',{}).get('reusedUnchanged',[])) for c in r['calls'] if c['tool']=='artifact.publish' and c['status']=='success'),
                       clarificationReplyCharacters=sum(len(p.get('answer','')) for p in r['pending'] if p['kind']=='clarification' and p['status']=='answered'),
                       reportedPromptTokens=sum(c.get('result',{}).get('usage',{}).get('prompt_tokens',0) for c in r['calls'] if c['tool']=='model.generate'),
                       reportedCompletionTokens=sum(c.get('result',{}).get('usage',{}).get('completion_tokens',0) for c in r['calls'] if c['tool']=='model.generate'),
                       completedSubtasks=sum(s['status']=='done' for s in r['steps']),totalSubtasks=len(r['steps']),
                       currentArtifacts=len([a for a in r['artifacts'] if not a.get('superseded')]),
                       answeredClarifications=sum(p['kind']=='clarification' and p['status']=='answered' for p in r['pending']),
                       answeredApprovals=sum(p['kind']=='approval' and p['status']=='answered' for p in r['pending']),
                       failedCalls=[{'tool':c['tool'],'taskId':c['taskId'],'error':c.get('result',{}).get('error')} for c in r['calls'] if c['status']=='failed'],
                       failureReasons=[{'taskId':s['id'],'status':s['status'],'reason':s.get('result','')[:1200]} for s in r['steps'] if s['status'] in ('failed','partial')],
                       reviews=[{k:v.get(k) for k in ('reviewer','targetTaskId','revision','verdict','summary')} for v in r['reviews']],
                       memoryRetrieved=len(r['memoryHits']),memoryExplicitlyAdopted=sum(bool(m.get('applied')) for m in r['memoryHits']),
                       steps=[{k:s.get(k) for k in ('id','title','agentId','status','elapsedMs','parentId','reviewOf','requiresArtifacts')} for s in r['steps']],error=r.get('error'))
            all_waits+=waits;all_models+=models;all_clarifications+=clarifications;all_approvals+=approvals
        rows.append(row)
    end=suite.get('finishedAt') or time.time();human=length(all_waits);wall=end-suite['startedAt']
    report={'suiteId':suite['id'],'status':suite['status'],'provider':manifest['health']['provider'],'model':manifest['health']['model'],
            'crewai':manifest['crewai'],'protocol':manifest['protocol'],'memoryEnabled':suite['memory'],
            'startedAtShanghai':datetime.fromtimestamp(suite['startedAt'],ZoneInfo('Asia/Shanghai')).isoformat(),
            'finishedAtShanghai':datetime.fromtimestamp(end,ZoneInfo('Asia/Shanghai')).isoformat() if suite.get('finishedAt') else None,
            'wallSeconds':round(wall,3),'simulatedUserWaitSeconds':round(human,3),
            'clarificationWaitSeconds':round(length(all_clarifications),3),'approvalWaitSeconds':round(length(all_approvals),3),
            'wallExcludingSimulatedUserWaitSeconds':round(wall-human,3),'modelRequestSeconds':round(length(all_models),3),
            'modelCalls':sum(r.get('modelCalls',0) for r in rows),'reportedTokens':sum(r.get('reportedTokens',0) for r in rows),
            'casesStarted':sum(bool(c.get('runId')) for c in suite['cases']),
            'executionCompletionRate':sum(bool(c.get('executionComplete')) for c in suite['cases'])/6,
            'productPassRate':sum(c.get('productVerdict')=='pass' for c in suite['cases'])/6,
            'successfulSixCaseDeliverySeconds':round(wall,3) if all(c.get('executionComplete') and c.get('productVerdict')=='pass' for c in suite['cases']) else None,
            'subtaskCompletionRate':sum(r.get('completedSubtasks',0) for r in rows)/max(1,sum(r.get('totalSubtasks',0) for r in rows)),
            'cases':rows,'simulatedUserDecisions':decisions,
            'limits':['All human/simulated-user waiting is included in primary wall time; subtracted wait is diagnostic only.',
                      'Clarification wait starts when the runtime creates a blocking clarification and ends at the recorded answer; question generation and post-answer processing remain model/host time.',
                      'The operator answers actual questions and logs assumptions; this is not a real-human latency measurement.',
                      'Model/tool receipt intervals are unioned, avoiding duplicate delegation and approval wait time.',
                      'Execution completion, agent product review and official course acceptance are separate.',
                      'No framework-only speedup or memory-benefit inference from this single run.']}
    report['localTokenLimitStops']=sum(any('Token budget exhausted' in f['reason'] for f in c.get('failureReasons',[])) for c in rows)
    integrity=folder/'integrity.json'
    if integrity.exists():report['integrity']=json.loads(integrity.read_text())
    (folder/'timing-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('folder',type=Path);args=parser.parse_args()
    r=summarize(args.folder)
    print(json.dumps({k:r[k] for k in ('suiteId','status','wallSeconds','simulatedUserWaitSeconds','modelCalls','executionCompletionRate','productPassRate')},ensure_ascii=False))
