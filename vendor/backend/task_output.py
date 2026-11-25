"""Structured evidence handoff, independently implemented with the standard library.

Design reference: CrewAI commit f4da03af1be0f61db71c8599a40c06dce98b9b48,
tasks/task_output.py (raw/expected_output/json_dict/agent/tool_failures) and
flow/persistence/sqlite.py (JSON state retained for restoration). No source or
dependency is copied. Evidence remains distinct from semantic task acceptance.
"""
import copy
import hashlib
import json
import re
from urllib.parse import urlsplit


_MAX_RAW = 18000
_MAX_ARTIFACTS = 40
_MAX_RECORDS = 16
_MAX_DETAIL = 1000
_UNCERTAIN = {'pending', 'running', 'unknown', 'interrupted', 'cancelled', 'reconciled'}
_TARGET_WIDTHS = {'url': 2048, 'query': 512, 'path': 500}


def _clip(value, width):
    if not isinstance(value, str) or len(value) <= width:
        return copy.deepcopy(value)
    marker = '[TRUNCATED]'
    return value[:max(0, width - len(marker))] + marker


def _json_object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key.')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('Nonfinite JSON constant.')

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise ValueError('A JSON object is required.')
    # JSON numbers such as 1e999 can overflow Python float; keep evidence safely
    # serializable by our strict persistence layer rather than injecting Infinity.
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
    return value


def _requested_target(call):
    """The actual requested locator, not a guessed target from error prose."""
    args=call.get('args')
    if not isinstance(args,dict):return None
    name=call.get('tool')
    kind=('url' if name=='web.fetch' else 'query' if name=='web.search'
          else 'path' if isinstance(name,str) and (name.startswith('file.') or name in ('code.run','code.test')) else None)
    value=args.get(kind) if kind else None
    if not isinstance(value,str) or not value:return None
    try:raw=value.encode('utf-8')
    except UnicodeEncodeError:return None
    width=_TARGET_WIDTHS[kind]
    target={'type':kind,'value':_clip(value,width),'characters':len(value),'truncated':len(value)>width}
    if target['truncated']:target['valueSha256']=hashlib.sha256(raw).hexdigest()
    return target


def _tool_record(call, failure_type):
    result = call.get('result')
    record = {key: copy.deepcopy(call.get(key)) for key in ('taskId', 'agentId', 'tool', 'status')}
    record.update(callId=call.get('id'), type=failure_type)
    if 'revision' in call:record['revision']=copy.deepcopy(call['revision'])
    target=_requested_target(call)
    if target is not None:record['requestedTarget']=target
    if isinstance(result, dict):
        error = result.get('error')
        if isinstance(error, dict):
            record['error'] = {key: _clip(error[key], _MAX_DETAIL if key == 'message' else 100)
                               for key in ('code', 'message', 'retryable') if key in error}
        elif isinstance(error, str):
            record['error'] = {'message': _clip(error, _MAX_DETAIL)}
        elif isinstance(result.get('stderr'), str) and result['stderr']:
            record['error'] = {'message': _clip(result['stderr'], _MAX_DETAIL)}
        for key in ('exitCode', 'timedOut', 'cancelled', 'denied'):
            if key in result:
                record[key] = copy.deepcopy(result[key])
    elif isinstance(result, str):
        record['error'] = {'message': _clip(result, _MAX_DETAIL)}
    if 'durationMs' in call:
        record['durationMs'] = call['durationMs']
    return record


def _successful_fetch_reference(task,call,result):
    """Exact current-producer audit reference; never forward its private body."""
    if (type(task.get('revision')) is not int or type(call.get('revision')) is not int
            or task['revision']!=call['revision'] or call.get('taskId')!=task.get('id')
            or not isinstance(task.get('agentId'),str) or not task['agentId']
            or call.get('agentId')!=task['agentId']):return None
    call_id,url,body=call.get('id'),result.get('url'),result.get('content')
    if (not isinstance(call_id,str) or not call_id or len(call_id)>120
            or not isinstance(url,str) or not url or len(url)>2048
            or not isinstance(body,str) or not body.strip() or result.get('contentOmitted') is True):return None
    try:
        parts=urlsplit(url)
        if (parts.scheme not in ('http','https') or not parts.hostname
                or parts.username is not None or parts.password is not None):return None
        digest=hashlib.sha256(body.encode('utf-8')).hexdigest()
    except (ValueError,UnicodeError):return None
    ref={'callId':call_id,'url':url,'status':'success','bodySha256':digest,
         'bodyCharacters':len(body),'bodyOmitted':True}
    if result.get('truncated') is True:ref['bodyTruncated']=True
    title=result.get('title')
    if isinstance(title,str) and title:
        ref.update(title=_clip(title,160),titleTruncated=len(title)>160)
    requested=_requested_target(call)
    if requested is not None and requested.get('value')!=url:ref['requestedTarget']=requested
    return ref


def _observed_work(task, calls):
    """Counts of current-revision receipts, not current files or source bodies."""
    counts = {'webFetchesWithBody': 0, 'fileWrites': 0, 'filePatches': 0, 'codeTestsExitedZero': 0}
    files, fetch_ids, fetch_refs, unattributed, unreferenced = [], [], [], 0, 0
    revision = task.get('revision', 0)
    for call in calls:
        if (call.get('taskId') == task.get('id') and call.get('agentId') == task.get('agentId')
                and call.get('tool') != 'llm.chat' and type(call.get('revision')) is not int):
            unattributed += 1
        if (call.get('taskId') != task.get('id') or call.get('agentId') != task.get('agentId')
                or type(call.get('revision')) is not int or type(revision) is not int or call['revision'] != revision):
            continue
        result = call.get('result')
        if (call.get('status') != 'success' or not isinstance(result, dict) or result.get('ok') is not True
                or any(result.get(key) for key in ('outcomeUnknown', 'needsApproval', 'deadlineExceeded', 'cancelled', 'timedOut'))):
            continue
        name = call.get('tool')
        if name == 'web.fetch' and isinstance(result.get('content'), str) and result['content'].strip() and result.get('contentOmitted') is not True:
            counts['webFetchesWithBody'] += 1
            fetch_ids.append(call.get('id'))
            ref=_successful_fetch_reference(task,call,result)
            if ref is not None:fetch_refs.append(ref)
            else:unreferenced+=1
        if name in ('file.write', 'file.patch'):
            counts['fileWrites' if name == 'file.write' else 'filePatches'] += 1
            path, sha = result.get('path'), result.get('sha256')
            if isinstance(path, str) and len(path) <= 500 and isinstance(sha, str) and re.fullmatch('[a-f0-9]{64}', sha):
                files.append({'callId': call.get('id'), 'path': path, 'sha256': sha})
        if name == 'code.test' and type(result.get('exitCode')) is int and result['exitCode'] == 0:
            counts['codeTestsExitedZero'] += 1
    return {'scope': 'current_task_revision', 'revision': revision, **counts,
            'unattributedRevisionCalls': unattributed,
            'recentFetchCallIds': fetch_ids[-3:], 'recentWrittenVersions': files[-3:],
            'writtenVersionReferencesOmitted': max(0, len(files)-3),
            **({'recentSuccessfulFetches':fetch_refs[-3:],
                'successfulFetchReferencesTotal':len(fetch_refs),
                'successfulFetchReferencesOmitted':max(0,len(fetch_refs)-3),
                'successfulFetchReferencesUnavailable':unreferenced,
                'fetchReferenceMeaning':'Producer fetch succeeded; only URL/hash metadata is present here. Recipient must explicitly fetch/read the source to verify claims; this is not source text, verification or acceptance.'}
               if counts['webFetchesWithBody'] else {}),
            'meaning': 'Call evidence only; private versions may change/be deleted. No source bodies, publication or acceptance is implied. Unattributed calls are not zero.'}


def build_task_output(task, run):
    """Build a detached JSON-compatible output from actual server task/run data.

    Missing validation stays None. Missing/malformed call logs stay None, rather
    than becoming an invented empty failure history. Permission denial is a tool
    outcome, not a model failure; unknown effects remain explicitly uncertain.
    Rebuild after review/rework so a cached prior verdict cannot silently survive.
    """
    if not isinstance(task, dict) or not isinstance(run, dict):
        raise ValueError('Task and run records must be dictionaries.')
    raw = task.get('result')
    contract = copy.deepcopy(task.get('outputContract'))
    output_validation = copy.deepcopy(task.get('outputValidation')) if isinstance(task.get('outputValidation'), dict) else None
    review_validation = copy.deepcopy(task.get('validation')) if isinstance(task.get('validation'), dict) else None
    criteria = task.get('acceptanceCriteria')
    artifacts = task.get('outputs')
    calls = run.get('calls')
    calls_available = (isinstance(task.get('id'), str) and bool(task['id'])
                       and isinstance(calls, list) and all(isinstance(call, dict) for call in calls))
    output = {'schemaVersion': 1, 'taskId': task.get('id'), 'agentId': task.get('agentId'),
              'status': task.get('status'), 'revision': task.get('revision'),
              'expectedOutput': [_clip(item, 1000) for item in criteria[:20]] if isinstance(criteria, list) else None,
              'raw': _clip(raw, _MAX_RAW) if isinstance(raw, str) else None,
              'rawTruncated': isinstance(raw, str) and len(raw) > _MAX_RAW,
              'outputFormat': 'raw', 'jsonDict': None,
              'artifactIds': copy.deepcopy(artifacts[:_MAX_ARTIFACTS]) if isinstance(artifacts, list) else None,
              'artifactIdsTotal': len(artifacts) if isinstance(artifacts, list) else None,
              'artifactIdsTruncated': len(artifacts) > _MAX_ARTIFACTS if isinstance(artifacts, list) else None,
              'outputContract': contract, 'outputValidation': output_validation,
              'reviewValidation': review_validation,
              'executionError': ({key:_clip(value, _MAX_DETAIL) for key,value in task['error'].items()
                                  if key in ('code','message','causeCode','retryable','phase')}
                                 if isinstance(task.get('error'),dict) else None),
              'observedWork': _observed_work(task,calls) if calls_available else None,
              'toolFailures': None, 'toolFailuresTotal': None, 'toolFailuresTruncated': None,
              'toolUncertainties': None, 'toolUncertaintiesTotal': None, 'toolUncertaintiesTruncated': None,
              'evidenceAvailability': {'raw': isinstance(raw, str), 'calls': calls_available,
                                       'outputValidation': output_validation is not None,
                                       'reviewValidation': review_validation is not None},
              'semanticQuality': 'unassessed'}
    if isinstance(criteria, list):
        output['expectedOutputTotal'] = len(criteria)
        output['expectedOutputTruncated'] = len(criteria) > 20 or any(isinstance(item, str) and len(item) > 1000 for item in criteria)
    passed = output_validation is not None and output_validation.get('status') == 'passed'
    kind = contract.get('kind') if isinstance(contract, dict) else None
    if kind == 'json' and passed and isinstance(raw, str) and len(raw) <= _MAX_RAW:
        try:
            output['jsonDict'] = _json_object(raw)
        except (ValueError, RecursionError):
            output['structuredIssue'] = 'Declared JSON output could not be safely reconstructed; raw evidence retained.'
        else:
            output['outputFormat'] = 'json'
    elif kind in ('artifact','delivery') and passed:
        output['outputFormat'] = kind
        if kind == 'delivery':
            output['deliveryId'] = (task.get('outputValidation') or {}).get('deliveryId')
    if calls_available:
        failures, uncertainties = [], []
        for call in calls:
            if call.get('taskId') != task.get('id') or not isinstance(call.get('tool'), str) or call['tool'] == 'llm.chat':
                continue
            status, result = call.get('status'), call.get('result')
            if status in _UNCERTAIN or isinstance(result, dict) and result.get('needsApproval'):
                uncertainties.append(_tool_record(call, 'outcome_uncertain'))
            elif status == 'denied':
                failures.append(_tool_record(call, 'permission_denied'))
            elif status == 'failed':
                failures.append(_tool_record(call, 'execution_failure'))
            elif status == 'success' and isinstance(result, dict) and result.get('ok') is False:
                record = _tool_record(call, 'reported_failure')
                record['statusConflictsWithResult'] = True
                failures.append(record)
        for key, records in (('toolFailures', failures), ('toolUncertainties', uncertainties)):
            output[key] = records[:_MAX_RECORDS]
            output[key + 'Total'] = len(records)
            output[key + 'Truncated'] = len(records) > _MAX_RECORDS
    return output


def _validation_for_context(value):
    if not isinstance(value, dict):
        return None
    scalar_keys = ('status', 'kind', 'semanticQuality', 'verdict', 'verified', 'assessed',
                   'targetRevision', 'reviewerTaskId')
    result = {key: copy.deepcopy(value[key]) for key in scalar_keys if key in value}
    if 'feedback' in value:
        result['feedback'] = _clip(value['feedback'], 320)
        result['feedbackTruncated'] = bool(value.get('feedbackTruncated')) or isinstance(value['feedback'], str) and len(value['feedback']) > 320
    for key in ('issues', 'checkedArtifactIds', 'matchedArtifactIds', 'evidenceCallIds'):
        if isinstance(value.get(key), list):
            items = value[key]
            result[key] = [_clip(item, 320) for item in items[:8]]
            result[key + 'Total'] = value.get(key + 'Total', len(items))
            result[key + 'Truncated'] = bool(value.get(key + 'Truncated')) or len(items) > 8 or any(isinstance(item, str) and len(item) > 320 for item in items)
    if isinstance(value.get('artifactHashes'), dict):
        hashes = value['artifactHashes']
        result['artifactHashes'] = dict(list(hashes.items())[:8])
        result['artifactHashesTotal'] = value.get('artifactHashesTotal', len(hashes))
        result['artifactHashesTruncated'] = bool(value.get('artifactHashesTruncated')) or len(hashes) > 8
    return result


def task_output_for_context(output):
    """Bounded prompt projection; dependencyResults.result already carries raw.

    Small jsonDict values remain exact. Large values are explicitly omitted, not
    clipped into fabricated structured facts. Original output objects are intact.
    """
    if not isinstance(output, dict):
        raise ValueError('A structured task output is required.')
    projected = copy.deepcopy(output)
    projected.pop('raw', None)
    projected['rawInDependencyResult'] = True
    for key in ('outputValidation', 'reviewValidation'):
        projected[key] = _validation_for_context(output.get(key))
    for key in ('toolFailures', 'toolUncertainties'):
        if isinstance(output.get(key), list):
            records=output[key]
            # Keep the original failure/rework reference and the latest actual
            # outcomes, rather than hide later URL-specific failures behind
            # four early search errors. The full bounded audit stays intact.
            projected[key] = copy.deepcopy(records if len(records)<=4 else [records[0],*records[-3:]])
            projected[key + 'Truncated'] = bool(output.get(key + 'Truncated')) or len(output[key]) > 4
            for record in projected[key]:
                if isinstance(record.get('error'), dict) and 'message' in record['error']:
                    message = record['error']['message']
                    record['error']['message'] = _clip(message, 320)
                    record['error']['messageTruncated'] = bool(record['error'].get('messageTruncated')) or isinstance(message, str) and len(message) > 320
    if isinstance(output.get('expectedOutput'), list):
        projected['expectedOutput'] = [_clip(item, 320) for item in output['expectedOutput'][:4]]
        projected['expectedOutputTruncated'] = bool(output.get('expectedOutputTruncated')) or len(output['expectedOutput']) > 4 or any(isinstance(item, str) and len(item) > 320 for item in output['expectedOutput'])
    if isinstance(output.get('artifactIds'), list):
        projected['artifactIds'] = copy.deepcopy(output['artifactIds'][:8])
        projected['artifactIdsTruncated'] = bool(output.get('artifactIdsTruncated')) or len(output['artifactIds']) > 8
    if output.get('jsonDict') is not None:
        raw_json = json.dumps(output['jsonDict'], ensure_ascii=False, allow_nan=False)
        if len(raw_json) > 4096:
            projected['jsonDict'] = None
            projected['jsonDictOmitted'] = True
            projected['jsonDictInDependencyResult'] = True
    return projected
