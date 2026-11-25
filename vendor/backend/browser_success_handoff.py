"""One decision prompt for repeated successful current-source reviewer tests.

No tool is executed and no review is accepted here. The normal review validator
must still check the original full receipts and every acceptance requirement.
"""
import copy
import hashlib

from .browser_failure_recovery import _key
from .browser_stagnation import _successful_checks, _valid_args
from .review_versions import browser_source_feedback
from .sandbox import ToolError, read_text


def _arguments(args):
    if not _valid_args(args):
        return None
    value = copy.deepcopy(args)
    # The engine clips this field to the remaining wall time before execution.
    value.pop('timeout', None)
    value.setdefault('resources', {})
    value.setdefault('storage', {})
    return _key(value)


def browser_success_handoff(run, task, action, agents, scope, snapshot_for, matches, artifacts, tools):
    """Return nonexecuting guidance once per current reviewer/source/test binding."""
    if (action.get('type') != 'tool' or action.get('name') != 'code.test'
            or 'review' not in agents.get(task.get('agentId'), {}).get('capabilities', [])
            or not isinstance(scope, dict) or type(task.get('revision')) is not int
            or task.get('_pendingTool') or task.get('_uncertainCalls')):
        return None
    proposed = _arguments(action.get('args'))
    if proposed is None or browser_source_feedback(run, task, action['args'], artifacts, tools, agents) is not None:
        return None
    calls = [c for c in run.get('calls', []) if c.get('tool') == 'code.test'
             and c.get('taskId') == task['id'] and c.get('agentId') == task['agentId']
             and type(c.get('revision')) is int and c['revision'] == task['revision']
             and c.get('runId', run['id']) == run['id']][-2:]
    if len(calls) != 2 or len({c.get('id') for c in calls}) != 2 or any(not c.get('id') for c in calls):
        return None
    observations = [_successful_checks(c) for c in calls]
    if (any(o is None for o in observations)
            or observations[0][0] != observations[1][0]
            or any(_arguments(c.get('args')) != proposed for c in calls)):
        return None
    # A source preflight deliberately defers unreadable paths to the gateway.
    # Reusing historical receipts additionally requires readable current bytes,
    # including every extra selected resource, to match the original manifest.
    current = {}
    for path in {action['args']['path'], *action['args'].get('resources', {}).values()}:
        try:
            base, relative = tools._location(task['agentId'], run['id'], path)
            current[path] = hashlib.sha256(read_text(base, relative).encode('utf-8')).hexdigest()
        except (ToolError, OSError):
            return None
    if current != observations[0][0]:
        return None
    targets = []
    for row in scope.get('targets', []):
        target = next((t for t in run.get('steps', []) if t.get('id') == row.get('taskId')), None)
        snapshot = snapshot_for(run, target) if target is not None else None
        if (target is not None and target.get('status') in ('done', 'partial') and isinstance(snapshot, dict)
                and all(c.get('reviewTargets', {}).get(target['id']) == snapshot
                        and matches(run, target, c) for c in calls)):
            targets.append({'taskId': target['id'], 'targetSnapshot': copy.deepcopy(snapshot)})
    if not targets:
        return None
    binding = _key({'runId': run['id'], 'taskId': task['id'], 'agentId': task['agentId'],
                    'revision': task.get('revision'), 'arguments': proposed, 'targets': targets,
                    'sourceManifest': observations[0][0]})
    signature = hashlib.sha256(binding.encode('utf-8')).hexdigest()
    if signature in task.get('_browserSuccessHandoffs', []):
        return None
    return {'ok': False, 'notExecuted': True, 'actionExecuted': False,
            'code': 'successful_browser_evidence_already_recorded', 'bindingSha256': signature,
            'error': {'code': 'successful_browser_evidence_already_recorded', 'retryable': False,
                      'message': 'Identical successful browser evidence is already recorded; choose a justified next action.'},
            'runId': run['id'], 'taskId': task['id'], 'revision': task.get('revision'),
            'evidenceCallIds': [c['id'] for c in calls], 'targets': targets,
            'instruction': 'These two actual successful tests already have full receipts in the host ledger. '
                'The compressed model view is not the acceptance receipt; review.evidenceCallIds resolves the originals. '
                'Inspect the requirements and submit an explicit review with these IDs and current targetSnapshot if sufficient. '
                'Otherwise test genuinely missing coverage, request evidence-based rework, or give an honest final partial. '
                'No review decision or permission is granted. This guidance is issued once for this binding; '
                'another identical proposal reaches the original loop stop. No extra time or execution is granted.'}
