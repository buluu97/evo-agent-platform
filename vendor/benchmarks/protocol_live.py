"""Opt-in paired live-model decision benchmark; never executes model tools.

Uses synthetic fixtures, the same model/settings for both checkouts, alternating
AB/BA order, and up to 3 decisions including real schema discovery. Network
failures and invalid outputs count as failures, not discarded observations.
This measures protocol decisions, not full task completion or model training.
"""
import argparse
import ast
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


CASES = Path(__file__).with_name('protocol_cases.json')


def acceptable(action, expected):
    if action.get('type') != expected['type']:
        return False
    for key in ('name', 'taskId', 'verdict', 'artifactId', 'toAgent', 'evidenceCallIds'):
        if key in expected and action.get(key) != expected[key]:
            return False
    args = action.get('args', {})
    if not isinstance(args, dict):
        return False
    for key in ('path', 'url'):
        if key in expected and args.get(key) != expected[key]:
            return False
    if 'queryContains' in expected and expected['queryContains'].lower() not in str(args.get('query', '')).lower():
        return False
    if 'agentId' in expected and action.get('task', {}).get('agentId') != expected['agentId']:
        return False
    value = args.get('content', action.get('text', ''))
    if any(part not in value for part in expected.get('contains', [])):
        return False
    if expected.get('python'):
        try:
            ast.parse(args.get('code', args.get('content', '')))
        except (SyntaxError, TypeError):
            return False
        if not args.get('code', args.get('content', '')).strip():
            return False
    return True


def child(args):
    sys.path.insert(0, str(Path(args.source).resolve()))
    from backend.agents import AGENTS
    from backend.config import Config
    from backend.provider import ModelClient
    from backend.tools import ToolGateway
    from backend import runtime
    case = json.loads(sys.stdin.read())
    config = Config(Path(args.config_root))
    config.model_timeout = args.timeout
    with tempfile.TemporaryDirectory(prefix='evo-protocol-') as temp:
        config.data_dir = Path(temp)
        agent = AGENTS[case['agent']]
        definitions = ToolGateway(config).definitions(case['agent'])
        prompt = runtime.action_prompt(agent, definitions)
        messages = [{'role':'system','content':prompt}, {'role':'user','content':case['goal']}]
        client = ModelClient(config)
        result = {'case':case['id'], 'role':case['agent'], 'valid':False,
                  'provider':config.provider, 'model':config.model,
                  'systemChars':len(prompt), 'requests':[], 'discoveryTurns':0}
        start = time.monotonic()
        for _ in range(3):
            sent = time.monotonic()
            request = {'inputChars':sum(len(m['content']) for m in messages)}
            try:
                response = client.complete(messages, max_tokens=args.max_tokens)
                request.update(seconds=round(time.monotonic()-sent, 4), usage=response.get('usage', {}))
                action = runtime.parse_object(response['content'])
                request['action'] = action
                result['requests'].append(request)
                if action.get('type') == 'describe_tool' and hasattr(runtime, 'describe_tool'):
                    reply = runtime.describe_tool(agent, definitions, action.get('name'))
                elif action.get('type') == 'describe_action' and hasattr(runtime, 'describe_action'):
                    reply = runtime.describe_action(agent, action.get('name'))
                else:
                    result['valid'] = acceptable(action, case['expected'])
                    break
                result['discoveryTurns'] += 1
                messages += [{'role':'assistant','content':response['content']},
                             {'role':'user','content':json.dumps(reply, ensure_ascii=False)}]
            except Exception as error:
                request.update(seconds=round(time.monotonic()-sent, 4), error=config.scrub(str(error)))
                if not result['requests'] or result['requests'][-1] is not request:
                    result['requests'].append(request)
                break
        result['seconds'] = round(time.monotonic()-start, 4)
        print(json.dumps(config.scrub(result), ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly opt in to paid course API requests')
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--candidate', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--config-root', required=True, type=Path, help='Local project containing .env; never copied')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--timeout', type=int, default=16)
    parser.add_argument('--max-tokens', type=int, default=400)
    parser.add_argument('--source', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required to contact the model service')
    if args.source:
        return child(args)
    if not args.baseline or not args.output:
        parser.error('--live, --baseline and --output are required')
    if not 1 <= args.repeats <= 5 or not 5 <= args.timeout <= 90:
        parser.error('Use 1–5 repeats and timeout 5–90 seconds')
    raw = CASES.read_bytes()
    cases = json.loads(raw)
    report = {'benchmark':'synthetic live protocol decisions; no tool execution',
              'fixturesSha256':hashlib.sha256(raw).hexdigest(),
              'timeoutSeconds':args.timeout, 'maxCompletionTokens':args.max_tokens,
              'repeats':args.repeats, 'maxTurns':3, 'order':'alternating AB/BA per case',
              'rows':[]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for repeat in range(args.repeats):
        for index, case in enumerate(cases):
            pair = [('baseline',args.baseline), ('candidate',args.candidate)]
            if (repeat+index) % 2:
                pair.reverse()
            for name, source in pair:
                command = [sys.executable, str(Path(__file__).resolve()), '--live', '--source',str(source),
                           '--config-root',str(args.config_root),'--timeout',str(args.timeout),
                           '--max-tokens',str(args.max_tokens)]
                began = time.monotonic()
                try:
                    process = subprocess.run(command, input=json.dumps(case), text=True,
                                             capture_output=True, timeout=3*args.timeout+10)
                    if process.returncode:
                        raise RuntimeError('Isolated benchmark process failed; inspect locally.')
                    row = json.loads(process.stdout)
                except Exception as error:
                    row = {'case':case['id'],'valid':False,'seconds':round(time.monotonic()-began,4),
                           'requests':[], 'error':str(error)}
                row.update(variant=name, repeat=repeat+1)
                report['rows'].append(row)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(json.dumps({k:row[k] for k in ('variant','repeat','case','valid','seconds')}, ensure_ascii=False), flush=True)
    report['summary'] = {}
    for name in ('baseline','candidate'):
        rows = [r for r in report['rows'] if r['variant']==name]
        requests = [q for r in rows for q in r['requests']]
        usage = [q['usage'] for q in requests if isinstance(q.get('usage'),dict)]
        report['summary'][name] = {'valid':sum(r['valid'] for r in rows), 'cases':len(rows),
            'medianSecondsAll':statistics.median(r['seconds'] for r in rows),
            'totalSecondsAll':sum(r['seconds'] for r in rows), 'requestCount':len(requests),
            'discoveryTurns':sum(r.get('discoveryTurns',0) for r in rows),
            'usageReportedRequests':len(usage),
            'reportedPromptTokens':sum(u.get('prompt_tokens',0) for u in usage),
            'reportedCompletionTokens':sum(u.get('completion_tokens',0) for u in usage),
            'reportedTotalTokens':sum(u.get('total_tokens',0) for u in usage)}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report['summary'], ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
