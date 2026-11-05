"""Security-associated Eastmoney news, bounded evidence and atomic checkpoints.

HTTP work only stages a checkpoint. The scan acknowledges it in the same short
transaction that archives the returned observations, so crashes cannot lose news.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .news import (EastmoneyNewsClient, EastmoneyResponseError, NewsTarget,
                   SHANGHAI, _clean_text, normalize_article, parse_published_at,
                   unwrap_jsonp)
from .quality import canonical_identity, canonical_url
from .query_policy import select_queries


ARTICLE_HOSTS = frozenset({'finance.eastmoney.com', 'stock.eastmoney.com', 'fund.eastmoney.com'})
MAX_BODY_BYTES = 1_000_000
MAX_BODY_CHARS = 8_000


def article_url(value: str) -> str:
    """Only fetch fixed official article paths; never arbitrary source links."""
    try:
        p = urlsplit(value)
        if (p.scheme not in {'http', 'https'} or p.hostname not in ARTICLE_HOSTS
                or p.username is not None or p.password is not None
                or p.port not in {None, 80, 443}
                or not re.fullmatch(r'/(?:a/\d+\.html|news/\d+,\d+\.html)', p.path)):
            raise ValueError('unsupported_article_url')
        return urlunsplit(('https', p.hostname, p.path, '', ''))
    except (ValueError, TypeError):
        raise ValueError('unsupported_article_url') from None


class _ArticleRedirect(HTTPRedirectHandler):
    max_redirections = 4
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, article_url(newurl))


def _body_transport(url: str, timeout: float) -> str:
    request = Request(article_url(url), headers={
        'User-Agent': 'stock-watch/1.0 (+local self-hosted monitor)',
        'Accept': 'text/html', 'Referer': 'https://quote.eastmoney.com/',
    })
    with build_opener(_ArticleRedirect()).open(request, timeout=timeout) as response:
        article_url(response.geturl())
        raw = response.read(MAX_BODY_BYTES + 1)
        charset = response.headers.get_content_charset() or 'utf-8'
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError('article_body_size_limit')
    return raw.decode(charset, errors='strict')


class _ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.stack = []
        self.content_depth = None
        self.skip_depth = 0
        self.parts = []
        self.finished = False

    def handle_starttag(self, tag, attrs):
        if tag in {'br', 'hr', 'img', 'input', 'meta', 'link', 'source', 'wbr'}:
            if self.content_depth is not None and tag == 'br':
                self.parts.append('\n')
            return
        self.depth += 1
        self.stack.append(tag)
        if not self.finished and self.content_depth is None and dict(attrs).get('id', '').lower() in {
                'contentbody', 'articlecontent', 'artibody', 'articlebody'}:
            self.content_depth = self.depth
        if self.content_depth is not None and tag in {'script', 'style', 'noscript', 'iframe'}:
            self.skip_depth += 1
        if self.content_depth is not None and not self.skip_depth and tag in {'p', 'div', 'tr', 'li'}:
            self.parts.append('\n')

    def handle_startendtag(self, tag, attrs):
        if tag == 'br' and self.content_depth is not None:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'br', 'hr', 'img', 'input', 'meta', 'link', 'source', 'wbr'}:
            return
        if tag not in self.stack:
            return
        index = len(self.stack) - 1 - self.stack[::-1].index(tag)
        if self.content_depth is not None:
            self.skip_depth = max(0, self.skip_depth - sum(t in {'script', 'style', 'noscript', 'iframe'} for t in self.stack[index:]))
            if index < self.content_depth:
                self.content_depth = None
                self.finished = True
        del self.stack[index:]
        self.depth = len(self.stack)

    def handle_data(self, data):
        if self.content_depth is not None and not self.skip_depth:
            self.parts.append(data)


def extract_body(html: str | bytes) -> tuple[str, str]:
    if isinstance(html, bytes):
        if len(html) > MAX_BODY_BYTES:
            raise ValueError('article_body_size_limit')
        html = html.decode('utf-8', errors='strict')
    if not isinstance(html, str) or len(html.encode('utf-8')) > MAX_BODY_BYTES:
        raise ValueError('article_body_size_limit')
    parser = _ArticleParser()
    parser.feed(html)
    body = _clean_text(' '.join(parser.parts))
    if len(body) < 20:
        raise ValueError('article_body_unavailable')
    return body[:MAX_BODY_CHARS], 'full_text_truncated' if len(body) > MAX_BODY_CHARS else 'full_text'


def _iso(value: datetime) -> str:
    return parse_published_at(value).astimezone(timezone.utc).isoformat(timespec='seconds')


@contextmanager
def _connection(database):
    if database is None:
        yield None
        return
    owned = not isinstance(database, sqlite3.Connection)
    if isinstance(database, sqlite3.Connection):
        conn = database
    elif callable(database):
        conn = database()
    else:
        value = str(database)
        if not value.startswith('file:'):
            value = Path(value).resolve().as_uri() + '?mode=ro'
        conn = sqlite3.connect(value, uri=True, timeout=3)
    try:
        yield conn
    finally:
        if owned:
            conn.close()


class EastmoneyStockNewsClient(EastmoneyNewsClient):
    """Use issuer-associated lists; search remains a visible fallback.

    ``last_fetch_status`` exposes safe codes. Partial pagination returns already
    obtained observations, but never stages a successful coverage checkpoint.
    ``acknowledge(conn, now)`` must follow archival in that same transaction.
    """
    source_namespace = 'eastmoney'
    security_associated = True
    stock_endpoint = 'https://np-listapi.eastmoney.com/comm/web/getListInfo'

    def __init__(self, *args, database=None, body_transport=None,
                 max_pages=25, body_refresh_seconds=900, max_body_requests=20,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.database = database
        self.body_transport = body_transport or _body_transport
        self.max_pages = max(1, min(100, int(max_pages)))
        self.body_refresh_seconds = max(60, int(body_refresh_seconds))
        self.max_body_requests = max(0, int(max_body_requests))
        self.last_fetch_status = {}
        self._pending = {}

    @staticmethod
    def security_code(target: NewsTarget) -> str | None:
        symbol = target.symbol or target.key
        match = re.fullmatch(r'(\d{6})\.(SH|SZ)', symbol.upper())
        if match:
            return ('1' if match[2] == 'SH' else '0') + '.' + match[1]
        match = re.fullmatch(r'(\d{4,5})\.HK', symbol.upper())
        if match:
            return '116.' + match[1].zfill(5)
        return None

    @staticmethod
    def _state_key(code):
        return 'stock-news-v1:' + hashlib.sha256(code.encode()).hexdigest()

    def begin_cycle(self):
        self._pending.clear()
        self.last_fetch_status.clear()

    @staticmethod
    def _resolved_identity(conn, identity, raw_id, url):
        if conn and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='quality_article_aliases'").fetchone():
            aliases = [('eastmoney:id', _clean_text(raw_id))] if raw_id else []
            if url:
                aliases.append(('eastmoney:url', canonical_url(url)))
            for namespace, alias in aliases:
                row = conn.execute('SELECT canonical_id FROM quality_article_aliases WHERE namespace=? AND alias=?',
                                   (namespace, alias)).fetchone()
                if row:
                    return row[0]
        return identity

    def build_stock_url(self, code, *, page=1, page_size=20):
        return self.stock_endpoint + '?' + urlencode({
            'cfh': 1, 'client': 'web', 'mTypeAndCode': code, 'type': 1,
            'pageSize': max(1, min(20, page_size)), 'pageIndex': page,
        })

    @staticmethod
    def _page(payload, expected):
        if not isinstance(payload, Mapping) or payload.get('code') != 1:
            raise EastmoneyResponseError('stock_list_response_invalid')
        data = payload.get('data')
        if not isinstance(data, Mapping) or type(data.get('page_index')) is not int or data['page_index'] != expected:
            raise EastmoneyResponseError('stock_list_page_mismatch')
        rows = data.get('list')
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise EastmoneyResponseError('stock_list_rows_invalid')
        return data, rows

    def _cached_body(self, article, now):
        try:
            with _connection(self.database) as conn:
                identity = self._resolved_identity(conn, canonical_identity(article), article.raw_id, article.url)
                row = conn.execute('SELECT article_json FROM subscription_articles WHERE identity_key=? ORDER BY fetched_at DESC LIMIT 1',
                                   (identity,)).fetchone() if conn else None
            cached = json.loads(row[0]) if row else {}
            checked = parse_published_at(cached.get('evidence_checked_at'))
            if (cached.get('title') == article.title and checked <= now
                    and cached.get('evidence_status') in {'full_text', 'full_text_truncated'}):
                return replace(article, summary=cached.get('summary', '')[:MAX_BODY_CHARS],
                               evidence_status=cached['evidence_status'], evidence_checked_at=_iso(checked))
        except (sqlite3.Error, ValueError, TypeError, OSError):
            pass
        return None

    def _evidence(self, articles, now):
        result, work = {}, []
        for index, article in enumerate(articles):
            cached = self._cached_body(article, now)
            if cached is not None:
                result[index] = cached
                checked = parse_published_at(cached.evidence_checked_at)
                if (now - checked).total_seconds() < self.body_refresh_seconds:
                    continue
                work.append((1, checked.timestamp(), index, article, cached))
            else:
                result[index] = replace(article, evidence_status='unavailable')
                work.append((0, index, index, article, None))
        # Prioritize unread evidence, then oldest verified content. Refreshing
        # the newest page every TTL must not starve later-page body debt.
        requests = 0
        for _, _, index, article, cached in sorted(work, key=lambda item: item[:3]):
            status, text = 'unavailable', article.summary
            checked = ''
            if requests < self.max_body_requests and now - timedelta(hours=24) <= article.published_at <= now + timedelta(minutes=5):
                try:
                    url = article_url(article.url)
                    requests += 1
                    text, status = extract_body(self.body_transport(url, self.timeout))
                    checked = _iso(now)
                except Exception:
                    # Private/error HTML is never logged or used as a fake body.
                    checked = _iso(now)
                if status.startswith('full_text'):
                    result[index] = replace(article, summary=text, evidence_status=status, evidence_checked_at=checked)
                elif cached is None:
                    result[index] = replace(article, evidence_status=status, evidence_checked_at=checked)
                # A failed revalidation never gives old evidence a new timestamp.
        return [result[i] for i in range(len(articles))]

    def _search(self, target, now, page_size, reason):
        self.last_fetch_status[target.key] = reason
        selected = select_queries(target, now)
        try:
            articles = super().fetch_target(selected, now=now, page_size=page_size)
        except Exception:
            if selected == target:
                raise
            articles = super().fetch_target(target, now=now, page_size=page_size)
        else:
            if not articles and selected != target:
                articles = super().fetch_target(target, now=now, page_size=page_size)
        return self._evidence([replace(a, retrieval_kind='search_fallback' if target.kind == 'stock' else 'search') for a in articles], now)

    def fetch_target(self, target, *, now=None, page_size=20):
        target = target if isinstance(target, NewsTarget) else NewsTarget.from_value(target)
        current = parse_published_at(now or datetime.now(SHANGHAI))
        code = self.security_code(target) if target.kind == 'stock' else None
        if code is None:
            return self._search(target, current, page_size,
                                'search_theme' if target.kind == 'theme' else 'fallback_unsupported_security')
        state_key = self._state_key(code)
        self._pending.pop(state_key, None)
        cutoff = current - timedelta(hours=24)
        page_size = max(1, min(20, int(page_size)))
        articles, observed_ids, pages = [], set(), set()
        head = None
        complete = False
        malformed = False
        for page in range(1, self.max_pages + 1):
            try:
                data, rows = self._page(unwrap_jsonp(self._request(self.build_stock_url(code, page=page, page_size=page_size))), page)
                signature = tuple(str(row.get('Art_Code') or row.get('Art_Url') or '') for row in rows)
                if signature and signature in pages:
                    raise EastmoneyResponseError('stock_list_repeated_page')
                pages.add(signature)
                page_times = []
                for row in rows:
                    try:
                        article = normalize_article(row, target)
                        if not article.url or not article.raw_id:
                            raise ValueError('stock_list_identity_missing')
                    except (ValueError, TypeError):
                        malformed = True
                        continue
                    if head is None:
                        head = article.raw_id
                    page_times.append(article.published_at if article.time_status == 'trusted' else current)
                    if article.raw_id not in observed_ids and (article.time_status != 'trusted' or article.published_at >= cutoff):
                        articles.append(replace(article, retrieval_kind='stock_associated'))
                        observed_ids.add(article.raw_id)
                total = data.get('totle_hits')
                effective_size = data.get('page_size')
                effective_size = effective_size if type(effective_size) is int and 0 < effective_size <= page_size else page_size
                has_total = type(total) is int and total >= 0
                older = bool(page_times) and len(page_times) == len(rows) and max(page_times) < cutoff
                # Revisit the overlapping 24h window even with an unchanged
                # head: later pages can gain issuer associations or corrections.
                # Cached evidence/results avoid repeating the expensive work.
                if (not rows or older
                        or has_total and (page-1) * effective_size + len(rows) >= total
                        or not has_total and len(rows) < effective_size):
                    complete = True
                    break
            except Exception:
                if not articles:
                    return self._search(target, current, page_size, 'fallback_associated_unavailable')
                self.last_fetch_status[target.key] = 'partial_associated_page_error'
                break
        if complete and not malformed:
            self.last_fetch_status[target.key] = 'associated_complete'
            if head is not None:
                self._pending[state_key] = {
                    'head_id': head, 'checked_at': _iso(current),
                    'covered_since': _iso(cutoff),
                    'observations': [(canonical_identity(a), a.raw_id, a.url) for a in articles],
                }
        elif target.key not in self.last_fetch_status:
            self.last_fetch_status[target.key] = 'partial_associated_invalid_record' if malformed else 'partial_associated_page_limit'
        return self._evidence(articles, current)

    def acknowledge(self, conn, now):
        """Commit only observations already archived in the caller transaction."""
        if not conn.in_transaction:
            raise ValueError('stock_checkpoint_requires_archive_transaction')
        for key, checkpoint in self._pending.items():
            expected = {self._resolved_identity(conn, identity, raw_id, url)
                        for identity, raw_id, url in checkpoint['observations']}
            actual = set()
            identities = list(expected)
            for offset in range(0, len(identities), 400):
                chunk = identities[offset:offset + 400]
                actual.update(r[0] for r in conn.execute(
                    'SELECT DISTINCT identity_key FROM subscription_articles WHERE identity_key IN (' + ','.join('?' for _ in chunk) + ')', chunk))
            if expected != actual:
                raise ValueError('stock_checkpoint_observations_not_archived')
            value = {name: checkpoint[name] for name in ('head_id', 'checked_at', 'covered_since')}
            conn.execute('INSERT INTO monitor_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                         (key, json.dumps(value, sort_keys=True), _iso(now)))
        self._pending.clear()
