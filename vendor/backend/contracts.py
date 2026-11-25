"""Optional deterministic output guardrails, never a semantic-quality verdict.

Inspired by CrewAI Task expected_output and function-based guardrails:
https://docs.crewai.com/en/concepts/tasks
This is an independent standard-library implementation, not CrewAI integration.
"""
import hashlib
import json
from pathlib import PurePosixPath
import re


def _protocol_error(message):
    # runtime imports this normalizer while validating plans. Keep the import
    # lazy so both modules can share ProtocolError without an import cycle.
    from .runtime import ProtocolError
    return ProtocolError(message)


_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,12}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_MAX_OUTPUT_BYTES = 256 * 1024
_MAX_ARTIFACTS = 40
_MAX_KEYS = 32
_MAX_EXTENSIONS = 16


def normalize_output_contract(value):
    """Validate an optional contract; absent contracts preserve legacy text.

    artifact.extensions filters matching output files, rather than forbidding
    useful ancillary files. An omitted/empty extension list accepts any supported
    nonempty published artifact. minCount counts distinct published file paths,
    not repeated registrations or revisions of the same file. No executable path or arbitrary verifier enters
    the contract. JSON requiredKeys refer only to top-level object keys.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > 4:
        raise _protocol_error('outputContract must be a bounded JSON object.')
    kind = value.get('kind')
    if not isinstance(kind, str) or kind not in ('text', 'artifact', 'json', 'delivery'):
        raise _protocol_error('outputContract.kind must be text, artifact, json or delivery.')
    allowed = {'text': {'kind'}, 'artifact': {'kind', 'extensions', 'minCount', 'candidateDelivery'},
               'json': {'kind', 'requiredKeys'}, 'delivery': {'kind'}}[kind]
    if set(value) - allowed:
        raise _protocol_error('outputContract contains unsupported fields for its kind.')
    normalized = {'kind': kind}
    if kind == 'artifact':
        extensions = value.get('extensions', [])
        if (not isinstance(extensions, list) or len(extensions) > _MAX_EXTENSIONS
                or any(not isinstance(extension, str) or not _EXTENSION.fullmatch(extension)
                       for extension in extensions)):
            raise _protocol_error('artifact extensions must be at most 16 file suffixes such as .html; paths, names and wildcards are forbidden.')
        canonical = [extension.lower() for extension in extensions]
        if len(set(canonical)) != len(canonical):
            raise _protocol_error('artifact extensions must be distinct.')
        minimum = value.get('minCount', 1)
        if isinstance(minimum, bool) or not isinstance(minimum, int) or not 1 <= minimum <= _MAX_ARTIFACTS:
            raise _protocol_error('artifact minCount must be an integer from 1 to 40.')
        normalized.update(extensions=canonical, minCount=minimum)
        if 'candidateDelivery' in value:
            if type(value['candidateDelivery']) is not bool:
                raise _protocol_error('artifact candidateDelivery must be a boolean.')
            normalized['candidateDelivery'] = value['candidateDelivery']
    elif kind == 'json':
        keys = value.get('requiredKeys', [])
        if (not isinstance(keys, list) or len(keys) > _MAX_KEYS
                or any(not isinstance(key, str) or not key.strip() or len(key) > 120
                       or any(ord(char) < 32 or ord(char) == 127 for char in key) for key in keys)):
            raise _protocol_error('json requiredKeys must contain at most 32 nonempty bounded object keys.')
        if len(set(keys)) != len(keys):
            raise _protocol_error('json requiredKeys must be distinct.')
        normalized['requiredKeys'] = list(keys)
    return normalized


def _result(status, kind, issues=(), checked=(), matched=()):
    return {'status': status, 'kind': kind, 'issues': list(issues)[:40],
            'checkedArtifactIds': list(checked), 'matchedArtifactIds': list(matched),
            'semanticQuality': 'unassessed'}


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON keys are ambiguous.')
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError('Nonfinite constants are not JSON.')


def validate_task_output(task, run, artifact_store, final_text):
    """Check a final result against the actual current-task output evidence.

    `passed` covers only the declared deterministic format/evidence checks.
    Text without an explicit contract is compatible and `not_configured`.
    ArtifactStore.get supplies immutable published content; live workspace paths
    and model assertions are never read as evidence. No model/network calls occur.
    """
    if not isinstance(task, dict) or not isinstance(run, dict):
        return _result('failed', 'text', ['Task and run records are required.'])
    contract = normalize_output_contract(task.get('outputContract'))
    kind = contract['kind'] if contract else 'text'
    if not isinstance(final_text, str) or not final_text.strip():
        return _result('failed', kind, ['Final text must be nonempty.'])
    try:
        raw_text = final_text.encode('utf-8')
    except UnicodeEncodeError:
        return _result('failed', kind, ['Final text must be valid UTF-8.'])
    if len(raw_text) > _MAX_OUTPUT_BYTES or '\0' in final_text:
        return _result('failed', kind, ['Final text exceeds 256 KiB or contains NUL.'])
    if contract is None:
        return _result('not_configured', kind)
    if kind == 'text':
        return _result('passed', kind)
    if kind == 'delivery':
        from .delivery import validate_delivery
        deliveries = [d for d in run.get('deliveries', []) if d.get('status') == 'packaged' and d.get('taskId') == task.get('id')
                      and d.get('agentId') == task.get('agentId')]
        if not deliveries:
            return _result('failed', kind, ['Create a real package_delivery before final; prose is not a downloadable product.'])
        try:
            snapshots = validate_delivery(deliveries[-1], run, artifact_store)
        except (ValueError, KeyError, TypeError) as error:
            return _result('failed', kind, [str(error)])
        result = _result('passed', kind, checked=[a['id'] for a in snapshots], matched=[a['id'] for a in snapshots])
        result['deliveryId'] = deliveries[-1]['id']
        return result
    if kind == 'json':
        try:
            parsed = json.loads(final_text, object_pairs_hook=_strict_object,
                                parse_constant=_reject_constant)
            # A valid-looking exponent can overflow Python float, and escaped
            # lone surrogates cannot be persisted as UTF-8. Match the structured
            # handoff and SQLite serialization requirements before marking pass.
            json.dumps(parsed, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (ValueError, RecursionError):
            return _result('failed', kind, ['final.text must itself be one finite UTF-8-serializable JSON object, without prose or Markdown fences.'])
        if not isinstance(parsed, dict):
            return _result('failed', kind, ['final.text must be a JSON object.'])
        missing = [key for key in contract['requiredKeys'] if key not in parsed]
        return _result('failed' if missing else 'passed', kind,
                       ['Missing required JSON key: ' + key for key in missing])

    output_ids = task.get('outputs', [])
    metadata = run.get('artifacts', [])
    if (not isinstance(output_ids, list) or len(output_ids) > _MAX_ARTIFACTS
            or any(not isinstance(artifact_id, str) or not _IDENTIFIER.fullmatch(artifact_id) for artifact_id in output_ids)):
        return _result('failed', kind, ['task.outputs must contain at most 40 safe artifact IDs.'])
    if len(set(output_ids)) != len(output_ids):
        return _result('failed', kind, ['Duplicate output IDs cannot count as distinct artifacts.'])
    if not isinstance(metadata, list) or len(metadata) > _MAX_ARTIFACTS or any(not isinstance(item, dict) for item in metadata):
        return _result('failed', kind, ['Run artifact metadata is invalid or exceeds its quota.'])
    by_id = {}
    for item in metadata:
        artifact_id = item.get('id')
        if not isinstance(artifact_id, str) or artifact_id in by_id:
            return _result('failed', kind, ['Run artifact metadata has invalid or duplicate IDs.'])
        by_id[artifact_id] = item
    if not callable(getattr(artifact_store, 'get', None)):
        return _result('failed', kind, ['A real artifact snapshot store is required.'])
    if (not isinstance(task.get('id'), str) or not isinstance(task.get('agentId'), str)
            or not isinstance(run.get('id'), str)):
        return _result('failed', kind, ['Current task, owner and run IDs are required.'])
    issues, checked, matched_by_path = [], [], {}
    for artifact_id in output_ids:
        item = by_id.get(artifact_id)
        if item is None:
            issues.append('Output is not registered in the current run: ' + artifact_id)
            continue
        try:
            snapshot = artifact_store.get(artifact_id)
        except Exception:
            issues.append('Published snapshot could not be verified: ' + artifact_id)
            continue
        if not isinstance(snapshot, dict):
            issues.append('Published snapshot is invalid: ' + artifact_id)
            continue
        if any(snapshot.get(key) != item.get(key) for key in ('id', 'name', 'ownerAgentId', 'runId', 'taskId', 'path', 'sha256', 'size', 'mimeType')):
            issues.append('Run metadata does not match the published snapshot: ' + artifact_id)
            continue
        if (snapshot.get('id') != artifact_id or snapshot.get('runId') != run['id']
                or snapshot.get('taskId') != task['id'] or snapshot.get('ownerAgentId') != task['agentId']
                or any(snapshot.get(key) or item.get(key) for key in
                       ('carriedForward', 'inheritedReadOnly', 'sourceRunId', 'sourceArtifactId'))):
            issues.append('Only this task owner\'s newly published outputs count; inherited or other-task artifacts do not: ' + artifact_id)
            continue
        content, name, path = snapshot.get('content'), snapshot.get('name'), snapshot.get('path')
        if not isinstance(content, str) or not content.strip() or '\0' in content:
            issues.append('Published output must contain nonempty UTF-8 text: ' + artifact_id)
            continue
        try:
            raw = content.encode('utf-8')
        except UnicodeEncodeError:
            issues.append('Published output is not valid UTF-8: ' + artifact_id)
            continue
        if len(raw) > _MAX_OUTPUT_BYTES or snapshot.get('size') != len(raw) or snapshot.get('sha256') != hashlib.sha256(raw).hexdigest():
            issues.append('Published output SHA256/size verification failed: ' + artifact_id)
            continue
        if not isinstance(name, str) or not name or name.startswith('.') or '\\' in name or PurePosixPath(name).name != name:
            issues.append('Published output has an invalid file name: ' + artifact_id)
            continue
        if not isinstance(path, str) or not path or PurePosixPath(path).name != name:
            issues.append('Published output has an invalid registered path: ' + artifact_id)
            continue
        checked.append(artifact_id)
        if not contract['extensions'] or PurePosixPath(name).suffix.lower() in contract['extensions']:
            # A task may publish multiple immutable versions of one file. Those
            # preserve evidence, but must not inflate the required file count.
            matched_by_path[path] = artifact_id
    matched = list(matched_by_path.values())
    if len(matched) < contract['minCount']:
        issues.append(f"Need at least {contract['minCount']} distinct verified current-task artifact file path(s) matching extensions; found {len(matched)}.")
    candidate = None
    if contract.get('candidateDelivery') and not issues:
        from .candidate_delivery import current_candidate
        candidate = current_candidate(task, run, artifact_store, matched, contract['minCount'])
        if candidate is None:
            issues.append('Prepare a current candidate ZIP from explicit published IDs before producer handoff: use publish_artifacts.delivery or package_delivery(candidate:true). A candidate is not independent acceptance or formal delivery.')
    result = _result('failed' if issues else 'passed', kind, issues, checked, matched)
    if candidate is not None:
        result['candidateDeliveryId'] = candidate['id']
    return result
