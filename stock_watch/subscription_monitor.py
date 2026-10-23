"""One worker, shared article processing and member-scoped durable fan-out.

Only configured focus strings are shared within an organisation. Members marked
``analysis_private`` receive a separate cache domain; personal background is not
part of the public analysis specification. Thresholds never enter cache keys.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from copy import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
import unicodedata
import uuid

from .analyzer import DeepSeekAnalyzer, validate_analysis
from .monitor import (
    MonitorService, MonitorSettings, RunLeaseLost, _as_mapping, _baseline_keys,
    _iso, open_connection, write_transaction,
)
from .news import NewsArticle, NewsTarget, parse_published_at
from .scheduler import SHANGHAI, as_shanghai, slot_for_time


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS subscription_articles (
 article_key TEXT PRIMARY KEY, identity_key TEXT NOT NULL, content_hash TEXT NOT NULL,
 source TEXT NOT NULL, article_json TEXT NOT NULL, published_at TEXT NOT NULL,
 fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_subscription_articles_time ON subscription_articles(published_at);
CREATE TABLE IF NOT EXISTS subscription_analysis (
 cache_key TEXT PRIMARY KEY, result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS member_evaluations (
 member_id INTEGER NOT NULL, article_key TEXT NOT NULL, target_key TEXT NOT NULL,
 evaluated_at TEXT NOT NULL, outcome TEXT NOT NULL,
 PRIMARY KEY(member_id,article_key,target_key)
);
CREATE TABLE IF NOT EXISTS member_baselines (
 member_id INTEGER NOT NULL, source TEXT NOT NULL, target_key TEXT NOT NULL,
 established_at TEXT NOT NULL, PRIMARY KEY(member_id,source,target_key)
);
CREATE TABLE IF NOT EXISTS member_legacy_seen (
 member_id INTEGER NOT NULL, article_key TEXT NOT NULL, seen_at TEXT NOT NULL,
 PRIMARY KEY(member_id,article_key)
);
"""


def copy_legacy_state(conn, member_id):
    """Copy the explicitly assigned owner's history inside the caller's transaction."""
    from .subscriptions import MemberConfigStore
    conn.execute("INSERT OR IGNORE INTO member_legacy_seen(member_id,article_key,seen_at) SELECT ?,article_key,seen_at FROM seen_articles", (member_id,))
    snapshot = MemberConfigStore(conn, member_id).snapshot()
    present = {r[0] for r in conn.execute("SELECT key FROM monitor_state")}
    for raw in snapshot.targets:
        target = NewsTarget.from_value(raw)
        if present.intersection(_baseline_keys(target)):
            conn.execute("INSERT OR IGNORE INTO member_baselines VALUES (?, 'eastmoney', ?, ?)",
                         (member_id, target.key, snapshot.updated_at))


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _normal(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def article_version(article, namespace=None):
    """Stable report identity; search excerpts and metadata aren't revisions."""
    from .quality import canonical_identity, revision_key
    identity = article.key if article.first_seen_at else canonical_identity(article, _normal(namespace or "eastmoney"))
    content = _digest([identity, revision_key(article)])
    return content, identity, content


def _article_dict(article):
    return {"key": article.key, "title": article.title, "summary": article.summary,
            "url": article.url, "published_at": article.published_at.isoformat(),
            "source": article.source, "target_keys": list(article.target_keys),
            "raw_id": article.raw_id, "legacy_keys": list(article.legacy_keys),
            "time_status": article.time_status,
            **{name: getattr(article, name).isoformat() if getattr(article, name) else None
               for name in ("source_published_at", "first_seen_at", "last_seen_at")}}


def _read_article(value):
    obj = json.loads(value)
    obj["published_at"] = parse_published_at(obj["published_at"])
    obj["target_keys"] = tuple(obj["target_keys"])
    obj["legacy_keys"] = tuple(obj.get("legacy_keys", ()))
    for name in ("source_published_at", "first_seen_at", "last_seen_at"):
        if obj.get(name):
            obj[name] = parse_published_at(obj[name])
    return NewsArticle(**obj)


@dataclass
class MemberScan:
    member: object
    snapshot: object
    settings: MonitorSettings
    targets: dict
    rules: dict


class SubscriptionMonitorService(MonitorService):
    """Reuse the global monitor lease; never create a per-member scheduler."""
    independent_subscriptions = True

    def __init__(self, *args, corp_id, **kwargs):
        self.budget_limits = kwargs.pop("budget_limits", None) or {}
        super().__init__(*args, **kwargs)
        if isinstance(self.analyzer, DeepSeekAnalyzer):
            # The service budget counts actual requests, including failures.
            # Retry unfinished work next cycle without spending a hidden retry.
            self.analyzer = copy(self.analyzer)
            self.analyzer.max_attempts = 1
        self.corp_id = corp_id
        self.source_namespace = _normal(getattr(self.fetcher, "source_namespace", "eastmoney"))

    def _members(self):
        from .subscriptions import MemberConfigStore, MemberRegistry
        scans, errors = [], []
        with open_connection(self.db) as conn:
            for member in MemberRegistry(conn, self.corp_id).active():
                try:
                    snapshot = MemberConfigStore(conn, member.id).snapshot()
                    settings = MonitorSettings.from_snapshot(snapshot, environ=self.environ)
                    data = _as_mapping(snapshot)
                    rules = {s["id"]: s for s in data.get("stocks", ())}
                    scans.append(MemberScan(member, snapshot, settings,
                                            {t.key: t for t in settings.targets}, rules))
                except Exception as exc:
                    errors.append(f"member {member.id}: {type(exc).__name__}")
        return scans, errors

    def _cache_key(self, content, target, member):
        return _digest([self.corp_id, member.id if getattr(member, "analysis_private", False) else "public",
                        content, target.key, target.kind, _normal(target.name),
                        sorted({_normal(f) for f in target.focus}),
                        getattr(self.analyzer, "model", type(self.analyzer).__name__),
                        getattr(self.analyzer, "model_revision", ""),
                        getattr(getattr(self.analyzer, "transport", None), "endpoint", ""),
                        getattr(self.analyzer, "prompt_version", "1"),
                        getattr(self.analyzer, "schema_version", "1")])

    def _current_member(self, conn, scan):
        from .subscriptions import MemberConfigStore, MemberRegistry
        member = MemberRegistry(conn, self.corp_id).get(scan.member.user_id)
        if member is None or not member.active or member.generation != scan.member.generation:
            return None
        latest = MemberConfigStore(conn, member.id).snapshot()
        return member if latest.config_version == scan.settings.config_version else None

    @staticmethod
    def _notify(scan, target, analysis):
        rule = scan.rules.get(target.key, {})
        levels = scan.settings.push_levels
        if rule.get("push_level") == "none":
            return False
        if rule.get("push_level") == "all":
            levels = ("重大", "重要", "普通")
        direction = rule.get("direction", "both")
        return (target.key in analysis.matched_targets and analysis.should_notify
                and getattr(analysis, "is_fundamental", True) is not False and analysis.level in levels and
                (direction == "both" or
                 direction == "positive" and analysis.impact == "利好" or
                 direction == "negative" and analysis.impact == "利空"))

    def run_once(self, **kwargs):
        from .subscription_scan import run_subscription_scan
        return run_subscription_scan(self, **kwargs)


__all__ = ["SubscriptionMonitorService", "SCHEMA_SQL", "copy_legacy_state", "article_version"]
