"""Adapt saved platform runs to the independent acceptance contract."""

from datetime import datetime
import copy
from hashlib import sha256
import json
from pathlib import Path

from .evaluation import EXECUTION_STATES, EvidenceError, local_file, object_from_json


_TASK_TERMINAL = {'done', 'partial', 'failed', 'cancelled', 'skipped', 'rejected'}


def _roles(platform,binding,agents=None):
    from .teams import get_team_agents
    registry=get_team_agents(platform.get('teamId','default')) if agents is None else agents
    if not isinstance(registry,dict) or not registry or any(not isinstance(value,dict) for value in registry.values()):
        raise ValueError('A real server-owned team registry is required.')
    roles={}
    for role,capability in (('coordinator','planning'),('research','research'),('executor','coding'),('reviewer','review')):
        matches=[actor for actor,metadata in registry.items() if capability in metadata.get('capabilities',[])]
        if len(matches)==1:roles[role]=matches[0]
    explicit=binding.get('roleBindings',{})
    if not isinstance(explicit,dict) or any(not isinstance(key,str) or not isinstance(value,str) or value not in registry for key,value in explicit.items()):
        raise ValueError('roleBindings must map role names to actual team agent IDs.')
    roles.update(explicit)
    if roles.get('reviewer') and 'review' not in registry[roles['reviewer']].get('capabilities',[]):
        raise ValueError('The independent reviewer binding needs the actual review capability.')
    if roles.get('reviewer') and roles.get('reviewer')==roles.get('executor'):
        raise ValueError('Executor and independent reviewer cannot be the same agent.')
    if binding.get('teamId',platform.get('teamId','default'))!=platform.get('teamId','default'):
        raise ValueError('The binding and frozen run belong to different teams.')
    return roles


def _human_waits(platform,events):
    modified_at={}
    for event in events:
        if event.get('type')=='approval_modified':
            try:modified_at[event['payload']['approvalId']]=int(datetime.fromisoformat(event['time'].replace('Z','+00:00')).timestamp()*1000)
            except (KeyError,ValueError,TypeError):continue
    waits=[]
    for approval in platform.get('approvals',[]):
        end=approval.get('resolvedAt')
        # A late informational reply does not change the original expiry or
        # turn an expired permission into an authorization.
        if approval.get('type')=='clarification':
            receipt=approval.get('clarificationReceipt') or approval.get('lateResponse') or {}
            if isinstance(receipt,dict) and type(receipt.get('receivedAt')) is int:end=receipt['receivedAt']
        if end is None and approval.get('status')=='expired':end=approval.get('expiresAt')
        if approval.get('status')=='modified' and approval.get('id') in modified_at:
            end=max(approval['createdAt'],modified_at[approval['id']])
        waits.append({'startedAtMs':approval['createdAt'],'endedAtMs':end,'approvalId':approval.get('id'),
                      'type':approval.get('type'),'status':approval.get('status'),'expiresAtMs':approval.get('expiresAt')})
    return waits


def _record(binding,platform,events,artifact_store,*,agents=None,source='platform'):
    run_id=binding['runId']
    if platform.get('id')!=run_id:raise ValueError('The evidence run ID does not match the requested run.')
    roles=_roles(platform,binding,agents)
    state=platform['status'];error=platform.get('error') or {}
    if state in EXECUTION_STATES:
        if state=='failed' and error.get('code')=='runtime':state='crashed'
        elif state in ('failed','partial') and error.get('code')=='timeout':state='timeout'
    elif binding.get('terminatedAtMs') is not None and state=='interrupted':state='crashed'
    elif binding.get('deadlineAtMs') is not None:state='timeout'
    else:raise ValueError(f'Run {run_id} needs a recorded terminal state, termination, or deadline.')
    tasks=[]
    for task in platform.get('steps',[]):
        if task.get('_planning') or task.get('kind')=='planning':continue
        revision=task.get('revision');known=type(revision) is int and revision>=0
        if revision is not None and not known:raise ValueError('Invalid task revision.')
        for prior in task.get('validationHistory',[]):
            old=prior.get('targetRevision')
            if type(old) is int and old>=0 and known and old!=revision:
                tasks.append({'taskId':task['id'],'revision':old,'status':'rework','reviewEvidence':copy.deepcopy(prior)})
        memory_ids=sorted({item['memoryId'] for item in platform.get('memoryHits',[])
                           if item.get('taskId')==task['id'] and item.get('applied')
                           and item.get('ownerAgentId')==task.get('agentId') and item.get('revision')==revision and known})
        tasks.append({'taskId':task['id'],'revision':revision,'revisionKnown':known,'ownerAgentId':task.get('agentId'),
                      'status':task['status'],'closed':task['status'] in _TASK_TERMINAL,'artifactIds':list(task.get('outputs',[])),
                      'result':task.get('result',''),'memoryIds':memory_ids,'errorCode':(task.get('error') or {}).get('code'),
                      'errorReason':(task.get('error') or {}).get('message'),
                      'attemptHistory':copy.deepcopy(task.get('attemptHistory',[]))})
    evidence=copy.deepcopy(binding.get('evidence',{}));calls={call['id']:call for call in platform.get('calls',[])}
    if 'research' in evidence:
        ref=evidence['research'];call=calls.get(ref.get('callId'),{});result=call_result(call)
        sources=result.get('sources',result.get('results',[]));sources=sources if isinstance(sources,list) else []
        sources=[item for item in sources if isinstance(item,dict) and isinstance(item.get('url'),str)]
        text=' '.join(item.get('title','') for item in sources if item['url']==ref.get('sourceUrl'))
        if result.get('url')==ref.get('sourceUrl'):text+=' '+str(result.get('content',''))
        valid=_matching_call(call,ref,roles.get('research')) and result.get('ok') is True
        evidence['research']={**ref,'tool':call.get('tool'),'status':call.get('status') if valid else None,
                              'returnedUrls':[item['url'] for item in sources],'sourceText':text}
    if 'review' in evidence:
        evidence['review']=platform_review(evidence['review'],calls,events,artifact_store,run_id,
                                            reviewer_id=roles.get('reviewer'))
    return {'runId':run_id,'caseId':binding['caseId'],'source':source,'teamId':platform.get('teamId','default'),
            'roleBindings':roles,'status':state,'startedAtMs':platform['startedAt'],'finishedAtMs':platform.get('finishedAt'),
            'terminatedAtMs':binding.get('terminatedAtMs'),'deadlineAtMs':binding.get('deadlineAtMs'),
            'errorCode':error.get('code') or (state if state in ('timeout','crashed','cancelled') else None),
            'errorReason':error.get('message') or binding.get('errorReason'),'humanWaits':_human_waits(platform,events),
            'tasks':tasks,'evidence':evidence,'rawRun':copy.deepcopy(platform),
            'memoryEnabled':platform.get('memoryEnabled'),'memoryCondition':binding.get('memoryCondition'),
            'memoryBoundary':binding.get('memoryBoundary'),'continuationHistory':copy.deepcopy(platform.get('continuationHistory',[])),
            'memoryChanges':[copy.deepcopy(event) for event in events if event.get('type') in ('memory_applied','memory_feedback')]}


def platform_record(binding,repository,artifact_store,*,agents=None):
    platform=repository.get_run(binding['runId'])
    if platform is None:raise ValueError(f'No saved platform run for {binding["runId"]}.')
    return _record(binding,platform,repository.events(binding['runId']),artifact_store,agents=agents)


def frozen_export_record(binding,export_path,*,agents=None):
    """Read a frozen JSON export, never a live DB, workspace, model or tool.

    Return normalized D evidence plus a defensive immutable artifact loader.
    The export SHA identifies bytes, not a digital signature or semantic verdict.
    Optional artifactFiles maps missing inline contents to confined relative files.
    """
    path=Path(export_path)
    if not path.is_file() or path.stat().st_size>32*1024*1024:raise ValueError('Frozen export must be a regular JSON file of at most 32 MiB.')
    with path.open('rb') as stream:raw=stream.read(32*1024*1024+1)
    if len(raw)>32*1024*1024:raise ValueError('Frozen export exceeds 32 MiB.')
    digest=sha256(raw).hexdigest()
    if binding.get('exportSha256',digest)!=digest:raise ValueError('Frozen export SHA256 mismatch.')
    document=object_from_json(raw);platform=document.get('run',document)
    if not isinstance(platform,dict) or not isinstance(platform.get('id'),str):raise ValueError('A frozen run object is required.')
    if document.get('runId',platform['id'])!=platform['id']:raise ValueError('Outer export and run IDs differ.')
    if document.get('case',binding['caseId'])!=binding['caseId']:raise ValueError('Frozen export belongs to another case.')
    files=binding.get('artifactFiles',document.get('artifactFiles',{}))
    if not isinstance(files,dict):raise ValueError('artifactFiles must be a mapping of artifact IDs to relative paths.')
    artifacts={}
    for item in platform.get('artifacts',[]):
        if not isinstance(item,dict) or not isinstance(item.get('id'),str) or item['id'] in artifacts:
            raise ValueError('Invalid or duplicate frozen artifact metadata.')
        artifacts[item['id']]=copy.deepcopy(item)
    def load(artifact_id):
        item=copy.deepcopy(artifacts.get(artifact_id))
        if item is None:raise EvidenceError('Artifact is absent from the frozen run.')
        if 'content' not in item and artifact_id in files:
            name=files[artifact_id]
            if (not isinstance(name,str) or not name or any(part in ('.','..') or part.startswith('.') for part in Path(name).parts)):
                raise EvidenceError('Artifact file references must be explicit public relative paths.')
            cursor=path.parent
            for part in Path(name).parts:
                cursor=cursor/part
                if cursor.is_symlink():raise EvidenceError('Artifact file references cannot traverse symlinks.')
            candidate=local_file(path.parent,name)
            if not candidate.is_file() or candidate.stat().st_size>1024*1024:
                raise EvidenceError('Frozen artifact file is missing or exceeds its size limit.')
            item['content']=candidate.read_text(encoding='utf-8')
        content=item.get('content')
        if not isinstance(content,str):raise EvidenceError('Frozen artifact content is missing; no workspace fallback is permitted.')
        data=content.encode('utf-8')
        if (len(data)>1024*1024 or item.get('runId')!=platform['id'] or item.get('sha256')!=sha256(data).hexdigest()
                or (item.get('size') is not None and item['size']!=len(data))):
            raise EvidenceError('Frozen artifact run/hash/size verification failed.')
        return item
    class Store:
        def get(self,artifact_id):return load(artifact_id)
    events=platform.get('events',[])
    result=_record(binding,platform,events,Store(),agents=agents,source='frozen_export')
    observed=document.get('observedAt',binding.get('observedAt'))
    if 'observedAt' in document and 'observedAt' in binding and document['observedAt']!=binding['observedAt']:
        raise ValueError('The binding cannot change the recorded observation time.')
    result.update(exportSha256=digest,observedAt=observed,observationStatus=document.get('observationStatus'),
                  exportMetadata={key:copy.deepcopy(document[key]) for key in
                                  ('case','title','sourceSha256','evidenceSource','executionStatus','stopReason','elapsedSec','planningTaskIds') if key in document})
    result.update({key:copy.deepcopy(document[key]) for key in ('elapsedSec','stopReason','evidenceSource','sourceSha256') if key in document})
    return result,load

def _matching_call(call, ref, owner):
    return (owner is not None and call.get('agentId')==owner and call.get('taskId')==ref.get('taskId')
            and type(call.get('revision')) is int and type(ref.get('revision')) is int and call['revision']==ref['revision'])


def call_result(call):
    value = call.get('result')
    return value if isinstance(value, dict) else {}


def platform_review(ref, calls, events, artifacts, run_id, *, reviewer_id='reviewer'):
    call = calls.get(ref.get('callId'), {})
    result = call_result(call)
    target = ref.get('target', {})
    snapshot = call.get('reviewTargets', {}).get(target.get('taskId'), {})
    output_id = ref.get('result', {}).get('artifactId')
    input_id = ref.get('inputArtifactId')

    finished = next((index for index, event in enumerate(events)
                     if event['type'] == 'tool_finished' and event['payload'].get('callId') == call.get('id')), None)

    def granted_path(artifact_id, name, digest):
        for event in events[:finished] if finished is not None else []:
            payload = event['payload']
            grant = payload.get('grant', {})
            if (event['type'] == 'artifact_granted' and payload.get('artifactId') == artifact_id
                    and payload.get('toAgent') == reviewer_id and grant.get('readOnly') is True
                    and grant.get('sha256') == digest and grant.get('path') == f'{artifact_id}/{name}'):
                return f'imports/{artifact_id}/{name}'
        return None

    args = call.get('args', {})
    try:input_artifact = artifacts.get(input_id) if isinstance(input_id, str) else None
    except (OSError,ValueError,KeyError,TypeError):input_artifact=None
    script_path = granted_path(target.get('artifactId'), 'summarize_orders.py', target.get('sha256'))
    input_path = granted_path(input_id, 'review_orders.csv', input_artifact['sha256']) if input_artifact else None
    registered = finished is not None and any(
        event['type'] == 'artifact_registered' and event['payload'].get('artifactId') == output_id
        for event in events[finished + 1:])

    valid = (_matching_call(call, ref, reviewer_id) and result.get('ok') is True and registered
             and snapshot.get('targetRevision') == target.get('revision')
             and snapshot.get('artifactHashes', {}).get(target.get('artifactId')) == target.get('sha256')
             and bool(script_path and input_path and input_artifact)
             and input_artifact['runId'] == run_id and input_artifact['name'] == 'review_orders.csv'
             and input_artifact.get('ownerAgentId') == reviewer_id
             and not any(event['type'] == 'artifact_granted' and event['payload'].get('artifactId') == input_id
                         and event['payload'].get('toAgent') != reviewer_id for event in events)
             and args.get('path') == script_path and args.get('args') == ['/' + input_path, 'review_summary.json'])
    return {**ref, 'tool': call.get('tool'), 'status': call.get('status') if valid else None,
            'sandboxId': result.get('sandboxId'), 'exitCode': result.get('exitCode'),
            'inputSha256': input_artifact['sha256'] if valid else None, 'readOnly': bool(valid)}
