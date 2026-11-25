"""Real web receipts from one task's current working messages, never a ledger.

The caller supplies parsed action/result records from that same actor/task view.
This module does no I/O, retrieval, summarization, or instruction parsing inside
source text. A projected index explicitly has no body; a SHA is not a body.
"""
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit


_SHA = re.compile(r'^[a-f0-9]{64}$')
_MARKER = 'web_fetch_marker_v1'
_PREFIXES = ('动作的真实结果：', '恢复工具结果：')


def normalize_result_url(value):
    """Syntax-only canonicalization; keep path/query case, order, and escaping."""
    if not isinstance(value, str) or not value or len(value) > 2048 or '\\' in value or any(ord(c) < 33 for c in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ('http', 'https') or not parts.hostname or parts.username is not None or parts.password is not None:
            return None
        host = parts.hostname.rstrip('.').encode('idna').decode('ascii').lower()
        port = parts.port
        scheme = parts.scheme.lower()
        authority = '[' + host + ']' if ':' in host else host
        if port is not None and port != (443 if scheme == 'https' else 80):
            authority += ':' + str(port)
        return urlunsplit((scheme, authority, parts.path or '/', parts.query, ''))
    except (ValueError, UnicodeError):
        return None


def web_record_identity(record):
    """Recognize a real paired fetch, or our explicitly body-less projection.

    A root sourceReferences entry, unpaired user text, failed result, or model
    instruction saying 'fetched' cannot become a cache entry here.
    """
    if not isinstance(record, dict) or type(record.get('actionIndex')) is not int or type(record.get('index')) is not int:
        return None
    if record['actionIndex'] < 2 or record['actionIndex'] >= record['index']:
        return None
    action, value = record.get('action'), record.get('value')
    if (not isinstance(action, dict) or action.get('type') != 'tool' or action.get('name') != 'web.fetch'
            or not isinstance(action.get('args'), dict) or not normalize_result_url(action['args'].get('url'))
            or not isinstance(value, dict) or value.get('ok') is not True or value.get('needsApproval')
            or record.get('prefix') not in _PREFIXES
            or not isinstance(record.get('message'), dict) or record['message'].get('role') != 'user'):
        return None
    url = normalize_result_url(value.get('url'))
    call_id = value.get('callId')
    if not url or not isinstance(call_id, str) or not call_id or len(call_id) > 120:
        return None
    body = value.get('content')
    if isinstance(body, str) and body.strip() and value.get('contentOmitted') is not True:
        try:
            raw = body.encode('utf-8')
        except UnicodeEncodeError:
            return None
        digest, characters, byte_count, present = hashlib.sha256(raw).hexdigest(), len(body), len(raw), True
    elif (value.get('cacheKind') == _MARKER and value.get('sourceType') == 'web.fetch'
            and value.get('contentOmitted') is True and 'content' not in value
            and value.get('sourceCallId') == call_id
            and value.get('requestedUrl') == action['args']['url']
            and isinstance(value.get('contentSha256'), str) and _SHA.fullmatch(value['contentSha256'])
            and type(value.get('contentCharacters')) is int and value['contentCharacters'] > 0
            and type(value.get('contentUtf8Bytes')) is int and value['contentUtf8Bytes'] > 0):
        digest, characters, byte_count, present = value['contentSha256'], value['contentCharacters'], value['contentUtf8Bytes'], False
    else:
        return None
    return {'url': url, 'contentSha256': digest, 'sourceCallId': call_id,
            'bodyPresent': present, 'contentCharacters': characters, 'contentUtf8Bytes': byte_count,
            'truncated': value.get('truncated') is True}


def collect_web_records(records):
    """Latest successful version per final URL; same-body rereads deduplicate.

    Preserve a still-visible real body when a later same-version marker has no
    body. A new SHA, even an omitted version, invalidates the old URL body.
    Returns copied record wrappers, newest first, with web/fullWeb/identity flags.
    """
    selected = {}
    valid = []
    for record in records:
        identity = web_record_identity(record)
        if identity:
            valid.append((record, identity))
    for record, identity in sorted(valid, key=lambda pair: pair[0]['index']):
        previous = selected.get(identity['url'])
        if (previous is not None and previous['webIdentity']['contentSha256'] == identity['contentSha256']
                and previous['webIdentity']['bodyPresent'] and not identity['bodyPresent']):
            continue
        selected[identity['url']] = {**record, 'web': True, 'fullWeb': identity['bodyPresent'], 'webIdentity': identity}
    return sorted(selected.values(), key=lambda item: item['index'], reverse=True)


def full_web_view(record, messages):
    """Original action/result pair only; never fabricate a body from an index."""
    identity = web_record_identity(record)
    if not identity:
        return None
    action_index, result_index = record['actionIndex'], record['index']
    if action_index >= len(messages) or result_index >= len(messages):
        return None
    action, result = messages[action_index], messages[result_index]
    if (action.get('role') != 'assistant' or result.get('role') != 'user'
            or result.get('content') != record['message'].get('content')):
        return None
    return [action, result]


def project_web_record(record, messages):
    """Whole valid JSON marker, paired with its original actual fetch action."""
    identity = web_record_identity(record)
    full = full_web_view(record, messages)
    if not identity or full is None:
        return None
    if not identity['bodyPresent']:
        return full  # Repeated compaction keeps the same explicit omission.
    value = record['value']
    title = value.get('title') if isinstance(value.get('title'), str) else ''
    marker = {'ok': True, 'callId': identity['sourceCallId'], 'sourceCallId': identity['sourceCallId'],
        'sourceType': 'web.fetch', 'cacheKind': _MARKER, 'url': identity['url'],
        'requestedUrl': record['action']['args']['url'], 'title': title[:300],
        'contentSha256': identity['contentSha256'], 'contentCharacters': identity['contentCharacters'],
        'contentUtf8Bytes': identity['contentUtf8Bytes'], 'contentOmitted': True,
        'truncated': identity['truncated'], 'contextBudgetReason': 'context_budget',
        'note': 'Previously fetched text is NOT present in this context. URL/SHA are only an index, not readable evidence.'}
    snapshot = value.get('sourceSnapshot')
    if (isinstance(snapshot, dict) and snapshot.get('sha256') == identity['contentSha256']
            and snapshot.get('url') == value.get('url')
            and isinstance(snapshot.get('path'), str)
            and re.fullmatch(r'imports/web-source-[a-f0-9]{64}/body\.txt', snapshot['path'])
            and snapshot.get('scope') == 'same_agent_same_run_readonly'):
        # Carry a real receipt's read pointer, not the omitted text. The regular
        # file.read tool must actually retrieve any passage used later.
        marker['sourceSnapshot'] = dict(snapshot)
    if isinstance(value.get('sourceSnapshotUnavailable'), str):
        marker['sourceSnapshotUnavailable'] = value['sourceSnapshotUnavailable']
    if len(title) > 300:
        marker['titleTruncated'] = True
    if isinstance(value.get('links'), list):
        marker.update(linksOmitted=True, linkCount=value.get('linkCount', len(value['links'])),
                      linksAreUnfetched=True,
                      linksSha256=hashlib.sha256(json.dumps(value['links'], ensure_ascii=False, sort_keys=True,
                                                            separators=(',', ':')).encode('utf-8')).hexdigest())
    return [full[0], {'role': 'user', 'content': record['prefix'] + json.dumps(marker, ensure_ascii=False, separators=(',', ':'))}]
