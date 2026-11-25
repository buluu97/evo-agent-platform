import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "vendor"))

"""User-created teams start as four unnamed slots with profile and prompt setup.

No model, Docker or network. The registry is cleared afterwards so later tests
still see only the built-in teams.
"""
from pathlib import Path
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from backend.config import Config
from backend.teams import (allows_role_setup, clear_custom_teams, create_custom_team,
                           exam_role_prompt_placeholder, get_team_agents, is_exam_team,
                           list_teams, load_custom_teams)


class CustomTeamRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name), {'LLM_API_KEY': 'custom-team-test'})
        load_custom_teams(self.config.data_dir)
        self.apps = []

    def tearDown(self):
        for app in self.apps:
            for team in app.teams.values():
                team['repo'].close()
        clear_custom_teams()
        self.tmp.cleanup()

    def test_new_team_matches_the_initial_exam_slots(self):
        before = [item['id'] for item in list_teams()]
        created = create_custom_team('  课程实验组  ')
        self.assertEqual(created['name'], '课程实验组')
        self.assertTrue(created['id'].startswith('custom-'))
        self.assertEqual([item['id'] for item in list_teams()][:len(before)], before)
        self.assertIn(created['id'], [item['id'] for item in list_teams()])
        self.assertFalse(is_exam_team(created['id']))
        self.assertTrue(allows_role_setup(created['id']))
        self.assertFalse(allows_role_setup('software'))
        agents = get_team_agents(created['id'])
        exam = get_team_agents('exam')
        self.assertEqual(list(agents), ['agent1', 'agent2', 'agent3', 'agent4'])
        placeholder = exam_role_prompt_placeholder()
        for slot, agent in agents.items():
            self.assertEqual(agent['name'], exam[slot]['name'])
            self.assertEqual(agent['tools'], exam[slot]['tools'])
            self.assertEqual(agent['profileIds'], exam[slot]['profileIds'])
            self.assertTrue(agent['rolePromptCustomizable'])
            self.assertFalse(agent['rolePromptInjected'])
            self.assertEqual(agent['rolePrompt'], placeholder)

    def test_names_must_be_new_and_the_registry_reloads(self):
        create_custom_team('实验一组')
        for name in ('', '   ', '期末测试团队', '实验一组', 'x' * 41, 'bad/name'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    create_custom_team(name)
        load_custom_teams(self.config.data_dir)
        names = [item['name'] for item in list_teams()]
        self.assertEqual(names.count('实验一组'), 1)
        stored = json.loads((self.config.data_dir / 'custom_teams.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['version'], 1)
        self.assertEqual(stored['teams'][0]['name'], '实验一组')

    def test_created_team_accepts_profile_and_prompt(self):
        from live.platform import Platform
        base = Path(self.tmp.name) / 'app'
        cfg = base / 'config'
        cfg.mkdir(parents=True)
        (cfg / '.env').write_text(
            'LLM_PROVIDER=deepseek\nLLM_BASE_URL=https://api.deepseek.com/v1\n'
            'LLM_MODEL=deepseek-flash\nLLM_API_KEY=offline-test-key\n', encoding='utf-8')
        app = Platform(base / 'data', cfg)
        self.apps.append(app)
        created = app.create_team('可配置小组', 'custom')
        snapshot = app.snapshot(created['id'])
        self.assertIn('availableProfiles', snapshot)
        self.assertFalse(snapshot['agents']['agent1']['rolePromptInjected'])
        prompt = 'You are the editor for this custom team.'
        snapshot = app.set_role_prompts(created['id'], {'agent1': {'prompt': prompt, 'profile': 'writer'}})
        agent = snapshot['agents']['agent1']
        self.assertEqual(agent['rolePrompt'], prompt)
        self.assertTrue(agent['rolePromptInjected'])
        self.assertEqual(agent['profileIds'], ['writer'])
        self.assertIn('file.write', agent['tools'])
        self.assertNotIn('code.run', agent['tools'])


class CustomTeamHttpTests(unittest.TestCase):
    def setUp(self):
        from live.platform import Platform
        from live.suite import SuiteManager
        import server
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        cfg = base / 'config'
        cfg.mkdir()
        (cfg / '.env').write_text(
            'LLM_PROVIDER=deepseek\nLLM_BASE_URL=https://api.deepseek.com/v1\n'
            'LLM_MODEL=deepseek-flash\nLLM_API_KEY=offline-test-key\n', encoding='utf-8')
        self.platform = Platform(base / 'data', cfg)
        suite = SuiteManager(self.platform, _ROOT / 'fixtures' / 'mid_test')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), server.handler(self.platform, suite))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)
        for team in self.platform.teams.values():
            team['repo'].close()
        clear_custom_teams()
        self.tmp.cleanup()

    def request(self, path, payload=None):
        import urllib.request
        headers = {'Host': '127.0.0.1:' + str(self.server.server_port)}
        if payload is None:
            request = urllib.request.Request(self.base + path, headers=headers)
        else:
            headers['Content-Type'] = 'application/json'
            headers['X-Crew-Client'] = '1'
            request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode())

    def test_post_creates_a_switchable_blank_team(self):
        created = self.request('/api/teams', {'name': '界面新建组', 'template': 'custom'})
        team_id = created['id']
        self.assertTrue(team_id.startswith('custom-'))
        state = self.request('/api/state?team=' + team_id)
        self.assertEqual(state['team']['name'], '界面新建组')
        self.assertEqual(set(state['agents']), {'agent1', 'agent2', 'agent3', 'agent4'})
        self.assertTrue(all(item['rolePromptCustomizable'] for item in state['agents'].values()))
        self.assertTrue(state['availableProfiles'])
        software = self.request('/api/state?team=software')
        self.assertNotIn('availableProfiles', software)


if __name__ == '__main__':
    unittest.main()
