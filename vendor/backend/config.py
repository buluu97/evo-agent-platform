"""Local configuration. Secrets never enter the public state or tool environments."""
from pathlib import Path
from urllib.parse import urlsplit
import base64
import os
import re


class Config:
    def __init__(self, root: Path, env: dict | None = None):
        self.root = Path(root).resolve()
        values = {}
        env_file = self.root / '.env'
        if env_file.is_file():
            for line in env_file.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('\"\'')
        values.update(os.environ if env is None else env)
        self.provider = values.get('LLM_PROVIDER', 'hkgai').lower()
        if self.provider not in ('hkgai', 'deepseek'):
            raise ValueError('课程只允许 hkgai 或 deepseek 模型服务。')
        default_base = 'https://api1.hkgai.net/v1' if self.provider == 'hkgai' else 'https://api.deepseek.com/v1'
        self.base_url = values.get('LLM_BASE_URL', default_base).rstrip('/')
        parsed = urlsplit(self.base_url)
        allowed = ('hkgai.net', 'hkchat.app') if self.provider == 'hkgai' else ('deepseek.com',)
        if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not any(parsed.hostname == host or (parsed.hostname or '').endswith('.' + host) for host in allowed)):
            raise ValueError('LLM_BASE_URL 必须是对应课程允许服务的 HTTPS 地址。')
        self.api_key = values.get('LLM_API_KEY', '')
        self.serpapi_api_key = values.get('SERPAPI_API_KEY', '')
        if (not isinstance(self.serpapi_api_key, str) or len(self.serpapi_api_key) > 4096
                or any(ord(char) < 33 or ord(char) == 127 for char in self.serpapi_api_key)):
            raise ValueError('SERPAPI_API_KEY must be a bounded token without whitespace or control characters.')
        self.model = values.get('LLM_MODEL', 't2_hkgai-v3_fp8_1m_e7' if self.provider == 'hkgai' else 'deepseek-chat')
        transport_mode=values.get('LLM_TRANSPORT_MODE','standard')
        if not isinstance(transport_mode,str):
            raise ValueError('LLM_TRANSPORT_MODE 必须为 standard 或 compatible。')
        self.transport_mode = transport_mode.strip().lower()
        if self.transport_mode not in ('standard','compatible'):
            raise ValueError('LLM_TRANSPORT_MODE 必须为 standard 或 compatible。')
        thinking_mode=values.get('LLM_THINKING_MODE','default')
        if not isinstance(thinking_mode,str):
            raise ValueError('LLM_THINKING_MODE 必须为 default 或 disabled。')
        self.thinking_mode=thinking_mode.strip().lower()
        if self.thinking_mode not in ('default','disabled'):
            raise ValueError('LLM_THINKING_MODE 必须为 default 或 disabled。')
        if self.thinking_mode=='disabled' and self.provider!='hkgai':
            raise ValueError('LLM_THINKING_MODE=disabled 目前仅在 HKGAI 验证，不能用于其他 provider。')
        json_mode=values.get('LLM_JSON_MODE','default')
        if not isinstance(json_mode,str):
            raise ValueError('LLM_JSON_MODE 必须为 default 或 json_object。')
        self.json_mode=json_mode.strip().lower()
        if self.json_mode not in ('default','json_object'):
            raise ValueError('LLM_JSON_MODE 必须为 default 或 json_object。')
        if self.json_mode=='json_object' and self.provider!='hkgai':
            raise ValueError('LLM_JSON_MODE=json_object 目前仅在 HKGAI 验证，不能用于其他 provider。')
        self.host = values.get('HOST', '127.0.0.1')
        self.port = int(values.get('PORT', '8080'))
        self.data_dir = self.root / '.runtime'
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.docker_image = values.get('DOCKER_IMAGE', 'python:3.11-alpine')
        self.browser_image = values.get('BROWSER_IMAGE', 'evo-agent-browser:1.64.1')
        self.git_remote = values.get('GIT_REMOTE', '')
        self.git_token = values.get('GIT_TOKEN', '')
        self.git_username = values.get('GIT_USERNAME', 'x-access-token')
        self.max_model_calls = max(2, min(60, int(values.get('MAX_MODEL_CALLS', '28'))))
        self.max_agent_turns = max(2, min(32, int(values.get('MAX_AGENT_TURNS', '8'))))
        self.model_timeout = max(5, min(180, int(values.get('MODEL_TIMEOUT', '120'))))
        self.max_completion_tokens = max(256, min(8000, int(values.get('MODEL_MAX_TOKENS', '6000'))))
        self.planning_max_tokens = int(values.get('PLANNING_MAX_TOKENS', '3000'))
        self.max_run_seconds = max(10, min(3600, int(values.get('MAX_RUN_SECONDS', '600'))))
        self.max_run_tokens = max(1000, min(500000, int(values.get('MAX_RUN_TOKENS', '48000'))))
        self.max_tasks = max(2, min(32, int(values.get('MAX_TASKS', '12'))))
        self.max_depth = max(1, min(5, int(values.get('MAX_DELEGATION_DEPTH', '3'))))
        self.parallel_tasks = max(1, min(4, int(values.get('PARALLEL_TASKS', '2'))))
        self.max_retries = max(0, min(3, int(values.get('MAX_RETRIES', '1'))))
        # Per text-generation operation only. Review/tool retry budgets retain
        # their existing meaning and the run deadline is never replenished.
        self.model_generation_retries=max(0,min(3,int(values.get('MODEL_GENERATION_RETRIES','2'))))
        self.approval_ttl = max(30, min(86400, int(values.get('APPROVAL_TTL_SECONDS', '900'))))
        # Deterministic observation cadence only; never extends run/model limits.
        self.progress_watchdog_seconds = max(60, min(120, int(values.get('PROGRESS_WATCHDOG_SECONDS', '90'))))
        self.max_context_chars = max(4000, min(80000, int(values.get('MAX_CONTEXT_CHARS', '28000'))))
        self.initial_context_chars=self.max_context_chars
        self.execution_profile='bounded'

    def apply_delivery_profile(self):
        """Apply explicit process-local delivery limits; never persist or change provider settings."""
        self.execution_profile='bounded'
        self.max_agent_turns = 24
        self.max_model_calls = 60
        self.max_run_tokens = 400000
        self.max_run_seconds = 1800
        self.max_context_chars = 40000
        self.max_retries = 1
        return {'maxAgentTurns': self.max_agent_turns, 'maxModelCalls': self.max_model_calls,
                'maxRunTokens': self.max_run_tokens, 'maxRunSeconds': self.max_run_seconds,
                'maxContextChars': self.max_context_chars, 'maxRetries': self.max_retries}

    def apply_time_only_profile(self):
        """Explicit process-local profile; effective run limits are truly null."""
        if self.execution_profile!='time_only':
            self._bounded_agent_turns=self.max_agent_turns
        self.execution_profile='time_only'
        self.max_agent_turns=self.max_model_calls=self.max_run_tokens=None
        self.max_run_seconds=1800
        self.max_context_chars=120000
        self.initial_context_chars=40000
        return {'profile':'time_only','maxAgentTurns':None,'maxModelCalls':None,'maxRunTokens':None,
                'maxRunSeconds':1800,'timeBasis':'wall_including_human_pause_and_restart',
                'maxContextChars':self.max_context_chars,'initialContextChars':self.initial_context_chars,
                'maxCompletionTokens':self.max_completion_tokens}

    @property
    def planning_max_tokens(self):
        # Harnesses may raise the completion budget after loading configuration.
        # Keep the requested planning setting, but never exceed the current cap.
        return min(self._planning_max_tokens, self.max_completion_tokens)

    @planning_max_tokens.setter
    def planning_max_tokens(self, value):
        self._planning_max_tokens = max(128, min(8000, int(value)))

    def scrub(self, value):
        if isinstance(value, str):
            if self.api_key:
                value = value.replace(self.api_key, '[REDACTED]')
            if self.serpapi_api_key:
                value = value.replace(self.serpapi_api_key, '[REDACTED]')
            if self.git_token:
                value = value.replace(self.git_token, '[REDACTED]')
                encoded=base64.b64encode((self.git_username+':'+self.git_token).encode()).decode()
                value=value.replace(encoded,'[REDACTED]')
            return re.sub(r'\bsk-[A-Za-z0-9_-]{16,}', '[REDACTED]', value)
        if isinstance(value, dict):
            return {key: self.scrub(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub(item) for item in value]
        return value
