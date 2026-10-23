"""Offline, conservative article quality and identity policy.

Search snippets are observations, not document revisions. All decisions expose
safe reason codes; original observations remain in SQLite for local auditing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import re
import sqlite3
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .news import NewsArticle, NewsTarget, SHANGHAI, _clean_text, parse_published_at

QUALITY_RULE_VERSION = "1"
QUALITY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS quality_articles (
    canonical_id TEXT PRIMARY KEY,
    revision_key TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    source_published_at TEXT,
    time_status TEXT NOT NULL DEFAULT 'trusted',
    target_keys_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS quality_article_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id TEXT NOT NULL REFERENCES quality_articles(canonical_id),
    revision_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source_published_at TEXT,
    raw_key TEXT NOT NULL,
    raw_id TEXT,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    source TEXT NOT NULL,
    time_status TEXT NOT NULL,
    rule_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_quality_observations_article
    ON quality_article_observations(canonical_id,id);
CREATE TABLE IF NOT EXISTS quality_article_revisions (
    canonical_id TEXT NOT NULL REFERENCES quality_articles(canonical_id),
    revision_key TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    update_evidence TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    PRIMARY KEY(canonical_id,revision_key)
);
CREATE TABLE IF NOT EXISTS quality_article_aliases (
    namespace TEXT NOT NULL,
    alias TEXT NOT NULL,
    canonical_id TEXT NOT NULL REFERENCES quality_articles(canonical_id),
    PRIMARY KEY(namespace,alias)
);
CREATE TABLE IF NOT EXISTS quality_events (
    event_id TEXT NOT NULL,
    event_version TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'unknown',
    event_date TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(event_id,event_version)
);
CREATE TABLE IF NOT EXISTS quality_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id TEXT NOT NULL,
    revision_key TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    mode TEXT NOT NULL,
    expires_at TEXT,
    decided_at TEXT NOT NULL
);
"""


def _hash(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def canonical_url(value: str) -> str:
    """Remove known tracking only; business query values remain identity data."""
    value = _clean_text(value)
    try:
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.scheme.lower() not in {"http", "https"}:
            return value.split("#", 1)[0]
        host = parsed.hostname.lower()
        port = parsed.port
        if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
            host += f":{port}"
        pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {
                     "spm", "from", "ref", "referrer", "gclid", "fbclid", "_t",
                 }]
        return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", urlencode(sorted(pairs)), ""))
    except ValueError:
        return value.split("#", 1)[0]


def canonical_identity(article: NewsArticle, namespace: str = "eastmoney") -> str:
    if article.raw_id:
        return _hash(namespace, "id", _clean_text(article.raw_id))
    if article.url:
        return _hash(namespace, "url", canonical_url(article.url))
    return _hash(namespace, "fallback", _clean_text(article.title), article.published_at.date().isoformat())


def canonicalize_article(article: NewsArticle) -> NewsArticle:
    return replace(article, key=canonical_identity(article), title=_clean_text(article.title),
                   summary=_clean_text(article.summary), source=_clean_text(article.source),
                   url=canonical_url(article.url))


_UPDATE = re.compile(r"(?:更正|修正|撤销|取消|终止|作废|撤回|由计划转为实施|已完成|正式实施|下调指引|上调指引)")
_FACT_NUMBER = re.compile(r"\d+(?:[.,]\d+)*(?:%|亿元|万元|亿美元|万美元|亿港元|万港元|亿|万|元|吨|片|台|股|倍|GW|MW|GWh|MWh)?", re.I)
_MATERIAL_NUMBER = re.compile(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(亿美元|万美元|亿港元|万港元|亿元|万元|GWh|MWh|GW|MW|%|元|吨|片|台|股|倍)", re.I)
_UPDATE_OBJECT = re.compile(r"订单|合同|回购|收购|并购|重组|减持|增持|项目|产线|建设|募资|指引")
_CORRECTION = re.compile(r"更正|修正|上调指引|下调指引")
_DESTINATION = re.compile(r"应为|实际为|更正为|修正为|调整为|变更为|下调为|上调为|改为|现为")
_METRICS = (
    ("net_profit", r"净利润"), ("revenue", r"营业收入|营收"),
    ("gross_margin", r"毛利率"), ("guidance", r"指引"),
    ("ownership", r"持股比例|持股比|股权比例"), ("capacity", r"产能"),
)


def _number_evidence(match: re.Match[str]) -> str:
    number = format(Decimal(match[1].replace(",", "")).normalize(), "f")
    unit = match[2].lower()
    scales = {"亿元": ("元", 100_000_000), "万元": ("元", 10_000),
              "亿美元": ("美元", 100_000_000), "万美元": ("美元", 10_000),
              "亿港元": ("港元", 100_000_000), "万港元": ("港元", 10_000)}
    if unit in scales:
        unit, scale = scales[unit]
        number = format((Decimal(number) * scale).normalize(), "f")
    return number + unit


def _update_evidence(summary: str) -> str:
    """Extract stable action/fact tokens, never arbitrary excerpt wording.

    A lone update word or an unfinished amount is insufficient evidence. Numeric
    corrections use the destination value, not the superseded number or dates.
    """
    statements = re.split(r"[。；;!?！？]", _clean_text(summary))
    updates: set[str] = set()
    for statement in statements:
        correction = _CORRECTION.search(statement)
        if correction and (
            re.search(r"(?:未|不|没有|否认|并未|尚未|可能|拟).{0,2}$", statement[max(0, correction.start()-5):correction.start()])
            or statement[correction.end():].startswith("前")
        ):
            correction = None
        if correction:
            tail = statement[correction.end():]
            tail = re.split(r"[,，](?=(?:较|比)?(?:原|此前|先前|去年|上年|更正前))", tail, maxsplit=1)[0]
            destinations = list(_DESTINATION.finditer(tail))
            if destinations:
                tail = tail[destinations[-1].end():]
            numbers = list(_MATERIAL_NUMBER.finditer(tail))
            if numbers:
                metric = next((name for name, pattern in _METRICS if re.search(pattern, statement)), "amount")
                action = "guidance_up" if correction[0] == "上调指引" else "guidance_down" if correction[0] == "下调指引" else "correction"
                updates.add(f"{action}:{metric}:{_number_evidence(numbers[0] if destinations else numbers[-1])}")
        for action in re.finditer(r"撤销|取消|终止|作废|撤回|由计划转为实施|已完成|正式实施", statement):
            prefix = statement[max(0, action.start()-5):action.start()]
            if re.search(r"(?:未|不|没有|否认|并未|尚未|可能|或将).{0,2}$", prefix):
                continue
            nearby = statement[max(0, action.start()-20):action.end()+20]
            objects = list(_UPDATE_OBJECT.finditer(nearby))
            if objects:
                relative_action = action.start() - max(0, action.start()-20)
                subject = min(objects, key=lambda match: min(abs(match.start()-relative_action), abs(match.end()-relative_action)))
                # Synonymous cancel actions are one fact; completion and actual
                # implementation remain different from a cancellation.
                category = "cancel" if action[0] in {"撤销", "取消", "终止", "作废", "撤回"} else "complete" if action[0] == "已完成" else "implement"
                updates.add(f"{category}:{subject[0]}")
    return "|".join(sorted(updates))


def revision_key(article: NewsArticle) -> str:
    return _hash("revision-v1", _clean_text(article.title), _update_evidence(article.summary))


@dataclass(frozen=True, slots=True)
class ContentDecision:
    kind: str
    reason: str
    urgent_hint: bool = False


@dataclass(frozen=True, slots=True)
class FreshnessDecision:
    mode: str
    expires_at: datetime | None
    reason: str


# A positive operational fact overrides a market-template match. Broad topic
# words like '公司' or '市场' deliberately do not qualify as operational facts.
_FUNDAMENTAL = re.compile(
    r"财报|业绩|营收|营业收入|净利润|毛利|盈利预警|亏损|业绩指引|订单|中标|合同|客户|"
    r"商业化|量产|产能|良率|供应|供需|需求|出货|产品.{0,5}(?:价格|提价|降价)|"
    r"(?:存储|芯片|原油|铜|钢|煤|电池|组件|硅料|面板|DRAM|NAND|HBM).{0,6}(?:价格|报价|涨价|降价)|"
    r"金价|银价|油价|铜价|钢价|煤价|锂价|天然气价格|"
    r"资本开支|融资偿债|债务|违约|并购|收购|重组|监管|政策|调查|处罚|诉讼|仲裁|"
    r"停产|复产|事故|召回|裁员|破产|清算|控制权|实控人|控股股东|高管|减持|增持|回购|"
    r"研发|新品|专利|出口管制|禁令|产销|销售额|销售量|现金流|分红|派息|募资|定增|可转债",
    re.I,
)
_URGENT = re.compile(r"违约|破产|清算|停产|重大事故|被立案|立案调查|欺诈|财务造假|盈利预警|业绩暴雷|订单.{0,12}(?:撤销|取消)|(?:撤销|取消).{0,8}订单|重大诉讼|出口禁令")
_DISCLOSURE = re.compile(r"公告|披露|宣布|通报|发布|获悉|确认|决定|发生|更正")
_NOISE_PATTERNS = (
    ("flow_daily", re.compile(r"(?:港股通|南向|北向|南北向|沪股通|深股通).{0,40}(?:净买入|净卖出|净买卖|净流入|净流出)")),
    ("margin_daily", re.compile(r"(?:融资余额|融券余额|融资融券|融资净买入|融资净偿还|融资买入额|两融日报)")),
    ("technical_indicator", re.compile(r"(?:MACD|KDJ|RSI|均线|金叉|死叉|技术指标|技术面|突破年线|跌破年线|突破半年线)", re.I)),
    ("trading_recap", re.compile(r"(?:收盘|午盘|早盘|尾盘|复盘|异动|涨跌幅|涨幅|跌幅|换手率|成交额|成交量|股价).{0,35}(?:\d|上涨|下跌|涨停|跌停|新高|新低)|(?:涨停|跌停|上涨|下跌)\d+(?:\.\d+)?%")),
    ("analyst_rating", re.compile(r"(?:目标价|维持.{0,8}评级|给予.{0,8}评级|买入评级|卖出评级)")),
)


def classify_article(article: NewsArticle) -> ContentDecision:
    title = _clean_text(article.title)
    text = title + "。" + _clean_text(article.summary)
    fundamental = bool(_FUNDAMENTAL.search(text))
    urgent = fundamental and bool(_URGENT.search(text))
    if fundamental:
        return ContentDecision("fundamental", "operating_fact", urgent)
    for reason, pattern in _NOISE_PATTERNS:
        if pattern.search(title):
            return ContentDecision("market_noise", reason)
    return ContentDecision("unknown", "needs_fact_review")


def _aware(value: datetime) -> datetime:
    return (value.replace(tzinfo=SHANGHAI) if value.tzinfo is None else value).astimezone(SHANGHAI)


def freshness(article: NewsArticle, now: datetime, backfill: bool = False) -> FreshnessDecision:
    now = _aware(now)
    if article.time_status != "trusted":
        return FreshnessDecision("unknown", None, "source_time_" + ("conflict" if article.time_status == "conflict" else "unknown"))
    published = _aware(article.source_published_at or article.published_at)
    if published > now + timedelta(minutes=5):
        return FreshnessDecision("future", None, "future_source_time")
    decision = classify_article(article)
    if backfill:
        # Explicitly scoped historical review has a separate approval path. It
        # cannot masquerade as a newly published real-time notification.
        return FreshnessDecision("expired", published + timedelta(hours=2), "explicit_backfill_only")
    normal_end = published + timedelta(hours=2)
    if now < normal_end:
        return FreshnessDecision("realtime", normal_end, "within_two_hours")
    morning_start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    morning_end = morning_start + timedelta(minutes=15)
    overnight_start = (morning_start - timedelta(days=1)).replace(hour=15)
    urgent_end = published + timedelta(hours=24)
    if decision.urgent_hint and _DISCLOSURE.search(article.title + article.summary) and now < urgent_end:
        return FreshnessDecision("urgent", urgent_end, "major_disclosure_24h_exception")
    if morning_start <= now < morning_end and overnight_start <= published <= morning_start and decision.kind == "fundamental":
        return FreshnessDecision("morning", morning_end, "overnight_operating_fact")
    return FreshnessDecision("expired", normal_end, "source_time_expired")


_EVENT_TYPES = (
    ("earnings", r"财报|业绩|净利润|营收|营业收入|毛利"),
    ("buyback", r"回购"), ("disposal", r"减持"), ("acquisition", r"并购|收购|重组"),
    ("order", r"订单|合同|中标"), ("capacity", r"产能|量产|投产|扩产"),
    ("regulatory", r"监管|调查|处罚|禁令|政策"), ("litigation", r"诉讼|仲裁"),
    ("credit", r"违约|破产|清算|偿债"),
)


def _event_type(text: str) -> str:
    return next((name for name, pattern in _EVENT_TYPES if re.search(pattern, text)), "unknown")


def event_identity(article: NewsArticle, target: NewsTarget | str, analysis: Any = None) -> tuple[str, str]:
    """Deduplicate proven exact facts across URLs without fuzzy title merging.

    Unknown stories stay article-scoped. Date, subject, type and normalized full
    factual title must agree to merge a reprint. Amounts remain in the version
    and are removed from the event stem only when an explicit correction exists.
    """
    del analysis  # Free model wording is not trusted identity evidence.
    title = _clean_text(article.title)
    title = re.sub(r"^(?:快讯|公告|独家|重磅|公司新闻|消息)[:：]", "", title)
    title = re.sub(r"[（(](?:转载|更新|更正)[）)]$", "", title)
    title = re.sub(r"[\s:：,，。.!！?？\-—]", "", title)
    subject = target.name if isinstance(target, NewsTarget) else str(target)
    if isinstance(target, NewsTarget):
        subject = re.sub(r"[-－](?:W|SW|S|B)$", "", target.name, flags=re.I)
    event_type = _event_type(title)
    date = _aware(article.source_published_at or article.published_at).date().isoformat()
    # Without an explicit subject in evidence, related targets must not turn two
    # unrelated supplier/customer stories into one event.
    has_subject = bool(subject and subject in title)
    if event_type == "unknown" or not has_subject:
        event_id = _hash("article-event-v1", article.key if article.first_seen_at else canonical_identity(article))
    else:
        stem = title
        if re.search(r"更正|修正", title):
            stem = re.sub(r"更正|修正", "", stem)
            stem = _FACT_NUMBER.sub("#", stem)
        event_id = _hash("event-v1", subject, event_type, date, stem)
    return event_id, _hash("event-version-v1", title, _update_evidence(article.summary))


def _iso(value: datetime) -> str:
    return _aware(value).isoformat()


class QualityStore:
    """Short local transactions for stable identity and unmodified evidence."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _representative(self, identity: str, existing: sqlite3.Row, article: NewsArticle) -> tuple[NewsArticle, bool]:
        """Keep accepted correction evidence when a later query omits it."""
        prior = self.conn.execute(
            "SELECT title,summary,update_evidence FROM quality_article_revisions WHERE canonical_id=? AND revision_key=?",
            (identity, existing["revision_key"]),
        ).fetchone()
        if prior is None:
            # Older schema-4 databases already retain original observations.
            # Recover a representative once, then persist it in the new table.
            observations = self.conn.execute(
                "SELECT title,summary FROM quality_article_observations WHERE canonical_id=? AND revision_key=? ORDER BY id DESC",
                (identity, existing["revision_key"]),
            ).fetchall()
            if not observations:
                return article, False
            prior = max(observations, key=lambda row: (bool(_update_evidence(row["summary"])), len(row["summary"])))
        old_evidence = set(_update_evidence(prior["summary"]).split("|")) - {""}
        new_evidence = set(_update_evidence(article.summary).split("|")) - {""}
        incoming_revision = revision_key(article)
        old_article = replace(article, title=prior["title"], summary=prior["summary"])
        same_title = article.title == prior["title"]
        if same_title and old_evidence and new_evidence <= old_evidence:
            # A subset is an incomplete excerpt, including an excerpt with no
            # correction words. It cannot erase previously accepted facts.
            return old_article, True
        if incoming_revision == existing["revision_key"]:
            return (old_article if len(prior["summary"]) >= len(article.summary) else article), bool(old_evidence)
        historical = self.conn.execute(
            "SELECT 1 FROM quality_article_revisions WHERE canonical_id=? AND revision_key=?",
            (identity, incoming_revision),
        ).fetchone()
        new_date = article.source_published_at or article.published_at
        reasserted = bool(
            existing["source_published_at"] and article.time_status == "trusted"
            and _aware(new_date) > parse_published_at(existing["source_published_at"])
            and re.search(r"再次更正|再度更正|恢复|重新调整|再次调整", article.title + article.summary)
        )
        if historical and not reasserted:
            return old_article, True
        return article, False

    def observe(self, article: NewsArticle, now: datetime, *, namespace: str = "eastmoney") -> NewsArticle:
        raw = article
        article = canonicalize_article(article)
        identity = canonical_identity(article, namespace)
        aliases = [("id", _clean_text(article.raw_id))] if article.raw_id else []
        if article.url:
            aliases.append(("url", article.url))
        own_transaction = not self.conn.in_transaction
        if own_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            for kind, alias in aliases:
                row = self.conn.execute("SELECT canonical_id FROM quality_article_aliases WHERE namespace=? AND alias=?", (namespace + ":" + kind, alias)).fetchone()
                if row:
                    identity = str(row[0])
                    break
            existing = self.conn.execute("SELECT * FROM quality_articles WHERE canonical_id=?", (identity,)).fetchone()
            retained_representative = False
            if existing:
                article, retained_representative = self._representative(identity, existing, article)
            observed = _iso(now)
            source_date = _iso(article.source_published_at or article.published_at) if article.time_status != "unknown" else None
            status = article.time_status
            first_seen = observed
            target_keys = set(article.target_keys)
            revision = revision_key(article)
            untrusted_revision = bool(existing and revision != existing['revision_key']
                                      and article.time_status != 'trusted')
            if existing:
                first_seen = existing["first_seen_at"]
                target_keys.update(json.loads(existing["target_keys_json"]))
                # A refresh of source time cannot grant a new lifetime. Keep the
                # earliest trusted date, and quarantine conflicting dates.
                if existing["source_published_at"]:
                    prior = parse_published_at(existing["source_published_at"])
                    if retained_representative and source_date and parse_published_at(source_date) <= prior:
                        # An old search excerpt cannot roll the disclosure date
                        # back after a proven correction was accepted.
                        source_date = _iso(prior)
                        status = existing["time_status"]
                    substantive_update = bool(
                        article.time_status == 'trusted'
                        and source_date and parse_published_at(source_date) > prior
                        and revision != existing["revision_key"]
                        and _UPDATE.search(article.title + "。" + article.summary)
                    )
                    if source_date and abs((parse_published_at(source_date) - prior).total_seconds()) > 300:
                        status = "trusted" if substantive_update else "conflict"
                    if not substantive_update and not untrusted_revision:
                        source_date = _iso(prior)
                    if status == "unknown" and not untrusted_revision:
                        status = existing["time_status"]
                else:
                    substantive_update = False
                if existing["time_status"] == "conflict" and not substantive_update and not untrusted_revision:
                    status = "conflict"
            # Keep an untrusted changed observation available for review, but
            # do not replace the current representative or lend it that fact's
            # trusted disclosure date. A later verified copy may still qualify.
            stored_revision = existing['revision_key'] if untrusted_revision else revision
            stored_source_date = existing['source_published_at'] if untrusted_revision else source_date
            stored_status = existing['time_status'] if untrusted_revision else status
            self.conn.execute("""INSERT INTO quality_articles(canonical_id,revision_key,first_seen_at,last_seen_at,source_published_at,time_status,target_keys_json)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(canonical_id) DO UPDATE SET revision_key=excluded.revision_key,
                last_seen_at=excluded.last_seen_at,source_published_at=excluded.source_published_at,time_status=excluded.time_status,
                target_keys_json=excluded.target_keys_json""",
                (identity, stored_revision, first_seen, observed, stored_source_date, stored_status, json.dumps(sorted(target_keys))))
            self.conn.execute("""INSERT INTO quality_article_observations(canonical_id,revision_key,observed_at,source_published_at,
                raw_key,raw_id,url,title,summary,source,time_status,rule_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identity, revision, observed, _iso(raw.source_published_at or raw.published_at) if raw.time_status != "unknown" else None,
                 raw.key, raw.raw_id, raw.url, raw.title, raw.summary, raw.source, raw.time_status, QUALITY_RULE_VERSION))
            if not untrusted_revision:
                self.conn.execute("""INSERT INTO quality_article_revisions(canonical_id,revision_key,title,summary,update_evidence,accepted_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(canonical_id,revision_key) DO UPDATE SET
                    title=excluded.title,summary=excluded.summary,update_evidence=excluded.update_evidence""",
                    (identity, revision, article.title, article.summary, _update_evidence(article.summary), observed))
            for kind, alias in aliases:
                self.conn.execute("INSERT OR IGNORE INTO quality_article_aliases(namespace,alias,canonical_id) VALUES(?,?,?)", (namespace + ":" + kind, alias, identity))
            if own_transaction:
                self.conn.commit()
        except BaseException:
            if own_transaction:
                self.conn.rollback()
            raise
        trusted_date = parse_published_at(source_date) if source_date else None
        return replace(article, key=identity, source_published_at=trusted_date,
                       published_at=trusted_date or article.published_at, time_status=status,
                       target_keys=tuple(sorted(target_keys)),
                       first_seen_at=parse_published_at(first_seen), last_seen_at=_aware(now))

    def get_article_times(self, identity: str) -> Mapping[str, Any] | None:
        row = self.conn.execute("SELECT first_seen_at,last_seen_at,source_published_at,time_status FROM quality_articles WHERE canonical_id=?", (identity,)).fetchone()
        return dict(row) if row else None

    def record_decision(self, article: NewsArticle, decision: ContentDecision, timing: FreshnessDecision, now: datetime) -> None:
        self.conn.execute("""INSERT INTO quality_decisions(canonical_id,revision_key,rule_version,kind,reason,mode,expires_at,decided_at)
            VALUES(?,?,?,?,?,?,?,?)""", (article.key, revision_key(article), QUALITY_RULE_VERSION,
            decision.kind, decision.reason, timing.mode, _iso(timing.expires_at) if timing.expires_at else None, _iso(now)))


__all__ = ["QUALITY_RULE_VERSION", "QUALITY_SCHEMA_SQL", "ContentDecision", "FreshnessDecision", "QualityStore",
           "canonical_url", "canonicalize_article", "canonical_identity", "revision_key", "classify_article", "freshness", "event_identity"]
