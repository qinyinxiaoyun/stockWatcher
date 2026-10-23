"""News retrieval and normalization.

The production client deliberately has a very small HTTP surface.  Tests (and
local development without network credentials) inject a callable transport,
so parsing, filtering and monitoring can be exercised entirely offline.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html import unescape
import hashlib
import json
import re
import time
import unicodedata
from typing import Any, Callable, Iterable, Mapping, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class NewsTarget:
    """A configured monitoring target and its operator-defined focus."""

    key: str
    name: str
    query: str
    queries: tuple[str, ...] = ()
    focus: tuple[str, ...] = ()
    effective_at: datetime | None = None
    kind: str = "stock"

    @classmethod
    def from_value(cls, value: str | Mapping[str, Any]) -> "NewsTarget":
        if isinstance(value, str):
            text = value.strip()
            if not text:
                raise ValueError("target cannot be blank")
            return cls(key=text, name=text, query=text, queries=(text,))
        key = str(value.get("key") or value.get("symbol") or value.get("code") or "").strip()
        name = str(value.get("name") or key).strip()
        query = str(value.get("query") or name or key).strip()
        raw_queries = value.get("queries") or (query,)
        if isinstance(raw_queries, str):
            raw_queries = [raw_queries]
        queries = tuple(dict.fromkeys(str(item).strip() for item in raw_queries if str(item).strip()))
        raw_focus = value.get("focus") or value.get("concerns") or ()
        if isinstance(raw_focus, str):
            raw_focus = [raw_focus]
        focus = tuple(str(item).strip() for item in raw_focus if str(item).strip())
        raw_effective_at = value.get("effective_at")
        effective_at = parse_published_at(raw_effective_at) if raw_effective_at else None
        kind = str(value.get("kind") or "stock").strip().lower()
        if not key or not name or not query:
            raise ValueError("target requires key/name/query")
        if kind not in {"stock", "theme"}:
            raise ValueError("target kind must be stock or theme")
        return cls(
            key=key,
            name=name,
            query=query,
            queries=queries or (query,),
            focus=focus,
            effective_at=effective_at,
            kind=kind,
        )


@dataclass(frozen=True, slots=True)
class NewsArticle:
    """Canonical article independent of an upstream response layout."""

    key: str
    title: str
    url: str
    published_at: datetime
    summary: str = ""
    source: str = "东方财富"
    target_keys: tuple[str, ...] = ()
    raw_id: str | None = None
    legacy_keys: tuple[str, ...] = ()
    # Unknown/conflicting source dates are retained for audit, never replaced
    # with retrieval time to make an article appear recent.
    time_status: str = "trusted"
    source_published_at: datetime | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None

    def with_targets(self, keys: Iterable[str]) -> "NewsArticle":
        merged = tuple(sorted(set(self.target_keys).union(keys)))
        return replace(self, target_keys=merged)


class EastmoneyResponseError(ValueError):
    """The HTTP response is not a successful Eastmoney article payload."""


class NewsTransport(Protocol):
    def __call__(self, url: str, timeout: float) -> str | bytes: ...


def _default_transport(url: str, timeout: float) -> str:
    request = Request(
        url,
        headers={
            "Accept": "application/json,text/javascript,*/*;q=0.8",
            "User-Agent": "stock-watch/1.0 (+local self-hosted monitor)",
            "Referer": "https://so.eastmoney.com/",
        },
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS endpoint
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise EastmoneyResponseError("Eastmoney response exceeds 2 MB")
    return raw.decode("utf-8", errors="strict")


def unwrap_jsonp(payload: str | bytes) -> Any:
    """Decode JSON or a conservative ``callback(JSON);`` JSONP envelope."""

    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="strict")
    text = payload.lstrip("\ufeff \t\r\n")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.fullmatch(
            r"\s*[A-Za-z_$][\w$\.]*\s*\(\s*(.*)\s*\)\s*;?\s*",
            text,
            flags=re.DOTALL,
        )
        if not match:
            raise ValueError("upstream response is neither JSON nor valid JSONP") from None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON inside JSONP response") from exc


def _article_rows(value: Any) -> list[Mapping[str, Any]]:
    """Locate article rows in known and legacy Eastmoney response shapes."""

    if isinstance(value, list):
        if all(isinstance(row, Mapping) for row in value):
            return list(value)
        return []
    if not isinstance(value, Mapping):
        return []

    # Prefer named article collections so an unrelated ``data`` list (for
    # example suggestions) is not selected first.
    preferred = (
        "cmsArticleWebOld",
        "cmsArticleWeb",
        "articleList",
        "newsList",
        "list",
        "items",
        "diff",
    )
    for key in preferred:
        if key in value:
            rows = _article_rows(value[key])
            if rows:
                return rows
    for key in ("result", "data", "response"):
        if key in value:
            rows = _article_rows(value[key])
            if rows:
                return rows
    return []


def _validated_article_rows(value: Any) -> list[Mapping[str, Any]]:
    """Validate success separately from a legitimate empty result set."""

    if not isinstance(value, Mapping):
        raise EastmoneyResponseError("Eastmoney response root must be an object")
    if value.get("code") != 0:
        raise EastmoneyResponseError("Eastmoney response code is not successful")
    result = value.get("result")
    if not isinstance(result, Mapping):
        raise EastmoneyResponseError("Eastmoney response has no result object")
    rows = result.get("cmsArticleWebOld")
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise EastmoneyResponseError("Eastmoney response has no valid article list")
    return list(rows)


def _first(item: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = item.get(name)
        if value not in (None, ""):
            return value
    return None


def _clean_text(value: Any) -> str:
    # Search highlighting wraps parts of words. Adding spaces for inline tags
    # turns e.g. 阿里<em>巴巴</em> into a different title and breaks matching.
    text = re.sub(r"</?(?:em|b|strong|span|font|mark)\b[^>]*>", "", str(value or ""), flags=re.I)
    text = unicodedata.normalize("NFKC", unescape(re.sub(r"<[^>]*>", " ", text)))
    text = " ".join(text.replace("\u200b", "").replace("\ufeff", "").split())
    text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff0-9])", "", text)
    return re.sub(r"(?<=[0-9])\s+(?=[\u3400-\u9fff])", "", text)


def parse_published_at(value: Any) -> datetime:
    """Parse timestamps used by Eastmoney; return an aware Shanghai time."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        number = float(value)
        if number > 10_000_000_000:  # milliseconds
            number /= 1000
        parsed = datetime.fromtimestamp(number, timezone.utc)
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        parsed = None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M",
            ):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            raise ValueError(f"unsupported article timestamp: {value!r}")
    else:
        raise ValueError("article timestamp is missing")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def normalize_article(item: Mapping[str, Any], target: NewsTarget) -> NewsArticle:
    title = _clean_text(_first(item, "title", "Title", "newsTitle", "Art_Title"))
    if not title:
        raise ValueError("article title is missing")
    url = str(
        _first(item, "url", "Url", "link", "articleUrl", "ArticleUrl", "Art_UniqueUrl")
        or ""
    ).strip()
    raw_id_value = _first(item, "id", "code", "articleId", "ArticleID", "Art_Code")
    raw_id = str(raw_id_value).strip() if raw_id_value not in (None, "") else None
    raw_published_at = _first(
            item,
            "published_at",
            "publishTime",
            "showTime",
            "date",
            "time",
            "Art_ShowTime",
        )
    try:
        published_at = parse_published_at(raw_published_at)
        time_status = "trusted"
        for field in ("published_at", "publishTime", "showTime", "date", "time", "Art_ShowTime"):
            if item.get(field) in (None, ""):
                continue
            try:
                alternative = parse_published_at(item[field])
            except (ValueError, OverflowError, OSError):
                continue
            if abs((alternative - published_at).total_seconds()) > 300:
                time_status = "conflict"
    except (ValueError, OverflowError, OSError):
        published_at = datetime(1970, 1, 1, tzinfo=SHANGHAI)
        time_status = "unknown"
    summary = _clean_text(
        _first(item, "summary", "content", "digest", "description", "Art_Content") or ""
    )
    source = str(_first(item, "source", "mediaName", "Art_MediaName") or "东方财富").strip()
    identity = raw_id or url or f"{title}\0{published_at.isoformat()}"
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    legacy_names = {target.name, target.query, *target.queries}
    # The imported legacy state used the pre-market-suffix company label.
    if target.name.startswith("阿里巴巴"):
        legacy_names.add("阿里巴巴")
    legacy_keys = tuple(
        sorted(
            hashlib.sha256(f"{name}|{url}".encode("utf-8")).hexdigest()[:24]
            for name in legacy_names
            if name and url
        )
    )
    return NewsArticle(
        key=key,
        raw_id=raw_id,
        title=title,
        url=url,
        published_at=published_at,
        summary=summary,
        source=source,
        target_keys=(target.key,),
        legacy_keys=legacy_keys,
        time_status=time_status,
        source_published_at=published_at if time_status != "unknown" else None,
    )


def _matches_target(article: NewsArticle, target: NewsTarget) -> bool:
    combined = f"{article.title} {article.summary}".casefold()
    raw_terms: list[str] = []
    for query in target.queries or (target.query,):
        raw_terms.extend(re.split(r"\s+(?:or|OR)\s+|[|,，;/]+", query))
    terms = [target.name, *(term.strip() for term in raw_terms)]
    return any(term and term.casefold() in combined for term in terms)


class EastmoneyNewsClient:
    """Minimal Eastmoney JSONP search client with an injectable transport."""

    endpoint = "https://search-api-web.eastmoney.com/search/jsonp"

    def __init__(
        self,
        transport: NewsTransport | Callable[..., str | bytes] | None = None,
        *,
        timeout: float = 10.0,
        endpoint: str | None = None,
        max_attempts: int = 2,
        backoff_seconds: float = 0.25,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport or _default_transport
        self.timeout = timeout
        self.endpoint = endpoint or self.endpoint
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)
        self.sleeper = sleeper

    def build_url(
        self, target: NewsTarget, *, page_size: int = 20, query: str | None = None
    ) -> str:
        # The JSON parameter is encoded as data, never interpolated into JSONP
        # source.  A timestamp callback is unnecessary for the monitor.
        query = {
            "uid": "",
            "keyword": query or target.query,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "time",
                    "pageIndex": 1,
                    "pageSize": page_size,
                }
            },
        }
        return f"{self.endpoint}?{urlencode({'cb': 'stockWatchCallback', 'param': json.dumps(query, ensure_ascii=False, separators=(',', ':'))})}"

    def _request(self, url: str) -> str | bytes:
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                try:
                    response = self.transport(url, self.timeout)
                except TypeError:
                    # Convenience for ``requests``-like fakes without weakening
                    # the public transport protocol.
                    response = self.transport(url, timeout=self.timeout)  # type: ignore[call-arg]
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()
                return getattr(response, "text", response)
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    self.sleeper(self.backoff_seconds * (2**attempt))
        assert last_error is not None
        raise EastmoneyResponseError(
            f"Eastmoney request failed after {self.max_attempts} attempt(s): "
            f"{type(last_error).__name__}"
        ) from last_error

    def fetch_target(
        self,
        target: NewsTarget | str | Mapping[str, Any],
        *,
        now: datetime | None = None,
        page_size: int = 20,
    ) -> list[NewsArticle]:
        del now  # supported for deterministic fake parity
        normalized_target = target if isinstance(target, NewsTarget) else NewsTarget.from_value(target)
        articles: list[NewsArticle] = []
        failures: list[Exception] = []
        successes = 0
        for query in normalized_target.queries or (normalized_target.query,):
            query_target = replace(normalized_target, query=query)
            try:
                payload = unwrap_jsonp(
                    self._request(
                        self.build_url(normalized_target, page_size=page_size, query=query)
                    )
                )
                rows = _validated_article_rows(payload)
                successes += 1
            except Exception as exc:
                failures.append(exc)
                continue
            for row in rows:
                try:
                    article = normalize_article(row, query_target)
                except ValueError:
                    # One malformed upstream row must not suppress valid articles.
                    continue
                if article.url and _matches_target(article, normalized_target):
                    articles.append(article)
        if successes == 0 and failures:
            raise EastmoneyResponseError(
                f"all {len(failures)} Eastmoney queries failed for target {normalized_target.key}"
            ) from failures[-1]
        return articles

    def fetch_all(
        self,
        targets: Iterable[NewsTarget | str | Mapping[str, Any]],
        *,
        now: datetime | None = None,
        page_size: int = 20,
    ) -> list[NewsArticle]:
        articles: list[NewsArticle] = []
        for target in targets:
            articles.extend(self.fetch_target(target, now=now, page_size=page_size))
        return articles


__all__ = [
    "EastmoneyNewsClient",
    "EastmoneyResponseError",
    "NewsArticle",
    "NewsTarget",
    "NewsTransport",
    "SHANGHAI",
    "normalize_article",
    "parse_published_at",
    "unwrap_jsonp",
]
