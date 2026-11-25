"""Server-owned team registries. Official Mid-Test identities are preserved verbatim.

No task answers, decompositions or pre-scripted human replies are included.
"""
import copy
import json
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from .agents import AGENTS, _COMMON, _sandbox

DEFAULT_TEAM = 'default'
TEAM_DEFINITIONS = {'default': {'id': 'default', 'name': '通用四角色团队',
    'description': 'Coordinator、Research、Executor、Reviewer 通用协作。', 'agents': AGENTS}}

TEAM_DEFINITIONS['software'] = {'id': 'software', 'name': '软件开发团队', 'description': 'Technical Lead、Developer、QA 和 Release 协作，连续交付与更新。', 'agents': {
    'lead': {
        'name': 'Technical Lead', 'label': '技术负责人', 'role': 'Technical Lead',
        'description': 'You are the technical lead of a software development team. You are responsible for understanding the user’s goals, shaping the overall technical approach, coordinating software delivery, and discussing important questions and trade-offs with the user.',
        'rolePrompt': 'You are the technical lead of a software development team. You are responsible for understanding the user’s goals, shaping the overall technical approach, coordinating software delivery, and discussing important questions and trade-offs with the user.',
        'primaryCapability': 'planning', 'capabilities': ['planning', 'synthesis', 'delegation', 'clarification'], 'cost': 1.0,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch'], 'sandbox': _sandbox('lead'),
    },
    'dev': {
        'name': 'Software Developer', 'label': '软件开发员', 'role': 'Software Developer',
        'description': 'You are the software developer on a software development team. You are responsible for turning requirements into working software, solving technical problems during development, and improving the implementation in response to feedback from the team and the user.',
        'rolePrompt': 'You are the software developer on a software development team. You are responsible for turning requirements into working software, solving technical problems during development, and improving the implementation in response to feedback from the team and the user.',
        'primaryCapability': 'coding', 'capabilities': ['coding', 'implementation', 'repair'], 'cost': 1.3,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch', 'code.run', 'code.test'], 'sandbox': _sandbox('dev'),
    },
    'qa': {
        'name': 'Test Engineer', 'label': '测试工程师', 'role': 'Test Engineer',
        'description': 'You are the test engineer on a software development team. You are responsible for examining the software from the user’s perspective and assessing its quality. Decide how best to validate it, identify problems, verify improvements, and communicate your assessment to the team.',
        'rolePrompt': 'You are the test engineer on a software development team. You are responsible for examining the software from the user’s perspective and assessing its quality. Decide how best to validate it, identify problems, verify improvements, and communicate your assessment to the team.',
        'primaryCapability': 'review', 'capabilities': ['review', 'testing', 'verification'], 'cost': 1.3,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch', 'code.run', 'code.test'], 'sandbox': _sandbox('qa'),
    },
    'release': {
        'name': 'Release Manager', 'label': '发布经理', 'role': 'Release Manager',
        'description': 'You are the release manager of a software development team. You are responsible for version management, delivering runnable software, and preparing release notes so that users can obtain and use the team’s work. You also support subsequent version updates.',
        'rolePrompt': 'You are the release manager of a software development team. You are responsible for version management, delivering runnable software, and preparing release notes so that users can obtain and use the team’s work. You also support subsequent version updates.',
        'primaryCapability': 'release', 'capabilities': ['release', 'versioning', 'delivery', 'git'], 'cost': 1.3,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'code.run', 'code.test', 'git.status', 'git.commit', 'git.push'], 'sandbox': _sandbox('release'),
    },
}}

TEAM_DEFINITIONS['newsroom'] = {'id': 'newsroom', 'name': '新闻编辑室', 'description': '总编、记者、事实核查员与编辑依据来源协作。', 'agents': {
    'chief_editor': {
        'name': 'Editor-in-Chief', 'label': '总编辑', 'role': 'Editor-in-Chief',
        'description': 'You are the editor-in-chief of a newsroom. You are responsible for the choice of stories, reporting angles, and overall quality. Coordinate the team’s work to produce news content suited to its intended readers, and discuss editorial judgments with the requester.',
        'rolePrompt': 'You are the editor-in-chief of a newsroom. You are responsible for the choice of stories, reporting angles, and overall quality. Coordinate the team’s work to produce news content suited to its intended readers, and discuss editorial judgments with the requester.',
        'primaryCapability': 'planning', 'capabilities': ['planning', 'synthesis', 'editorial_direction', 'clarification'], 'cost': 1.0,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch'], 'sandbox': _sandbox('chief_editor'),
    },
    'reporter': {
        'name': 'Reporter', 'label': '记者', 'role': 'Reporter',
        'description': 'You are a reporter in a newsroom. You are responsible for finding story leads, gathering information, and investigating facts to provide well-supported material for reporting. Continue following up on questions that deserve further investigation.',
        'rolePrompt': 'You are a reporter in a newsroom. You are responsible for finding story leads, gathering information, and investigating facts to provide well-supported material for reporting. Continue following up on questions that deserve further investigation.',
        'primaryCapability': 'research', 'capabilities': ['research', 'reporting', 'source_verification'], 'cost': 1.0,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch'], 'sandbox': _sandbox('reporter'),
    },
    'fact_checker': {
        'name': 'Fact-Checker', 'label': '事实核查员', 'role': 'Fact-Checker',
        'description': 'You are the fact-checker in a newsroom. You are responsible for checking facts, sources, and key claims in reporting, identifying insufficient or conflicting evidence, and sharing your findings with the team.',
        'rolePrompt': 'You are the fact-checker in a newsroom. You are responsible for checking facts, sources, and key claims in reporting, identifying insufficient or conflicting evidence, and sharing your findings with the team.',
        'primaryCapability': 'review', 'capabilities': ['review', 'verification', 'fact_checking'], 'cost': 1.0,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch'], 'sandbox': _sandbox('fact_checker'),
    },
    'editor': {
        'name': 'Editor', 'label': '编辑', 'role': 'Editor',
        'description': 'You are an editor in a newsroom. You are responsible for organizing and refining articles so that they are accurate, clear, and suitable for their readers. Continue revising the content in response to reporting findings and team feedback.',
        'rolePrompt': 'You are an editor in a newsroom. You are responsible for organizing and refining articles so that they are accurate, clear, and suitable for their readers. Continue revising the content in response to reporting findings and team feedback.',
        'primaryCapability': 'editing', 'capabilities': ['editing', 'writing', 'publication'], 'cost': 1.0,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch'], 'sandbox': _sandbox('editor'),
    },
}}

TEAM_DEFINITIONS['fund'] = {'id': 'fund', 'name': '证券投资基金团队', 'description': '基金经理、投研、风控及市场人员形成模拟研究与客户材料。', 'agents': {
    'fund_manager': {
        'name': 'Fund Manager', 'label': '基金经理', 'role': 'Fund Manager',
        'description': 'You are the fund manager of a securities investment fund. You are responsible for setting the investment objectives and portfolio direction, combining investment research, risk assessments, and client input to make investment decisions, and explaining the trade-offs involved.',
        'rolePrompt': 'You are the fund manager of a securities investment fund. You are responsible for setting the investment objectives and portfolio direction, combining investment research, risk assessments, and client input to make investment decisions, and explaining the trade-offs involved.',
        'primaryCapability': 'planning', 'capabilities': ['planning', 'synthesis', 'portfolio_direction', 'clarification'], 'cost': 1.0,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch'], 'sandbox': _sandbox('fund_manager'),
    },
    'research': {
        'name': 'Investment Research Analyst', 'label': '投资研究员', 'role': 'Investment Research Analyst',
        'description': 'You are an investment research analyst at a securities investment fund. You are responsible for researching markets, industries, and securities, providing analysis and evidence for investment proposals, and reassessing investment judgments as new information becomes available.',
        'rolePrompt': 'You are an investment research analyst at a securities investment fund. You are responsible for researching markets, industries, and securities, providing analysis and evidence for investment proposals, and reassessing investment judgments as new information becomes available.',
        'primaryCapability': 'research', 'capabilities': ['research', 'market_analysis', 'data_analysis'], 'cost': 1.3,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch', 'code.run', 'code.test'], 'sandbox': _sandbox('research'),
    },
    'risk': {
        'name': 'Risk Manager', 'label': '风险经理', 'role': 'Risk Manager',
        'description': 'You are the risk manager of a securities investment fund. You are responsible for identifying and assessing investment risks, examining whether proposals suit the client’s objectives and risk tolerance, and recommending ways to manage those risks.',
        'rolePrompt': 'You are the risk manager of a securities investment fund. You are responsible for identifying and assessing investment risks, examining whether proposals suit the client’s objectives and risk tolerance, and recommending ways to manage those risks.',
        'primaryCapability': 'review', 'capabilities': ['review', 'verification', 'risk_assessment'], 'cost': 1.3,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch', 'code.run', 'code.test'], 'sandbox': _sandbox('risk'),
    },
    'marketing': {
        'name': 'Marketing Specialist', 'label': '市场专员', 'role': 'Marketing Specialist',
        'description': 'You are the marketing specialist at a securities investment fund. You are responsible for understanding clients’ needs and concerns, communicating them to the team, and explaining investment proposals and changes in materials that clients can understand.',
        'rolePrompt': 'You are the marketing specialist at a securities investment fund. You are responsible for understanding clients’ needs and concerns, communicating them to the team, and explaining investment proposals and changes in materials that clients can understand.',
        'primaryCapability': 'communication', 'capabilities': ['communication', 'client_needs', 'presentation'], 'cost': 1.0,
        'tools': ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete', 'web.search', 'web.fetch'], 'sandbox': _sandbox('marketing'),
    },
}}

# Final-exam-ready team: four slots whose identities are injected at runtime.
# Tool whitelists stay fixed per slot so an injected prompt cannot broaden
# permissions. Role prompts default to a neutral placeholder; the UI publishes
# real role text through the set_role_prompts action before each run.
EXAM_TEAM_ID = 'exam'
EXAM_PLACEHOLDER = (
    'This role identity has not been configured yet. Wait for the human '
    'requester to inject a specific role prompt from the workbench before '
    'acting. Do not invent an identity, scope, or workflow.')
EXAM_MAX_ROLE_PROMPT_CHARS = 8000

# Server-owned capability profiles the workbench can bind to each exam slot.
# Capabilities and tools are fixed per profile so an injected role prompt (or
# a workbench selection) cannot broaden permissions beyond this table.
# Each profile is declared in business terms; the role prompt supplies the
# narrative identity, this profile supplies the action space.
_FILE_TOOLS = ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete']
_WEB_TOOLS = ['web.search', 'web.fetch']
_CODE_TOOLS = ['code.run', 'code.test']
_GIT_TOOLS = ['git.status', 'git.commit', 'git.push']

# One requirement, copied onto every capability profile and into the action prompt.
SHARED_PROFILE_REQUIREMENT = (
    '共享要求：高效完成当前任务，避免重复动作。'
    '已经成功或已经记录的读取、发布和审查不要再做一遍；'
    '同一未变更成果不要重复发布或重复提交相同结论。要求已满足时直接交卷。'
)

def _profile_description(text):
    return text.rstrip() + ' ' + SHARED_PROFILE_REQUIREMENT

EXAM_PROFILES = {
    'planner': {
        'label': '策划 / 协调',
        'description': _profile_description('策划、依赖拆解、人工澄清、最终汇总；不直接写产品代码。'),
        'primaryCapability': 'planning',
        'capabilities': ['planning', 'synthesis', 'delegation', 'clarification'],
        'tools': [*_FILE_TOOLS, *_WEB_TOOLS],
        'cost': 1.0,
    },
    'researcher': {
        'label': '研究 / 取证',
        'description': _profile_description('公开资料检索、来源整理、带引用交接；不运行代码。'),
        'primaryCapability': 'research',
        'capabilities': ['research', 'web_search', 'source_verification', 'summarization'],
        'tools': [*_FILE_TOOLS, *_WEB_TOOLS],
        'cost': 1.0,
    },
    'coder': {
        'label': '实现',
        'description': _profile_description('在独立 Docker 沙箱实现与测试代码、运行脚本、查阅公开资料；不提交或推送 Git。'),
        'primaryCapability': 'coding',
        'capabilities': ['coding', 'implementation', 'testing', 'repair'],
        'tools': [*_FILE_TOOLS, *_WEB_TOOLS, *_CODE_TOOLS],
        'cost': 1.3,
    },
    'release': {
        'label': '版本 / 发布',
        'description': _profile_description('核对可运行版本、编写发布说明、提交并推送 Git；不负责公开资料检索。'),
        'primaryCapability': 'release',
        'capabilities': ['release', 'versioning', 'delivery', 'git'],
        'tools': [*_FILE_TOOLS, *_CODE_TOOLS, *_GIT_TOOLS],
        'cost': 1.3,
    },
    'reviewer': {
        'label': '独立审查 / QA',
        'description': _profile_description('独立核验、测试与返工反馈；可跑 code.test 但不推送 Git。'),
        'primaryCapability': 'review',
        'capabilities': ['review', 'testing', 'verification', 'quality_assurance'],
        'tools': [*_FILE_TOOLS, *_CODE_TOOLS],
        'cost': 1.1,
    },
    'writer': {
        'label': '写作 / 编辑',
        'description': _profile_description('整理、改写与定稿文本内容；可读取公开来源，不运行代码。'),
        'primaryCapability': 'editing',
        'capabilities': ['editing', 'writing', 'publication', 'summarization'],
        'tools': [*_FILE_TOOLS, *_WEB_TOOLS],
        'cost': 1.0,
    },
    'analyst': {
        'label': '分析 / 风控',
        'description': _profile_description('市场、风险或数据分析；可读取来源并运行脚本；不推送 Git。'),
        'primaryCapability': 'market_analysis',
        'capabilities': ['market_analysis', 'data_analysis', 'risk_assessment', 'summarization'],
        'tools': [*_FILE_TOOLS, *_WEB_TOOLS, *_CODE_TOOLS],
        'cost': 1.2,
    },
}
EXAM_DEFAULT_PROFILES = {'agent1': 'planner', 'agent2': 'researcher',
                         'agent3': 'coder', 'agent4': 'reviewer'}


def list_exam_profiles():
    return [{'id': key, 'label': value['label'], 'description': value['description'],
             'primaryCapability': value['primaryCapability'],
             'capabilities': list(value['capabilities']),
             'tools': list(value['tools'])}
            for key, value in EXAM_PROFILES.items()]


def normalize_exam_profile_ids(value):
    """Accept one profile id or a list. Order follows the profile table."""
    if isinstance(value, str):
        value = [value]
    if (not isinstance(value, list) or not value
            or any(not isinstance(item, str) or not item for item in value)):
        raise ValueError('至少选择一个 capability profile。')
    if any(item not in EXAM_PROFILES for item in value):
        raise ValueError('未知的 capability profile。')
    selected = set(value)
    return [key for key in EXAM_PROFILES if key in selected]


def apply_exam_profiles(agent, profile_ids):
    """Union the selected profiles. Permissions stay inside the profile table."""
    ids = normalize_exam_profile_ids(profile_ids)
    profiles = [EXAM_PROFILES[key] for key in ids]
    tools, capabilities = [], []
    for profile in profiles:
        for tool in profile['tools']:
            if tool not in tools:
                tools.append(tool)
        for capability in profile['capabilities']:
            if capability not in capabilities:
                capabilities.append(capability)
    agent['profileIds'] = ids
    agent['profileId'] = ids[0]
    agent['primaryCapability'] = profiles[0]['primaryCapability']
    agent['capabilities'] = capabilities
    agent['tools'] = tools
    agent['cost'] = max(profile['cost'] for profile in profiles)
    return ids


def apply_exam_profile(agent, profile_id):
    """Rewrite one agent dict so its capability surface matches a profile.

    Returns the applied profile id. Raises ValueError for unknown profiles.
    """
    return apply_exam_profiles(agent, [profile_id])[0]


def _exam_agent(slot, profile_id):
    agent = {
        'name': 'Agent ' + str(slot), 'label': 'Agent ' + str(slot),
        'role': 'Agent ' + str(slot),
        'description': EXAM_PLACEHOLDER, 'rolePrompt': EXAM_PLACEHOLDER,
        'rolePromptCustomizable': True, 'rolePromptInjected': False,
        'sandbox': _sandbox('agent' + str(slot)),
    }
    apply_exam_profile(agent, profile_id)
    return agent

TEAM_DEFINITIONS[EXAM_TEAM_ID] = {
    'id': EXAM_TEAM_ID, 'name': '期末测试团队',
    'description': '四个未命名 Agent，其身份提示词与能力 profile 在工作台运行前由人工选择；工具白名单仍由后端固定。',
    'customRolePrompts': True, 'customProfiles': True,
    'agents': {slot: _exam_agent(int(slot[-1]), EXAM_DEFAULT_PROFILES[slot])
               for slot in ('agent1', 'agent2', 'agent3', 'agent4')},
}

def is_exam_team(team_id):
    return team_id == EXAM_TEAM_ID

def exam_role_prompt_limit():
    return EXAM_MAX_ROLE_PROMPT_CHARS

def exam_role_prompt_placeholder():
    return EXAM_PLACEHOLDER

_CUSTOM_LOCK = threading.Lock()
_CUSTOM_TEAMS = {}
_REGISTRY_PATH = None
_CUSTOM_ID = re.compile(r'^custom-[0-9a-f]{8}$')
_MAX_CUSTOM_TEAMS = 12
_MAX_TEAM_NAME = 40
_CUSTOM_DESCRIPTION = (
    '自建团队。四个未命名 Agent，可选择能力 profile 并注入角色提示词；'
    '工具白名单仍由后端 profile 表固定。')


def _team_summary(team):
    return {key: team[key] for key in ('id', 'name', 'description')}


def team_record(team_id):
    """Built-in definition, else a user-created team, else None."""
    if team_id in TEAM_DEFINITIONS:
        return TEAM_DEFINITIONS[team_id]
    return _CUSTOM_TEAMS.get(team_id)


def allows_role_setup(team_id):
    """Exam team and user-created teams can take profiles and role prompts."""
    record = team_record(team_id)
    return bool(record and record.get('customRolePrompts') and record.get('customProfiles'))


def list_teams():
    with _CUSTOM_LOCK:
        custom = [_team_summary(team) for team in _CUSTOM_TEAMS.values()]
    return [_team_summary(team) for team in TEAM_DEFINITIONS.values()] + custom


def get_team_agents(team_id=DEFAULT_TEAM):
    record = team_record(team_id)
    if record is None:
        raise ValueError('未知团队。')
    return copy.deepcopy(record['agents'])


def _valid_team_name(name, existing):
    if not isinstance(name, str):
        raise ValueError('请填写团队名称。')
    cleaned = name.strip()
    if not cleaned:
        raise ValueError('请填写团队名称。')
    if len(cleaned) > _MAX_TEAM_NAME:
        raise ValueError('团队名称不能超过 ' + str(_MAX_TEAM_NAME) + ' 个字符。')
    if any(ord(char) < 32 or char in '\\/' for char in cleaned):
        raise ValueError('团队名称不能包含控制字符或路径符号。')
    if cleaned in existing:
        raise ValueError('已有同名团队。')
    return cleaned


def _custom_definition(team_id, name):
    return {
        'id': team_id, 'name': name, 'description': _CUSTOM_DESCRIPTION,
        'customRolePrompts': True, 'customProfiles': True,
        'agents': {slot: _exam_agent(int(slot[-1]), EXAM_DEFAULT_PROFILES[slot])
                   for slot in ('agent1', 'agent2', 'agent3', 'agent4')},
    }


def _write_custom_registry(path):
    payload = {'version': 1, 'teams': [
        {'id': team['id'], 'name': team['name'], 'createdAt': team['createdAt']}
        for team in _CUSTOM_TEAMS.values()]}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def load_custom_teams(data_dir):
    """Replace the in-memory registry with the file in this data directory."""
    path = Path(data_dir) / 'custom_teams.json'
    loaded = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError('自建团队登记无法读取。') from exc
        if not isinstance(raw, dict) or raw.get('version') != 1 or not isinstance(raw.get('teams'), list):
            raise ValueError('自建团队登记格式无效。')
        if len(raw['teams']) > _MAX_CUSTOM_TEAMS:
            raise ValueError('自建团队登记超过上限。')
        names = {team['name'] for team in TEAM_DEFINITIONS.values()}
        for item in raw['teams']:
            if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                    or _CUSTOM_ID.fullmatch(item['id']) is None or item['id'] in loaded):
                raise ValueError('自建团队登记包含无效标识。')
            name = _valid_team_name(item.get('name'), names)
            names.add(name)
            record = _custom_definition(item['id'], name)
            record['createdAt'] = item.get('createdAt') if isinstance(item.get('createdAt'), str) else ''
            loaded[item['id']] = record
    global _REGISTRY_PATH
    with _CUSTOM_LOCK:
        _CUSTOM_TEAMS.clear()
        _CUSTOM_TEAMS.update(loaded)
        _REGISTRY_PATH = path
    return [_team_summary(team) for team in loaded.values()]


def create_custom_team(name):
    """Add one blank team with the exam slot layout. Profiles and prompts come later."""
    with _CUSTOM_LOCK:
        if _REGISTRY_PATH is None:
            raise ValueError('团队登记尚未打开。')
        if len(_CUSTOM_TEAMS) >= _MAX_CUSTOM_TEAMS:
            raise ValueError('最多新建 ' + str(_MAX_CUSTOM_TEAMS) + ' 个团队。')
        names = {team['name'] for team in TEAM_DEFINITIONS.values()}
        names.update(team['name'] for team in _CUSTOM_TEAMS.values())
        cleaned = _valid_team_name(name, names)
        team_id = 'custom-' + secrets.token_hex(4)
        while team_id in _CUSTOM_TEAMS or team_id in TEAM_DEFINITIONS:
            team_id = 'custom-' + secrets.token_hex(4)
        record = _custom_definition(team_id, cleaned)
        record['createdAt'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        _CUSTOM_TEAMS[team_id] = record
        try:
            _write_custom_registry(_REGISTRY_PATH)
        except OSError:
            _CUSTOM_TEAMS.pop(team_id, None)
            raise ValueError('自建团队未能保存。') from None
        return _team_summary(record)


def clear_custom_teams():
    """Drop the in-memory registry. Tests use this so later cases see built-in teams only."""
    global _REGISTRY_PATH
    with _CUSTOM_LOCK:
        _CUSTOM_TEAMS.clear()
        _REGISTRY_PATH = None