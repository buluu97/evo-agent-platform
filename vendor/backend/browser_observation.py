"""Small copies of observed page state; never instructions or acceptance evidence."""
import copy
import json


def failure_observation_view(source):
    if not isinstance(source, dict) or source.get('source') != 'actual failing-page DOM in this test':
        return None
    view = {'untrustedObservedState': True, 'valuesAreBoundedPreviews': True}
    for key in ('pageId', 'runtimeUTC', 'timezone'):
        if isinstance(source.get(key), str):
            view[key] = source[key][:80]
    for key in ('stepIndex', 'matchCount'):
        if type(source.get(key)) is int and source[key] >= 0:
            view[key] = source[key]
    messages = source.get('visibleMessages')
    if isinstance(messages, list):
        view['visibleMessages'] = [{key: row[key][:120 if key == 'text' else 40]
            for key in ('role', 'id', 'text') if isinstance(row.get(key), str)}
            for row in messages[:3] if isinstance(row, dict)]
        omitted = source.get('messagesOmitted', 0)
        view['messagesOmitted'] = (max(0, omitted) if type(omitted) is int else 0) + max(0, len(messages) - 3)
    target = source.get('target')
    if isinstance(target, dict):
        view['target'] = {key: target[key][:80] for key in ('tag', 'id', 'text') if isinstance(target.get(key), str)}
        cell = target.get('tableCell')
        if isinstance(cell, dict):
            value = {key: cell[key] for key in ('domCellIndex', 'domRowIndex') if type(cell.get(key)) is int}
            value.update({key: cell[key][:80] for key in ('rowFirstCellText', 'firstHeaderRowCellTextAtIndex') if isinstance(cell.get(key), str)})
            value['mapping'] = 'DOM index; spans unresolved'
            view['target']['tableCell'] = value
    controls = source.get('formValues')
    if isinstance(controls, list):
        view['visibleFormValues'] = []
        for control in controls[:10]:
            if not isinstance(control, dict) or control.get('type') in ('password', 'hidden', 'file'):
                continue
            name, value = control.get('id') or control.get('name') or control.get('type'), control.get('value')
            if isinstance(name, str) and isinstance(value, str):
                view['visibleFormValues'].append(name[:40] + '=' + value[:80])
        omitted = source.get('formValuesOmitted', 0)
        view['formValuesOmitted'] = max(0, omitted) if type(omitted) is int else 0
        view['formValuesOmitted'] += max(0, len(controls) - 10)
    # Explicitly lose optional fields as needed, never silently exceed context.
    while len(json.dumps(view, ensure_ascii=False)) > 1200 and view.get('visibleFormValues'):
        view['visibleFormValues'].pop()
        view['formValuesOmitted'] += 1
    while len(json.dumps(view, ensure_ascii=False)) > 1200 and len(view.get('visibleMessages', [])) > 1:
        view['visibleMessages'].pop()
        view['messagesOmitted'] += 1
    if len(json.dumps(view, ensure_ascii=False)) > 1200:
        view.pop('target', None)
        view['targetOmitted'] = True
    return copy.deepcopy(view)
