import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "vendor"))

"""Exam-team role prompt injection: policy, persistence, and runtime refusals.

No model, Docker or network is exercised. The tests cover only the server-owned
behaviour of the new ``set_role_prompts`` action: default placeholder, prompt
sanitation and length caps, team-scoped rejection, refusal while a run is
active, SQLite persistence, capability profile switching, ToolGateway
permission sync, and the fixed profile allowlist regardless of input text.
"""
from pathlib import Path
import tempfile
import unittest

from backend.runtime import action_prompt
from backend.sandbox import ToolError
from backend.tools import ToolGateway
from live.platform import ActionError, Platform
from backend.teams import (EXAM_DEFAULT_PROFILES, EXAM_PROFILES, EXAM_TEAM_ID,
                           apply_exam_profile, exam_role_prompt_limit,
                           exam_role_prompt_placeholder,
                           get_team_agents, is_exam_team, list_exam_profiles,
                           list_teams)
from backend.tools import ToolError


class ExamTeamRegistryTests(unittest.TestCase):
    def test_exam_team_is_listed_with_four_numbered_agents(self):
        self.assertIn('exam', [team['id'] for team in list_teams()])
        self.assertTrue(is_exam_team('exam'))
        self.assertFalse(is_exam_team('default'))
        agents = get_team_agents('exam')
        self.assertEqual(list(agents), ['agent1', 'agent2', 'agent3', 'agent4'])
        placeholder = exam_role_prompt_placeholder()
        for slot, agent in agents.items():
            self.assertTrue(agent['rolePromptCustomizable'])
            self.assertFalse(agent['rolePromptInjected'])
            self.assertEqual(agent['rolePrompt'], placeholder)
            self.assertEqual(agent['description'], placeholder)
            self.assertEqual(agent['profileId'], EXAM_DEFAULT_PROFILES[slot])

    def test_profile_list_covers_every_registered_profile(self):
        published = list_exam_profiles()
        self.assertEqual({entry['id'] for entry in published}, set(EXAM_PROFILES))
        for entry in published:
            self.assertIn('避免重复动作', entry['description'])
        prompt = action_prompt({'name': 'Agent', 'rolePrompt': 'Role.', 'capabilities': []}, [])
        self.assertIn('避免重复动作', prompt)
        # Each profile exposes a non-empty tool list and a stable primary cap.
        for entry in list_exam_profiles():
            self.assertTrue(entry['tools'])
            self.assertTrue(entry['capabilities'])
            self.assertIn(entry['primaryCapability'], entry['capabilities'])

    def test_apply_exam_profile_rejects_unknown_profile(self):
        agents = get_team_agents('exam')
        with self.assertRaises(ValueError):
            apply_exam_profile(agents['agent1'], 'bogus')

    def test_fixed_tool_allowlist_per_default_profile(self):
        agents = get_team_agents('exam')
        self.assertIn('web.search', agents['agent1']['tools'])
        self.assertIn('web.fetch', agents['agent2']['tools'])
        self.assertIn('code.test', agents['agent3']['tools'])
        self.assertIn('web.search', agents['agent3']['tools'])
        self.assertNotIn('git.push', agents['agent3']['tools'])
        self.assertIn('code.test', agents['agent4']['tools'])
        self.assertNotIn('git.push', agents['agent4']['tools'])
        release = dict(agents['agent4'])
        apply_exam_profile(release, 'release')
        self.assertIn('git.push', release['tools'])
        self.assertIn('code.test', release['tools'])
        self.assertNotIn('web.search', release['tools'])



def _platform(tmp):
    base = Path(tmp)
    cfg = base / 'config'
    cfg.mkdir()
    (cfg / '.env').write_text(
        'LLM_PROVIDER=deepseek\nLLM_BASE_URL=https://api.deepseek.com/v1\n'
        'LLM_MODEL=deepseek-flash\nLLM_API_KEY=offline-test-key\n', encoding='utf-8')
    return Platform(base / 'data', cfg)


class ExamRolePromptActionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.platform = _platform(self._tmp.name)
        self.team = 'exam'

    def tearDown(self):
        for team in self.platform.teams.values():
            team['repo'].close()
        self._tmp.cleanup()

    def test_injection_updates_agents_and_persists_state(self):
        before = {aid: list(agent['tools']) for aid, agent in self.platform.team(self.team)['state']['agents'].items()}
        snapshot = self.platform.set_role_prompts(self.team, {'agent1': 'You are A1.', 'agent3': 'You are A3.'})
        agents = self.platform.team(self.team)['state']['agents']
        self.assertEqual(agents['agent1']['rolePrompt'], 'You are A1.')
        self.assertTrue(agents['agent1']['rolePromptInjected'])
        self.assertFalse(agents['agent2']['rolePromptInjected'])
        self.assertEqual(before, {aid: list(agent['tools']) for aid, agent in agents.items()})
        self.assertEqual(snapshot['agents']['agent1']['rolePrompt'], 'You are A1.')

    def test_restart_recovers_injected_prompts_from_state(self):
        self.platform.set_role_prompts(self.team, {'agent2': 'You are A2.'})
        root, config = self.platform.root, self.platform.config_root
        for team in self.platform.teams.values():
            team['repo'].close()
        revived = Platform(root, config)
        self.platform = revived
        self.assertEqual(revived.team(self.team)['state']['agents']['agent2']['rolePrompt'], 'You are A2.')
        self.assertTrue(revived.team(self.team)['state']['agents']['agent2']['rolePromptInjected'])
        self.assertFalse(revived.team(self.team)['state']['agents']['agent1']['rolePromptInjected'])

    def test_clearing_resets_slot_to_placeholder(self):
        self.platform.set_role_prompts(self.team, {'agent1': 'You are A1.'})
        self.platform.set_role_prompts(self.team, {'agent1': '   '})
        agent = self.platform.team(self.team)['state']['agents']['agent1']
        self.assertFalse(agent['rolePromptInjected'])
        self.assertEqual(agent['rolePrompt'], exam_role_prompt_placeholder())
        self.assertNotIn('agent1', self.platform.team(self.team)['state']['rolePrompts'])

    def test_unknown_agent_rejected(self):
        with self.assertRaises(ActionError) as caught:
            self.platform.set_role_prompts(self.team, {'agent9': 'nope'})
        self.assertEqual(caught.exception.status, 400)

    def test_length_cap_rejected(self):
        with self.assertRaises(ActionError) as caught:
            self.platform.set_role_prompts(self.team, {'agent1': 'x' * (exam_role_prompt_limit() + 1)})
        self.assertEqual(caught.exception.status, 400)

    def test_non_string_value_rejected(self):
        with self.assertRaises(ActionError):
            self.platform.set_role_prompts(self.team, {'agent1': 123})

    def test_empty_prompts_object_rejected(self):
        with self.assertRaises(ActionError):
            self.platform.set_role_prompts(self.team, {})

    def test_active_run_blocks_injection(self):
        team = self.platform.team(self.team)
        team['repo'].save_run({'id': 'run-fake', 'status': 'running', 'channelId': 'group', 'teamId': self.team}, 'group')
        team['state']['runs'].append('run-fake')
        with self.assertRaises(ActionError) as caught:
            self.platform.set_role_prompts(self.team, {'agent1': 'You are A1.'})
        self.assertEqual(caught.exception.status, 409)

    def test_profile_switch_updates_tools_and_gateway_copy(self):
        self.platform.set_role_prompts(self.team, {'agent2': {'profile': 'analyst', 'prompt': 'You are a market analyst.'}})
        agent = self.platform.team(self.team)['state']['agents']['agent2']
        self.assertEqual(agent['profileId'], 'analyst')
        self.assertEqual(agent['primaryCapability'], 'market_analysis')
        self.assertIn('code.test', agent['tools'])
        gateway = ToolGateway(self.platform.config, agents={aid: dict(item) for aid, item in self.platform.team(self.team)['state']['agents'].items()})
        self.platform.contexts['held'] = {'team': self.platform.team(self.team), 'agents': {}, 'gateway': gateway, 'run': {'status': 'done'}}
        self.platform.set_role_prompts(self.team, {'agent2': {'profile': 'writer'}})
        self.assertEqual(gateway.agents['agent2']['tools'], self.platform.team(self.team)['state']['agents']['agent2']['tools'])
        self.assertNotIn('code.test', gateway.agents['agent2']['tools'])

    def test_profile_switch_persists_across_restart(self):
        self.platform.set_role_prompts(self.team, {'agent3': {'profile': 'writer'}})
        root, config = self.platform.root, self.platform.config_root
        for team in self.platform.teams.values():
            team['repo'].close()
        revived = Platform(root, config)
        self.platform = revived
        agent = revived.team(self.team)['state']['agents']['agent3']
        self.assertEqual(agent['profileId'], 'writer')
        self.assertNotIn('code.run', agent['tools'])
        self.assertNotIn('git.push', agent['tools'])

    def test_profile_only_edit_leaves_prompt_untouched(self):
        self.platform.set_role_prompts(self.team, {'agent4': 'You are the fact-checker.'})
        self.platform.set_role_prompts(self.team, {'agent4': {'profile': 'writer'}})
        agent = self.platform.team(self.team)['state']['agents']['agent4']
        self.assertEqual(agent['profileId'], 'writer')
        self.assertEqual(agent['rolePrompt'], 'You are the fact-checker.')
        self.assertTrue(agent['rolePromptInjected'])

    def test_unknown_profile_rejected(self):
        with self.assertRaises(ActionError) as caught:
            self.platform.set_role_prompts(self.team, {'agent1': {'profile': 'bogus'}})
        self.assertEqual(caught.exception.status, 400)

    def test_multiple_profiles_union_tools_and_persist(self):
        self.platform.set_role_prompts(self.team, {'agent3': {'profile': ['release', 'coder', 'coder']}})
        agent = self.platform.team(self.team)['state']['agents']['agent3']
        self.assertEqual(agent['profileIds'], ['coder', 'release'])
        self.assertEqual(agent['profileId'], 'coder')
        self.assertIn('web.search', agent['tools'])
        self.assertIn('git.push', agent['tools'])
        self.assertEqual(self.platform.team(self.team)['state']['roleProfiles']['agent3'], ['coder', 'release'])
        root, config = self.platform.root, self.platform.config_root
        for team in self.platform.teams.values():
            team['repo'].close()
        revived = Platform(root, config)
        self.platform = revived
        self.assertEqual(revived.team(self.team)['state']['agents']['agent3']['profileIds'], ['coder', 'release'])
        self.assertIn('git.push', revived.team(self.team)['state']['agents']['agent3']['tools'])

    def test_tool_gateway_denies_actions_outside_new_profile(self):
        self.platform.set_role_prompts(self.team, {'agent3': {'profile': 'writer'}})
        gateway = ToolGateway(self.platform.config, agents=self.platform.team(self.team)['state']['agents'])
        with self.assertRaises(ToolError) as caught:
            gateway.execute('agent3', 'run-ignored', 'git.push', {})
        self.assertEqual(caught.exception.code, 'permission')

    def test_snapshot_exposes_available_profiles(self):
        snapshot = self.platform.snapshot(self.team)
        self.assertEqual({item['id'] for item in snapshot['availableProfiles']}, set(EXAM_PROFILES))

    def test_non_exam_team_snapshot_has_no_profiles(self):
        self.assertNotIn('availableProfiles', self.platform.snapshot('software'))

    def test_software_team_rejects_set_role_prompts(self):
        with self.assertRaises(ActionError) as caught:
            self.platform.set_role_prompts('software', {'lead': 'nope'})
        self.assertEqual(caught.exception.status, 400)


if __name__ == '__main__':
    unittest.main()
