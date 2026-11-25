"""Opt-in real-model + public web + Docker four-role integration smoke test.

Runs only a synthetic programming task in a fresh explicit data directory.
Never approves prompts, configures Git, copies credentials, or uses test doubles.
An external failure remains a failure in the report. Not a benchmark success rate.
"""
import argparse
import json
import sys
import time
from pathlib import Path


GOAL = '''Use all four roles for this small task. Research: fetch the public CPython README at https://raw.githubusercontent.com/python/cpython/3.11/README.rst and report one sourced fact. Executor: implement sumsquares.py with sum_squares(n) returning the sum of squares of integers from 1 to n inclusive (n=0 returns 0); test n=0,1,3,10 against 0,1,14,385, then publish the source artifact. Reviewer: read the actual authorized source artifact, run your own independent code.test in Docker against it, and submit a review of the Executor task using your successful test callId. Imported artifacts are at /imports/<artifact-id>/<name> inside Docker. Coordinator: finish with a summary of actual source, execution, independent review and any limitations, depending on all three specialists. Keep this to four tasks and concise outputs. No Git operations or human approval should be needed; do not overwrite existing files. Do not invent tool results.'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--config-root', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--deadline', type=int, default=180)
    parser.add_argument('--model-timeout', type=int, default=20)
    args = parser.parse_args()
    if not args.live or not 20 <= args.deadline <= 600:
        parser.error('--live and a deadline of 20..600 seconds are required')
    if args.data_dir.exists():
        parser.error('--data-dir must be a new empty location')
    sys.path.insert(0, str(args.source.resolve()))
    from backend.agents import AGENTS
    from backend.config import Config
    from backend.engine import Platform
    from backend.tools import ToolGateway
    config = Config(args.config_root)
    args.data_dir.mkdir(parents=True, mode=0o700)
    config.data_dir = args.data_dir.resolve()
    config.model_timeout = args.model_timeout
    config.max_completion_tokens = 800
    config.max_retries = 0
    config.max_agent_turns = 12
    config.max_model_calls = 32
    config.max_run_seconds = args.deadline
    config.max_run_tokens = 90000
    config.git_remote = config.git_token = ''
    gateway = ToolGateway(config)
    capability = gateway.capabilities()
    if not capability.get('dockerAvailable') or not capability.get('dockerImageReady'):
        parser.error('Real Docker and its configured image must be ready; no fallback')
    app = Platform(config, tools=gateway)
    started = time.monotonic()
    last = None
    reason = None
    try:
        app.action({'action':'send','channelId':'group','mode':'task','participants':list(AGENTS), 'text':GOAL})
        while time.monotonic()-started < args.deadline:
            idle = app.wait_for_idle(.5)
            state = app.snapshot()
            run = state['sessions']['group']['run']
            progress = {'status':run['status'], 'modelCalls':run['modelCalls'],
                        'tasks':[{k:t.get(k) for k in ('agentId','status')} for t in run['steps']]}
            if progress != last:
                print(json.dumps(progress,ensure_ascii=False), flush=True)
                last = progress
            if any(a.get('status')=='pending' for a in run.get('approvals',[])):
                reason = 'The benchmark needs human input; no automatic approval was given.'
                break
            if idle:
                break
        else:
            reason = 'Wall-clock deadline reached.'
        run = app.snapshot()['sessions']['group']['run']
        tasks = [t for t in run['steps'] if not t.get('_planning')]
        calls = run['calls']
        reviews = [t.get('validation',{}) for t in tasks if t.get('agentId')=='executor']
        checks = {
            'runDone':run['status']=='done',
            'fourRolesDone':{t.get('agentId') for t in tasks if t['status']=='done'} == set(AGENTS),
            'realWebSuccess':any(c['tool']=='web.fetch' and c['status']=='success' and c.get('result',{}).get('ok') for c in calls),
            'executorDockerSuccess':any(c['agentId']=='executor' and c['tool'] in ('code.run','code.test') and c['status']=='success' and c.get('result',{}).get('exitCode')==0 for c in calls),
            'independentVerifiedReview':bool(reviews) and all(v.get('verified') and v.get('verdict')=='pass' and v.get('evidenceCallIds') for v in reviews),
            'coordinatorSynthesis':any(t.get('agentId')=='coordinator' and t.get('finalSummary') and t['status']=='done' for t in tasks)}
        report = {'kind':'live HKGAI/DeepSeek + real web + real Docker; synthetic task',
                  'provider':config.provider, 'model':config.model, 'goal':GOAL,
                  'passed':all(checks.values()), 'checks':checks, 'stopReason':reason,
                  'seconds':round(time.monotonic()-started,3),
                  'settings':{'maxCompletionTokens':800,'modelTimeout':args.model_timeout,
                              'maxModelCalls':32,'maxAgentTurns':12,'maxRunTokens':90000,'maxRetries':0},
                  'run':run}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(config.scrub(report),ensure_ascii=False,indent=2))
        print(json.dumps({k:report[k] for k in ('passed','checks','stopReason','seconds')},ensure_ascii=False),flush=True)
        return 0 if report['passed'] else 1
    finally:
        app.shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
