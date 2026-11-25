"""Reproducible, labeled retrieval regression benchmark; no model or network.

This authored fixture measures retrieval, not end-to-end agent task success.
Run against any checkout with --source and preserve the full JSON output.
"""
from pathlib import Path
import argparse
import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import types


FIXTURE = Path(__file__).parent / 'fixtures' / 'memory_retrieval_v2.json'


def _backend(source):
    source = Path(source).resolve()
    package = '_retrieval_' + hashlib.sha256(str(source).encode()).hexdigest()[:12]
    if package not in sys.modules:
        module = types.ModuleType(package)
        module.__path__ = [str(source / 'backend')]
        sys.modules[package] = module
    spec = importlib.util.spec_from_file_location(package + '.memory', source / 'backend' / 'memory.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    persistence = sys.modules[package + '.persistence']
    return module.MemoryService, persistence.Repository


def _lesson_key(item):
    return (item['owner'], item.get('visibility'), item.get('channelId'), item.get('kind'),
            str(item.get('title', '')).strip(),
            str(item.get('lesson', '')).strip(), str(item.get('trigger', '')).strip(),
            str(item.get('conditions', '')).strip())


def evaluate(source, fixture_path=FIXTURE):
    source = Path(source).resolve()
    fixture_path = Path(fixture_path)
    fixture = json.loads(fixture_path.read_text())
    service_class, repository_class = _backend(source)
    rows = []
    with tempfile.TemporaryDirectory() as directory:
        repository = repository_class(Path(directory))
        try:
            service = service_class(repository, clock=lambda: fixture['clock'])
            instant = service._iso(fixture['clock'])
            for specification in fixture['memories']:
                item = dict(specification, memorySchema=2, owner=fixture['owner'], ownerAgentId=fixture['owner'],
                            kind='procedural', layer='procedural', status='verified', verified=True,
                            visibility='group', channelId='group', createdAt=instant, lastActivityAt=instant,
                            confirmation={'type': 'human', 'id': 'fixture-confirmation-' + specification['id']},
                            sourceTaskId='fixture-task-' + specification['id'], evidence=['fixture-only'])
                repository.add_memory(fixture['owner'], item)
            for query in fixture['queries']:
                ranked = service.recall(fixture['owner'], query['query'], fixture['channelId'], limit=100)
                identifiers = [item['id'] for item in ranked]
                relevant = set(query['relevant'])
                first = next((rank for rank, identifier in enumerate(identifiers, 1) if identifier in relevant), None)
                top3 = ranked[:3]
                duplicate_slots = len(top3) - len({_lesson_key(item) for item in top3})
                rows.append({**copy.deepcopy(query), 'ranking': identifiers,
                             'top1': int(bool(identifiers and identifiers[0] in relevant)),
                             'reciprocalRank': 1 / first if first else 0,
                             'precision3': sum(identifier in relevant for identifier in identifiers[:3]) / 3,
                             'duplicateSlots3': duplicate_slots,
                             'contextChars4': sum(len(json.dumps(item, ensure_ascii=False)) for item in ranked[:4])})
        finally:
            repository.close()
    def aggregate(items):
        return {'queries': len(items),
                'top1': round(sum(row['top1'] for row in items) / len(items), 6),
                'mrr': round(sum(row['reciprocalRank'] for row in items) / len(items), 6),
                'precision3': round(sum(row['precision3'] for row in items) / len(items), 6),
                'duplicateSlots3': sum(row['duplicateSlots3'] for row in items),
                'meanContextChars4': round(sum(row['contextChars4'] for row in items) / len(items), 2)}
    return {'fixtureSha256': hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
            'sourceMemorySha256': hashlib.sha256((source / 'backend' / 'memory.py').read_bytes()).hexdigest(),
            'source': str(source), 'description': fixture['description'], 'metrics': aggregate(rows),
            'byLanguage': {language: aggregate([row for row in rows if row['language'] == language])
                           for language in sorted({row['language'] for row in rows})},
            'results': rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--fixture', type=Path, default=FIXTURE)
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    result = evaluate(options.source, options.fixture)
    if options.output:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: result[key] for key in ('fixtureSha256', 'sourceMemorySha256', 'metrics', 'byLanguage')},
                     ensure_ascii=False, indent=2))
