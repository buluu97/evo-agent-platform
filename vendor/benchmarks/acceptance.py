"""Evaluate one saved run per case and write an acceptance report."""

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import NamedTemporaryFile
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.artifacts import ArtifactStore
from backend.evaluation import evaluate_case, local_file, object_from_json, persist_result, summarize
from backend.evaluation_platform import platform_record, frozen_export_record
from backend.order_acceptance import ExactJsonVerifier, OrdersVerifier, read_utf8
from backend.persistence import Repository


VERIFIERS = {'orders-csv': OrdersVerifier, 'exact-json': ExactJsonVerifier}


def entries(path, field, key):
    document = object_from_json(read_utf8(path))
    if type(document['schemaVersion']) is not int or document['schemaVersion'] != 1:
        raise ValueError(f'Unsupported manifest version in {path.name}.')
    rows = document[field]
    ids = [row[key] for row in rows]
    if not ids or any(not isinstance(value, str) or not value.strip() for value in ids) or len(set(ids)) != len(ids):
        raise ValueError(f'{field} must have unique, nonempty {key} values.')
    return document, dict(zip(ids, rows))


def recorded_artifacts(run, directory):
    def load(artifact_id):
        metadata = run.get('artifacts', {}).get(artifact_id)
        if metadata is None:
            return None
        return {**metadata, 'content': read_utf8(local_file(directory, metadata['file']))}
    return load


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as file:
            tmp = Path(file.name)
            json.dump(report, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write('\n')
        tmp.replace(path)
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


def evaluate(args):
    suite, cases = entries(args.suite, 'cases', 'caseId')
    _, bindings = entries(args.runs, 'runs', 'caseId')
    if cases.keys() != bindings.keys():
        raise ValueError('Supply one run or terminal failure record for every suite case.')
    if len({record['runId'] for record in bindings.values()}) != len(bindings):
        raise ValueError('Each case needs its own run ID.')

    verifiers = {case_id: VERIFIERS[case['verifier']](case, args.suite.parent, args.docker_image)
                 for case_id, case in cases.items()}
    batch_id = 'acceptance-' + uuid4().hex
    with ExitStack() as resources:
        repository = Repository(args.data_dir)
        resources.callback(repository.close)
        platform = None
        if args.platform_data_dir is not None:
            if not (args.platform_data_dir / 'platform.sqlite').is_file():
                raise ValueError('The platform data directory must contain an existing platform.sqlite.')
            platform = repository if args.platform_data_dir.resolve() == args.data_dir.resolve() else Repository(args.platform_data_dir)
            if platform is not repository:
                resources.callback(platform.close)
            from backend.teams import get_team_agents
            saved=platform.load_state() or {}
            store = ArtifactStore(args.platform_data_dir,agents=get_team_agents(saved.get('teamId','default')))

        prepared = []
        for case_id in cases:
            binding = bindings[case_id]
            if binding.get('exportPath'):
                run,loader=frozen_export_record(binding,local_file(args.runs.parent,binding['exportPath']))
            elif platform is not None:
                run = platform_record(binding, platform, store)
                loader = store.get
            else:
                if binding.get('source') not in ('handwritten', 'recorded'):
                    raise ValueError('External records must declare source as handwritten or recorded.')
                run = binding
                loader = recorded_artifacts(run, args.runs.parent)
            prepared.append((case_id, run, loader))

        results = [evaluate_case(cases[case_id], run, loader, verifiers[case_id])
                   for case_id, run, loader in prepared]
        report = {'schemaVersion': 1, 'suiteId': suite['suiteId'], 'batchId': batch_id,
                  'cases': results, 'summary': summarize(results)}
        for result in results:
            persist_result(repository, suite['suiteId'], batch_id, result)
        write_report(args.output, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True, help='Evaluator-owned case manifest.')
    parser.add_argument('--runs', type=Path, required=True, help='Trusted evidence or platform bindings manifest.')
    parser.add_argument('--platform-data-dir', type=Path, help='Read authoritative runs and artifact snapshots from this directory.')
    parser.add_argument('--data-dir', type=Path, required=True, help='Directory for SQLite evaluation records.')
    parser.add_argument('--output', type=Path, required=True, help='Path for the full JSON report.')
    parser.add_argument('--docker-image', default='python:3.11-alpine', help='Local Docker image used for verification.')
    args = parser.parse_args(argv)
    if args.output.resolve() in {args.suite.resolve(), args.runs.resolve(), (args.data_dir / 'platform.sqlite').resolve()}:
        parser.error('--output must be a separate file.')
    if args.platform_data_dir and args.output.resolve() == (args.platform_data_dir / 'platform.sqlite').resolve():
        parser.error('--output cannot overwrite the platform database.')
    try:
        report = evaluate(args)
    except (KeyError, OSError, TypeError, ValueError, sqlite3.Error) as error:
        print(f'Acceptance input or storage error: {error}', file=sys.stderr)
        return 2
    for result in report['cases']:
        print(f"{result['caseId']}: {result['executionStatus']} / {result['goalVerdict']} "
              f"({result['elapsedMs']} ms, human wait {result['humanWaitMs']} ms)")
    print(json.dumps(report['summary'], indent=2))
    print(f'Report: {args.output}')
    return 0 if all(result['goalVerdict'] == 'pass' for result in report['cases']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
