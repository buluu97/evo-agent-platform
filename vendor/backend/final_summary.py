"""Keep explicitly text-only final synthesis from recreating upstream work.

The caller supplies its current gated task. This pure admission check neither
executes an action nor establishes run ownership, permissions or acceptance.
Legacy summaries and summaries with artifact/delivery contracts retain their
existing behavior. Unknown actions/tools remain the original protocol's job.
"""

_WORK_TOOLS = frozenset({
    'file.write', 'file.patch', 'file.delete',
    'code.run', 'code.test', 'git.commit', 'git.push',
})
_WORK_ACTIONS = frozenset({
    'publish_artifact', 'publish_artifacts', 'package_delivery',
    'delegate', 'grant_artifact', 'review', 'message', 'reject',
})


def final_summary_feedback(task, action):
    """Return nonexecuting feedback for new work in a running text summary.

    A read/final returning None is not authorization or validation; all original
    ACL, phase, schema, deadline and final-output checks still apply. In
    particular code.test is execution, not a read-only inspection: Python/JS
    can change files, and fresh tests belong to the declared validation task.
    """
    if (not isinstance(task, dict) or not isinstance(action, dict)
            or task.get('status') != 'running' or task.get('finalSummary') is not True):
        return None
    contract = task.get('outputContract')
    if not isinstance(contract, dict) or contract.get('kind') != 'text':
        return None
    kind = action.get('type')
    if not isinstance(kind, str):
        return None
    name = action.get('name') if kind == 'tool' else None
    if not (kind in _WORK_ACTIONS
            or kind == 'tool' and isinstance(name, str) and name in _WORK_TOOLS):
        return None
    return {
        'ok': False, 'actionExecuted': False, 'notExecuted': True,
        'error': {
            'code': 'final_summary_read_only', 'retryable': False,
            'message': 'This final synthesis has an explicit text contract; new product work was not executed.',
        },
        'taskId': task.get('id'),
        'rejectedAction': {'type': kind, **({'name': name} if kind == 'tool' else {})},
        'instruction': (
            'Use existing authorized artifacts, actual source/test/review evidence, delivery IDs and exact gaps '
            'to provide your own final.text. Substantive writing, implementation, testing, publication, packaging '
            'and new review decisions belong to the declared upstream producer/reviewer/delivery tasks; '
            'do not rebuild or redelegate their missing work from this final synthesis. '
            'An identical-byte write or a new copy is still a write, not needed for this text contract. '
            'Existing read-only tools and final remain subject to their original schema, ACL, phase and deadline checks. '
            'This feedback creates no final response, output, review, permission, time or budget. '
            'Choose done only if the unchanged original criteria and required product acceptance are actually met; '
            'otherwise explicitly return final with partial/failed and the remaining gaps.'),
    }
