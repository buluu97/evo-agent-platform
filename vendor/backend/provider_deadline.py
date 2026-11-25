"""Opt-in killable provider boundary; never cancels an upstream server request.

The normal provider path does not import or use this module. A deadline bounds
the parent's slot/pipe wait and the lifetime of its dedicated network process,
including blocking DNS and TLS pacing. SIGKILL/reaping has a bounded cleanup
allowance; kernel/process-creation failures are not a claimed hard real-time SLA.
"""
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

from .provider import ProviderError
from .generation_retry import trusted_generation_failure

MAX_REQUEST_BYTES = 2_000_000
MAX_WORKER_BYTES = 2_000_000
REAP_SECONDS = 1.0
_SETTINGS = ('provider', 'base_url', 'api_key', 'model', 'model_timeout',
             'max_completion_tokens', 'transport_mode', 'thinking_mode', 'json_mode')


def _worker_command():
    # Trusted local source only. No request, secret, user script or config path
    # is placed in argv. -I also prevents PYTHONPATH/site-user code injection.
    return [sys.executable, '-I', '-B', '-u', str(Path(__file__).with_name('provider_worker.py'))]


def _worker_environment():
    # Keep the existing proxy/VPN/TLS route. Do not forward LLM/Git keys or the
    # full service environment to a child that needs only provider credentials.
    names = {'PATH', 'HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TMPDIR',
             'SYSTEMROOT', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUEST_METHOD'}
    names.update(name for name in os.environ if name.lower() in
                 {'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'ftp_proxy'})
    return {name: os.environ[name] for name in names if name in os.environ}


def _stop_owned_group(process):
    """Kill exactly the session created below, then actually reap its leader."""
    started = time.monotonic()
    killed = False
    # Once reaped, its numeric PID can eventually be reused. Never send a
    # signal to a previously reaped leader on a later protocol-error path.
    if process.returncode is not None:
        return {'workerTerminated': True, 'workerReaped': True,
                'workerGroupKillSent': False, 'cleanupMs': 0.0}
    try:
        os.killpg(process.pid, signal.SIGKILL)
        killed = True
    except ProcessLookupError:
        pass
    except OSError:
        # No broad process scans or names: target only this owned process.
        try:
            process.kill()
            killed = True
        except OSError:
            pass
    try:
        process.wait(timeout=REAP_SECONDS)
        reaped = True
    except (subprocess.TimeoutExpired, OSError):
        reaped = False
    return {'workerTerminated': process.poll() is not None, 'workerReaped': reaped,
            'workerGroupKillSent': killed,
            'cleanupMs': round((time.monotonic() - started) * 1000, 3)}


def _exchange(payload, deadline):
    """Bound both directions of the pipe without communicate's unbounded buffer."""
    process = None
    output = bytearray()
    diag = {'deadlineBoundary': 'provider_process', 'workerStarted': False,
            'requestMayContinueServerSide': False, 'usageUnknown': False,
            'workerResponseBytes': 0}

    def fail(message, code, phase):
        if process is not None:
            diag.update(_stop_owned_group(process))
            if not diag.get('workerReaped'):
                diag['causeCode'] = code
                message = '无法确认网络进程已经终止；未声称取消成功，也不会自动重发。'
                code, phase = 'worker_termination_failed', 'worker_cleanup'
        raise ProviderError(message, code, False, phase=phase, transport=diag)

    if time.monotonic() >= deadline:
        fail('模型调用在启动网络进程前已到截止时间；未发送请求。', 'deadline_exceeded', 'worker_start')
    if os.name != 'posix':
        fail('此可终止网络进程边界目前仅支持 POSIX；没有降级到线程。', 'deadline_unsupported', 'worker_start')
    try:
        process = subprocess.Popen(_worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=_worker_environment(),
                                   close_fds=True, start_new_session=True)
        # Once started, absence of an answer cannot prove that the server did
        # not receive or continue the request. Do not replay automatically.
        diag.update(workerStarted=True, requestMayContinueServerSide=True, usageUnknown=True)
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, 'input')
            selector.register(process.stdout, selectors.EVENT_READ, 'output')
            position = 0
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    fail('模型网络进程达到截止时间；本地进程已要求终止，上游调用可能仍消耗未返回的用量。',
                         'deadline_exceeded', 'worker_deadline')
                for key, _ in selector.select(remaining):
                    if time.monotonic() >= deadline:
                        fail('模型结果到达时已超过截止时间，未采用晚到结果。', 'deadline_exceeded', 'worker_deadline')
                    if key.data == 'input':
                        try:
                            position += os.write(key.fileobj.fileno(), payload[position:position + 65536])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            position = len(payload)
                        if position == len(payload):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        try:
                            block = os.read(key.fileobj.fileno(), min(65536, MAX_WORKER_BYTES + 1 - len(output)))
                        except BlockingIOError:
                            continue
                        if block:
                            output.extend(block)
                            diag['workerResponseBytes'] = len(output)
                            if len(output) > MAX_WORKER_BYTES:
                                fail('网络进程返回数据超过限制；未采用部分响应。', 'worker_output_limit', 'worker_read')
                        else:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail('模型响应超过截止时间，未采用晚到结果。', 'deadline_exceeded', 'worker_deadline')
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            fail('模型网络进程未在截止时间内退出。', 'deadline_exceeded', 'worker_deadline')
        if time.monotonic() >= deadline:
            fail('模型进程退出时已超过截止时间，未采用晚到结果。', 'deadline_exceeded', 'worker_deadline')
        diag.update(workerTerminated=True, workerReaped=True, workerExitCode=returncode)
        if returncode != 0:
            fail('网络进程异常退出；未将不完整输出作为模型结果。', 'worker_transport', 'worker_exit')
        return bytes(output), diag
    except ProviderError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        # Do not expose child stderr, config, payload or raw OS exception text.
        fail('无法完成有界网络进程通信；未自动重发请求。', 'worker_transport', 'worker_pipe')
    finally:
        if process is not None:
            for stream in (process.stdin, process.stdout):
                if stream is not None and not stream.closed:
                    stream.close()


def complete_before_deadline(client, messages, max_tokens, deadline, *, json_protocol=False):
    started = time.monotonic()
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise ProviderError('deadline 必须为有限的 absolute time.monotonic 值。', 'parameters',
                            phase='deadline_validation')
    if deadline <= started:
        raise ProviderError('模型调用已到截止时间；未启动进程或发送请求。', 'deadline_exceeded',
                            phase='wait_slot', transport={'workerStarted': False,
                                'requestMayContinueServerSide': False, 'usageUnknown': False})
    acquired = client._slots.acquire(timeout=max(0, deadline - time.monotonic()))
    if not acquired:
        raise ProviderError('等待模型连接槽时达到截止时间；未启动网络进程。', 'deadline_exceeded',
                            phase='wait_slot', transport={'workerStarted': False,
                                'requestMayContinueServerSide': False, 'usageUnknown': False})
    try:
        settings = {name: getattr(client.config, name, 'default' if name in ('thinking_mode', 'json_mode')
                                  else 'standard' if name == 'transport_mode' else None) for name in _SETTINGS}
        try:
            payload = json.dumps({'settings': settings, 'messages': messages, 'max_tokens': max_tokens,
                                  'json_protocol': json_protocol}, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (ValueError, TypeError, UnicodeError):
            raise ProviderError('网络进程请求不能编码为有限 UTF-8 JSON。', 'parameters', phase='worker_prepare') from None
        if len(payload) > MAX_REQUEST_BYTES:
            raise ProviderError('网络进程请求超过大小限制。', 'parameters', phase='worker_prepare')
        raw, diag = _exchange(payload, deadline)
        try:
            envelope = json.loads(raw)
            # Reject overflow-to-inf, NaN, malformed Unicode and partial JSON.
            json.dumps(envelope, allow_nan=False, ensure_ascii=False).encode('utf-8')
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ProviderError('网络进程未返回完整有效的有界 JSON；未重发请求。', 'worker_protocol',
                                phase='worker_decode', transport=diag) from None
        if not isinstance(envelope, dict) or type(envelope.get('ok')) is not bool:
            raise ProviderError('网络进程响应协议无效。', 'worker_protocol', phase='worker_decode', transport=diag)
        if time.monotonic() >= deadline:
            raise ProviderError('模型结果解析时已超过截止时间，未采用晚到结果。', 'deadline_exceeded',
                                phase='worker_deadline', transport=diag)
        if envelope['ok']:
            result = envelope.get('result')
            if not isinstance(result, dict) or not isinstance(result.get('content'), str):
                raise ProviderError('网络进程的模型结果结构无效。', 'worker_protocol', phase='worker_decode', transport=diag)
            transport = result.get('transport')
            result['transport'] = {**(transport if isinstance(transport, dict) else {}), **diag,
                                   'requestMayContinueServerSide': False,
                                   'usageUnknown': not bool(result.get('usage')),
                                   'deadlineBoundaryElapsedMs': round((time.monotonic() - started) * 1000, 3)}
            return client.config.scrub(result)
        error = envelope.get('error')
        if not isinstance(error, dict):
            raise ProviderError('网络进程错误协议无效。', 'worker_protocol', phase='worker_decode', transport=diag)
        phase = error.get('phase') if isinstance(error.get('phase'), str) else 'worker_response'
        not_sent = phase in ('prepare_request', 'wait_slot', 'worker_prepare')
        transport = error.get('transport')
        transport = {**(transport if isinstance(transport, dict) else {}), **diag,
                     'requestMayContinueServerSide': not not_sent, 'usageUnknown': not not_sent}
        # A worker error returned no accepted model result or executed action.
        # Its generation-only retry signal is separate from generic retryable:
        # the latter stays false so no caller reruns the entire task or tools.
        transport.update(modelResultAccepted=False,upstreamRetryable=error.get('retryable') is True)
        transport['generationRetryEligible']=trusted_generation_failure(str(error.get('code','provider')),phase,
                                                                        transport['upstreamRetryable'],transport)
        if transport['generationRetryEligible']:transport['retryScope']='model_generation_only'
        raise ProviderError(client.config.scrub(str(error.get('message', '模型网络进程失败。')))[:1000],
                            str(error.get('code', 'provider'))[:80], False,
                            phase=phase, transport=client.config.scrub(transport))
    finally:
        client._slots.release()
