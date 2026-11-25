"""Independent acceptance, revision-aware metrics and persistence."""

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter


EXECUTION_STATES = {'done', 'partial', 'failed', 'timeout', 'crashed', 'cancelled'}
VERDICTS = ('pass', 'fail', 'unassessed')


class EvidenceError(ValueError):
    """Missing or mismatched acceptance evidence."""


def object_from_json(text):
    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError('Duplicate JSON object key.')
        return result

    def reject_constant(value):
        raise ValueError(f'Invalid JSON number {value}.')

    value=json.loads(text, object_pairs_hook=unique_object, parse_constant=reject_constant)
    # Reject valid-looking exponents that overflow Python float, as well as
    # escaped lone surrogates that cannot be stored as UTF-8 evidence.
    json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8')
    return value


def local_file(directory, name):
    root = directory.resolve()
    path = (root / name).resolve()
    if Path(name).is_absolute() or not path.is_relative_to(root):
        raise ValueError('File references must stay inside the manifest directory.')
    return path


@dataclass(frozen=True)
class Check:
    verdict: str
    reason: str
    tasks: tuple = ()
    evidence: tuple = ()
    error_code: str | None = None
    details: dict | None = None


class RunEvidence:
    """Resolve blobs only through an explicitly bound current task revision."""

    def __init__(self, run, load_artifact):
        self.run = run
        self.load_artifact = load_artifact
        self.tasks = {}
        self.history = []
        seen = set()
        for task in run.get('tasks', []):
            if task.get('planning'):
                continue
            task_id, revision = task['taskId'], task.get('revision')
            unknown=revision is None and task.get('revisionKnown') is False
            if not task_id or (not unknown and (type(revision) is not int or revision < 0)):
                raise ValueError('Each business task needs an ID and a nonnegative integer revision.')
            if (task_id, revision) in seen:
                raise ValueError(f'Duplicate revision {revision} for task {task_id}.')
            seen.add((task_id, revision))
            self.history.append(task)
            previous = self.tasks.get(task_id)
            if previous is None or unknown or (type(previous.get('revision')) is int and type(revision) is int and revision > previous['revision']):
                self.tasks[task_id] = task

    def task(self, ref, owner=None):
        if not isinstance(ref, dict):
            raise EvidenceError('A task and revision binding is required.')
        task = self.tasks.get(ref.get('taskId'))
        owner=self.run.get('roleBindings',{}).get(owner,owner)
        if (task is None or type(task.get('revision')) is not int or type(ref.get('revision')) is not int
                or task['revision'] != ref['revision']
                or (owner is not None and task.get('ownerAgentId') != owner)):
            raise EvidenceError('Evidence does not identify the current task revision and owner.')
        return task

    def artifact(self, ref, filename, owner=None):
        task = self.task(ref, owner)
        artifact_id = ref.get('artifactId')
        if not artifact_id or artifact_id not in task.get('artifactIds', []):
            raise EvidenceError('The artifact is absent from the current task revision.')
        try:
            item = self.load_artifact(artifact_id)
        except (OSError, ValueError) as error:
            raise EvidenceError(f'The artifact snapshot for {filename} could not be read.') from error
        if (not item or item.get('id') != artifact_id or item.get('runId') != self.run['runId']
                or item.get('taskId') != task['taskId']
                or item.get('ownerAgentId') != task.get('ownerAgentId')
                or item.get('name') != filename):
            raise EvidenceError(f'Invalid or missing artifact metadata for {filename}.')
        digest = sha256(item['content'].encode('utf-8')).hexdigest()
        if digest != item.get('sha256') or digest != ref.get('sha256'):
            raise EvidenceError(f'Artifact SHA-256 mismatch for {filename}.')
        return task['taskId'], item


def timing(run):
    started = run['startedAtMs']
    end = run.get('finishedAtMs')
    if end is None:
        end = run.get('terminatedAtMs')
    if end is None:
        end = run.get('deadlineAtMs')
    if type(started) is not int or type(end) is not int or started < 0 or end < started:
        raise ValueError('A run needs valid assignment and final output, termination or deadline timestamps.')

    intervals = []
    for wait in run.get('humanWaits', []):
        first, last = wait['startedAtMs'], wait.get('endedAtMs')
        last = end if last is None else last
        if type(first) is not int or type(last) is not int or last < first:
            raise ValueError('Invalid human wait interval.')
        if max(first, started) < min(last, end):
            intervals.append((max(first, started), min(last, end)))
    total_wait, cursor = 0, started
    for first, last in sorted(intervals):
        total_wait += max(0, last - max(first, cursor))
        cursor = max(cursor, last)
    return end - started, total_wait


def evaluate_case(case, run, load_artifact, verifier):
    if run['caseId'] != case['caseId'] or run['status'] not in EXECUTION_STATES:
        raise ValueError('Case binding or execution state is invalid.')
    elapsed, human_wait = timing(run)
    evidence = RunEvidence(run, load_artifact)
    start = perf_counter()
    checks = verifier.evaluate(evidence)
    if tuple(checks) != verifier.check_ids:
        raise ValueError('The verifier must return every declared check exactly once and in order.')

    check_results = []
    by_task = {task_id: [] for task_id in evidence.tasks}
    for check_id, result in checks.items():
        if result.verdict not in VERDICTS or any(task_id not in by_task for task_id in result.tasks):
            raise ValueError(f'Invalid verdict or task binding for check {check_id}.')
        check = {'checkId': check_id, 'verdict': result.verdict, 'reason': result.reason,
                 'taskRefs': [{'taskId': task_id, 'revision': evidence.tasks[task_id]['revision']}
                              for task_id in result.tasks],
                 'evidenceIds': list(result.evidence), 'errorCode': result.error_code, 'details': result.details}
        check_results.append(check)
        for task_id in result.tasks:
            by_task[task_id].append(check)

    tasks, feedback = [], []
    for task_id, task in evidence.tasks.items():
        supporting = by_task[task_id]
        verdicts = {check['verdict'] for check in supporting}
        verdict = 'fail' if 'fail' in verdicts else 'pass' if verdicts == {'pass'} else 'unassessed'
        reason = ' '.join(check['reason'] for check in supporting) or 'No independent evidence was bound to this task.'
        evidence_ids = sorted({item for check in supporting for item in check['evidenceIds']})
        task_result = {
            'taskId': task_id, 'revision': task['revision'], 'ownerAgentId': task.get('ownerAgentId'),
            'executionStatus': task['status'], 'verdict': verdict, 'reason': reason,
            'checkIds': [check['checkId'] for check in supporting], 'evidenceIds': evidence_ids,
            'memoryIds': task.get('memoryIds', []), 'errorCode': task.get('errorCode'),
            'errorReason': task.get('errorReason'),
        }
        tasks.append(task_result)
        if evidence_ids and verdict in ('pass', 'fail') and task_result['memoryIds'] and task_result['ownerAgentId']:
            stage = 'final' if task.get('closed') and run['status'] in ('done', 'partial', 'failed') else 'review'
            feedback.append({**task_result, 'runId': run['runId'], 'stage': stage})

    verdicts = {check['verdict'] for check in check_results}
    goal = ('fail' if run['status'] not in ('done', 'partial') or 'fail' in verdicts
            else 'pass' if verdicts == {'pass'} else 'unassessed')
    return {
        'caseId': case['caseId'], 'runId': run['runId'], 'source': run['source'],
        'executionStatus': run['status'], 'goalVerdict': goal,
        'errorCode': run.get('errorCode'), 'errorReason': run.get('errorReason'),
        'elapsedMs': elapsed, 'humanWaitMs': human_wait,
        'verifierMs': round((perf_counter() - start) * 1000, 3), 'verifierType': case['verifier'],
        'checks': check_results, 'tasks': tasks, 'feedback': feedback,
        'taskRevisionHistory': [
            {key: task[key] for key in ('taskId', 'revision', 'status', 'artifactIds', 'reviewEvidence') if key in task}
            for task in evidence.history],
        'memory': {'enabled': run.get('memoryEnabled'), 'condition': run.get('memoryCondition'),
                   'boundary': run.get('memoryBoundary'), 'changes': run.get('memoryChanges', [])},
        'teamId':run.get('teamId'),'roleBindings':run.get('roleBindings',{}),
        'exportSha256':run.get('exportSha256'),'observedAt':run.get('observedAt'),
        'observationStatus':run.get('observationStatus'),
        'continuationHistory':run.get('continuationHistory',[]),
        'attemptHistory':{task['taskId']:task.get('attemptHistory',[]) for task in evidence.tasks.values()},
    }


def summarize(results):
    def rate(verdicts):
        counts = Counter(verdicts)
        total = sum(counts.values())
        return {'passed': counts['pass'], 'failed': counts['fail'], 'unassessed': counts['unassessed'],
                'total': total, 'rate': counts['pass'] / total if total else None}

    def measured(field):
        values=[row.get(field) for row in results if type(row.get(field)) in (int,float)]
        return sum(values) if values else None
    return {
        'overallGoals': rate(row['goalVerdict'] for row in results),
        'businessTasks': rate(task['verdict'] for row in results for task in row['tasks']),
        'fixedChecks': rate(check['verdict'] for row in results for check in row['checks']),
        'executionStates': dict(Counter(row['executionStatus'] for row in results)),
        'failureModes': dict(Counter(
            [row['errorCode'] for row in results if row['errorCode']]
            + [task['errorCode'] for row in results for task in row['tasks'] if task['errorCode']]
            + [check['errorCode'] for row in results for check in row['checks'] if check['errorCode']])),
        'elapsedMs': measured('elapsedMs'),
        'humanWaitMs': measured('humanWaitMs'),
        'verifierMs': round(measured('verifierMs'),3) if measured('verifierMs') is not None else None,
        'missingTimingCases':sum(row.get('elapsedMs') is None for row in results),
    }


def persist_result(repository, suite_id, batch_id, result):
    goal = result['goalVerdict']
    status = {'pass': 'passed', 'fail': 'failed', 'unassessed': 'unassessed'}[goal]
    repository.save_evaluation({
        **result, 'id': f'{batch_id}-{result["caseId"]}', 'suiteId': suite_id,
        'batchId': batch_id, 'taskId': result['runId'], 'task': result['caseId'],
        'status': status, 'verified': goal == 'pass',
        'memoryEnabled': (result.get('memory') or {}).get('enabled'),
        'elapsedSec': result['elapsedMs'] / 1000 if type(result.get('elapsedMs')) in (int,float) else None,
        'verifier': {'type': result['verifierType'], 'passed': goal == 'pass'} if goal != 'unassessed' else None,
    })
