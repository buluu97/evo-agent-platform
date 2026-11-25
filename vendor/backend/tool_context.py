"""Bounded browser-result model views, never replacements for full tool evidence.

Hashes are SHA256 of canonical JSON, NOT the bytes of reportPath. No report is
read here. Long values are labelled previews; paths/source hashes are retained
whole or explicitly omitted. The caller must keep run.calls unmodified.
"""
import copy
import hashlib
import json
import re
from .browser_observation import failure_observation_view

_MARKER = 'browser-tool-context-v1'
_SHA = re.compile(r'^[a-f0-9]{64}$')


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _view(value, width):
    if isinstance(value, str):
        if len(value) <= width:
            return value
        preview = {'preview': value[:width], 'originalCharacters': len(value),
                   'sha256': hashlib.sha256(value.encode('utf-8')).hexdigest(), 'truncated': True}
        return preview if _size(preview) < _size(value) else value
    if value is None or type(value) in (bool, int, float):
        return value
    if _size(value) <= width:
        return copy.deepcopy(value)
    return {'valueOmitted': True, 'canonicalJSONCharacters': _size(value), 'sha256': _hash(value)}


def _reference(value):
    if (isinstance(value, dict) and isinstance(value.get('path'), str) and value['path']
            and isinstance(value.get('sha256'), str) and _SHA.fullmatch(value['sha256'])):
        return {key: value[key] for key in ('path', 'sha256')}
    return None


def _detail(item, width, index):
    keys = ('action', 'pageId', 'selector', 'key', 'name', 'expected', 'actual',
            'expectedDifferentFrom', 'passed', 'ok', 'error')
    return {'sourceIndex': index, **{key: _view(item[key], width) for key in keys if key in item}}


def _update_omissions(projection):
    browser, totals = projection['browser'], projection['sourceTotals']
    kept_assertions = len(browser.get('failedAssertions', []))
    kept_steps = int('failedStep' in browser) + sum(bool(row.get('alsoFailedStep')) for row in browser.get('failedAssertions', []))
    projection['omissions'] = {
        'assertions': None if totals['assertions'] is None else totals['assertions'] - kept_assertions,
        'failedAssertions': None if totals['failedAssertions'] is None else totals['failedAssertions'] - kept_assertions,
        'steps': None if totals['steps'] is None else max(0, totals['steps'] - kept_steps),
        'testedFiles': None if totals['testedFiles'] is None else totals['testedFiles'] - len(browser.get('testedFiles', [])),
        'console': totals['console'], 'stdoutCharacters': totals['stdoutCharacters'],
        'stderrCharacters': totals['stderrCharacters'],
        'pageErrors': None if totals['pageErrors'] is None else totals['pageErrors'] - len(browser.get('pageErrors', [])),
        'httpErrors': None if totals['httpErrors'] is None else totals['httpErrors'] - len(browser.get('httpErrors', [])),
        'blockedRequests': None if totals['blockedRequests'] is None else totals['blockedRequests'] - len(browser.get('blockedRequests', [])),
        'sourceManifest': totals['sourceManifest'], 'screenshotMetadata': totals['screenshotMetadata'],
        'faults': totals['faults'], 'dialogResponses': totals['dialogResponses'], 'pages': totals['pages'],
        'failureObservation': totals.get('failureObservation', 0) - int('failureObservation' in browser),
    }
    if 'readFaults' in totals:
        projection['omissions']['readFaults'] = totals['readFaults']
    projection['sourceBindingsComplete'] = bool(
        projection.get('sourceBindingsValid') is True and not browser.get('entryOmitted')
        and projection['omissions']['testedFiles'] == 0)


def _fit(projection, limit):
    """Keep the primary failure; remove whole references with explicit counts."""
    _update_omissions(projection)
    if _size(projection) <= limit:
        return projection
    browser = projection['browser']
    # A model can recover exact full values through reportPath/full call audit.
    for width in (96, 32):
        for item in [projection, browser, *browser.get('failedAssertions', []),
                     *([browser['failedStep']] if 'failedStep' in browser else [])]:
            for key in ('error', 'selector', 'expected', 'actual', 'key', 'name'):
                if key in item:
                    value = item[key]
                    if key == 'selector' and isinstance(value, str) and len(value) <= 256:
                        continue
                    if isinstance(value, dict) and value.get('truncated') is True and isinstance(value.get('preview'), str):
                        value['preview'] = value['preview'][:width]
                    else:
                        item[key] = _view(value, width)
        if _size(projection) <= limit:
            return projection
    while browser.get('testedFiles') and _size(projection) > limit:
        browser['testedFiles'].pop()
        _update_omissions(projection)
    for key in ('pageErrors', 'httpErrors', 'blockedRequests'):
        while browser.get(key) and _size(projection) > limit:
            browser[key].pop(); _update_omissions(projection)
    while len(browser.get('failedAssertions', [])) > 1 and _size(projection) > limit:
        browser['failedAssertions'].pop(); _update_omissions(projection)
    if browser.get('failedAssertions') and 'failedStep' in browser and _size(projection) > limit:
        browser.pop('failedStep'); _update_omissions(projection)
    if _size(projection) > limit and 'entry' in browser:
        browser.pop('entry'); browser['entryOmitted'] = True; _update_omissions(projection)
    if _size(projection) > limit and 'failureObservation' in browser:
        browser.pop('failureObservation'); _update_omissions(projection)
    if _size(projection) > limit and 'reportPath' in browser:
        browser.pop('reportPath'); browser['reportPathOmitted'] = True
    if _size(projection) > limit:
        # Explicitly unavailable failure detail is preferable to broken JSON or
        # a pretend complete assertion/report. Provenance remains intact.
        browser.pop('failedAssertions', None); browser.pop('failedStep', None)
        browser.pop('error', None); projection.pop('error', None)
        projection['failureDetailsOmitted'] = True; _update_omissions(projection)
    if _size(projection) > limit:
        raise ValueError('The projection budget cannot hold even the source/evidence indexes.')
    return projection


def compact_browser_result(result, max_chars=2600):
    """Return a detached bounded view, or None for non-browser/invalid JSON.

    Executed/ok are copied from the original; neither is inferred from arguments
    or missing values. Formal acceptance must always use the full tool result.
    readonlyBrowserSource is separate from reported execution: use it when a
    model-view projection is used to invalidate/retain private source caches.
    """
    if type(max_chars) is not int or max_chars < 1600:
        raise ValueError('Browser model projection needs at least 1600 characters.')
    if not isinstance(result, dict) or result.get('runtime') != 'browser':
        return None
    try:
        _encoded(result)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
    if result.get('modelViewOnly') == _MARKER:
        # Preserve the original full-result provenance over repeated compression.
        if not isinstance(result.get('sourceResultSHA256'), str) or not _SHA.fullmatch(result['sourceResultSHA256']):
            return None
        try:
            return _fit(copy.deepcopy(result), max_chars)
        except (KeyError, TypeError, ValueError):
            return None
    source = result.get('browser')
    if not isinstance(source, dict):
        source = {}
    list_fields = ('assertions', 'steps', 'testedFiles', 'console', 'pageErrors', 'httpErrors', 'blockedRequests',
                   'sourceManifest', 'faults', 'dialogResponses', 'pages')
    totals = {key: len(source[key]) if isinstance(source.get(key), list) else None for key in list_fields}
    totals.update(stdoutCharacters=len(result['stdout']) if isinstance(result.get('stdout'), str) else None,
                  stderrCharacters=len(result['stderr']) if isinstance(result.get('stderr'), str) else None,
                  screenshotMetadata=int(isinstance(source.get('screenshot'), dict)) if 'screenshot' in source else None)
    totals['failureObservation'] = int(isinstance(source.get('failureObservation'), dict))
    if 'readFaults' in source:
        totals['readFaults'] = len(source['readFaults']) if isinstance(source['readFaults'], list) else None
    failures = [(i, item) for i, item in enumerate(source.get('assertions') or [])
                if isinstance(item, dict) and item.get('passed') is False] if isinstance(source.get('assertions'), list) else []
    totals['failedAssertions'] = len(failures) if totals['assertions'] is not None else None
    projection = {key: copy.deepcopy(result[key]) for key in
                  ('ok', 'runtime', 'test', 'exitCode', 'timedOut', 'cancelled', 'deadlineExceeded', 'outcomeUnknown', 'callId') if key in result}
    projection.update(modelViewOnly=_MARKER, fullResultRequiredForAcceptance=True,
        sourceResultSHA256=_hash(result), sourceResultCharacters=_size(result),
        sourceBrowserSHA256=_hash(source), hashEncoding='canonical-json-utf8-not-report-file-bytes',
        sourceTotals=totals, evidenceComplete=False)
    if result.get('ok') is True and source.get('ok') is True and result.get('callId'):
        projection['receiptAccess'] = ('review.evidenceCallIds resolves this callId to the full host receipt. '
            'Rerunning adds no detail. Review current sources and remaining criteria before deciding.')
    browser = {key: copy.deepcopy(source[key]) for key in
               ('engine', 'executed', 'ok', 'assertionCount', 'assertionsPassed', 'faultCoverageComplete', 'readFaultCoverageComplete', 'dialogCoverageComplete', 'reportPath') if key in source}
    projection['browser'] = browser
    observed = failure_observation_view(source.get('failureObservation'))
    if observed is not None:
        browser['failureObservation'] = observed
    for container, original in ((projection, result), (browser, source)):
        if 'error' in original:
            container['error'] = _view(original['error'], 192)
    browser['failedAssertions'] = [_detail(item, 192, index) for index, item in failures[:2]]
    steps = source.get('steps') if isinstance(source.get('steps'), list) else []
    failed_step = next(((i, item) for i, item in reversed(list(enumerate(steps)))
                        if isinstance(item, dict) and item.get('ok') is False), None)
    if failed_step:
        index, item = failed_step
        matching = next((row for row in browser['failedAssertions'] if all(row.get(key) == item.get(key)
                         for key in ('action', 'pageId', 'selector', 'expected', 'actual'))), None)
        if matching is not None:
            matching['alsoFailedStep'] = True
            if 'error' in item:
                matching['error'] = _view(item['error'], 192)
        else:
            browser['failedStep'] = _detail(item, 192, index)
        if isinstance(item.get('error'), str) and len(item['error']) > 192:
            # The actionable failure owns the detailed error. The two outer
            # copies are normally the same stack/log prefixed once more.
            projection['duplicateErrorFieldsOmitted'] = sum('error' in container for container in (projection, browser))
            projection.pop('error', None); browser.pop('error', None)
    elif source.get('error') == result.get('error') and 'error' in browser:
        browser.pop('error'); projection['duplicateErrorFieldsOmitted'] = 1
    entry = _reference(source.get('entry'))
    original_files = source.get('testedFiles')
    files = [_reference(item) for item in original_files] if isinstance(original_files, list) else []
    valid_files = isinstance(original_files, list) and all(item is not None for item in files)
    browser['testedFiles'] = [item for item in files if item is not None]
    if entry is not None:
        browser['entry'] = entry
    else:
        browser['entryOmitted'] = True
    manifest = source.get('sourceManifest')
    manifest_refs = [_reference(item) for item in manifest] if isinstance(manifest, list) else []
    manifest_valid = isinstance(manifest, list) and all(item is not None for item in manifest_refs)
    projection['sourceBindingsValid'] = bool(entry and valid_files and entry in files)
    projection['readonlyBrowserSource'] = bool(source.get('executed') is True
        and projection['sourceBindingsValid'] and manifest_valid and entry in manifest_refs
        and all(item in manifest_refs for item in files)
        and not any(result.get(key) for key in ('deadlineExceeded', 'outcomeUnknown', 'cancelled')))
    for field in ('pageErrors', 'httpErrors', 'blockedRequests'):
        if isinstance(source.get(field), list) and source[field]:
            browser[field] = [_view(source[field][0], 192)]
    return _fit(projection, max_chars)
