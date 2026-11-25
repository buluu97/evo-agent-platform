"""Score frozen original Mid-Test exports, with evaluator-owned independent evidence. No model calls."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.mid_test_live import load_suite
from backend.evaluation import evaluate_case, summarize, persist_result
from backend.evaluation_platform import frozen_export_record
from backend.persistence import Repository
from backend.product_acceptance import RUBRIC_VERSION, RUBRICS, ProductVerifier, platform_qa_evidence
from backend.teams import get_team_agents


def write_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False); stream.write('\n')
    temporary.replace(path)


def build_suite(root, requirements_path=None):
    root = Path(root).resolve()
    confirmed = []
    requirements_sha = None
    if requirements_path:
        raw = Path(requirements_path).read_bytes(); requirements_sha = hashlib.sha256(raw).hexdigest()
        document = json.loads(raw)
        for answer in document.get('answers', []):
            if answer.get('caseId') not in RUBRICS or not isinstance(answer.get('receiptRef'), str) or not answer['receiptRef'].strip() or not isinstance(answer.get('answer'), str) or not answer['answer'].strip():
                raise ValueError('Verified requirements must retain a known case, actual receipt reference and original answer.')
            item = copy.deepcopy(answer)
            item.setdefault('deliveredToRun', None)
            item.setdefault('deliveryStatus', 'historical_preference_not_confirmed_delivered')
            confirmed.append(item)
    cases = []
    for scenario in load_suite(root):
        for task in scenario['tasks']:
            cases.append({'caseId': task['id'], 'teamId': scenario['team'], 'title': task['title'], 'verifier': 'mid-test-product',
                'prompt': task['text'], 'sourceSha256': task['sha256'],
                'verifiedRequirements': [answer for answer in confirmed if answer['caseId'] == task['id']],
                'checks': copy.deepcopy(RUBRICS[task['id']])})
    files = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(root.rglob('*.md')) if path.is_file() and not path.is_symlink()}
    return {'schemaVersion': 1, 'suiteId': RUBRIC_VERSION, 'officialScore': False,
            'scope': 'Fixed self-built acceptance rubric for the original six tasks and actual requester answers; not official grading.',
            'rubricRevisionNotes': ['v1 was an ungraded draft; v1.1 accepts actual frozen result text for articles/proposals without inventing a download requirement.',
                'v1.2 follows the original Task 1 scope: cancellation state persists after refresh; retaining historical cancellation audit records is assessed only for the Task 2 dashboard.',
                'v1.2 accepts both original control task.text/task.sha256 and legacy prompt/sourceSha256 exports; all present provenance values and persisted run.title must match the original task.'],
            'packFilesSha256': files, 'verifiedRequirementsSourceSha256': requirements_sha, 'cases': cases,
            'boundaries': ['name-only identification and single-browser local storage are accepted requester scope, not automatic failures.',
                'Multi-tab overwrites/storage failure are reported as reliability boundary tests, not an invented global-concurrency requirement.',
                'Independent operator evidence never becomes a platform QA Agent call or review.',
                'Workspace snapshots may support prototype checks but cannot satisfy published ZIP/delivery checks.',
                'Missing source/fact/calculation verification remains unassessed; HTTP success or artifact presence does not prove business completion.',
                'v1.3 historical requester preferences are not hidden run inputs: deliveredToRun requires an explicit operator-verified recorded reply binding; unrelayed presets/offline preferences are diagnostic alignment notes only.']}


def applicable_requirements(case, run):
    """Integrity-check an operator's relay annotation; never infer delivery from task keywords."""
    result = []
    for original in case.get('verifiedRequirements', []):
        item = copy.deepcopy(original); binding = item.get('deliveryEvidence') or {}
        receipts = [approval.get('clarificationReceipt') for approval in run.get('rawRun', {}).get('approvals', [])
                    if approval.get('type') == 'clarification' and approval.get('id') == binding.get('approvalId')]
        matched = next((receipt for receipt in receipts if isinstance(receipt, dict)
            and receipt.get('id') == binding.get('receiptId') and receipt.get('runId') == run['runId']
            and isinstance(receipt.get('answer'), str)
            and hashlib.sha256(receipt['answer'].encode()).hexdigest() == binding.get('answerSha256')), None)
        if item.get('deliveredToRun') == run['runId'] and matched:
            item['deliveryStatus'] = 'recorded_operator_verified_relay'
        else:
            item['deliveredToRun'] = None
            item['deliveryStatus'] = 'not_recorded_for_this_run'
        result.append(item)
    return result


def _bindings(team):
    agents = get_team_agents(team)
    mapping = {}
    for canonical, capability in [('coordinator', 'planning'), ('reviewer', 'review'), ('executor', 'coding'), ('research', 'research'), ('release', 'release')]:
        selected = next((agent_id for agent_id, value in agents.items() if value.get('primaryCapability') == capability), None)
        if selected: mapping[canonical] = selected
    return agents, mapping


def unavailable(case, reason, status='not_observed', run_id=None):
    return {'caseId': case['caseId'], 'runId': run_id, 'source': 'unavailable_export',
            'executionStatus': status, 'goalVerdict': 'unassessed', 'independentProductVerdict': 'unassessed',
            'operatorAssisted': False, 'platformQaEvidence': [], 'errorCode': 'unavailable_export', 'errorReason': reason,
            'elapsedMs': None, 'humanWaitMs': None, 'verifierMs': None, 'verifierType': 'mid-test-product',
            'checks': [{'checkId': check['id'], 'verdict': 'unassessed', 'reason': reason, 'taskRefs': [], 'evidenceIds': [], 'errorCode': None, 'details': None} for check in case['checks']],
            'tasks': [], 'feedback': [], 'taskRevisionHistory': [],
            'memory': {'enabled': None, 'condition': None, 'boundary': None, 'changes': []}}


def assess_exports(suite, exports_dir, evidence_dir=None):
    exports_dir = Path(exports_dir)
    rows, run_ids = [], {}
    for original_case in suite['cases']:
        case = copy.deepcopy(original_case); case_id = case['caseId']
        path = exports_dir / (case_id + '.json')
        if not path.is_file(): rows.append(unavailable(case, 'No frozen export exists for this required case.')); continue
        try:
            document = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(document, dict): raise ValueError('Frozen export must be an object.')
        except (ValueError, OSError) as error:
            rows.append(unavailable(case, str(error), 'invalid_export')); continue
        raw = document.get('run', document)
        run_id = raw.get('id') if isinstance(raw, dict) else None
        if not run_id:
            rows.append(unavailable(case, 'Case did not start or has no identifiable persisted run.', document.get('executionStatus', 'not_run'))); continue
        run_ids[case_id] = run_id
        if case_id.endswith('-2'): case['previousRunId'] = run_ids.get(case_id[:-1] + '1')
        try:
            task_source = document.get('task') if isinstance(document.get('task'), dict) else {}
            prompts = ([document['prompt']] if 'prompt' in document else []) + ([task_source['text']] if 'text' in task_source else [])
            source_hashes = ([document['sourceSha256']] if 'sourceSha256' in document else []) + ([task_source['sha256']] if 'sha256' in task_source else [])
            if not prompts or any(prompt != case['prompt'] for prompt in prompts):
                raise ValueError('Observed prompt differs from the frozen original task.')
            if not source_hashes or any(digest != case['sourceSha256'] for digest in source_hashes):
                raise ValueError('Task source SHA differs from the original pack.')
            if 'id' in task_source and task_source['id'] != case_id:
                raise ValueError('Original task metadata identifies a different case.')
            if not isinstance(raw.get('title'), str) or raw['title'].strip() != case['prompt'].strip():
                raise ValueError('Persisted run title does not contain the actual original task prompt; an outer wrapper is not proof of the request.')
            agents, roles = _bindings(case['teamId'])
            run, loader = frozen_export_record({'caseId': case_id, 'runId': run_id, 'roleBindings': roles}, path, agents=agents)
            if run.get('teamId') != case['teamId']: raise ValueError('Frozen export belongs to another team.')
            case['verifiedRequirements'] = applicable_requirements(case, run)
            if evidence_dir:
                candidates = [Path(evidence_dir) / case_id / 'assessment.json', Path(evidence_dir) / (case_id + '.json')]
                existing = [candidate for candidate in candidates if candidate.is_file()]
                if len(existing) > 1: raise ValueError('Use one unambiguous independent assessment per case.')
                proof_path = existing[0] if existing else candidates[0]
            else: proof_path = None
            if proof_path and proof_path.is_file():
                if any(part in ('workspaces', 'imports') for part in proof_path.resolve().parts): raise ValueError('Independent evidence must be outside agent-controlled directories.')
                proof = json.loads(proof_path.read_text(encoding='utf-8'))
            else: proof = None
            verifier = ProductVerifier(case, proof, proof_path.parent if proof_path else None)
            result = evaluate_case(case, run, loader, verifier)
            verdicts = {check['verdict'] for check in result['checks']}
            result.update(independentProductVerdict='fail' if 'fail' in verdicts else 'pass' if verdicts == {'pass'} else 'unassessed',
                          operatorAssisted=verifier.operator_assisted, platformQaEvidence=platform_qa_evidence(run),
                          teamId=run.get('teamId'), exportSha256=run.get('exportSha256'),
                          verifiedRequirements=case['verifiedRequirements'])
            rows.append(result)
        except (ValueError, TypeError, KeyError, OSError) as error:
            rows.append(unavailable(case, str(error), raw.get('status', 'invalid_export'), run_id))
    metrics = summarize(rows)
    metrics['independentProducts'] = {verdict: sum(row['independentProductVerdict'] == verdict for row in rows) for verdict in ('pass', 'fail', 'unassessed')}
    return {'schemaVersion': 1, 'suiteId': suite['suiteId'], 'officialScore': False,
            'assessmentScope': 'Frozen observations and independent evaluator attestations; unit fixtures are never real-run results.',
            'cases': rows, 'summary': metrics, 'suite': suite}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite-root', type=Path, required=True)
    parser.add_argument('--requirements', type=Path)
    parser.add_argument('--exports-dir', type=Path)
    parser.add_argument('--evidence-dir', type=Path)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--suite-only', action='store_true')
    args = parser.parse_args(argv)
    if args.output.exists(): parser.error('Use a fresh output file; previous evidence is not overwritten.')
    if not args.suite_only and not args.exports_dir: parser.error('--exports-dir is required for assessment.')
    try:
        suite = build_suite(args.suite_root, args.requirements)
        report = suite if args.suite_only else assess_exports(suite, args.exports_dir, args.evidence_dir)
        if args.data_dir and not args.suite_only:
            repository = Repository(args.data_dir)
            try:
                batch = 'mid-test-acceptance-' + uuid4().hex
                for row in report['cases']: persist_result(repository, suite['suiteId'], batch, row)
            finally: repository.close()
        write_json(args.output, report)
    except (ValueError, TypeError, KeyError, OSError) as error:
        print('Acceptance input error: ' + str(error), file=sys.stderr); return 2
    print(json.dumps({'report': str(args.output), 'cases': len(suite['cases']), 'checks': sum(len(c['checks']) for c in suite['cases']),
                      'officialScore': False, 'summary': report.get('summary')}, ensure_ascii=False))
    return 0 if args.suite_only or all(row['independentProductVerdict'] == 'pass' for row in report['cases']) else 1


if __name__ == '__main__': raise SystemExit(main())
