import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "vendor"))

"""Group-task handoff cards. No network and no live model."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path

import threading
from backend.teams import clear_custom_teams
from live.platform import Platform


DIGEST = 'ab' * 32
NEWS_DIGEST = 'cd' * 32
DEV_REASON = 'booking-dev-schema-failure'


def ended_run(run_id, status, title, participants, artifacts, steps):
    return {
        'id': run_id, 'mode': 'task', 'status': status, 'title': title,
        'startedAt': 1, 'finishedAt': None,
        'participants': participants, 'steps': steps, 'artifacts': artifacts,
        'acceptanceGaps': [], 'calls': [], 'approvals': [], 'events': [],
        'conversation': [], 'transcript': [],
    }


class HandoffCardTests(unittest.TestCase):
    def test_software_page_names_hashes_and_existing_records(self):
        from backend.handoff import build_handoff
        card = build_handoff(ended_run(
            'run-software', 'partial', '会议室预约系统', ['lead', 'dev', 'qa', 'release'],
            [{'name': 'index.html', 'path': 'index.html', 'ownerAgentId': 'dev', 'sha256': DIGEST}],
            [{'id': 'implement', 'title': '实现预约页', 'agentId': 'dev', 'status': 'partial',
              'error': {'code': 'response_schema'}}]), 'software')
        self.assertIn(DIGEST, card['text'])
        self.assertIn('使用已有记录、不要另造数据', card['text'])
        self.assertEqual(card['files'][0]['readOnly'], True)
        self.assertLessEqual(len(card['text']), 2000)

    def test_news_report_keeps_sources_and_is_not_the_new_article(self):
        from backend.handoff import build_handoff
        card = build_handoff(ended_run(
            'run-news', 'done', '校园人工智能报道', ['chief_editor', 'reporter'],
            [{'name': 'campus-ai-report.md', 'path': 'campus-ai-report.md',
              'ownerAgentId': 'reporter', 'sha256': NEWS_DIGEST}],
            [{'id': 'report', 'title': '撰写报道', 'agentId': 'reporter', 'status': 'done'}]), 'newsroom')
        self.assertIn('沿用来源、范围和未定结论', card['text'])
        self.assertIn('旧文件不是新题目的正文', card['text'])
        self.assertIn(NEWS_DIGEST, card['text'])

    def test_failed_run_without_files_still_writes_a_short_card(self):
        from backend.handoff import build_handoff
        card = build_handoff(ended_run('run-empty', 'failed', '空任务', ['lead'], [], []), 'software')
        self.assertIn('没有可继承的产物', card['text'])

    def test_own_experience_stays_with_that_agent_and_run(self):
        from backend.handoff import own_experience
        records = [
            {'ownerAgentId': 'dev', 'kind': 'episodic', 'sourceRunId': 'run-software',
             'id': 'episode-dev', 'title': 'Implement booking', 'success': False,
             'reason': DEV_REASON, 'call_ids': ['call-book']},
            {'ownerAgentId': 'qa', 'kind': 'episodic', 'sourceRunId': 'run-software',
             'id': 'episode-qa', 'title': 'Check', 'success': True, 'reason': 'qa-only', 'call_ids': []},
            {'ownerAgentId': 'dev', 'kind': 'episodic', 'sourceRunId': 'run-other',
             'id': 'episode-old', 'title': 'Old', 'success': False, 'reason': 'other-run', 'call_ids': []},
        ]
        dev = own_experience(records, 'dev', 'run-software')
        self.assertEqual([item['reason'] for item in dev], [DEV_REASON])
        self.assertEqual(own_experience(records, 'qa', 'run-software')[0]['reason'], 'qa-only')
        self.assertEqual(own_experience(records, 'lead', 'run-software'), [])



DIGEST = 'ab' * 32
NEWS_DIGEST = 'cd' * 32
DEV_REASON = 'booking-dev-schema-failure'


def env_dir(base):
    cfg = Path(base) / 'config'
    cfg.mkdir(parents=True)
    (cfg / '.env').write_text(
        'LLM_PROVIDER=deepseek\nLLM_BASE_URL=https://api.deepseek.com/v1\n'
        'LLM_MODEL=deepseek-flash\nLLM_API_KEY=offline-test-key\n', encoding='utf-8')
    return cfg


class HandoffFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            for ctx in list(app.contexts.values()):
                thread = ctx.get('thread')
                if thread: thread.join(4)
            for team in app.teams.values():
                team['repo'].close()
        clear_custom_teams()
        self.temp.cleanup()

    def platform(self, executor):
        root = Path(self.temp.name) / str(len(self.apps))
        app = Platform(root / 'data', env_dir(root), executor=executor)
        self.apps.append(app)
        return app

    def test_group_task_writes_one_card_and_the_next_plan_reads_it(self):
        seen = []
        def execute(app, ctx, step, description, expected, tools):
            seen.append(description)
            if step['id'] == 'planning':
                return json.dumps({'tasks': [
                    {'id': 'implement', 'agentId': 'dev', 'title': '实现看板',
                     'instructions': 'Build the board from the existing record.',
                     'dependsOn': [], 'requiresArtifacts': False, 'expectedOutput': 'page'}]})
            ctx['run']['artifacts'].append({
                'name': 'index.html', 'path': 'index.html', 'ownerAgentId': 'dev',
                'sha256': DIGEST, 'taskId': step['id']})
            return '看板已交付'
        app = self.platform(execute)
        run = app.start('software', 'group', '做预约页', ['lead', 'dev'], seconds=60)
        app.contexts[run['id']]['thread'].join(4)
        card = app.team('software')['repo'].load_handoff()
        self.assertEqual(card['teamId'], 'software')
        self.assertEqual(card['runId'], run['id'])
        self.assertIn(DIGEST, card['text'])
        self.assertIn('使用已有记录、不要另造数据', card['text'])
        self.assertTrue(card['files'][0]['readOnly'])
        self.assertNotIn('<html', card['text'])
        self.assertTrue(any(item['role'] == 'system' and item['text'].startswith('交接卡')
                            for item in app.team('software')['state']['channels']['group']['messages']))
        app.team('software')['memory'].record_episode(
            'dev', {'id': card['runId'], 'participants': ['lead', 'dev']},
            {'id': 'schema', 'title': '记录结构', 'agentId': 'dev', 'revision': 0},
            False, DEV_REASON, ['call-book'], 'group')
        app.reset('software', 'group')
        self.assertEqual(app.team('software')['state']['channels']['group']['messages'], [])
        self.assertEqual(app.team('software')['repo'].load_handoff()['runId'], card['runId'])
        seen.clear()
        nxt = app.start('software', 'group', '做管理员看板，使用已有预约记录', ['lead', 'dev'], seconds=60)
        app.contexts[nxt['id']]['thread'].join(4)
        plan = next(text for text in seen if 'Plan the original request' in text)
        payload = json.loads(plan.split('\nREQUEST: ', 1)[1])
        self.assertIn(DIGEST, json.dumps(payload['handoff']))
        self.assertNotIn(DEV_REASON, json.dumps(payload['handoff'].get('ownExperience', [])))
        worker = next(text for text in seen if 'OWN TASK EXPERIENCE' in text)
        self.assertIn(DEV_REASON, worker)
        news_seen = []
        def news_execute(app, ctx, step, description, expected, tools):
            news_seen.append(description)
            if step['id'] == 'planning':
                return json.dumps({'tasks': [
                    {'id': 'check', 'agentId': 'fact_checker', 'title': '核对旧报道',
                     'instructions': 'Check the claim.', 'dependsOn': [],
                     'requiresArtifacts': False, 'expectedOutput': 'note'}]})
            return '已核对'
        # Same process, separate data root, so the software card is not visible.
        other = self.platform(news_execute)
        other.start('newsroom', 'group', '核查另一篇报道', ['chief_editor', 'fact_checker'], seconds=60)
        other.contexts[other.team('newsroom')['state']['channels']['group']['runId']]['thread'].join(4)
        other_plan = next(text for text in news_seen if 'Plan the original request' in text)
        other_payload = json.loads(other_plan.split('\nREQUEST: ', 1)[1])
        self.assertNotIn('handoff', other_payload)
        self.assertNotIn(DIGEST, json.dumps(other_payload))

    def test_cancel_writes_a_card_and_pause_does_not(self):
        gate = threading.Event()
        started = threading.Event()
        def execute(app, ctx, step, description, expected, tools):
            if step['id'] == 'planning':
                return json.dumps({'tasks': [
                    {'id': 'work', 'agentId': 'dev', 'title': '还在做',
                     'instructions': 'Keep going.', 'dependsOn': [],
                     'requiresArtifacts': False, 'expectedOutput': 'page'}]})
            started.set()
            gate.wait(8)
            return 'stopped'
        app = self.platform(execute)
        run = app.start('software', 'group', '还在做', ['lead', 'dev'], seconds=60)
        self.assertTrue(started.wait(3))
        app.control('software', run['id'], 'pause')
        self.assertIsNone(app.team('software')['repo'].load_handoff())
        app.control('software', run['id'], 'cancel')
        gate.set()
        app.contexts[run['id']]['thread'].join(4)
        card = app.team('software')['repo'].load_handoff()
        self.assertEqual(card['status'], 'cancelled')
        self.assertEqual(card['runId'], run['id'])

    def test_private_channel_does_not_publish_a_team_card(self):
        def execute(app, ctx, step, description, expected, tools):
            return 'private reply'
        app = self.platform(execute)
        run = app.start('software', 'dev', '私聊', mode='task', seconds=60)
        app.contexts[run['id']]['thread'].join(4)
        self.assertIsNone(app.team('software')['repo'].load_handoff())


if __name__ == '__main__':
    unittest.main()
