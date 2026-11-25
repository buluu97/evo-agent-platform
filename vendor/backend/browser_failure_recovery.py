"""Return observed deterministic browser assertion failures without rerunning them.

This is a narrowly scoped tool-error observation, inspired by CrewAI's bounded
tool error feedback. It neither repairs product code nor accepts a deliverable.
Only host-bound declarative browser reports are eligible; arbitrary code tests,
unknown effects and changed workspace versions keep their existing guards.
"""
import copy
import hashlib
import json
import re
from .browser_observation import failure_observation_view


def _key(value):
    try:return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    except (TypeError,ValueError,RecursionError):return None


def _manifest(value):
    if not isinstance(value,list) or not value or len(value)>64:return None
    rows={}
    for item in value:
        if not isinstance(item,dict):return None
        path=item.get('path');sha=item.get('sha256')
        if (not isinstance(path,str) or not path or len(path)>1000 or path in rows
                or not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha)):return None
        rows[path]=sha
    return rows


def _report(call):
    result=call.get('result')
    if (call.get('status')!='failed' or not isinstance(result,dict) or result.get('ok') is not False
            or result.get('runtime')!='browser' or result.get('exitCode')!=1
            or any(result.get(k) or call.get(k) for k in ('outcomeUnknown','deadlineExceeded','timedOut','cancelled','outputLimitExceeded'))
            or result.get('notExecuted') or result.get('modelViewOnly') or result.get('needsApproval')
            or result.get('actionExecuted') is False):return None
    report=result.get('browser')
    if (not isinstance(report,dict) or report.get('engine')!='chromium' or report.get('executed') is not True
            or report.get('ok') is not False):return None
    manifest=_manifest(report.get('sourceManifest'));tested=_manifest(report.get('testedFiles'))
    entry=report.get('entry')
    if (not manifest or not tested or not isinstance(entry,dict)
            or manifest.get(entry.get('path'))!=entry.get('sha256')
            or tested.get(entry.get('path'))!=entry.get('sha256')
            or any(manifest.get(path)!=sha for path,sha in tested.items())):return None
    assertions=report.get('assertions')
    if not isinstance(assertions,list):return None
    failed=[a for a in assertions if isinstance(a,dict) and a.get('passed') is False]
    if not failed:return None
    return manifest,report,failed


def browser_failure_trend(run,task):
    """Describe the same actual failure across tested source versions.

    This is historical progress feedback, not the unchanged-source execution
    guard below. It neither blocks a new test nor diagnoses code vs test error.
    """
    if not isinstance(run,dict) or not isinstance(task,dict):return None
    if (any(not isinstance(value,str) or not value for value in
            (run.get('id'),task.get('id'),task.get('agentId')))
            or type(task.get('revision')) is not int or task['revision']<0):return None
    calls=run.get('calls')
    if not isinstance(calls,list) or any(not isinstance(call,dict) for call in calls):return None
    from .stage_control import _unresolved_owner_effects
    if _unresolved_owner_effects(run,task):return None
    history=[];signature=None;seen=set()
    for call in calls:
        if call.get('runId',run['id'])!=run['id']:return None
        if call.get('agentId')!=task['agentId'] or call.get('taskId')!=task['id']:continue
        if type(call.get('revision')) is not int or call['revision']!=task['revision']:continue
        if call.get('tool')!='code.test':continue
        args=call.get('args');identifier=call.get('id')
        if not isinstance(identifier,str) or not identifier or identifier in seen:return None
        seen.add(identifier)
        parsed=_report(call)
        if (not isinstance(args,dict) or args.get('language')!='browser' or 'code' in args
                or not isinstance(args.get('checks'),list) or not args['checks'] or parsed is None):
            history=[];signature=None;continue
        manifest,report,failed=parsed
        setup=copy.deepcopy(args);setup.setdefault('timeout',20)
        assertion={key:copy.deepcopy(failed[0][key]) for key in
                   ('action','pageId','selector','expected','actual','passed') if key in failed[0]}
        key=_key({'setup':setup,'failure':assertion})
        manifest_key=_key(_manifest(report['testedFiles']))
        if key is None or len(key)>64000 or manifest_key is None:
            history=[];signature=None;continue
        if signature!=key:history=[]
        signature=key
        history.append({'callId':identifier,'testedFilesSha256':hashlib.sha256(manifest_key.encode()).hexdigest(),
                        'failedAssertion':assertion,'failureObservation':failure_observation_view(report.get('failureObservation'))})
    versions={row['testedFilesSha256'] for row in history}
    if len(history)<3 or len(versions)<2:return None
    latest=history[-1]
    result={'runId':run['id'],'taskId':task['id'],'agentId':task['agentId'],'revision':task['revision'],
            'scope':'historical_actual_tests_only','observedFailureCount':len(history),'testedSourceVersions':len(versions),
            'recentTests':[{key:row[key] for key in ('callId','testedFilesSha256')} for row in history[-6:]],
            'earlierTestsOmitted':max(0,len(history)-6),'latestFailedAssertion':latest['failedAssertion'],
            'latestFailureObservation':latest['failureObservation'],'cause':'unassessed','currentUntestedChanges':'unassessed',
            'instruction':'The same test setup and failure persisted across different actually tested source versions. '
                'File edits alone have not resolved this observed failure. Reassess the test setup, clock/date/timezone, '
                'selector and expected result against the observations before another code change. Correct a test only '
                'when evidence shows its expectation is wrong; do not weaken product requirements. If the cause remains '
                'unclear, explicitly publish usable files with failures disclosed for independent QA. This observation '
                'does not block a new test, publish files, diagnose a cause, accept work, extend time or grant permission.'}
    encoded=_key(result)
    return result if encoded is not None and len(encoded)<=8000 else None


def browser_failure_feedback(run,task,action):
    """Reject the third identical failed browser proposal on unchanged sources.

    Calls and bodies are never created here. Different browser tests, unknown
    calls or any possible workspace mutation break the streak. A rejected
    proposal is not another failure and cannot inflate this observed history.
    """
    if not all(isinstance(v,dict) for v in (run,task,action)):return None
    args=action.get('args');revision=task.get('revision')
    if (action.get('type')!='tool' or action.get('name')!='code.test'
            or not isinstance(args,dict) or args.get('language')!='browser' or 'code' in args
            or not isinstance(args.get('checks'),list) or not args['checks']
            or type(revision) is not int or revision<0
            or any(not isinstance(v,str) or not v for v in (run.get('id'),task.get('id'),task.get('agentId')))):return None
    # ToolGateway records the validated default timeout. Compare the same
    # effective setup when a proposal omits that single documented default.
    effective=copy.deepcopy(args)
    effective.setdefault('timeout',20)
    encoded=_key(effective)
    if encoded is None or len(encoded)>64000:return None
    calls=run.get('calls')
    if not isinstance(calls,list) or any(not isinstance(c,dict) for c in calls):return None
    failures=[];sources=None;seen=set()
    for call in calls:
        if call.get('agentId')!=task['agentId']:continue
        tool=call.get('tool');call_args=call.get('args')
        # Imports are immutable; all private writes/patches/deletes and arbitrary
        # executed code can invalidate observed source versions, even on failure.
        mutation=(tool in ('file.write','file.patch','file.delete','code.run')
                  or tool=='code.test' and (not isinstance(call_args,dict) or call_args.get('language')!='browser'))
        if mutation or call.get('status') in ('unknown','running','pending') and tool!='llm.chat':
            failures=[];sources=None
            continue
        if tool!='code.test':continue
        if (call.get('taskId')!=task['id'] or type(call.get('revision')) is not int
                or call['revision']!=revision or not isinstance(call_args,dict) or _key(call_args)!=encoded):
            failures=[];sources=None
            continue
        call_id=call.get('id')
        if not isinstance(call_id,str) or not call_id or call_id in seen:return None
        seen.add(call_id)
        parsed=_report(call)
        if parsed is None:
            failures=[];sources=None
            continue
        manifest,report,failed=parsed
        if sources!=manifest:failures=[]
        sources=manifest
        failures.append((call_id,report,failed))
    if len(failures)<2:return None
    call_id,report,failed=failures[-1]
    diagnostics=[]
    for item in failed[:3]:
        row={}
        for key in ('action','pageId','selector','expected','actual','passed'):
            if key in item:
                value=copy.deepcopy(item[key])
                if isinstance(value,str) and len(value)>800:row[key]=value[:800];row[key+'Truncated']=True
                else:row[key]=value
        diagnostics.append(row)
    observed=failure_observation_view(report.get('failureObservation'))
    return {'ok':False,'actionExecuted':False,'notExecuted':True,
        'runId':run['id'],'taskId':task['id'],'agentId':task['agentId'],'revision':revision,
        'tool':'code.test','error':{'code':'repeated_browser_assertion_failure','retryable':False,
            'message':'Only this identical browser test proposal is rejected after at least two failures on the same source manifest. Corrected tests are not prohibited by this guard; this proposal was not executed.'},
        'rejectionScope':{'kind':'identical_proposal_on_unchanged_sources',
                          'allBrowserTestingBlocked':False,'correctedProposalRequiresNormalChecks':True,
                          'newPermissionGranted':False,
                          'instruction':'retryable=false describes this unchanged proposal. Propose an evidence-based correction as a new type=tool, name=code.test action. Original stage, deadline, tool schema and access checks still apply; a new test is not automatically authorized or executed.'},
        'observedFailures':{'count':len(failures),'callIds':[v[0] for v in failures[-4:]],
                            'callIdsTruncated':len(failures)>4,'latestCallId':call_id,
                            'failedAssertions':diagnostics,'failedAssertionsOmitted':max(0,len(failed)-3)},
        'sourceManifest':[{'path':path,'sha256':sha} for path,sha in sources.items()],
        'failureObservation':observed,
        'pageDate':{'status':'see_actual_failure_observation' if observed and observed.get('runtimeUTC') else 'not_recorded_in_this_browser_report'},
        'instruction':'Inspect the actual expected/actual assertion and current source before retrying. Check clock/date/timezone, '
            'test setup and owner/interaction semantics; do not change correct product behavior merely to satisfy an incorrect test. '
            'Correct the test setup/expectation if evidence shows it is wrong, or repair the real product defect and retest. '
            'Existing private code may be published explicitly for independent QA with failures/gaps disclosed; it is not published or accepted by this feedback. '
            'No repeated test, source edit, automatic publication, pass, new permission or time extension occurred. '
            'Unknown outcomes, arbitrary Python/JavaScript execution and other mutation loops keep their original safety rules.'}
