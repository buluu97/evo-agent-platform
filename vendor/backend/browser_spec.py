"""Validation for the declarative, server-owned offline browser harness."""
import json
import re
from .sandbox import ToolError, relative_parts

_ACTIONS = {
    'click': {'selector'}, 'fill': {'selector', 'value'}, 'select': {'selector', 'value'},
    'press': {'selector', 'key'}, 'reload': set(), 'assert_text': {'selector', 'text'},
    'assert_count': {'selector', 'count'}, 'assert_value': {'selector', 'value'},
    'assert_visible': {'selector'}, 'assert_storage': {'key', 'value'},
    'assert_attribute': {'selector', 'name', 'value'},
    'capture_canvas': {'selector', 'key'}, 'assert_canvas_changed': {'selector', 'key'},
    'assert_storage_missing': {'key'}, 'storage_write_denied': {'enabled'}, 'storage_read_denied': {'enabled'}, 'new_page': {'pageId'},
    'respond_dialog': {'dialogType', 'accept'},
}


def validate_browser_args(args):
    if args.get('code') is not None or args.get('args'):
        raise ToolError('parameter', 'Browser mode accepts an HTML path and declarative checks, not source code or CLI args.')
    path = args.get('path')
    relative_parts(path)
    if not path.lower().endswith('.html'):
        raise ToolError('parameter', 'Browser mode requires a relative HTML entry path.')
    checks = args.get('checks')
    if not isinstance(checks, list) or not 1 <= len(checks) <= 60:
        raise ToolError('parameter', 'Browser checks must contain 1–60 declarative steps.')
    assertions = 0
    pages = {'main'}
    for step in checks:
        if not isinstance(step, dict) or not isinstance(step.get('action'), str) or step['action'] not in _ACTIONS:
            raise ToolError('parameter', 'Unsupported browser check action.')
        action = step['action']
        required = _ACTIONS[action]
        optional = {'pageId'} | ({'exact'} if action == 'assert_text' else set()) | ({'key'} if action in ('storage_write_denied','storage_read_denied') else set()) | ({'value'} if action == 'respond_dialog' else set())
        if set(step) - {'action'} - required - optional or not required.issubset(step):
            raise ToolError('parameter', f'Invalid fields for browser action {action}.')
        for key in required - {'count', 'enabled', 'accept'}:
            if not isinstance(step[key], str) or '\0' in step[key] or len(step[key]) > (65536 if key == 'value' else 2000):
                raise ToolError('parameter', f'Browser {key} must be a bounded string.')
            if key in ('selector', 'key') and not step[key]:
                raise ToolError('parameter', f'Browser {key} cannot be empty.')
        if action == 'assert_count' and (isinstance(step['count'], bool) or not isinstance(step['count'], int) or not 0 <= step['count'] <= 10000):
            raise ToolError('parameter', 'Browser assertion count must be an integer between 0 and 10000.')
        if 'exact' in step and not isinstance(step['exact'], bool):
            raise ToolError('parameter', 'Browser exact must be a boolean.')
        page_id = step.get('pageId', 'main')
        if not isinstance(page_id, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}', page_id):
            raise ToolError('parameter', 'pageId must be a short identifier, never a URL or path.')
        if action == 'new_page':
            if page_id in pages or len(pages) >= 3:
                raise ToolError('parameter', 'new_page must create a distinct page; at most three pages share one test context.')
            pages.add(page_id)
        elif page_id not in pages:
            raise ToolError('parameter', 'Use new_page before referring to that pageId.')
        if action == 'respond_dialog':
            if step['dialogType'] not in ('alert', 'confirm', 'prompt') or type(step['accept']) is not bool:
                raise ToolError('parameter', 'respond_dialog requires alert/confirm/prompt and a boolean accept.')
            if 'value' in step and (step['dialogType'] != 'prompt' or not step['accept'] or
                    not isinstance(step['value'], str) or '\0' in step['value'] or len(step['value']) > 2000):
                raise ToolError('parameter', 'Only an accepted prompt can supply a bounded response value.')
        if action in ('storage_write_denied','storage_read_denied'):
            if type(step['enabled']) is not bool:
                raise ToolError('parameter', action+'.enabled must be a boolean.')
            if 'key' in step and (not isinstance(step['key'], str) or not step['key'] or '\0' in step['key'] or len(step['key']) > 2000):
                raise ToolError('parameter', 'Fault key must be a nonempty bounded storage key.')
        assertions += action.startswith('assert_')
    if assertions < 1:
        raise ToolError('parameter', 'Browser testing requires at least one real assertion.')
    for key, max_items, max_value in [('storage', 20, 65536), ('resources', 39, 240)]:
        mapping = args.get(key, {})
        if not isinstance(mapping, dict) or len(mapping) > max_items:
            raise ToolError('parameter', f'Browser {key} must be a bounded string mapping.')
        for name, value in mapping.items():
            if not isinstance(name, str) or not name or len(name) > 240 or '\0' in name or not isinstance(value, str) or len(value) > max_value or '\0' in value:
                raise ToolError('parameter', f'Invalid browser {key} entry.')
            if key == 'resources':
                relative_parts(name)
                relative_parts(value)
        if len(json.dumps(mapping, ensure_ascii=False).encode()) > (65536 if key == 'storage' else 32768):
            raise ToolError('parameter', f'Browser {key} exceeds its size limit.')
    return path, checks


def read_fault_coverage(report,checks):
    """Recompute read-fault evidence in the host, independently of report.ok."""
    if not isinstance(report,dict) or not isinstance(checks,list) or any(not isinstance(s,dict) for s in checks):return False
    expected=[]
    for index,step in enumerate(checks):
        if step.get('action')=='storage_read_denied' and step.get('enabled') is True:
            page=step.get('pageId','main')
            restored=next((i+1 for i,s in enumerate(checks[index+1:],index+1)
                           if s.get('action')=='storage_read_denied' and s.get('pageId','main')==page),None)
            expected.append((index+1,restored,step))
    epochs=report.get('readFaults',[])
    if not isinstance(epochs,list) or len(epochs)!=len(expected):return False
    if expected and report.get('executed') is not True:return False
    seen=set()
    for epoch,(enabled_at,restored_at,step) in zip(epochs,expected):
        if not isinstance(epoch,dict):return False
        fault_id=epoch.get('id');keys=epoch.get('blockedKeys');operations=epoch.get('operations')
        if (not isinstance(fault_id,str) or not 1<=len(fault_id)<=80 or fault_id in seen
                or epoch.get('pageId')!=step.get('pageId','main') or epoch.get('key')!=step.get('key')
                or type(epoch.get('enabledAtStep')) is not int or epoch['enabledAtStep']!=enabled_at
                or epoch.get('restoredAtStep')!=restored_at
                or type(epoch.get('blockedReads')) is not int or epoch['blockedReads']<=0
                or type(epoch.get('blockedHarnessReads')) is not int or epoch['blockedHarnessReads']<0
                or not isinstance(keys,list) or not 1<=len(keys)<=50 or any(not isinstance(k,str) or len(k)>2000 for k in keys)
                or len(set(keys))!=len(keys)
                or not isinstance(operations,list) or not 1<=len(operations)<=50 or len(operations)>epoch['blockedReads']):return False
        seen.add(fault_id)
        for operation in operations:
            if (not isinstance(operation,dict) or operation.get('operation')!='getItem'
                    or not isinstance(operation.get('key'),str) or operation['key'] not in keys
                    or step.get('key') is not None and operation['key']!=step['key']):return False
    return True


def dialog_coverage(report, checks):
    """Bind every explicitly requested response to an actually handled dialog."""
    expected = [step for step in checks if step['action'] == 'respond_dialog']
    responses = report.get('dialogResponses', [])
    return (isinstance(responses, list) and len(responses) == len(expected) and
        all(isinstance(response, dict) and response.get('pageId') == step.get('pageId', 'main')
            and response.get('dialogType') == step['dialogType']
            and response.get('accept') is step['accept']
            and response.get('value') == step.get('value')
            and response.get('observedType') == step['dialogType']
            and response.get('handled') is True and not response.get('error')
            for response, step in zip(responses, expected)))
