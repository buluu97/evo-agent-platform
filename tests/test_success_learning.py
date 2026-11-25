import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "vendor"))

"""Light-touch memory learning: success candidates, auto-confirm, planner view.

These tests cover the behaviour added by ``backend.success_learning`` and the
Planner's prior-experience injection. No real model, Docker or network is
exercised; the memory service and engine are driven through their public APIs.
"""
import copy
from pathlib import Path
import tempfile
import unittest

from backend.config import Config
from backend.memory import MemoryService
from backend.persistence import Repository
from backend.success_learning import (propose_success_memory,
                                      record_success_outcome, task_signature)


class SignatureTests(unittest.TestCase):
    def test_signature_stable_for_same_agent_title_contract(self):
        a = {'agentId': 'executor', 'title': 'Build booking app',
             'outputContract': {'kind': 'artifact'}}
        b = {'agentId': 'executor', 'title': ' build booking app ',
             'outputContract': {'kind': 'artifact'}}
        self.assertEqual(task_signature(a), task_signature(b))

    def test_signature_shifts_when_contract_changes(self):
        base = {'agentId': 'executor', 'title': 'Build dashboard',
                'outputContract': {'kind': 'artifact'}}
        variant = copy.deepcopy(base)
        variant['outputContract'] = {'kind': 'text'}
        self.assertNotEqual(task_signature(base), task_signature(variant))

    def test_signature_requires_agent_and_title(self):
        self.assertIsNone(task_signature({'title': 'no agent'}))
        self.assertIsNone(task_signature({'agentId': 'executor'}))
        self.assertIsNone(task_signature({'agentId': 'executor', 'title': '   '}))


class MemoryServiceSuccessStreakTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.tmp.name))
        self.memory = MemoryService(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _candidate(self):
        return self.memory.propose(
            'executor', 'Reuse the known winning plan for booking apps.',
            'booking app build', 'task-origin', 'run-origin',
            [{'type': 'call', 'id': 'call-1'}],
            conditions='outputContract=artifact',
            signature='sig-booking')

    def test_single_success_keeps_candidate(self):
        candidate = self._candidate()
        outcome = self.memory.record_success_streak(
            candidate['id'], 'executor', 'task-a', 'run-a')
        self.assertTrue(outcome['applied'])
        self.assertFalse(outcome.get('promoted'))
        self.assertEqual(outcome['memory']['status'], 'candidate')
        self.assertEqual(outcome['memory']['successWitnesses'], ['task-a'])

    def test_two_distinct_successes_auto_confirm(self):
        candidate = self._candidate()
        self.memory.record_success_streak(candidate['id'], 'executor', 'task-a', 'run-a')
        outcome = self.memory.record_success_streak(
            candidate['id'], 'executor', 'task-b', 'run-b')
        self.assertTrue(outcome.get('promoted'))
        self.assertEqual(outcome['memory']['status'], 'verified')
        self.assertTrue(outcome['memory']['verified'])
        confirmation = outcome['memory']['confirmation']
        self.assertEqual(confirmation['type'], 'result')
        self.assertIn('run-b', confirmation['id'])

    def test_same_task_id_does_not_count_twice(self):
        candidate = self._candidate()
        first = self.memory.record_success_streak(
            candidate['id'], 'executor', 'task-a', 'run-a')
        second = self.memory.record_success_streak(
            candidate['id'], 'executor', 'task-a', 'run-b')
        self.assertTrue(first['applied'])
        self.assertFalse(second['applied'])
        self.assertEqual(second['reason'], 'duplicate_witness')
        self.assertEqual(second['memory']['status'], 'candidate')

    def test_rejected_memory_is_left_alone(self):
        candidate = self._candidate()
        self.memory.confirm(candidate['id'], 'executor', False,
                            {'type': 'human', 'id': 'rejected-by-user'})
        outcome = self.memory.record_success_streak(
            candidate['id'], 'executor', 'task-a', 'run-a')
        self.assertFalse(outcome['applied'])
        self.assertEqual(outcome['reason'], 'non_active_status')

    def test_threshold_must_be_at_least_two(self):
        candidate = self._candidate()
        with self.assertRaises(ValueError):
            self.memory.record_success_streak(
                candidate['id'], 'executor', 'task-a', 'run-a', threshold=1)

    def test_find_by_signature_is_scoped_per_owner(self):
        own = self._candidate()
        self.assertEqual(
            self.memory.find_by_signature('executor', 'sig-booking')['id'],
            own['id'])
        self.assertIsNone(self.memory.find_by_signature('research', 'sig-booking'))


class ProposeSuccessMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(Path(self.tmp.name))
        self.memory = MemoryService(self.repo)
        self.run = {'id': 'run-1'}
        self.task = {'id': 'task-1', 'agentId': 'executor',
                     'title': 'Build booking app', 'goal': 'build it',
                     'outputContract': {'kind': 'artifact'}}
        self.calls = [{'id': 'call-x', 'taskId': 'task-1'}]

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_first_call_proposes_candidate_with_signature(self):
        memory, created = propose_success_memory(
            self.memory, self.run, self.task, self.calls, 'group')
        self.assertTrue(created)
        self.assertEqual(memory['status'], 'candidate')
        self.assertTrue(memory['sourceSignature'].startswith('sig-'))

    def test_second_call_reuses_existing_candidate(self):
        propose_success_memory(self.memory, self.run, self.task, self.calls, 'group')
        another_task = {**self.task, 'id': 'task-2'}
        memory, created = propose_success_memory(
            self.memory, {'id': 'run-2'}, another_task, self.calls, 'group')
        self.assertFalse(created)
        self.assertTrue(memory['id'].startswith('memory-'))
        candidates = [m for m in self.memory.list('executor') if m['kind'] == 'procedural']
        self.assertEqual(len(candidates), 1)

    def test_tasks_without_signature_are_ignored(self):
        memory, created = propose_success_memory(
            self.memory, self.run, {'id': 'task-x', 'title': ''}, [], 'group')
        self.assertIsNone(memory)
        self.assertFalse(created)

    def test_record_success_outcome_rejects_missing_agent(self):
        outcome = record_success_outcome(
            self.memory, {'id': 'nope'}, {'id': 't'}, self.run)
        self.assertFalse(outcome['applied'])



class PlannerExperienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        cfg = base / 'config'
        cfg.mkdir()
        (cfg / '.env').write_text(
            'LLM_PROVIDER=deepseek\nLLM_BASE_URL=https://api.deepseek.com/v1\n'
            'LLM_MODEL=deepseek-flash\nLLM_API_KEY=offline-test-key\n', encoding='utf-8')
        from live.platform import Platform
        from backend.teams import clear_custom_teams
        self.clear = clear_custom_teams
        self.platform = Platform(base / 'data', cfg)
        self.team = self.platform.team(self.platform.create_team('经验组', 'default')['id'])

    def tearDown(self):
        for team in self.platform.teams.values():
            team['repo'].close()
        self.clear()
        self.tmp.cleanup()

    def _verified(self, agent_id, trigger, lesson):
        memory = self.platform.memory if False else self.team['memory'].propose(
            agent_id, lesson, trigger, 'task-prev', 'run-prev',
            [{'type': 'call', 'id': 'call-prev'}])
        self.team['memory'].confirm(memory['id'], agent_id, True,
                                     {'type': 'result', 'id': 'test-fixture'})
        return memory

    def _experience(self, title, participants):
        ctx = {'team': self.team, 'agents': self.team['state']['agents']}
        return self.platform._planner_prior_experience(
            ctx, {'title': title, 'channelId': 'group'}, participants)

    def test_planner_experience_includes_verified_memory_for_participant(self):
        self._verified('executor', 'build booking app', 'Start with storage schema before UI.')
        experience = self._experience('Build the next booking app', ['coordinator', 'executor'])
        self.assertTrue(experience)
        self.assertTrue(any('Start with storage schema before UI' in entry['title'] for entry in experience))
        for entry in experience:
            self.assertIn('memoryId', entry)
            self.assertIn('confidence', entry)
            self.assertNotIn('lesson', entry)

    def test_planner_experience_empty_without_relevant_memory(self):
        self.assertEqual(self._experience('Investigate detectors', ['coordinator', 'research']), [])

    def test_planner_experience_ignores_unverified(self):
        self.team['memory'].propose(
            'executor', 'Unconfirmed draft only', 'build booking app',
            'task-a', 'run-a', [{'type': 'call', 'id': 'call-a'}])
        self.assertEqual(self._experience('build booking app', ['executor']), [])


if __name__ == '__main__':
    unittest.main()
