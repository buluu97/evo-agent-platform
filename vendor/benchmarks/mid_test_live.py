"""Run the supplied three-team Mid Test without model/tool substitutes.

The external Markdown pack supplies role prompts and task text, never executable
test setup. Each team keeps one Platform instance across its two tasks. A local
UI is available for genuine human replies; the runner never approves actions or
answers clarification questions. Execution status is not independent quality.
"""
import argparse
import copy
import hashlib
import json
import re
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

SCENARIOS = [('software', 'scenario_1_software_engineering'),
             ('newsroom', 'scenario_2_newsroom'),
             ('fund', 'scenario_3_securities_fund')]


def load_suite(root):
    root = Path(root)
    result = []
    for team, prefix in SCENARIOS:
        folders = sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith(prefix))
        if len(folders) != 1:
            raise ValueError(f'Expected exactly one {prefix} folder')
        folder = folders[0]
        role_text = (folder / '01_role_initialization.md').read_text(encoding='utf-8')
        prompts = re.findall(r'```text\s*\n(.*?)\n```', role_text, re.S)
        ids = re.findall(r'^\|\s*`([^`]+)`\s*\|', role_text, re.M)
        if len(ids) != 4 or len(prompts) != 4 or len(set(ids)) != 4:
            raise ValueError(f'{team}: expected four distinct role IDs and prompts')
        tasks = []
        for filename in ('02_task_1.md', '03_task_2.md'):
            raw = (folder / filename).read_text(encoding='utf-8')
            blocks = re.findall(r'```text\s*\n(.*?)\n```', raw, re.S)
            if len(blocks) != 1:
                raise ValueError(f'{filename}: expected exactly one task text block')
            tasks.append({'id': f'{team}-{len(tasks)+1}', 'title': raw.splitlines()[0].lstrip('# '),
                          'text': blocks[0], 'source': str((folder / filename).resolve()),
                          'sha256': hashlib.sha256(raw.encode()).hexdigest()})
        result.append({'team': team, 'roles': dict(zip(ids, prompts)), 'tasks': tasks})
    return result


def evidence_checks(run, expected_roles):
    tasks = [t for t in run.get('steps', []) if not t.get('_planning') and t.get('kind') != 'planning']
    calls = run.get('calls', [])
    artifacts = run.get('artifacts', [])
    return {'planned': bool(tasks),
            'allRolesParticipated': set(expected_roles).issubset({t.get('agentId') for t in tasks if t.get('turns', 0)}),
            'allRolesDeclaredDone': set(expected_roles).issubset({t.get('agentId') for t in tasks if t.get('status') == 'done'}),
            'publishedArtifactsTotal': len(artifacts),
            'newPublishedArtifacts': sum(not a.get('sourceRunId') for a in artifacts),
            'inheritedArtifacts': sum(bool(a.get('sourceRunId')) for a in artifacts),
            'realWebCallsSucceeded': sum(c.get('status') == 'success' and c.get('tool') in ('web.search', 'web.fetch') for c in calls),
            'realCodeCallsSucceeded': sum(c.get('status') == 'success' and c.get('tool') in ('code.run', 'code.test') and isinstance(c.get('result'), dict) and c['result'].get('exitCode') == 0 for c in calls),
            'declaredDoneSubtasks': sum(t.get('status') == 'done' for t in tasks),
            'plannedSubtasks': len(tasks),
            'qualityVerdict': 'unassessed',
            'note': 'Participation, process exit and agent done status are evidence, not task quality or official scores.'}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


class CaseObservationError(RuntimeError):
    """Carry the exact owned run ID into fault cleanup, never infer it by title."""
    def __init__(self, message, run_id=None):
        super().__init__(message)
        self.run_id = run_id


class _RunReplaced(Exception):
    pass


def _public_record(value):
    if isinstance(value, dict):
        return {key: _public_record(item) for key, item in value.items() if not key.startswith('_')}
    if isinstance(value, list):
        return [_public_record(item) for item in value]
    return value


def _original_record(app, run_id, fallback):
    """Recover only the bound run; never read a replacement run as its evidence."""
    record = app.repo.get_run(run_id)
    if isinstance(record, dict) and record.get('id') == run_id:
        return copy.deepcopy(record), 'repository'
    return copy.deepcopy(fallback), 'last_observed_snapshot'


def _followup_blocked(previous_result, current):
    return (bool(previous_result and (previous_result.get('observationStatus') == 'external_run_changed'
                                    or previous_result.get('executionStatus') == 'runner_error'))
            or current.get('status') in ('paused', 'waiting_human', 'waiting_children', 'interrupted', 'running'))


def run_case(app, task, output, deadline, human_wait, emit=print):
    start, expected_run_id = time.monotonic(), None
    try:
        sent = app.action({'action': 'send', 'channelId': 'group', 'mode': 'task',
                           'participants': list(app.agents), 'text': task['text']})
        initial = sent['sessions']['group']['run']
        if not isinstance(initial, dict) or not isinstance(initial.get('id'), str) or not initial['id']:
            raise ValueError('send returned no identifiable run')
        expected_run_id = initial['id']
        return _run_bound_case(app, task, output, deadline, human_wait, emit,
                               start, expected_run_id, initial)
    except Exception as error:
        raise CaseObservationError(str(error), expected_run_id) from error


def _run_bound_case(app, task, output, deadline, human_wait, emit, start, expected_run_id, initial):
    last = None
    last_owned = copy.deepcopy(initial)
    pending_since = None
    reason = None
    observation_status = 'observed'
    evidence_source = 'current_owned_run'

    def observe():
        state = app.snapshot()
        current = state['sessions']['group'].get('run')
        if not isinstance(current, dict) or current.get('id') != expected_run_id:
            raise _RunReplaced()
        return state, current

    def control(action):
        try:
            app.action({'action': action, 'channelId': 'group', 'runId': expected_run_id})
        except Exception:
            # A UI action may replace the run between observation and control.
            # Platform's runId gate must reject the stale request; do not retry
            # it without the binding or operate on the replacement run.
            observe()
            raise

    try:
        while True:
            state, run = observe()
            last_owned = copy.deepcopy(run)
            approvals = [a for a in run.get('approvals', []) if a.get('status') == 'pending']
            progress = {'case': task['id'], 'runId': expected_run_id, 'status': run['status'],
                        'modelCalls': run.get('modelCalls'),
                        'tasks': [{k: t.get(k) for k in ('id', 'agentId', 'title', 'status')} for t in run['steps']],
                        'pendingHuman': [{k: a.get(k) for k in ('id', 'agentId', 'type', 'title', 'description', 'choices', 'args')} for a in approvals]}
            if progress != last:
                emit(json.dumps(app.config.scrub(progress), ensure_ascii=False), flush=True)
                last = progress
                write_json(output / (task['id'] + '-progress.json'), app.config.scrub(progress))
            if run['status'] in ('done', 'partial', 'failed', 'cancelled'):
                app.wait_for_idle(app.config.model_timeout + 2)
                break
            if approvals:
                pending_since = pending_since or time.monotonic()
                if time.monotonic() - pending_since > human_wait:
                    reason = 'waiting_human: no response within the observation window; no automatic answer/approval'
                    control('pause')
                    break
            else:
                pending_since = None
            if time.monotonic() - start > deadline:
                reason = 'wall_clock_deadline'
                control('cancel')
                app.wait_for_idle(app.config.model_timeout + 2)
                break
            time.sleep(.5)
        observe()
        # Bind public evidence, private measurement and transcript in one lock.
        # Otherwise the UI could replace the run between two snapshot reads.
        with app.lock:
            current = app.state['sessions']['group'].get('run')
            if not isinstance(current, dict) or current.get('id') != expected_run_id:
                raise _RunReplaced()
            measured = copy.deepcopy(current)
            group_transcript = copy.deepcopy(app.state['sessions']['group']['messages'])
    except _RunReplaced:
        reason = 'external_run_changed: original run preserved; replacement user run was not controlled or counted'
        observation_status = 'external_run_changed'
        measured, evidence_source = _original_record(app, expected_run_id, last_owned)
        group_transcript = copy.deepcopy(measured.get('groupHistory', []) + measured.get('conversation', []))
    run = _public_record(measured)
    report = {'case': task['id'], 'title': task['title'], 'prompt': task['text'], 'sourceSha256': task['sha256'],
              'runId': expected_run_id, 'observationStatus': observation_status, 'evidenceSource': evidence_source,
              'executionStatus': run['status'], 'stopReason': reason,
              'elapsedSec': round(time.monotonic()-start, 3),
              'checks': evidence_checks(measured, list(app.agents)),
              'planningTaskIds': [t['id'] for t in measured.get('steps', []) if t.get('_planning')], 'run': run,
              'groupTranscript': _public_record(group_transcript)}
    write_json(output / (task['id'] + '.json'), app.config.scrub(report))
    artifacts_dir = output / 'artifacts' / task['id']
    for artifact in run.get('artifacts', []):
        # Names originate in the registry, but revalidate export boundaries.
        name, aid = artifact.get('name', ''), artifact.get('id', '')
        if Path(name).name != name or not re.fullmatch(r'[A-Za-z0-9_-]+', aid):
            continue
        target = artifacts_dir / aid / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(artifact.get('content', ''), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--suite-root', type=Path, required=True)
    parser.add_argument('--config-root', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--time-only', action='store_true', help='Use the platform wall-deadline profile without total call, turn or token caps.')
    parser.add_argument('--scenario', choices=['all', 'software', 'newsroom', 'fund'], default='all')
    parser.add_argument('--port', type=int, default=8081)
    parser.add_argument('--deadline', type=int, default=600)
    parser.add_argument('--human-wait', type=int, default=180)
    parser.add_argument('--model-timeout', type=int, default=60)
    parser.add_argument('--max-calls', type=int, default=48)
    parser.add_argument('--max-retries', type=int, default=1)
    parser.add_argument('--completion-tokens', type=int, default=6000)
    parser.add_argument('--planning-tokens', type=int, default=3000)
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required for real course API requests')
    if args.data_dir.exists():
        parser.error('Use a fresh --data-dir; existing runs are never overwritten')
    if args.output.exists():
        parser.error('Use a fresh --output directory; earlier evidence must not be mixed in')
    if not (30 <= args.deadline <= 1800 and 30 <= args.human_wait <= 900 and 5 <= args.model_timeout <= 180 and 4 <= args.max_calls <= 60):
        parser.error('Out-of-range time or request budget')
    if not (0 <= args.max_retries <= 3 and 256 <= args.completion_tokens <= 8000 and 256 <= args.planning_tokens <= args.completion_tokens):
        parser.error('Out-of-range retry or output budget')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend.config import Config
    from backend.server import TeamHub, make_handler
    from backend.teams import get_team_agents
    suite = load_suite(args.suite_root)
    if args.scenario != 'all':
        suite = [s for s in suite if s['team'] == args.scenario]
    for scenario in suite:
        configured = get_team_agents(scenario['team'])
        if {k: v.get('rolePrompt', '').strip() for k,v in configured.items()} != {k:v.strip() for k,v in scenario['roles'].items()}:
            parser.error(f"Role prompts differ from the supplied pack for {scenario['team']}")
    config = Config(args.config_root)
    config.root = Path(__file__).resolve().parents[1]
    args.data_dir.mkdir(parents=True, mode=0o700)
    config.data_dir = args.data_dir.resolve()
    config.model_timeout = args.model_timeout
    config.max_completion_tokens = args.completion_tokens
    config.planning_max_tokens = args.planning_tokens
    config.max_model_calls = args.max_calls
    config.max_agent_turns = 16
    config.max_run_seconds = args.deadline
    config.max_run_tokens = 200000
    config.max_retries = args.max_retries
    if args.time_only:
        config.apply_time_only_profile()
        config.max_run_seconds = args.deadline
    config.approval_ttl = max(900, args.human_wait + 60)
    # No remote publishing is authorized by a benchmark runner on its own.
    config.git_remote = config.git_token = ''
    hub = TeamHub(config)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(hub))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report = {'kind': 'Mid Test real-model observation, not official grading',
              'provider': config.provider, 'configuredModel': config.model,
              'settings': {'deadlinePerTask': args.deadline, 'humanWait': args.human_wait,
                           'transportMode': getattr(config, 'transport_mode', 'standard'),
                           'requestedThinkingMode': getattr(config, 'thinking_mode', 'default'),
                           'modelTimeout': args.model_timeout, 'executionProfile': getattr(config, 'execution_profile', 'bounded'),
                           'maxModelCallsPerTask': config.max_model_calls,
                           'maxRunTokens': config.max_run_tokens,
                           'maxCompletionTokens': args.completion_tokens, 'planningMaxTokens': args.planning_tokens,
                           'maxAgentTurns': config.max_agent_turns, 'maxRetries': args.max_retries},
              'sourceFilesSha256': {str(path.relative_to(config.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                                    for path in sorted([*(config.root / 'backend').glob('*.py'), Path(__file__).resolve()])},
              'cases': [], 'qualityVerdict': 'unassessed'}
    print(json.dumps({'ui':f'http://127.0.0.1:{args.port}/#workspace','output':str(args.output)}, ensure_ascii=False), flush=True)
    try:
        for scenario in suite:
            app = hub.get(scenario['team'])
            previous_result = None
            for task in scenario['tasks']:
                current = app.snapshot()['sessions']['group'].get('run') or {}
                if _followup_blocked(previous_result, current):
                    result = {'case':task['id'], 'title':task['title'], 'executionStatus':'not_run',
                              'stopReason':'Previous task needs human input, observation changed, or runner faulted; current user session preserved',
                              'checks':{'qualityVerdict':'unassessed'}}
                    write_json(args.output / (task['id']+'.json'), result)
                else:
                    try:
                        result = run_case(app, task, args.output, args.deadline, args.human_wait)
                    except Exception as error:
                        # Persist harness/transport/export faults as faults; do not
                        # lose the rest of the suite or borrow an older run's work.
                        result = {'case':task['id'], 'title':task['title'], 'prompt':task['text'],
                                  'executionStatus':'runner_error', 'stopReason':config.scrub(str(error)),
                                  'checks':{'qualityVerdict':'unassessed'}}
                        try:
                            owned_id = getattr(error, 'run_id', None)
                            if owned_id:
                                try:
                                    current_run = app.snapshot()['sessions']['group'].get('run') or {}
                                    if current_run.get('id') == owned_id and current_run.get('status') in ('running','paused','waiting_human','waiting_children','interrupted'):
                                        app.action({'action':'cancel','channelId':'group','runId':owned_id})
                                        app.wait_for_idle(config.model_timeout + 2)
                                except Exception as cleanup_error:
                                    result['cleanupError'] = config.scrub(str(cleanup_error))
                                # Even a stale cleanup request must not prevent
                                # exporting the original run's durable evidence.
                                original, source = _original_record(app, owned_id, {'id':owned_id, 'status':'unknown'})
                                result.update(runId=owned_id,run=_public_record(original),evidenceSource=source)
                        except Exception as cleanup_error:
                            result['cleanupError'] = config.scrub(str(cleanup_error))
                        write_json(args.output / (task['id']+'.json'), result)
                previous_result = result
                report['cases'].append({k:v for k,v in result.items() if k not in ('run','groupTranscript')})
                write_json(args.output / 'suite-results.json', config.scrub(report))
                print(json.dumps({'caseFinished':result['case'], 'status':result['executionStatus'], 'reason':result.get('stopReason')}, ensure_ascii=False), flush=True)
        return 0
    finally:
        server.shutdown()
        server.server_close()
        hub.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
