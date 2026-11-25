"""Path confinement and bounded Docker execution; no host-code fallback."""
from pathlib import Path, PurePosixPath
import copy
import errno
import hashlib
import json
import math
import os
import re
import selectors
import signal
import shutil
import stat
import subprocess
import threading
import time
import uuid

from .agents import AGENTS

MAX_FILE_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
SAFE_TEXT_SUFFIXES = {'.md', '.txt', '.py', '.json', '.csv', '.tsv', '.log', '.yaml', '.yml', '.toml', '.ini', '.js', '.mjs', '.cjs', '.html', '.css', '.sql', '.xml'}
_ID = re.compile(r'^[A-Za-z0-9_-]{1,96}$')
_IMAGE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9./_:@-]{0,180}$')


class ToolError(Exception):
    def __init__(self, code, message, retryable=False, *, outcome_unknown=False):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.retryable = bool(retryable)
        self.outcome_unknown = bool(outcome_unknown)


def check_deadline(deadline, *, outcome_unknown=False):
    """Validate a server-owned absolute monotonic deadline without changing shared state."""
    if deadline is None:
        return
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise ToolError('parameter', 'deadline must be a finite absolute monotonic time.')
    if time.monotonic() >= deadline:
        suffix = ' The operation may have completed; reconcile its real state before retrying.' if outcome_unknown else ' No new operation was started.'
        raise ToolError('deadline', 'The run time deadline has expired.' + suffix, outcome_unknown=outcome_unknown)


def bounded_timeout(timeout, deadline=None):
    check_deadline(deadline)
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        check_deadline(deadline)
    return min(timeout, remaining)


def identifier(value, label='identifier'):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ToolError('parameter', f'{label} must be 1–96 letters, digits, underscores or hyphens.')
    return value


def relative_parts(value, allow_root=False):
    if not isinstance(value, str) or len(value) > 240 or not value:
        raise ToolError('parameter', 'path must be a nonempty relative string of at most 240 characters.')
    if allow_root and value in ('.', ''):
        return ()
    if value.startswith(('/', '~')) or '\\' in value or ':' in value or any(ord(c) < 32 for c in value):
        raise ToolError('permission', 'Absolute, device and control-character paths are forbidden.')
    pieces = value.split('/')
    if len(pieces) > 12 or any(not p or p in ('.', '..') or p.startswith('.') for p in pieces):
        raise ToolError('permission', 'Traversal, hidden files, .git and .env paths are forbidden.')
    if any(len(p) > 120 for p in pieces):
        raise ToolError('parameter', 'A path component is too long.')
    return tuple(pieces)


def secure_directory(base, *parts):
    """Create private directories, refusing symlinks in application-controlled descendants."""
    base = Path(base)
    if base.is_symlink():
        raise ToolError('permission', 'Data root must not be a symlink.')
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = base
    for part in parts:
        identifier(part, 'directory name')
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ToolError('permission', 'A workspace directory is not a real directory.')
    return current


def confined_path(base, value, allow_root=False):
    parts = relative_parts(value, allow_root)
    base = Path(base)
    if base.is_symlink():
        raise ToolError('permission', 'Symlinked directory is forbidden.')
    result = base
    for part in parts:
        result = result / part
        if result.is_symlink():
            raise ToolError('permission', 'Symlinked paths are forbidden.')
    try:
        result.resolve(strict=False).relative_to(base.resolve())
    except ValueError as error:
        raise ToolError('permission', 'Path escapes the private workspace.') from error
    return result


def _parent_fd(base, value, create=False):
    """Open each parent via openat/O_NOFOLLOW to prevent symlink-swap races."""
    parts = relative_parts(value)
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(base, flags)
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd, parts[-1]
    except OSError as error:
        if 'fd' in locals():
            os.close(fd)
        raise ToolError('permission' if error.errno in (errno.ELOOP, errno.ENOTDIR) else 'permanent', 'Cannot access a confined workspace path.') from error


def read_text(base, value, limit=MAX_FILE_BYTES):
    parent, name = _parent_fd(base, value)
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), dir_fd=parent)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ToolError('permission', 'Only regular, non-linked files may be read.')
        if info.st_size > limit:
            raise ToolError('parameter', 'File exceeds the text-size limit.')
        content = bytearray()
        while len(content) <= limit:
            block = os.read(fd, min(65536, limit + 1 - len(content)))
            if not block:
                break
            content.extend(block)
        if len(content) > limit or b'\0' in content:
            raise ToolError('parameter', 'File is too large or contains binary data.')
        try:
            return bytes(content).decode('utf-8')
        except UnicodeDecodeError as error:
            raise ToolError('parameter', 'Only UTF-8 text is supported.') from error
    except OSError as error:
        raise ToolError('permission' if error.errno in (errno.ELOOP, errno.EACCES) else 'permanent', 'Cannot read the requested regular file.') from error
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def write_text(base, value, content, overwrite=False):
    if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_FILE_BYTES or '\0' in content:
        raise ToolError('parameter', 'content must be UTF-8 text of at most 256 KiB.')
    parent, name = _parent_fd(base, value, create=True)
    fd = None
    try:
        # Open without truncation; verify inode/type before changing any existing file.
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        if not overwrite:
            flags |= os.O_EXCL
        fd = os.open(name, flags, 0o600, dir_fd=parent)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ToolError('permission', 'Only regular, non-linked files may be modified.')
        if overwrite:
            os.ftruncate(fd, 0)
        data = content.encode('utf-8')
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
    except OSError as error:
        raise ToolError('permission' if error.errno in (errno.ELOOP, errno.EEXIST, errno.EACCES) else 'permanent', 'Cannot modify the confined file.') from error
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def delete_file(base, value):
    parent, name = _parent_fd(base, value)
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ToolError('permission', 'Only regular, non-linked files may be deleted.')
        os.unlink(name, dir_fd=parent)
    except OSError as error:
        raise ToolError('permanent', 'Cannot delete the requested regular file.') from error
    finally:
        os.close(parent)


def run_process(argv, *, cwd=None, env=None, timeout=20, deadline=None, max_output_bytes=MAX_OUTPUT_BYTES):
    """No shell, bounded stdout/stderr, and a deadline. Output overflow terminates the process."""
    start = time.monotonic()
    if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= 4 * 1024 * 1024:
        raise ToolError('parameter', 'Invalid subprocess output bound.')
    timeout = bounded_timeout(timeout, deadline)
    stop_at = start + timeout if deadline is None else min(start + timeout, deadline)
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, start_new_session=True)
    except OSError as error:
        raise ToolError('permanent', f'Cannot start executable: {Path(argv[0]).name}.') from error
    selector = selectors.DefaultSelector()
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    for key, stream in (('stdout', process.stdout), ('stderr', process.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, key)
    timed_out = False
    overflow = False

    def kill():
        # Git helpers / trusted DNS children must not survive a killed parent process.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()

    try:
        while selector.get_map():
            remaining = stop_at - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for selected, _ in selector.select(min(0.1, remaining)):
                block = os.read(selected.fileobj.fileno(), 65536)
                if not block:
                    selector.unregister(selected.fileobj)
                    continue
                target = buffers[selected.data]
                room = max_output_bytes - len(target)
                target.extend(block[:max(0, room)])
                if len(block) > room:
                    overflow = True
                    break
            if overflow:
                break
        if timed_out or overflow:
            kill()
        try:
            process.wait(timeout=2 if timed_out or overflow else max(0.001, stop_at - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            kill()
            process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        kill()
        process.wait()
        raise ToolError('transient', 'Subprocess communication failed.', retryable=True)
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return {'exitCode': process.returncode, 'stdout': buffers['stdout'].decode('utf-8', 'replace'), 'stderr': buffers['stderr'].decode('utf-8', 'replace'), 'timedOut': timed_out, 'outputLimitExceeded': overflow, 'durationMs': round((time.monotonic() - start) * 1000)}


class SandboxManager:
    def __init__(self, config, agents=None):
        self.data_dir = Path(config.data_dir)
        self.namespace_id='ns-'+hashlib.sha256(str(self.data_dir.resolve()).encode()).hexdigest()[:20]
        self.agents = copy.deepcopy(AGENTS if agents is None else agents)
        if not isinstance(self.agents, dict) or not self.agents:
            raise ToolError('parameter', 'A nonempty agent registry is required.')
        for agent_id in self.agents:
            identifier(agent_id, 'agent_id')
        self.image = getattr(config, 'docker_image', 'python:3.11-alpine')
        self.browser_image = getattr(config, 'browser_image', 'evo-agent-browser:1.64.1')
        if not isinstance(self.image, str) or not _IMAGE.fullmatch(self.image):
            raise ToolError('parameter', 'Invalid Docker image name.')
        if not isinstance(self.browser_image, str) or not _IMAGE.fullmatch(self.browser_image):
            raise ToolError('parameter', 'Invalid browser Docker image name.')
        self.docker = shutil.which('docker')
        self._containers = {}
        self._cancelled = set()
        self._lock = threading.RLock()

    def workspace(self, agent_id, run_id):
        if agent_id not in self.agents:
            raise ToolError('permission', 'Unknown agent.')
        identifier(run_id, 'run_id')
        return secure_directory(self.data_dir, 'workspaces', agent_id, run_id)

    def imports(self, agent_id, run_id):
        if agent_id not in self.agents:
            raise ToolError('permission', 'Unknown agent.')
        identifier(run_id, 'run_id')
        return secure_directory(self.data_dir, 'imports', agent_id, run_id)

    def _isolation(self,agent_id,run_id,name):
        return {'ownerAgentId':agent_id,'runId':run_id,'namespaceId':self.namespace_id,'containerId':name,
                'workspace':'private_per_agent_run','imports':'explicit_read_only',
                'network':'none','resourceScope':'per_invocation','hostFallback':False}

    def _labels(self,agent_id,run_id):
        return ['--label','evo.agent='+agent_id,'--label','evo.run='+run_id,
                '--label','evo.namespace='+self.namespace_id]

    def _environment(self):
        # Docker CLI may use the user's Docker Desktop context; no model/API secrets are forwarded.
        return {key: value for key, value in os.environ.items() if key in ('PATH', 'HOME', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CONFIG', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH', 'LANG')}

    def capabilities(self, deadline=None):
        check_deadline(deadline)
        result = {'dockerInstalled': bool(self.docker), 'dockerAvailable': False, 'dockerImage': self.image, 'dockerImageReady': False,
                  'browserImage': self.browser_image, 'browserImageReady': False, 'javascriptAvailable': False,
                  'network': 'none', 'hostFallback': False}
        if not self.docker:
            result['reason'] = 'Docker CLI is not installed. Code execution requires Docker.'
            return result
        try:
            options = {} if deadline is None else {'deadline': deadline}
            info = run_process([self.docker, 'info', '--format', '{{.ServerVersion}}'], env=self._environment(), timeout=bounded_timeout(5, deadline), **options)
            result['dockerAvailable'] = info['exitCode'] == 0 and not info['timedOut']
            if result['dockerAvailable']:
                result['version'] = info['stdout'].strip()
                image = run_process([self.docker, 'image', 'inspect', '--format', '{{.Id}}', self.image], env=self._environment(), timeout=bounded_timeout(5, deadline), **options)
                result['dockerImageReady'] = image['exitCode'] == 0 and not image['timedOut']
                browser = run_process([self.docker, 'image', 'inspect', '--format', '{{.Id}}', self.browser_image], env=self._environment(), timeout=bounded_timeout(5, deadline), **options)
                result['browserImageReady'] = browser['exitCode'] == 0 and not browser['timedOut']
                result['javascriptAvailable'] = result['browserImageReady']
            if not result['dockerAvailable']:
                result['reason'] = 'Docker daemon is unavailable; start Docker Desktop/Engine.'
            elif not result['dockerImageReady']:
                result['reason'] = 'The configured image is not local. An operator must pull it explicitly.'
        except ToolError as error:
            if error.code == 'deadline':
                raise
            result['reason'] = error.message
        return result

    def run(self, agent_id, run_id, args, test=False, deadline=None):
        check_deadline(deadline)
        language = args.get('language', 'python')
        if language not in ('python', 'javascript', 'browser'):
            raise ToolError('parameter', 'language must be python, javascript, or browser.')
        if language == 'browser':
            return self._run_browser(agent_id, run_id, args, test=test, deadline=deadline)
        if any(key in args for key in ('checks', 'storage', 'resources')):
            raise ToolError('parameter', 'checks, storage, and resources are available only in browser mode.')
        workspace = self.workspace(agent_id, run_id)
        imports = self.imports(agent_id, run_id)
        with self._lock:
            if run_id in self._cancelled:
                raise ToolError('cancelled', 'This run was cancelled.')
        status = self.capabilities() if deadline is None else self.capabilities(deadline=deadline)
        check_deadline(deadline)
        ready = status.get('dockerImageReady') if language == 'python' else status.get('browserImageReady')
        if not status['dockerAvailable'] or not ready:
            raise ToolError('permanent', status.get('reason', 'Docker is unavailable.'))
        supplied_code = args.get('code')
        supplied_path = args.get('path')
        if bool(supplied_code is not None) == bool(supplied_path is not None):
            raise ToolError('parameter', 'Supply exactly one of code or path.')
        # Reject malformed requests before creating a generated private script.
        arguments = args.get('args', [])
        if not isinstance(arguments, list) or len(arguments) > 30 or any(not isinstance(item, str) or len(item) > 1024 or '\0' in item for item in arguments):
            raise ToolError('parameter', 'args must contain at most 30 short strings.')
        duration = args.get('timeout', 20)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 1 <= duration <= 60:
            raise ToolError('parameter', 'timeout must be between 1 and 60 seconds.')
        if any(',' in str(path) for path in (workspace, imports)):
            raise ToolError('parameter', 'Docker bind paths cannot contain commas.')
        generated = None
        if supplied_code is not None:
            if not isinstance(supplied_code, str) or not supplied_code.strip() or len(supplied_code.encode('utf-8')) > 64 * 1024:
                raise ToolError('parameter', 'code must be nonempty source of at most 64 KiB.')
            generated = f'run_{uuid.uuid4().hex}.' + ('py' if language == 'python' else 'cjs')
            check_deadline(deadline)
            write_text(workspace, generated, supplied_code)
            script = '/workspace/' + generated
        else:
            relative_parts(supplied_path)
            if supplied_path.startswith('imports/'):
                imported = supplied_path[len('imports/'):]
                read_text(imports, imported)
                script = '/imports/' + imported
            else:
                read_text(workspace, supplied_path)
                script = '/workspace/' + supplied_path
        name = 'evo-' + uuid.uuid4().hex
        entrypoint = ['--entrypoint', 'python3', self.image, '-B', '-s', '-E'] if language == 'python' else ['--entrypoint', 'node', self.browser_image, '--max-old-space-size=128']
        command = [self.docker, 'run', '--rm', '--pull=never', '--name', name,
                   *self._labels(agent_id,run_id),
                   '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                   '--memory=256m', '--memory-swap=256m', '--cpus=1', '--pids-limit=64',
                   '--ulimit', 'fsize=8388608:8388608', '--ulimit', 'nofile=128:128',
                   '--user', f'{os.getuid()}:{os.getgid()}',
                   '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777',
                   '--mount', f'type=bind,src={workspace.resolve()},dst=/workspace',
                   '--mount', f'type=bind,src={imports.resolve()},dst=/imports,readonly',
                   '--workdir', '/workspace', '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYTHONUNBUFFERED=1',
                   *entrypoint, script, *arguments]
        with self._lock:
            check_deadline(deadline)
            if run_id in self._cancelled:
                if generated:
                    delete_file(workspace, generated)
                raise ToolError('cancelled', 'This run was cancelled before container creation.')
            self._containers[name] = run_id
        try:
            options = {} if deadline is None else {'deadline': deadline}
            result = run_process(command, env=self._environment(), timeout=bounded_timeout(duration, deadline), **options)
            result.update({'ok': result['exitCode'] == 0 and not result['timedOut'] and not result['outputLimitExceeded'], 'sandboxId': name, 'test': bool(test), 'runtime': language,
                           'isolation':self._isolation(agent_id,run_id,name)})
            if result['timedOut']:
                result['error'] = 'Code execution timed out.'
            elif result['outputLimitExceeded']:
                result['error'] = 'Code execution exceeded the 64 KiB stdout/stderr limit.'
            with self._lock:
                if run_id in self._cancelled:
                    result.update({'ok': False, 'cancelled': True, 'error': 'Run cancelled.'})
            if deadline is not None and time.monotonic() >= deadline:
                result.update(ok=False, timedOut=True, deadlineExceeded=True, outcomeUnknown=True,
                              error='Run deadline reached; container stopped. Reconcile any files written before retrying.')
            return result
        finally:
            # --rm handles normal exits; exact generated names prevent killing unrelated containers.
            try:
                run_process([self.docker, 'rm', '-f', name], env=self._environment(), timeout=5)
            except ToolError:
                pass
            with self._lock:
                self._containers.pop(name, None)
            if generated:
                try:
                    delete_file(workspace, generated)
                except ToolError:
                    pass

    def _run_browser(self, agent_id, run_id, args, test=False, deadline=None):
        check_deadline(deadline)
        from .browser_spec import validate_browser_args, dialog_coverage
        entry_path, checks = validate_browser_args(args)
        duration = args.get('timeout', 20)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 1 <= duration <= 60:
            raise ToolError('parameter', 'timeout must be between 1 and 60 seconds.')
        workspace = self.workspace(agent_id, run_id)
        imports = self.imports(agent_id, run_id)
        with self._lock:
            if run_id in self._cancelled:
                raise ToolError('cancelled', 'This run was cancelled.')
        status = self.capabilities() if deadline is None else self.capabilities(deadline=deadline)
        check_deadline(deadline)
        if not status.get('dockerAvailable') or not status.get('browserImageReady'):
            raise ToolError('permanent', 'The browser image is unavailable. Build the pinned docker/browser image explicitly; no host fallback is allowed.')
        case_id = 'browser-' + uuid.uuid4().hex
        frozen = secure_directory(self.data_dir, 'browser-inputs', agent_id, run_id, case_id)
        app = secure_directory(frozen, 'app')
        evidence = secure_directory(workspace, 'browser-evidence', case_id)
        report_path = f'browser-evidence/{case_id}/report.json'
        screenshot_path = f'browser-evidence/{case_id}/screenshot.png'
        runner = Path(__file__).with_name('browser_runner.cjs').resolve()
        if any(',' in str(path) for path in (frozen, evidence, runner)):
            raise ToolError('parameter', 'Docker bind paths cannot contain commas.')
        name = 'evo-' + uuid.uuid4().hex
        manifest = []
        try:
            resources = {Path(entry_path).name: entry_path}
            for virtual, source in args.get('resources', {}).items():
                if virtual in resources:
                    raise ToolError('parameter', 'A resource cannot replace the declared entry page.')
                resources[virtual] = source
            total = 0
            for virtual, source in resources.items():
                check_deadline(deadline)
                relative_parts(virtual)
                if Path(virtual).suffix.lower() not in ('.html', '.js', '.mjs', '.cjs', '.css', '.json', '.txt', '.svg', '.xml'):
                    raise ToolError('parameter', 'Browser resources must be explicit static text files.')
                relative_parts(source)
                base, relative = (imports, source[len('imports/'):]) if source.startswith('imports/') else (workspace, source)
                try:
                    content = read_text(base, relative)
                except ToolError as error:
                    # Describe only paths explicitly supplied by this caller.
                    # Do not probe alternatives, disclose a host path, grant an
                    # import, or turn a failed preflight into a browser result.
                    scope = 'read-only imports' if source.startswith('imports/') else 'this agent/run private workspace'
                    label = 'entry page' if not manifest else 'resource'
                    message = ('Browser preflight could not read the requested '+label+' URL '
                               +json.dumps(virtual, ensure_ascii=True)+' from source path '
                               +json.dumps(source, ensure_ascii=True)+' ('+scope+'). '
                               'No browser/container was executed. Original error: '+error.message+' '
                               'Check this exact mapping against existing accessible files/imports before retrying; '
                               'a prior-run artifact is not a current private workspace file. No fallback path or permission was added.')
                    raise ToolError(error.code, message, error.retryable,
                                    outcome_unknown=error.outcome_unknown) from error
                raw = content.encode('utf-8')
                total += len(raw)
                if total > 4 * 1024 * 1024:
                    raise ToolError('parameter', 'Browser resource bundle exceeds 4 MiB.')
                digest = hashlib.sha256(raw).hexdigest()
                write_text(app, virtual, content)
                manifest.append({'urlPath': virtual, 'path': source, 'sha256': digest})
            entry = {key: manifest[0][key] for key in ('path', 'sha256')}
            spec = {'entry': entry, 'entryUrl': manifest[0]['urlPath'], 'files': manifest,
                    'checks': checks, 'storage': args.get('storage', {})}
            write_text(frozen, 'request.json', json.dumps(spec, ensure_ascii=False))
            command = [self.docker, 'run', '--rm', '--pull=never', '--init', '--name', name,
                       *self._labels(agent_id,run_id),
                       '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                       '--memory=512m', '--memory-swap=512m', '--cpus=1', '--pids-limit=128', '--shm-size=128m',
                       '--ulimit', 'fsize=8388608:8388608', '--ulimit', 'nofile=256:256',
                       '--user', f'{os.getuid()}:{os.getgid()}',
                       '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777',
                       '--mount', f'type=bind,src={frozen.resolve()},dst=/input,readonly',
                       '--mount', f'type=bind,src={runner},dst=/runner/browser_runner.cjs,readonly',
                       '--mount', f'type=bind,src={evidence.resolve()},dst=/evidence',
                       '--workdir', '/tmp', '--env', 'HOME=/tmp', '--env', 'NODE_ENV=production',
                       '--entrypoint', 'node', self.browser_image, '/runner/browser_runner.cjs']
            with self._lock:
                check_deadline(deadline)
                if run_id in self._cancelled:
                    raise ToolError('cancelled', 'This run was cancelled before container creation.')
                self._containers[name] = run_id
            options = {} if deadline is None else {'deadline': deadline}
            result = run_process(command, env=self._environment(), timeout=bounded_timeout(duration, deadline), **options)
            try:
                report = json.loads(read_text(workspace, report_path, limit=MAX_OUTPUT_BYTES))
                if not isinstance(report, dict):
                    raise ValueError('invalid browser report')
            except (ToolError, ValueError, TypeError):
                report = {'engine': 'chromium', 'executed': False, 'error': 'The browser did not produce a complete trusted report.'}
            # Entry/hash and manifest are calculated here, never accepted from model args or test stdout.
            report['entry'] = entry
            allowed = {item['path']: item['sha256'] for item in manifest}
            tested = report.get('testedFiles', [])
            valid_tested = isinstance(tested, list) and all(isinstance(item, dict) and isinstance(item.get('path'), str) and isinstance(item.get('sha256'), str) and
                allowed.get(item.get('path')) == item.get('sha256') for item in tested)
            if not valid_tested or entry not in tested:
                report.update(ok=False, error='Browser evidence did not match the frozen entry/resource hashes.')
                report['testedFiles'] = []
            report['sourceManifest'] = [{key: item[key] for key in ('path', 'sha256')} for item in manifest]
            expected_faults = [step for step in checks if step['action'] == 'storage_write_denied' and step['enabled']]
            faults = report.get('faults', [])
            fault_coverage = (isinstance(faults, list) and len(faults) == len(expected_faults)
                and all(isinstance(epoch, dict) and type(epoch.get('blockedWrites')) is int and epoch['blockedWrites'] > 0
                    and epoch.get('pageId') == step.get('pageId', 'main') and epoch.get('key') == step.get('key')
                    for epoch, step in zip(faults, expected_faults)))
            report['faultCoverageComplete'] = fault_coverage
            if not fault_coverage:
                report['ok'] = False
                report['faultCoverageError'] = 'Requested storage fault coverage was not actually exercised or its evidence is incomplete.'
                report.setdefault('error', report['faultCoverageError'])
            report['dialogCoverageComplete'] = dialog_coverage(report, checks)
            if not report['dialogCoverageComplete']:
                report['ok'] = False
                report['dialogCoverageError'] = 'A requested dialog response was not actually handled with the declared page/type/value.'
                report.setdefault('error', report['dialogCoverageError'])
            try:
                parent, filename = _parent_fd(workspace, screenshot_path)
                try:
                    fd = os.open(filename, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), dir_fd=parent)
                    try:
                        info = os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 8 <= info.st_size <= 4 * 1024 * 1024:
                            raise ToolError('permission', 'Invalid browser screenshot file.')
                        raw = bytearray()
                        while len(raw) <= 4 * 1024 * 1024:
                            block = os.read(fd, 65536)
                            if not block:
                                break
                            raw.extend(block)
                        if not raw.startswith(b'\x89PNG\r\n\x1a\n') or len(raw) > 4 * 1024 * 1024:
                            raise ToolError('permission', 'Invalid browser screenshot data.')
                        report['screenshot'] = {'path': screenshot_path, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
                    finally:
                        os.close(fd)
                finally:
                    os.close(parent)
            except (ToolError, OSError):
                report.update(ok=False, screenshotError='No valid bounded screenshot was captured.')
            report['reportPath'] = report_path
            assertions = report.get('assertions', [])
            expected_assertions = sum(step['action'].startswith('assert_') for step in checks)
            passed = isinstance(assertions, list) and len(assertions) == expected_assertions and all(isinstance(item, dict) and item.get('passed') is True for item in assertions)
            ok = (result['exitCode'] == 0 and not result['timedOut'] and not result['outputLimitExceeded']
                  and report.get('ok') is True and report.get('executed') is True and passed
                  and 'screenshot' in report and not report.get('pageErrors') and not report.get('httpErrors') and not report.get('blockedRequests'))
            with self._lock:
                cancelled = run_id in self._cancelled
            if cancelled:
                ok = False
                report['error'] = 'Run cancelled.'
            report['ok'] = ok
            report['assertionCount'] = len(assertions) if isinstance(assertions, list) else 0
            report['assertionsPassed'] = sum(isinstance(item, dict) and item.get('passed') is True for item in assertions) if isinstance(assertions, list) else 0
            result.update(ok=ok, runtime='browser', test=bool(test), sandboxId=name, browser=report,
                          isolation=self._isolation(agent_id,run_id,name))
            if deadline is not None and time.monotonic() >= deadline:
                result.update(ok=False, timedOut=True, deadlineExceeded=True, outcomeUnknown=True)
                report.update(ok=False, error='Run deadline reached; browser stopped. Partial evidence is not a successful test.')
            if cancelled:
                result['cancelled'] = True
            if not ok:
                result['error'] = 'Browser test timed out.' if result['timedOut'] else report.get('error', 'Browser assertions, page errors, or evidence capture failed.')
            write_text(workspace, report_path, json.dumps(report, ensure_ascii=False, indent=2), overwrite=True)
            return result
        finally:
            try:
                run_process([self.docker, 'rm', '-f', name], env=self._environment(), timeout=5)
            except ToolError:
                pass
            with self._lock:
                self._containers.pop(name, None)
            shutil.rmtree(frozen, ignore_errors=True)

    def cancel(self, run_id):
        identifier(run_id, 'run_id')
        with self._lock:
            self._cancelled.add(run_id)
            targets = [name for name, owner_run in self._containers.items() if owner_run == run_id]
        for name in targets:
            if self.docker:
                try:
                    run_process([self.docker, 'rm', '-f', name], env=self._environment(), timeout=5)
                except ToolError:
                    pass
        return {'ok': True, 'cancelledContainers': len(targets)}
