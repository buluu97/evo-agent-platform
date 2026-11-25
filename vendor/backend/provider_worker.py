"""Trusted one-request provider subprocess. Config enters only through stdin."""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.provider import ModelClient, ProviderError

_LIMIT = 2_000_000


class _WorkerConfig:
    def __init__(self, values):
        if not isinstance(values, dict):
            raise ValueError('Invalid settings')
        for name in ('provider', 'base_url', 'api_key', 'model', 'transport_mode', 'thinking_mode', 'json_mode'):
            value = values.get(name)
            if not isinstance(value, str) or '\0' in value or len(value) > (16000 if name == 'api_key' else 2000):
                raise ValueError('Invalid provider setting')
            setattr(self, name, value)
        if self.provider not in ('hkgai', 'deepseek'):
            raise ValueError('Invalid provider')
        url = urlsplit(self.base_url)
        domains = ('hkgai.net', 'hkchat.app') if self.provider == 'hkgai' else ('deepseek.com',)
        if (url.scheme != 'https' or url.username or url.password or url.query or url.fragment
                or not any(url.hostname == host or (url.hostname or '').endswith('.' + host) for host in domains)):
            raise ValueError('Invalid provider URL')
        for name, upper in (('model_timeout', 180), ('max_completion_tokens', 8000)):
            value = values.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= upper:
                raise ValueError('Invalid provider limit')
            if name == 'max_completion_tokens' and not isinstance(value, int):
                raise ValueError('Invalid completion limit')
            setattr(self, name, value)

    def scrub(self, value):
        if isinstance(value, str):
            if self.api_key:
                value = value.replace(self.api_key, '[REDACTED]')
            return re.sub(r'\bsk-[A-Za-z0-9_-]{16,}', '[REDACTED]', value)
        if isinstance(value, dict):
            return {key: self.scrub(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub(item) for item in value]
        return value


def main():
    try:
        raw = sys.stdin.buffer.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            raise ValueError('Request too large')
        packet = json.loads(raw)
        if not isinstance(packet, dict) or type(packet.get('json_protocol')) is not bool:
            raise ValueError('Invalid request')
        config = _WorkerConfig(packet['settings'])
        result = ModelClient(config)._complete(packet.get('messages'), packet.get('max_tokens'),
                                               json_protocol=packet['json_protocol'])
        response = {'ok': True, 'result': result}
    except ProviderError as error:
        response = {'ok': False, 'error': {'message': str(error), 'code': error.code,
                    'retryable': error.retryable, 'phase': error.phase, 'transport': error.transport}}
    except BaseException:
        # No request/config/exception repr or child traceback reaches logs.
        response = {'ok': False, 'error': {'message': '网络进程的输入或执行无效。',
                    'code': 'worker_protocol', 'phase': 'worker_prepare'}}
    try:
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > _LIMIT:
            raise ValueError('Output too large')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        encoded = b'{"ok":false,"error":{"message":"Bounded worker response unavailable.","code":"worker_output_limit","phase":"worker_encode"}}'
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
