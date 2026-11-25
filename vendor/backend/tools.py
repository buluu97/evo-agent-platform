"""Structured server-side Tool Gateway. Never executes a model-supplied shell command."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, urljoin, urlencode, parse_qs
import hashlib
import bisect
import base64
import copy
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import stat
import sys
import threading
import time

from .agents import AGENTS
from .search_results import yahoo_search_results, bing_search_results, _target
from .sandbox import (ToolError, SandboxManager, MAX_FILE_BYTES, SAFE_TEXT_SUFFIXES,
                      identifier, relative_parts, confined_path, secure_directory,
                      read_text, write_text, delete_file, run_process, check_deadline, bounded_timeout)

MAX_WEB_BYTES = 512 * 1024
MAX_READ_WINDOW_BYTES = 32 * 1024
MAX_READ_WINDOW_LINES = 200
MAX_FIND_MATCHES = 20
MAX_FIND_SNIPPET_BYTES = 8 * 1024
_SHA = re.compile(r'^[a-f0-9]{64}$')
_COMMIT = re.compile(r'^[a-f0-9]{40,64}$')


def _schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


_PATH = {'type': 'string', 'maxLength': 240, 'description': 'Relative private path, or read-only imports/<artifact-id>/<name>. Hidden and parent paths are forbidden.'}
_TEXT = {'type': 'string', 'maxLength': MAX_FILE_BYTES}
_EXPECTED = {'type': 'string', 'description': 'SHA256 returned by the pending approval; must match current file content.', 'pattern': '^[a-f0-9]{64}$'}
_CODE_PARAMETERS = {
    'language': {'type': 'string', 'enum': ['python', 'javascript', 'browser'], 'description': 'Default python. javascript executes Node in Docker. browser opens the HTML path with the fixed offline Chromium harness.'},
    'code': {'type': 'string', 'maxLength': 65536}, 'path': _PATH,
    'args': {'type': 'array', 'items': {'type': 'string', 'maxLength': 1024}, 'maxItems': 30},
    'timeout': {'type': 'number', 'minimum': 1, 'maximum': 60},
    'checks': {'type': 'array', 'maxItems': 60, 'description': 'Browser only; at least one assert_* is required. pageId defaults to main. new_page opens the same entry in the same context, max 3 pages; new pages never reseed shared localStorage. For localStorage products, key-specific storage_write_denied and two pre-opened pages can test save failures/stale-tab conflicts.',
        'items': _schema({'action': {'type': 'string', 'enum': ['click', 'fill', 'select', 'press', 'reload', 'assert_text', 'assert_count', 'assert_value', 'assert_visible', 'assert_storage', 'assert_storage_missing', 'assert_attribute', 'capture_canvas', 'assert_canvas_changed', 'storage_write_denied', 'storage_read_denied', 'new_page', 'respond_dialog']},
            'selector': {'type': 'string'}, 'value': {'type': 'string'}, 'key': {'type': 'string'},
            'text': {'type': 'string'}, 'name': {'type': 'string'}, 'exact': {'type': 'boolean'}, 'count': {'type': 'integer'},
            'pageId': {'type': 'string', 'maxLength': 32, 'description': 'main by default; create a named page first with new_page. No URLs.'},
            'dialogType': {'type': 'string', 'enum': ['alert', 'confirm', 'prompt']},
            'accept': {'type': 'boolean', 'description': 'respond_dialog only: arm the NEXT dialog in this page before click/press. Optional value only for accepted prompt (max2000 chars). Every armed response must actually handle the matching dialog; unused/type-mismatched responses fail coverage.'},
            'enabled': {'type': 'boolean', 'description': 'storage_write_denied denies API writes; storage_read_denied denies only localStorage.getItem. false restores that fault in this page. Optional key targets one key; omitted targets all. Each enabled segment must block the matching application operation. Read faults persist across reload; disable before assert_storage/assert_storage_missing to inspect real persisted records. Harness assertions never count as application read-fault coverage.'}}, ['action'])},
    'storage': {'type': 'object', 'maxProperties': 20, 'additionalProperties': {'type': 'string'}, 'description': 'Browser only; initial localStorage string values seeded once, never reset on reload. Total at most 64 KiB.'},
    'resources': {'type': 'object', 'maxProperties': 39, 'additionalProperties': {'type': 'string'}, 'description': 'Browser only; explicit relative URL filename → private or granted imports/<artifact-id>/<name> source. Rebuilds relative JS/CSS resources without cross-agent access. The HTML entry is served at its basename.'},
}
_SCHEMAS = {
    'file.read': _schema({'path': _PATH,
        'startLine': {'type': 'integer', 'minimum': 1, 'maximum': 1000000, 'description': '1-based inclusive line window; default 1. Cannot combine with find.'},
        'endLine': {'type': 'integer', 'minimum': 1, 'maximum': 1000000, 'description': '1-based inclusive end; default EOF. A window is bounded to 200 lines / 32 KiB.'},
        'find': {'type': 'string', 'minLength': 1, 'maxLength': 1024, 'description': 'Nonempty, case-sensitive literal, not regex. Returns matches[].content with exact source locations; cannot combine with startLine/endLine.'},
        'contextLines': {'type': 'integer', 'minimum': 0, 'maximum': 20, 'description': 'Only with find; surrounding lines before/after each match, default 3. At most 20 matches and 32 KiB total.'}}, ['path']),
    'file.list': _schema({'path': _PATH}),
    'file.write': _schema({'path': _PATH, 'content': _TEXT, 'expectedSha256': _EXPECTED}, ['path', 'content']),
    'file.patch': _schema({'path': _PATH, 'old': _TEXT, 'new': _TEXT, 'expectedSha256': _EXPECTED}, ['path', 'old', 'new']),
    'file.delete': _schema({'path': _PATH, 'expectedSha256': _EXPECTED}, ['path']),
    'web.search': _schema({'query': {'type': 'string', 'maxLength': 1000}, 'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 8}}, ['query']),
    'web.fetch': _schema({'url': {'type': 'string', 'maxLength': 2048}}, ['url']),
    'code.run': _schema(copy.deepcopy(_CODE_PARAMETERS)),
    'code.test': _schema(copy.deepcopy(_CODE_PARAMETERS)),
    'git.status': _schema({}),
    'git.commit': _schema({'message': {'type': 'string', 'maxLength': 500}, 'files': {'type': 'array', 'items': _PATH, 'maxItems': 40}}, ['message']),
    'git.push': _schema({'remote': {'type': 'string', 'maxLength': 2048}, 'branch': {'type': 'string', 'maxLength': 240}, 'commit': {'type': 'string', 'pattern': '^[a-f0-9]{40,64}$'}}, ['remote', 'branch', 'commit']),
}
_DESCRIPTIONS = {
    'file.read': 'Read UTF-8 in this private workspace or granted imports. Use find:"functionName" with contextLines, or 1-based inclusive startLine/endLine to inspect targeted code instead of repeatedly reading whole modules. Full read remains the default. sha256 always hashes the complete file. Window results preserve exact UTF-8/line endings with actual ranges and partial-line flags; find returns matches[].content. mode identifies partial reads; truncated means a safety limit omitted requested text/matches, not that the requested window is the whole file.',
    'file.list': 'List public files in this private workspace or read-only imports.',
    'file.write': 'Create UTF-8 text. Existing-file replacement requires human approval bound to its SHA256; prefer patch.',
    'file.patch': 'Apply one unique exact old→new text replacement; requires human approval and unchanged original SHA256.',
    'file.delete': 'Delete one regular private file after human approval bound to its SHA256.',
    'web.search': 'Search public web sources through SerpAPI (Google organic results); returns discovery links, not verified source bodies. The server requires SERPAPI_API_KEY.',
    'web.fetch': 'Fetch bounded public HTTP(S) text and actual page links with DNS/IP pinning and per-redirect SSRF checks. Links are discovery only: fetch a target before citing its content.',
    'code.run': 'Run Python (default), JavaScript/Node, or declarative browser checks exclusively in an offline resource-limited Docker container. Python/JS require exactly one code/path; browser requires an HTML path plus checks.',
    'code.test': 'Verify Python/JavaScript or actual HTML in offline Docker Chromium. Steps: click(selector); fill/select(selector,value); press(selector,key); reload(); assert_text(selector,text,exact?); assert_count(selector,count); assert_value(selector,value); assert_visible(selector); assert_storage(key,value); assert_storage_missing(key) checks real null; assert_attribute(selector,name,value); capture_canvas(selector,key) then assert_canvas_changed(selector,key). respond_dialog(dialogType=alert|confirm|prompt,accept:boolean,value?) arms one response BEFORE the click/press that opens the next native dialog; value only for accepted prompt. Unused or type-mismatched responses fail coverage. All steps optionally pageId (default main); new_page(pageId) opens the same whitelisted entry with shared storage, no reseed, max3. For localStorage apps, storage_write_denied(enabled:true,key?) intercepts setItem/removeItem/clear only in the selected page; false restores it. A targeted clear is denied only if target key exists; key omitted blocks all API writes. Every enabled segment must block a real matching write or test coverage is incomplete. Use this plus explicit UI/storage assertions to check save failures, and pre-open two pages before writes to check stale-tab conflicts. storage_read_denied(enabled:true,key?) denies only localStorage.getItem in the selected page, persists across reload, and leaves sessionStorage unchanged. false restores reads. Require actual application blockedReads; assertions do not count. Disable it before assert_storage/assert_storage_missing to verify prior records were not overwritten after a read failure. Include at least one assertion and explicit JS/CSS resource mapping. Results include per-page assertions/errors, actual fault counts/keys, screenshotPageId and tested file hashes; no arbitrary evaluate or external URL.',
    'git.status': 'Inspect this run’s isolated Git repository and return a pushTarget for the fixed configured remote.',
    'git.commit': 'Create a real local commit of specified safe text files; Git metadata and hooks are isolated from code.',
    'git.push': 'Push the exact approved commit to the fixed HTTPS remote and per-agent/per-run branch; no force or shell.',
}


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.title = []
        self.ignore = 0
        self.in_title = False
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style', 'noscript', 'svg'):
            self.ignore += 1
        if tag == 'title':
            self.in_title = True
        if tag == 'a':
            self.current = {'url': attrs.get('href', ''), 'title': '', 'class': attrs.get('class', ''),
                            'label': '' if self.ignore else attrs.get('aria-label', '') or attrs.get('title', '')}
        if tag == 'img' and self.current is not None and not self.ignore and not self.current.get('label'):
            self.current['label'] = attrs.get('alt', '')
        if tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'tr'):
            self.blocks.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'svg') and self.ignore:
            self.ignore -= 1
        if tag == 'title':
            self.in_title = False
        if tag == 'a' and self.current is not None:
            self.links.append(self.current)
            self.current = None

    def handle_data(self, value):
        if self.ignore:
            return
        self.blocks.append(value)
        if self.in_title:
            self.title.append(value)
        if self.current is not None:
            self.current['title'] += value


def _discovered_links(links, page_url, max_count=80, max_chars=18000):
    """Bounded anchor discovery, not fetched target evidence or permission.

    No DNS or target requests occur here. Every future fetch still passes the
    normal public-address and redirect checks. Ignore fragment-only navigation,
    credentials and executable URLs; preserve actual relative-link resolution.
    """
    found = {}
    for link in links:
        raw = link.get('url', '').strip()
        if not raw or raw.startswith('#') or '\\' in raw or any(ord(c) < 32 or ord(c) == 127 for c in raw):
            continue
        title = re.sub(r'\s+', ' ', link.get('title', '').strip() or link.get('label', '')).strip()[:300]
        if not title:
            continue
        try:
            target = urljoin(page_url, raw)
            parts = urlsplit(target)
            if (parts.scheme not in ('http', 'https') or not parts.hostname
                    or parts.username is not None or parts.password is not None or len(target) > 2048):
                continue
            if parts.port not in (None, 80, 443) or parts.hostname.rstrip('.').lower() in ('localhost', 'localhost.localdomain'):
                continue
            try:
                if not ipaddress.ip_address(parts.hostname).is_global:
                    continue
            except ValueError:
                pass
            target = urlunsplit((parts.scheme, parts.netloc, parts.path or '/', parts.query, ''))
        except ValueError:
            continue
        if target == page_url:
            continue
        old = found.get(target)
        if old is None or len(title) > len(old['title']):
            found[target] = {'title': title, 'url': target}
    selected = []
    for link in found.values():
        if len(selected) >= max_count:
            break
        if len(json.dumps(selected + [link], ensure_ascii=False)) > max_chars:
            continue
        selected.append(link)
    return {'links': selected, 'linkCount': len(found), 'linksTruncated': len(selected) < len(found),
            'linksAreUnfetched': True,
            'linkEvidenceNote': 'Anchors observed on this page only. Target contents were not fetched; these links are not sources supporting target claims.'}


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, address, port, timeout, tls=False):
        super().__init__(host, port=port, timeout=timeout)
        self.address = address
        self.tls = tls

    def connect(self):
        sock = socket.create_connection((self.address, self.port), timeout=self.timeout)
        try:
            actual = ipaddress.ip_address(sock.getpeername()[0])
            if not _public_address(actual):
                raise ToolError('permission', 'The connected address is not public.')
            if self.tls:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
            self.sock = sock
        except BaseException:
            sock.close()
            raise


def _public_address(address):
    if address.version == 6 and address.ipv4_mapped:
        return _public_address(address.ipv4_mapped)
    return address.is_global and not (address.is_multicast or address.is_reserved or address.is_loopback or address.is_link_local)


def _source_window(content, offsets, start_line, end_line, byte_limit, *, anchor=None, match_length=0):
    """Return a precise source slice; byte caps never split a UTF-8 code point."""
    requested_start, requested_end = offsets[start_line - 1], offsets[end_line]
    start = requested_start

    def prefix(position):
        return content[position:requested_end].encode('utf-8')[:byte_limit].decode('utf-8', 'ignore')

    snippet = prefix(start)
    if anchor is not None and start + len(snippet) < anchor + match_length:
        # A minified/long line must not cause the actual match to disappear from its snippet.
        start = max(requested_start, anchor - 128)
        snippet = prefix(start)
        if start + len(snippet) < anchor + match_length:
            start = anchor
            snippet = prefix(start)
    stop = start + len(snippet)
    first = bisect.bisect_right(offsets, start)
    last = bisect.bisect_left(offsets, stop)
    return {'content': snippet, 'startLine': first, 'endLine': last,
            'startColumn': start - offsets[first - 1] + 1,
            'endColumn': stop - offsets[last - 1],
            'partialFirstLine': start != offsets[first - 1],
            'partialLastLine': stop != offsets[last],
            'truncated': start != requested_start or stop != requested_end}


def _read_selection(path, content, args):
    lines = content.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    total = len(lines)
    result = {'ok': True, 'path': path, 'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
              'totalLines': total, 'truncated': False}
    if 'find' in args:
        literal, context = args['find'], args.get('contextLines', 3)
        count = content.count(literal)
        matches, position, remaining = [], 0, MAX_READ_WINDOW_BYTES
        literal_bytes = len(literal.encode('utf-8'))
        while len(matches) < min(count, MAX_FIND_MATCHES) and remaining >= literal_bytes:
            position = content.find(literal, position)
            if position < 0:
                break
            match_line = bisect.bisect_right(offsets, position)
            match_end = position + len(literal)
            match_end_line = bisect.bisect_left(offsets, match_end)
            start, end = max(1, match_line - context), min(total, match_end_line + context)
            snippet = _source_window(content, offsets, start, end, min(remaining, MAX_FIND_SNIPPET_BYTES),
                                     anchor=position, match_length=len(literal))
            snippet.update(matchLine=match_line, matchColumn=position - offsets[match_line - 1] + 1,
                           matchEndLine=match_end_line, matchEndColumn=match_end - offsets[match_end_line - 1])
            matches.append(snippet)
            remaining -= len(snippet['content'].encode('utf-8'))
            position = match_end  # Like str.count, literal matches do not overlap.
        result.update(mode='find', find=literal, contextLines=context, matchCount=count,
                      returnedMatches=len(matches), matches=matches,
                      truncated=len(matches) < count or any(match['truncated'] for match in matches))
        return result
    if 'startLine' in args or 'endLine' in args:
        start, end = args.get('startLine', 1), args.get('endLine', total)
        result.update(mode='lines', requestedStartLine=start, requestedEndLine=end)
        if start > total:
            result.update(content='', startLine=None, endLine=None, startColumn=None, endColumn=None,
                          partialFirstLine=False, partialLastLine=False, outOfRange=True)
            return result
        selected_end = min(end, total, start + MAX_READ_WINDOW_LINES - 1)
        result.update(_source_window(content, offsets, start, selected_end, MAX_READ_WINDOW_BYTES))
        result['truncated'] = result['truncated'] or selected_end < min(end, total)
        result['outOfRange'] = False
        return result
    result.update(mode='full', content=content)
    return result


_DNS_RESOLVER = 'import json,socket,sys; print(json.dumps(list(dict.fromkeys(i[4][0] for i in socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),type=socket.SOCK_STREAM)))))'
_WEB_FETCH_WORKER = '''import json,sys
sys.path.insert(0,sys.argv[1])
from backend.tools import ToolGateway,ToolError
gateway=object.__new__(ToolGateway)
gateway.timeout=float(sys.argv[3])
try:
    result={'response':gateway._fetch_url(sys.argv[2])}
except ToolError as error:
    result={'error':{'code':error.code,'message':error.message,'retryable':error.retryable}}
print(json.dumps(result,ensure_ascii=False))
'''

# Credentials stay in the worker environment. SerpAPI metadata is not returned.
_SERP_SEARCH_WORKER = '''import json,os,sys
from urllib.parse import urlencode
sys.path.insert(0,sys.argv[1])
from backend.tools import ToolGateway,ToolError
gateway=object.__new__(ToolGateway)
gateway.timeout=float(sys.argv[2])
gateway._reject_redirects=True
try:
    response=gateway._fetch_url('https://serpapi.com/search.json?'+urlencode({
        'engine':'google','q':os.environ['SERPAPI_QUERY'],'api_key':os.environ['SERPAPI_API_KEY']}))
    if response['truncated']:
        raise ToolError('transient','SerpAPI response exceeded the response limit.',True)
    payload=json.loads(response['content'])
    if not isinstance(payload,dict):
        raise ValueError('Invalid object')
    result={'payload':{'error':bool(payload.get('error')),
                       'organic_results':payload.get('organic_results',[])}}
except ToolError as error:
    result={'error':{'code':error.code,'message':error.message,'retryable':error.retryable}}
except Exception:
    result={'error':{'code':'transient','message':'SerpAPI returned invalid JSON or transport failed.','retryable':True}}
print(json.dumps(result,ensure_ascii=False))
'''


def _resolve_addresses(host, port, deadline=None):
    if deadline is None:
        return list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    # getaddrinfo has no reliable thread-level timeout. Only this fixed trusted resolver
    # runs in a killable child; no model-supplied code or credentials are forwarded.
    result = run_process([sys.executable, '-I', '-c', _DNS_RESOLVER, host, str(port)],
                         env={'PATH': os.defpath}, timeout=bounded_timeout(60, deadline), deadline=deadline)
    check_deadline(deadline)
    if result['timedOut']:
        raise ToolError('deadline', 'DNS resolution reached the run deadline; no HTTP connection was started.')
    if result['exitCode'] or result['outputLimitExceeded']:
        raise ToolError('transient', 'DNS lookup failed.', retryable=True)
    try:
        values = json.loads(result['stdout'])
        if not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values):
            raise ValueError('invalid DNS result')
        return values
    except (TypeError, ValueError):
        raise ToolError('transient', 'DNS lookup returned an invalid bounded result.', retryable=True) from None


def _validate_url(url, deadline=None):
    """Resolve once and return validated IPs; connections pin one IP, avoiding DNS rebinding."""
    check_deadline(deadline)
    if not isinstance(url, str) or not 1 <= len(url) <= 2048 or '\\' in url or any(ord(c) < 33 for c in url):
        raise ToolError('parameter', 'URL must be a short public HTTP(S) URL without whitespace or backslashes.')
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').rstrip('.').encode('idna').decode('ascii').lower()
        port = parts.port or (443 if parts.scheme == 'https' else 80)
    except (ValueError, UnicodeError) as error:
        raise ToolError('parameter', 'Invalid URL.') from error
    if parts.scheme not in ('http', 'https') or parts.username is not None or parts.password is not None or not host:
        raise ToolError('permission', 'Only credential-free public HTTP(S) URLs are allowed.')
    if port != (443 if parts.scheme == 'https' else 80):
        raise ToolError('permission', 'Only the standard HTTP/HTTPS ports are allowed.')
    if '%' in host or host == 'localhost' or host.endswith(('.localhost', '.local', '.internal', '.home', '.lan')) or host in ('metadata.google.internal', 'metadata.google', 'instance-data'):
        raise ToolError('permission', 'Local and metadata hosts are forbidden.')
    try:
        direct = ipaddress.ip_address(host)
    except ValueError:
        direct = None
        if '.' not in host or not re.fullmatch(r'[a-z0-9.-]+', host):
            raise ToolError('permission', 'The host must be a public DNS name.')
    if direct is not None:
        addresses = [str(direct)]
    else:
        try:
            addresses = _resolve_addresses(host, port, deadline)
        except socket.gaierror as error:
            raise ToolError('transient', 'DNS lookup failed.', retryable=True) from error
    if not addresses or any(not _public_address(ipaddress.ip_address(address)) for address in addresses):
        raise ToolError('permission', 'Private, loopback, metadata, multicast and mixed public/private DNS targets are forbidden.')
    authority = f'[{host}]' if ':' in host else host
    normalized = urlunsplit((parts.scheme, authority, parts.path or '/', parts.query, ''))
    return normalized, host, port, addresses


class ToolGateway:
    def __init__(self, config, agents=None):
        self.data_dir = Path(config.data_dir)
        self.agents = copy.deepcopy(AGENTS if agents is None else agents)
        self.sandbox = SandboxManager(config, agents=self.agents)
        for agent in self.agents.values():
            allowed = agent.get('tools') if isinstance(agent, dict) else None
            if not isinstance(allowed, list) or any(name not in _SCHEMAS for name in allowed):
                raise ToolError('parameter', 'Each agent needs an explicit allowlist of supported tools.')
        self.git_remote = getattr(config, 'git_remote', '')
        self._serpapi_api_key = getattr(config, 'serpapi_api_key', '')
        self._git_token = getattr(config, 'git_token', '')
        self._git_username = getattr(config, 'git_username', 'x-access-token')
        if not isinstance(self._git_token, str) or len(self._git_token) > 4096 or any(ord(char) < 33 or ord(char) == 127 for char in self._git_token):
            raise ToolError('parameter', 'GIT_TOKEN must be a bounded token without whitespace or control characters.')
        if not isinstance(self._git_username, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,80}', self._git_username):
            raise ToolError('parameter', 'GIT_USERNAME must be a short credential username without colons or control characters.')
        self._git_basic = base64.b64encode((self._git_username + ':' + self._git_token).encode('utf-8')).decode('ascii') if self._git_token else ''
        self.timeout = max(1, min(60, getattr(config, 'tool_timeout', 20)))
        self.git = shutil.which('git')
        self._locks = {}
        self._lock = threading.RLock()
        if self.git_remote:
            self._check_remote_format(self.git_remote)

    def workspace(self, agent_id, run_id):
        return self.sandbox.workspace(agent_id, run_id)

    def capabilities(self):
        return {**self.sandbox.capabilities(), 'gitInstalled': bool(self.git), 'gitRemoteConfigured': bool(self.git_remote), 'gitTokenConfigured': bool(self._git_token), 'fileSizeLimit': MAX_FILE_BYTES, 'webByteLimit': MAX_WEB_BYTES}

    def definitions(self, agent_id):
        if agent_id not in self.agents:
            raise ToolError('permission', 'Unknown agent.')
        return [
            {'name': name, 'description': _DESCRIPTIONS[name], 'parameters': _SCHEMAS[name],
             'risk': 'high' if name in ('git.push', 'file.delete') else 'medium' if name in ('file.write', 'file.patch', 'code.run', 'code.test', 'git.commit') else 'low',
             'permission': 'human_approval' if name in ('git.push', 'file.delete', 'file.patch') else 'approval_on_overwrite' if name == 'file.write' else 'agent_allowlist',
             'idempotent': name in ('file.read', 'file.list', 'file.write', 'web.search', 'web.fetch', 'git.status'), 'timeout': 60 if name.startswith('code.') else self.timeout}
            for name in self.agents[agent_id]['tools']
        ]

    def cancel(self, run_id):
        return self.sandbox.cancel(run_id)

    def _redact(self, value):
        if isinstance(value, str):
            if self._git_token:
                variants = {self._git_token, self._git_basic, base64.b64encode(self._git_token.encode('utf-8')).decode('ascii')}
                for secret in sorted(variants, key=len, reverse=True):
                    value = value.replace(secret, '[REDACTED]')
            return value
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def _validate_args(self, name, args):
        if not isinstance(args, dict) or not all(isinstance(key, str) for key in args):
            raise ToolError('parameter', 'Tool arguments must be a JSON object.')
        schema = _SCHEMAS[name]
        unknown=sorted(set(args)-set(schema['properties']))
        missing=sorted(set(schema['required'])-set(args))
        if unknown or missing:
            details=[]
            if unknown:details.append('unexpected fields: '+', '.join(unknown))
            if missing:details.append('missing fields: '+', '.join(missing))
            raise ToolError('parameter', f'{name}: '+ '; '.join(details)+'. Allowed fields: '+', '.join(schema['properties']))
        for key, value in args.items():
            rule = schema['properties'][key]
            kind = rule['type']
            if kind == 'string':
                if not isinstance(value, str) or '\0' in value or len(value) > rule.get('maxLength', MAX_FILE_BYTES):
                    raise ToolError('parameter', f'{key} must be a bounded string.')
                if rule.get('pattern') and not re.fullmatch(rule['pattern'], value):
                    raise ToolError('parameter', f'{key} has an invalid format.')
                if rule.get('enum') and value not in rule['enum']:
                    raise ToolError('parameter', f'{key} must be one of {rule["enum"]}.')
            elif kind in ('number', 'integer'):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or kind == 'integer' and not isinstance(value, int) or not rule.get('minimum', 0) <= value <= rule.get('maximum', 60):
                    raise ToolError('parameter', f'{key} is out of range.')
            elif kind == 'array':
                if rule.get('items', {}).get('type') == 'object':
                    if not isinstance(value, list) or len(value) > rule.get('maxItems', 40) or any(not isinstance(item, dict) for item in value):
                        raise ToolError('parameter', f'{key} must be a bounded object array.')
                elif not isinstance(value, list) or len(value) > rule.get('maxItems', 40) or any(not isinstance(item, str) or '\0' in item or len(item) > rule.get('items', {}).get('maxLength', 1024) for item in value):
                    raise ToolError('parameter', f'{key} must be a bounded string array.')
            elif kind == 'object':
                if not isinstance(value, dict) or len(value) > rule.get('maxProperties', 40):
                    raise ToolError('parameter', f'{key} must be a bounded object.')
        if name == 'file.read':
            if 'find' in args and ('startLine' in args or 'endLine' in args):
                raise ToolError('parameter', 'Choose either line windows or literal find, not both.')
            if 'contextLines' in args and 'find' not in args:
                raise ToolError('parameter', 'contextLines requires find.')
            if args.get('find') == '':
                raise ToolError('parameter', 'find must be a nonempty literal.')
            if 'endLine' in args and args.get('startLine', 1) > args['endLine']:
                raise ToolError('parameter', 'startLine cannot exceed endLine.')

    def execute(self, agent_id, run_id, name, args, approved=False, deadline=None):
        check_deadline(deadline)
        if agent_id not in self.agents or name not in self.agents[agent_id]['tools'] or name not in _SCHEMAS:
            raise ToolError('permission', 'This agent is not allowed to use that tool.')
        identifier(run_id, 'run_id')
        self._validate_args(name, args)
        workspace = self.workspace(agent_id, run_id)
        with self._lock:
            lock = self._locks.setdefault((agent_id, run_id), threading.RLock())
        acquired = lock.acquire() if deadline is None else lock.acquire(timeout=bounded_timeout(60 * 60 * 24, deadline))
        if not acquired:
            raise ToolError('deadline', 'The run deadline expired while waiting for its workspace lock. No operation was started.')
        try:
            check_deadline(deadline)
            try:
                if name.startswith('file.'):
                    result = self._file(agent_id, run_id, name, args, bool(approved), deadline=deadline)
                elif name.startswith('web.'):
                    options = {} if deadline is None else {'deadline': deadline}
                    result = self._search(args, **options) if name == 'web.search' else self._web_fetch(args['url'], **options)
                    if name == 'web.fetch' and result.get('ok') is True:
                        check_deadline(deadline)
                        # The snapshot contains exactly the delivered, scrubbed
                        # text. It is not another fetch or a generated summary.
                        result = self._redact(result)
                        result.update(self._snapshot_web_source(agent_id, run_id, result))
                elif name.startswith('code.'):
                    options = {} if deadline is None else {'deadline': deadline}
                    result = self.sandbox.run(agent_id, run_id, args, test=name == 'code.test', **options)
                    if args.get('language')=='browser':
                        from .browser_spec import read_fault_coverage
                        checks=args.get('checks',[])
                        browser=result.get('browser')
                        if any(step.get('action')=='storage_read_denied' for step in checks) or isinstance(browser,dict) and 'readFaults' in browser:
                            complete=isinstance(browser,dict) and read_fault_coverage(browser,checks)
                            if isinstance(browser,dict):browser['readFaultCoverageComplete']=complete
                            if not complete:
                                message='A declared read fault lacks actual matching application getItem evidence; harness assertions do not count.'
                                result.update(ok=False,error=message)
                                if isinstance(browser,dict):browser.update(ok=False,readFaultCoverageError=message)
                            if isinstance(browser,dict) and isinstance(browser.get('reportPath'),str) and re.fullmatch(r'browser-evidence/browser-[a-f0-9]{32}/report\.json',browser['reportPath']):
                                write_text(workspace,browser['reportPath'],json.dumps(browser,ensure_ascii=False,indent=2),overwrite=True)
                else:
                    result = self._git(agent_id, run_id, name, args, bool(approved), deadline=deadline)
                # A late result must never be promoted to success. Keep already-produced
                # process evidence for reconciliation, rather than claiming no effect.
                if deadline is not None and time.monotonic() >= deadline:
                    result.update(ok=False, deadlineExceeded=True, outcomeUnknown=not result.get('needsApproval') and name not in ('file.read', 'file.list', 'web.search', 'web.fetch', 'git.status'),
                                  error='The run deadline expired during this operation; reconcile any possible effects before retrying.')
                return self._redact(result)
            except ToolError as error:
                raise ToolError(error.code, self._redact(error.message), error.retryable,
                                outcome_unknown=error.outcome_unknown or error.code == 'deadline' and name in ('git.commit', 'git.push')) from None
            except (OSError, ValueError) as error:
                raise ToolError('permanent', 'The tool could not complete its bounded operation.') from None
        finally:
            lock.release()

    def _snapshot_web_source(self, agent_id, run_id, result):
        """Own fetched text, readable through existing read-only imports only.

        This creates no product artifact, cross-agent grant, or acceptance.
        Failure to retain a snapshot never changes a successful network receipt
        into a retryable fetch: the caller still has its actual returned text.
        """
        body, url = result.get('content'), result.get('url')
        if not isinstance(body, str) or not isinstance(url, str):
            return {'sourceSnapshotUnavailable': 'missing_delivered_text_or_url'}
        try:
            raw = body.encode('utf-8')
            encoded_url = url.encode('utf-8')
        except UnicodeEncodeError:
            return {'sourceSnapshotUnavailable': 'invalid_utf8_snapshot_text_or_url'}
        if len(raw) > MAX_FILE_BYTES or '\0' in body:
            return {'sourceSnapshotUnavailable': 'text_exceeds_snapshot_file_limit'}
        digest = hashlib.sha256(raw).hexdigest()
        identity = hashlib.sha256(encoded_url + b'\0' + digest.encode('ascii')).hexdigest()
        relative = 'web-source-' + identity + '/body.txt'
        try:
            base = self.sandbox.imports(agent_id, run_id)
            target = confined_path(base, relative)
            if target.exists():
                if read_text(base, relative) != body:
                    return {'sourceSnapshotUnavailable': 'existing_snapshot_mismatch'}
            else:
                snapshots = [item for item in base.iterdir() if item.name.startswith('web-source-')]
                # Forty bounded files per owner/run, independent of product
                # artifact quotas. No eviction or hidden refetch on exhaustion.
                if len(snapshots) >= 40:
                    return {'sourceSnapshotUnavailable': 'owner_run_snapshot_limit'}
                write_text(base, relative, body)
            return {'sourceSnapshot': {
                'path': 'imports/' + relative, 'sha256': digest,
                'url': url, 'characters': len(body), 'utf8Bytes': len(raw),
                'truncated': result.get('truncated') is True,
                'scope': 'same_agent_same_run_readonly',
                'instruction': 'This is the actual fetched text snapshot, not a live update or verified claim. '
                    'Use file.read with find/contextLines or startLine/endLine to recover missing passages; '
                    'check sha256 and cite the original URL. A reference alone is not source evidence.'}}
        except (ToolError, OSError, ValueError):
            return {'sourceSnapshotUnavailable': 'snapshot_storage_unavailable'}

    def _location(self, agent_id, run_id, path, writable=False, allow_root=False):
        if path in ('imports', 'imports/') and allow_root:
            if writable:
                raise ToolError('permission', 'Artifact imports are read-only.')
            return self.sandbox.imports(agent_id, run_id), '.'
        if isinstance(path, str) and path.startswith('imports/'):
            if writable:
                raise ToolError('permission', 'Artifact imports are read-only.')
            base, relative = self.sandbox.imports(agent_id, run_id), path[len('imports/'):]
        else:
            base, relative = self.workspace(agent_id, run_id), path
        confined_path(base, relative, allow_root)
        return base, relative

    def _approval(self, name, args, title, description):
        return {'ok': False, 'needsApproval': True, 'tool': name, 'args': dict(args), 'approval': {'title': title, 'description': description}}

    def _file(self, agent_id, run_id, name, args, approved, deadline=None):
        check_deadline(deadline)
        value = args.get('path', '.')
        base, relative = self._location(agent_id, run_id, value, writable=name not in ('file.read', 'file.list'), allow_root=name == 'file.list')
        path = confined_path(base, relative, allow_root=name == 'file.list')
        if name == 'file.read':
            content = read_text(base, relative)
            return _read_selection(value, content, args)
        if name == 'file.list':
            if not path.is_dir():
                raise ToolError('parameter', 'The requested path is not a directory.')
            entries = []
            for child in sorted(path.iterdir()):
                if child.name.startswith('.') or child.is_symlink():
                    continue
                info = child.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1 or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    continue
                entries.append({'name': child.name, 'type': 'directory' if child.is_dir() else 'file', 'size': info.st_size})
                if len(entries) >= 100:
                    break
            return {'ok': True, 'path': value, 'entries': entries}
        exists = path.exists()
        old = read_text(base, relative) if exists else None
        digest = hashlib.sha256(old.encode()).hexdigest() if old is not None else None
        expected = args.get('expectedSha256')
        if expected is not None and expected != digest:
            raise ToolError('permission', 'The file changed since it was inspected/approved; read it again and request a new approval.')
        if name in ('file.patch', 'file.delete') and not exists:
            raise ToolError('permanent', 'The file does not exist.')
        replacement = args.get('content')
        if name == 'file.patch':
            before, after = args['old'], args['new']
            if not before or old.count(before) != 1:
                raise ToolError('parameter', 'Patch old text must occur exactly once; read the file and use a unique exact match.')
            if before == after:
                return {'ok':False, 'unchanged':True, 'path':value, 'sha256':digest,
                        'error':'Patch old and new are identical. The file is unchanged. Move to the next correction or hand off if all required corrections are complete.'}
            replacement = old.replace(before, after, 1)
        if replacement is not None and (len(replacement.encode('utf-8')) > MAX_FILE_BYTES or '\0' in replacement):
            raise ToolError('parameter', 'Resulting text exceeds 256 KiB or contains binary data.')
        if exists:
            if not approved:
                bound = {**args, 'expectedSha256': digest}
                return self._approval(name, bound, f'Approve {name} for {value}?', f'Only this {agent_id}/{run_id} file; existing SHA256 {digest}. Replacement/deletion will be rejected if the file changes.')
            if expected != digest:
                raise ToolError('permission', 'Approval must include the exact expectedSha256 returned by the pending request.')
        elif approved and expected is not None:
            raise ToolError('permission', 'An approved existing file disappeared; request approval again.')
        if name == 'file.delete':
            check_deadline(deadline)
            delete_file(base, relative)
            return {'ok': True, 'path': value, 'deleted': True}
        check_deadline(deadline)
        write_text(base, relative, replacement, overwrite=exists)
        return {'ok': True, 'path': value, 'sha256': hashlib.sha256(replacement.encode()).hexdigest(), 'bytes': len(replacement.encode()), 'created': not exists}

    @staticmethod
    def _validate_url(url, deadline=None):
        return _validate_url(url, deadline=deadline)

    def _fetch_url(self, url, deadline=None):
        if deadline is not None:
            # Socket timeouts alone do not bound DNS or slow-drip HTTP headers. This
            # fixed worker reuses the complete pinned/redirect-checked transport;
            # the parent kills its process group at the single absolute deadline.
            timeout = bounded_timeout(self.timeout, deadline)
            result = run_process([sys.executable, '-I', '-c', _WEB_FETCH_WORKER,
                                  str(Path(__file__).resolve().parent.parent), url, str(timeout)],
                                 env={'PATH': os.defpath}, timeout=timeout, deadline=deadline,
                                 max_output_bytes=MAX_WEB_BYTES * 6 + 8192)
            check_deadline(deadline)
            if result['timedOut']:
                raise ToolError('transient', 'Public web request exceeded its total timeout.', retryable=True)
            if result['exitCode'] or result['outputLimitExceeded']:
                raise ToolError('transient', 'Bounded public web transport failed.', retryable=True)
            try:
                payload = json.loads(result['stdout'])
                if not isinstance(payload, dict):
                    raise ValueError('invalid response')
                if 'error' in payload:
                    error = payload['error']
                    raise ToolError(error['code'], error['message'], error.get('retryable', False))
                response = payload['response']
                if not isinstance(response, dict) or not isinstance(response.get('content'), str) or len(response['content'].encode('utf-8')) > MAX_WEB_BYTES * 3:
                    raise ValueError('invalid response')
                return response
            except (KeyError, TypeError, ValueError):
                raise ToolError('transient', 'Bounded public web transport returned invalid evidence.', retryable=True) from None
        deadline = time.monotonic() + self.timeout
        current = url
        for redirect in range(6):
            normalized, host, port, addresses = _validate_url(current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ToolError('transient', 'Web request timed out.', retryable=True)
            parsed = urlsplit(normalized)
            connection = _PinnedHTTP(host, addresses[0], port, min(remaining, self.timeout), tls=parsed.scheme == 'https')
            try:
                connection.request('GET', urlunsplit(('', '', parsed.path, parsed.query, '')), headers={'User-Agent': 'EvoAgentCourse/1.0', 'Accept': 'text/html,text/plain,application/json;q=0.9', 'Accept-Encoding': 'identity', 'Connection': 'close'})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location or redirect == 5:
                        raise ToolError('permanent', 'Too many or invalid redirects.')
                    current = urljoin(normalized, location)
                    continue
                if response.status != 200:
                    retryable = response.status in (408, 429, 500, 502, 503, 504)
                    raise ToolError('transient' if retryable else 'permanent', f'Public web server returned HTTP {response.status}.', retryable)
                encoding = response.getheader('Content-Encoding', 'identity').lower()
                if encoding not in ('', 'identity'):
                    raise ToolError('permanent', 'Compressed web responses are rejected to bound decompression size.')
                mime = response.getheader('Content-Type', '').split(';')[0].strip().lower()
                if not (mime.startswith('text/') or mime in ('application/json', 'application/xml', 'application/xhtml+xml')):
                    raise ToolError('parameter', 'Only public text/HTML/JSON/XML resources are supported.')
                chunks = bytearray()
                while len(chunks) <= MAX_WEB_BYTES:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ToolError('transient', 'Web request exceeded its total deadline.', True)
                    if connection.sock:
                        connection.sock.settimeout(min(remaining, self.timeout))
                    block = response.read1(min(16384, MAX_WEB_BYTES + 1 - len(chunks)))
                    if not block:
                        break
                    chunks.extend(block)
                truncated = len(chunks) > MAX_WEB_BYTES
                raw = bytes(chunks[:MAX_WEB_BYTES])
                charset = response.headers.get_content_charset() or 'utf-8'
                try:
                    text = raw.decode(charset, 'replace')
                except LookupError:
                    text = raw.decode('utf-8', 'replace')
                return {'url': normalized, 'contentType': mime, 'content': text, 'truncated': truncated}
            except ToolError:
                raise
            except (OSError, http.client.HTTPException) as error:
                raise ToolError('transient', 'Public web request failed or timed out.', retryable=True) from error
            finally:
                connection.close()
        raise ToolError('permanent', 'Redirect limit exceeded.')

    def _web_fetch(self, url, deadline=None):
        response = self._fetch_url(url) if deadline is None else self._fetch_url(url, deadline=deadline)
        check_deadline(deadline)
        content = response['content']
        title = response['url']
        discovery = {}
        if 'html' in response['contentType']:
            parser = _TextParser()
            parser.feed(content)
            title = ''.join(parser.title).strip()[:300] or title
            content = re.sub(r'[ \t\r\f\v]+', ' ', ''.join(parser.blocks))
            content = re.sub(r'\n\s*\n+', '\n\n', content).strip()
            discovery = _discovered_links(parser.links, response['url'])
        if not content.strip():
            raise ToolError('no-readable-content', 'The fetched response contains no readable content after text extraction; no source content was retrieved.')
        return {'ok': True, 'url': response['url'], 'title': title, 'content': content[:120000], 'truncated': response['truncated'] or len(content) > 120000, 'sources': [{'title': title, 'url': response['url']}], **discovery}

    def _serp_response(self, query, deadline):
        timeout = bounded_timeout(self.timeout, deadline)
        result = run_process([sys.executable, '-I', '-c', _SERP_SEARCH_WORKER,
                              str(Path(__file__).resolve().parent.parent), str(timeout)],
                             env={'PATH': os.defpath, 'SERPAPI_API_KEY': self._serpapi_api_key,
                                  'SERPAPI_QUERY': query},
                             timeout=timeout, deadline=deadline,
                             max_output_bytes=MAX_WEB_BYTES * 6 + 8192)
        check_deadline(deadline)
        if result['timedOut'] or result['exitCode'] or result['outputLimitExceeded']:
            raise ToolError('search_unavailable', 'SerpAPI transport failed or exceeded its deadline.', True)
        try:
            response = json.loads(result['stdout'])
            if 'error' in response:
                error = response['error']
                raise ToolError(error['code'], error['message'], error.get('retryable', False))
            payload = response['payload']
            if not isinstance(payload, dict):
                raise ValueError('Invalid payload')
            return payload
        except (KeyError, TypeError, ValueError):
            raise ToolError('search_unavailable', 'SerpAPI returned invalid search evidence.', True) from None

    def _search(self, args, deadline=None):
        check_deadline(deadline)
        query = args['query'].strip()
        if not query:
            raise ToolError('parameter', 'Search query must not be empty.')
        if not self._serpapi_api_key:
            raise ToolError('configuration', 'SERPAPI_API_KEY is not configured. Set it in the server .env and restart; no alternate search provider is used.')
        overall = min(time.monotonic()+self.timeout, deadline) if deadline is not None else time.monotonic()+self.timeout
        payload = self._serp_response(query, overall)
        check_deadline(overall)
        if payload.get('error'):
            raise ToolError('search_unavailable', 'SerpAPI reported an API error; check credentials and search quota.', False)
        rows = payload.get('organic_results', [])
        if not isinstance(rows, list):
            raise ToolError('search_unavailable', 'SerpAPI returned malformed organic results.', True)
        source_url = 'https://serpapi.com/search.json?' + urlencode({'engine': 'google', 'q': query})
        results, seen, unresolved = [], set(), []
        for row in rows[:100]:
            if not isinstance(row, dict):
                continue
            title, url = row.get('title'), row.get('link')
            target = _target(url, source_url) if isinstance(url, str) else None
            if (not isinstance(title, str) or not title.strip() or not target or target in seen
                    or self._serpapi_api_key in title or self._serpapi_api_key in target):
                continue
            seen.add(target)
            parsed = urlsplit(target)
            if (parsed.hostname or '').lower() in ('google.com', 'www.google.com') and parsed.path.startswith('/goto'):
                unresolved.append(title.strip()[:160])
                continue
            results.append({'title': title.strip()[:300], 'url': target})
            if len(results) >= args.get('max_results', 5):
                break
        result = {'ok': True, 'query': query, 'results': results, 'provider': 'SerpAPI',
                'sourceUrl': source_url,
                'providerAttempts': [{'provider': 'SerpAPI', 'sourceUrl': source_url,
                                      'status': 'success', 'resultCount': len(results)}],
                'resultsAreUnfetched': True,
                'evidenceMeaning': 'Search returned discovery links only. Fetch/read each target before verifying claims.'}
        if unresolved:
            result['unresolvedRedirectTitles'] = unresolved[:8]
            result['unresolvedRedirectMeaning'] = 'Titles only. These hits were Google redirect links without a fetchable URL; do not fetch google.com/goto.'
        return result

    @staticmethod
    def _duckduckgo_results(response, limit):
        parser = _TextParser()
        parser.feed(response['content'])
        results = []
        seen = set()
        for link in parser.links:
            if 'result-link' not in link['class']:
                continue
            target = urljoin(response['url'], link['url'])
            parsed = urlsplit(target)
            if parsed.hostname and parsed.hostname.endswith('duckduckgo.com'):
                target = parse_qs(parsed.query).get('uddg', [target])[0]
            parsed = urlsplit(target)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or target in seen:
                continue
            seen.add(target)
            results.append({'title': re.sub(r'\s+', ' ', link['title']).strip()[:300], 'url': target})
            if len(results) >= limit:
                break
        return results

    @staticmethod
    def _check_remote_format(remote):
        if not isinstance(remote, str) or not 1 <= len(remote) <= 2048 or any(ord(c) < 33 for c in remote) or '\\' in remote:
            raise ToolError('parameter', 'GIT_REMOTE must be a fixed credential-free HTTPS URL.')
        try:
            parsed = urlsplit(remote)
            valid = parsed.scheme == 'https' and parsed.hostname and parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment and parsed.port in (None, 443)
        except ValueError:
            valid = False
        if not valid:
            raise ToolError('permission', 'Local, SSH, credential-bearing and non-HTTPS Git remotes are forbidden.')

    def _git_env(self):
        home = secure_directory(self.data_dir, 'git-home')
        return {'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(home), 'LANG': 'C.UTF-8', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                'GIT_TERMINAL_PROMPT': '0', 'GIT_AUTHOR_NAME': 'Evo Agent', 'GIT_AUTHOR_EMAIL': 'agent@localhost.invalid', 'GIT_COMMITTER_NAME': 'Evo Agent', 'GIT_COMMITTER_EMAIL': 'agent@localhost.invalid'}

    def _git_base(self, agent_id, run_id, deadline=None):
        check_deadline(deadline)
        if not self.git:
            raise ToolError('permanent', 'Git is not installed.')
        metadata = secure_directory(self.data_dir, 'gitmeta', agent_id, run_id)
        workspace = self.workspace(agent_id, run_id)
        if not (metadata / 'HEAD').is_file():
            initialized = self._git_process([self.git, 'init', '--bare', '--template=', str(metadata)], deadline=deadline)
            if initialized['exitCode']:
                raise ToolError('permanent', 'Cannot initialize isolated Git metadata.')
        options = [self.git, '--git-dir=' + str(metadata.resolve()), '--work-tree=' + str(workspace.resolve()),
                   '-c', 'core.bare=false', '-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false', '-c', 'core.pager=cat',
                   '-c', 'credential.helper=', '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always', '-c', 'http.followRedirects=false',
                   '-c', 'submodule.recurse=false', '-c', 'commit.gpgsign=false', '-c', 'gc.auto=0']
        branch = f'agent-runs/{agent_id}/{run_id}'
        head = self._git_process([*options, 'symbolic-ref', 'HEAD', 'refs/heads/' + branch], workspace=workspace, deadline=deadline)
        if head['exitCode']:
            raise ToolError('permanent', 'Cannot select the fixed per-run branch.')
        return options, workspace, branch

    def _git_process(self, command, workspace=None, push_env=None, deadline=None):
        environment = self._git_env()
        if push_env is not None:
            environment.update(push_env)
        options = {} if deadline is None else {'deadline': deadline}
        result = self._redact(run_process(command, cwd=workspace, env=environment,
                                         timeout=bounded_timeout(self.timeout, deadline), **options))
        check_deadline(deadline, outcome_unknown=True)
        if result['timedOut'] or result['outputLimitExceeded']:
            raise ToolError('transient', 'Git timed out or exceeded its output limit.', retryable=True)
        return result

    def _git_command(self, base, workspace, arguments, push_env=None, deadline=None):
        return self._git_process([*base, *arguments], workspace=workspace, push_env=push_env, deadline=deadline)

    def _head(self, base, workspace, deadline=None):
        head = self._git_command(base, workspace, ['rev-parse', '--verify', 'HEAD'], deadline=deadline)
        return head['stdout'].strip() if head['exitCode'] == 0 else None

    def _safe_commit_files(self, workspace, requested):
        if requested is None:
            requested = []
            for directory, folders, files in os.walk(workspace, followlinks=False):
                folders[:] = [item for item in folders if not item.startswith('.') and not (Path(directory) / item).is_symlink() and item != 'imports']
                for name in files:
                    if name.startswith('.') or Path(name).suffix.lower() not in SAFE_TEXT_SUFFIXES:
                        continue
                    requested.append((Path(directory) / name).relative_to(workspace).as_posix())
                    if len(requested) > 40:
                        raise ToolError('parameter', 'Select at most 40 explicit text files to commit.')
        if not requested:
            raise ToolError('parameter', 'There are no safe text files to commit.')
        safe = []
        for value in requested:
            relative_parts(value)
            if value.startswith('imports/') or Path(value).suffix.lower() not in SAFE_TEXT_SUFFIXES:
                raise ToolError('permission', 'Only private public-name text files may be committed.')
            read_text(workspace, value)
            if value not in safe:
                safe.append(value)
        return safe

    def _git(self, agent_id, run_id, name, args, approved, deadline=None):
        base, workspace, branch = self._git_base(agent_id, run_id, deadline=deadline)
        if name == 'git.status':
            result = self._git_command(base, workspace, ['status', '--short', '--untracked-files=normal', '--ignore-submodules=all'], deadline=deadline)
            result.update({'ok': result['exitCode'] == 0, 'pushTarget': {'remote': self.git_remote, 'branch': branch, 'commit': self._head(base, workspace, deadline=deadline)}})
            return result
        if name == 'git.commit':
            message = args['message'].strip()
            if not message or '\n' in message or '\r' in message:
                raise ToolError('parameter', 'Commit message must be one nonempty line.')
            files = self._safe_commit_files(workspace, args.get('files'))
            added = self._git_command(base, workspace, ['add', '--', *files], deadline=deadline)
            if added['exitCode']:
                raise ToolError('permanent', 'Git could not stage the selected safe files.')
            result = self._git_command(base, workspace, ['commit', '--no-gpg-sign', '-m', message, '--only', '--', *files], deadline=deadline)
            result.update({'ok': result['exitCode'] == 0, 'files': files, 'pushTarget': {'remote': self.git_remote, 'branch': branch, 'commit': self._head(base, workspace, deadline=deadline)}})
            return result
        if not self.git_remote:
            raise ToolError('permission', 'GIT_REMOTE is not configured; no push target is authorized.')
        self._check_remote_format(self.git_remote)
        expected = {'remote': self.git_remote, 'branch': branch, 'commit': self._head(base, workspace, deadline=deadline)}
        if not expected['commit'] or any(args[key] != value for key, value in expected.items()):
            raise ToolError('permission', 'Push target must exactly match the configured remote, fixed branch and current commit returned by git.status/commit.')
        _validate_url(self.git_remote, deadline=deadline)
        if not approved:
            return self._approval(name, expected, 'Approve the exact Git push?', f"Remote: {expected['remote']}\nBranch: {branch}\nCommit: {expected['commit']}\nNo force push or other target is allowed.")
        if not self._git_token:
            raise ToolError('permission', 'GIT_TOKEN is not configured; authenticated HTTPS push is unavailable. No global credential helper is used.')
        normalized, host, port, addresses = _validate_url(self.git_remote, deadline=deadline)
        address = addresses[0]
        pinned = f'{host}:{port}:[{address}]' if ':' in address else f'{host}:{port}:{address}'
        origin = urlsplit(normalized).scheme + '://' + urlsplit(normalized).netloc
        # GitHub's official checkout helper uses Basic base64(username:token). Scope the
        # header to the fixed remote origin and supply it only through this subprocess env.
        # Redirects remain disabled; no header is written to Git config or placed in argv.
        push_env = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': f'http.{origin}/.extraheader',
                    'GIT_CONFIG_VALUE_0': 'AUTHORIZATION: basic ' + self._git_basic}
        result = self._git_command([*base, '-c', 'http.curloptResolve=' + pinned], workspace,
            ['push', '--porcelain', '--no-verify', '--', self.git_remote, f"{expected['commit']}:refs/heads/{branch}"], push_env=push_env, deadline=deadline)
        result.update({'ok': result['exitCode'] == 0, 'pushTarget': expected})
        return result
