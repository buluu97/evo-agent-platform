"""Bounded organic search-link extraction; never source bodies or verified claims.

Independent stdlib parser. DDGS's official Yahoo adapter (commit
8e74959141106702645020dcb853c6453b5e5195, ddgs/engines/yahoo.py) documents
organic relsrch / Title / h3 structure. No dependency or implementation is copied.
"""
from html.parser import HTMLParser
import ipaddress
import re
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit


_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


def _target(value, source_url):
    if not isinstance(value, str) or len(value) > 8192:
        return None
    try:
        url = urljoin(source_url, value)
        parsed = urlsplit(url)
        if parsed.hostname == 'r.search.yahoo.com' and '/RU=' in parsed.path:
            url = unquote(parsed.path.split('/RU=', 1)[1].split('/RK=', 1)[0].split('/RS=', 1)[0])
            parsed = urlsplit(url)
        if (len(url) > 2048 or parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or any(ord(c) < 33 for c in url) or '\\' in url):
            return None
        host = parsed.hostname.lower().rstrip('.')
        if (host == 'yahoo.com' or host.endswith('.yahoo.com')
                or host == 'bing.com' or host.endswith('.bing.com') or host == 'localhost'
                or host.endswith(('.localhost', '.local', '.internal')) or '.' not in host):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))
    except (ValueError, UnicodeError):
        return None


class _YahooOrganic(HTMLParser):
    def __init__(self, provider='yahoo'):
        super().__init__(convert_charrefs=True)
        self.provider = provider
        self.stack = []
        self.active = None
        self.rows = []
        self.aborted = False

    def handle_starttag(self, tag, attrs):
        if self.aborted or tag in _VOID:
            return
        if len(self.stack) >= 256:
            self.aborted = True
            self.stack.clear()
            self.active = None
            return
        attributes = dict(attrs)
        self.stack.append((tag, attributes))
        depth = len(self.stack)
        classes = (attributes.get('class') or '').split()
        excluded = any({'ad', 'ads', 'genaipaa', 'b_ad', 'b_ans', 'b_syd'} & set((attr.get('class') or '').lower().split())
                       or attr.get('data-type') in ('ad', 'advertisement', 'ai-answer')
                       for _, attr in self.stack)
        organic = ((tag == 'div' and 'relsrch' in classes and 'algo' in classes) if self.provider == 'yahoo'
                   else tag == 'li' and 'b_algo' in classes)
        if self.active is None and organic and not excluded:
            self.active = {'depth': depth, 'href': None, 'title': [], 'titleChars': 0, 'titleDepth': None,
                           'anchorDepth': None, 'closedTitle': False}
        row = self.active
        if row is None:
            return
        if self.provider == 'bing':
            # Only the complete linked h2 inside an organic li is a result.
            if (tag == 'a' and row['href'] is None and not excluded
                    and any(node == 'h2' for node, _ in self.stack[row['depth']:])):
                row.update(href=attributes.get('href'), anchorDepth=depth, titleDepth=depth)
            return
        if tag == 'a' and row['href'] is None and any(
                node == 'div' and 'compTitle' in (attr.get('class') or '').split()
                for node, attr in self.stack[row['depth']:]):
            row['href'] = attributes.get('href')
            row['anchorDepth'] = depth
        if (tag == 'h3' and row['href'] is not None and row['anchorDepth'] is not None
                and depth > row['anchorDepth'] and not row['closedTitle'] and not excluded):
            row['titleDepth'] = depth

    def handle_startendtag(self, tag, attrs):
        if tag not in _VOID:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.aborted:
            return
        matching = next((i for i in range(len(self.stack)-1, -1, -1) if self.stack[i][0] == tag), None)
        if matching is None:
            return
        depth = matching + 1
        row = self.active
        if row is not None:
            if row['titleDepth'] is not None and depth <= row['titleDepth']:
                row['closedTitle'] = True
                row['titleDepth'] = None
            if row['anchorDepth'] is not None and depth <= row['anchorDepth']:
                row['anchorDepth'] = None
                if not row['closedTitle']:
                    row['href'] = None
                    row['title'] = []
                    row['titleChars'] = 0
            if depth <= row['depth']:
                if row['closedTitle'] and len(self.rows) < 100:
                    self.rows.append({'url': row['href'], 'title': ''.join(row['title'])})
                self.active = None
        del self.stack[matching:]

    def handle_data(self, data):
        row = self.active
        if (row is not None and row['titleDepth'] is not None
                and not any(tag in ('script', 'style', 'noscript') for tag, _ in self.stack)):
            # A pathological page cannot create an unbounded result label.
            remaining = 1200 - row['titleChars']
            if remaining > 0:
                part = data[:remaining]
                if part:
                    row['title'].append(part)
                    row['titleChars'] += len(part)


def _results(content, source_url, limit, provider):
    parser = _YahooOrganic(provider)
    parser.feed(content)
    parser.close()
    if parser.aborted:
        return []
    results, seen = [], set()
    for row in parser.rows:
        url = _target(row['url'], source_url)
        title = re.sub(r'\s+', ' ', row['title']).strip()[:300]
        if not url or not title or url in seen:
            continue
        seen.add(url)
        results.append({'title': title, 'url': url})
        if len(results) >= limit:
            break
    return results


def yahoo_search_results(content, source_url, limit=5):
    return _results(content, source_url, limit, 'yahoo')


def bing_search_results(content, source_url, limit=5):
    """Direct organic links observed in live Bing HTML; omit wrapped/ad/AI links."""
    return _results(content, source_url, limit, 'bing')
