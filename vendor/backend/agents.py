"""Server-owned heterogeneous agents. Tools are allowlisted, never selected by a prompt."""
_COMMON = ['file.read', 'file.list', 'file.write', 'file.patch', 'file.delete']

def _sandbox(agent_id):
    return {
        'id': agent_id,
        'workspace': f'workspaces/{agent_id}/<run-id>',
        'imports': f'imports/{agent_id}/<run-id> (read-only)',
        'policy': 'Private run workspace; explicit artifact imports only. Code runs in a separate network-disabled Docker container, never on the host.',
    }

AGENTS = {
    'coordinator': {
        'name': 'Coordinator', 'label': '协调员', 'role': '任务拆解、依赖管理、协调与汇总',
        'description': '规划任务 DAG、根据能力委派任务、处理澄清并汇总可核验的成果。',
        'primaryCapability': 'planning',
        'capabilities': ['planning', 'delegation', 'synthesis', 'clarification'], 'cost': 1.0,
        'tools': [*_COMMON], 'sandbox': _sandbox('coordinator'),
    },
    'research': {
        'name': 'Research', 'label': '研究员', 'role': '公开资料搜索、来源整理与交接',
        'description': '搜索公开资料，核对来源与适用时间，产出带引用的文本成果。',
        'primaryCapability': 'research',
        'capabilities': ['research', 'web_search', 'source_verification', 'summarization'], 'cost': 1.0,
        'tools': [*_COMMON, 'web.search', 'web.fetch'], 'sandbox': _sandbox('research'),
    },
    'executor': {
        'name': 'Executor', 'label': '执行员', 'role': '代码、数据处理、测试、修复和 Git',
        'description': '在独立 Docker 沙箱实现和测试代码，记录修复证据；远端推送必须取得具体授权。',
        'primaryCapability': 'coding',
        'capabilities': ['coding', 'data_analysis', 'testing', 'repair', 'git'], 'cost': 1.3,
        'tools': [*_COMMON, 'code.run', 'code.test', 'git.status', 'git.commit', 'git.push'], 'sandbox': _sandbox('executor'),
    },
    'reviewer': {
        'name': 'Reviewer', 'label': '审核员', 'role': '独立核验、测试与返工反馈',
        'description': '在自己的可写工作区运行验证；他人授权成果以只读副本导入，保留独立验收证据。',
        'primaryCapability': 'review',
        'capabilities': ['review', 'testing', 'verification', 'quality_assurance'], 'cost': 1.1,
        'tools': [*_COMMON, 'code.run', 'code.test'], 'sandbox': _sandbox('reviewer'),
    },
}
