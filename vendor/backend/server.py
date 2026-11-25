"""Serve the existing UI and a small same-origin JSON API on one port."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
import argparse
import copy
import json
import mimetypes
import threading

from .config import Config
from .engine import Platform, ActionError
from .teams import DEFAULT_TEAM, get_team_agents, list_teams
from .delivery import delivery_zip

PUBLIC_FILES = {'index.html', 'styles.css', 'panels.css', 'js/main.js', 'js/view.js',
                'js/store.js', 'js/workbench.js', 'js/panels.js', 'js/data.js'}


class TeamHub:
    """One lazy runtime per fixed team; legacy data stays in the default runtime."""
    def __init__(self, config, default_platform=None, platform_factory=Platform):
        self.config = config
        self.factory = platform_factory
        self.platforms = {DEFAULT_TEAM: default_platform} if default_platform is not None else {}
        self.lock = threading.RLock()
        self.closing = False

    def get(self, team_id=DEFAULT_TEAM):
        if not isinstance(team_id, str):
            raise ActionError('团队标识无效。')
        try:
            agents = get_team_agents(team_id)
        except (ValueError, KeyError):
            raise ActionError('团队不存在。', 404) from None
        with self.lock:
            if self.closing:
                raise ActionError('服务正在停止。', 503)
            if team_id not in self.platforms:
                config = copy.copy(self.config)
                if team_id != DEFAULT_TEAM:
                    config.data_dir = self.config.data_dir / 'teams' / team_id
                    config.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.platforms[team_id] = self.factory(config, agents=agents, team_id=team_id)
            return self.platforms[team_id]

    def shutdown(self):
        with self.lock:
            if self.closing:
                return
            self.closing = True
            platforms = list(self.platforms.values())
        for platform in platforms:
            platform.shutdown()
        for platform in platforms:
            # A cancelled HTTP request may still be finishing its provider call.
            # Never close SQLite underneath a late worker that still owns it.
            if platform.wait_for_idle(max(5, platform.config.model_timeout + 2)):
                platform.repo.close()


def _run_history(platform):
    # Metadata only; full results are requested explicitly for the selected team.
    with platform.repo.lock:
        records = platform.repo.db.execute('SELECT channel,payload FROM runs ORDER BY rowid DESC LIMIT 200').fetchall()
    fields = ('id', 'title', 'status', 'mode', 'startedAt', 'finishedAt', 'participants')
    return [{**{key: run[key] for key in fields if key in run}, 'channelId': channel}
            for channel, payload in records for run in [json.loads(payload)]]


def _run_detail(platform, run_id):
    run = platform.repo.get_run(run_id)
    if run is None:
        return None
    result = platform._public(run)
    if 'conversation' not in result:
        with platform.lock:
            result['conversation'] = [copy.deepcopy(message)
                for session in platform.state.get('sessions', {}).values()
                for message in session.get('messages', []) if message.get('runId') == run_id]
    return result


def make_handler(platform):
    # Existing integrations may still pass a single Platform.
    hub = platform if isinstance(platform, TeamHub) else TeamHub(platform.config, default_platform=platform)
    class Handler(BaseHTTPRequestHandler):
        server_version = 'EvoMAC/0.2'

        def log_message(self, fmt, *args):
            # Request paths and bodies are deliberately not logged.
            pass

        def _headers(self, status, content_type, length, disposition=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(length))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            if disposition:
                self.send_header('Content-Disposition', disposition)
            self.end_headers()

        def _json(self, status, body):
            content = json.dumps(hub.config.scrub(body), ensure_ascii=False).encode('utf-8')
            self._headers(status, 'application/json; charset=utf-8', len(content))
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _valid_host(self):
            # Prevent DNS rebinding to the local development service.
            try:
                parsed = urlsplit('http://' + self.headers.get('Host', ''))
                return parsed.hostname in ('localhost', '127.0.0.1', '::1') and parsed.port == self.server.server_port
            except ValueError:
                return False

        def do_GET(self):
            if not self._valid_host():
                return self._json(403, {'error': '只接受本机访问。'})
            path = unquote(urlsplit(self.path).path)
            if path == '/api/teams':
                return self._json(200, {'teams': list_teams(), 'defaultTeamId': DEFAULT_TEAM})
            if path.startswith('/api/'):
                try:
                    values = parse_qs(urlsplit(self.path).query, keep_blank_values=True).get('team', [DEFAULT_TEAM])
                    if len(values) != 1:
                        raise ActionError('每次请求只能指定一个团队。')
                    selected = hub.get(values[0])
                    if path == '/api/health':
                        return self._json(200, {'ok': True, **selected.snapshot()['connection']})
                    if path == '/api/state':
                        return self._json(200, selected.snapshot())
                    if path == '/api/runs':
                        return self._json(200, {'teamId': values[0], 'runs': _run_history(selected)})
                    parts = path.strip('/').split('/')
                    if len(parts) == 5 and parts[:2] == ['api','runs'] and parts[3] == 'deliveries':
                        run = selected.repo.get_run(parts[2])
                        delivery = next((d for d in (run or {}).get('deliveries',[]) if d.get('id') == parts[4]), None)
                        if delivery is None:
                            return self._json(404, {'error':'此团队中不存在该交付包。'})
                        payload = delivery_zip(delivery, run, selected.artifacts)
                        self._headers(200, 'application/zip', len(payload), 'attachment; filename="delivery.zip"')
                        try:
                            self.wfile.write(payload)
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                        return
                    if path.startswith('/api/runs/'):
                        run = _run_detail(selected, path[len('/api/runs/'):])
                        return self._json(200, run) if run else self._json(404, {'error': '此团队中不存在该任务。'})
                    return self._json(404, {'error': 'Not found'})
                except ValueError as error:
                    return self._json(getattr(error, 'status', 400), {'error': str(error)[:500]})
            name = 'index.html' if path == '/' else path.lstrip('/')
            # Never serve .env, Python source, SQLite, agent files, or arbitrary paths.
            if name not in PUBLIC_FILES:
                return self._json(404, {'error': 'Not found'})
            file = hub.config.root / name
            if not file.is_file() or file.is_symlink() or not file.resolve().is_relative_to(hub.config.root):
                return self._json(404, {'error': 'Not found'})
            data = file.read_bytes()
            kind = mimetypes.guess_type(name)[0] or 'application/octet-stream'
            self._headers(200, kind + '; charset=utf-8', len(data))
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            if not self._valid_host():
                return self._json(403, {'error': '只接受本机访问。'})
            origin = self.headers.get('Origin')
            if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'):
                return self._json(403, {'error': '拒绝跨站请求。'})
            if self.headers.get('X-EvoMAC-Client') != '1':
                return self._json(403, {'error': '缺少客户端请求标记。'})
            if urlsplit(self.path).path != '/api/action':
                return self._json(404, {'error': 'Not found'})
            if self.headers.get_content_type() != 'application/json':
                return self._json(415, {'error': '请使用 application/json。'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size <= 0 or size > 200000:
                    return self._json(413, {'error': '请求体大小无效。'})
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ActionError('请求必须为 JSON 对象。')
                selected = hub.get(payload.get('team', DEFAULT_TEAM))
                return self._json(200, selected.action({key: value for key, value in payload.items() if key != 'team'}))
            except (json.JSONDecodeError, UnicodeError, ValueError) as error:
                return self._json(getattr(error, 'status', 400), {'error': str(error)[:500]})
            except Exception:
                return self._json(500, {'error': '后端处理失败，请检查服务终端。'})
    return Handler


def main():
    parser = argparse.ArgumentParser(description='Run the EvoMAC local backend + UI')
    parser.add_argument('--port', type=int)
    profiles=parser.add_mutually_exclusive_group()
    profiles.add_argument('--delivery', action='store_true', help='Explicitly use the larger, bounded delivery profile for this process only')
    profiles.add_argument('--time-only', action='store_true', help='Explicit 30-minute wall deadline; no total token, call or task-turn caps')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    config = Config(root)
    delivery_limits = config.apply_delivery_profile() if args.delivery else None
    time_limits = config.apply_time_only_profile() if args.time_only else None
    hub = TeamHub(config)
    port = args.port or config.port
    # Local development service. Exposing this on a public interface requires authentication.
    server = ThreadingHTTPServer(('127.0.0.1', port), make_handler(hub))
    print(f'EvoMAC: http://127.0.0.1:{port}/#workspace', flush=True)
    print(f'Provider: {config.provider} | Model: {config.model} | API configured: {bool(config.api_key)}', flush=True)
    if delivery_limits is not None:
        print('Delivery profile limits (upper bounds, not estimated usage): ' + json.dumps(delivery_limits), flush=True)
    if time_limits is not None:
        print('Time-only profile (wall time includes human waiting, pause and restart): '+json.dumps(time_limits),flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
