"""Offline v4 identity upgrade: preserve decisions, not just accepted alerts.

The fixture reproduces the old hash rather than generating old rows through
the new revision helper. In particular the original quality migration marker
already exists, as it does on an installation that has run schema 4.
"""
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json

import pytest

from stock_watch.analyzer import FakeAnalyzer
from stock_watch.db import SCHEMA_VERSION, connect, init_db
from stock_watch.monitor import _iso
from stock_watch.news import NewsArticle, NewsTarget, SHANGHAI, _clean_text
from stock_watch.quality import canonical_identity, _update_evidence
from stock_watch.subscription_monitor import (
    SubscriptionMonitorService, _article_dict, _digest, article_version,
)
from stock_watch.subscriptions import MemberConfigStore, MemberRegistry


NOW = datetime(2026, 11, 3, 10, tzinfo=SHANGHAI)
CORP = "offline-event-upgrade-corp"
OUTCOMES = ("baseline", "evaluated", "filtered:v1", "expired:v1")


class Fetcher:
    def __init__(self, article):
        self.article = article

    def fetch_target(self, target, **kwargs):
        return [replace(self.article, target_keys=(target.key,))]


def old_revision(article):
    # Exact pre-event-facts revision-v1 algorithm; do not call revision_key().
    values = ("revision-v1", _clean_text(article.title), _update_evidence(article.summary))
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


@pytest.fixture
def legacy_state(tmp_path):
    path = tmp_path / "schema-four.sqlite"
    conn = connect(path)
    init_db(conn)
    member = MemberRegistry(conn, CORP).add("synthetic-upgrade-member")
    store = MemberConfigStore(conn, member.id, clock=lambda: NOW-timedelta(days=2))
    store.apply_operation(
        {"op": "add_stock", "symbol": "123456.SH", "name": "合成公司", "focus": "营收"},
        actor=member.user_id,
    )
    snapshot = store.snapshot()
    target = NewsTarget.from_value(snapshot.targets[0])
    raw = NewsArticle(
        key="synthetic-original", raw_id="synthetic-original",
        title="合成公司公告2026年前三季度业绩",
        summary="营业收入100亿元。", url="https://example.invalid/schema-four-original",
        published_at=NOW-timedelta(minutes=10), target_keys=(target.key,),
    )
    canonical = canonical_identity(raw)
    article = replace(raw, key=canonical, source_published_at=raw.published_at,
                      first_seen_at=NOW-timedelta(minutes=5), last_seen_at=NOW-timedelta(minutes=5))
    revision = old_revision(article)
    old_key = _digest([canonical, revision])
    assert old_key != article_version(article, "eastmoney")[0]
    conn.execute("INSERT INTO quality_articles VALUES(?,?,?,?,?,?,?)", (
        canonical, revision, _iso(article.first_seen_at), _iso(article.last_seen_at),
        article.published_at.isoformat(), "trusted", json.dumps([target.key]),
    ))
    conn.execute("INSERT INTO quality_article_revisions VALUES(?,?,?,?,?,?)", (
        canonical, revision, article.title, article.summary, _update_evidence(article.summary),
        _iso(article.first_seen_at),
    ))
    conn.execute("INSERT INTO subscription_articles VALUES(?,?,?,?,?,?,?)", (
        old_key, canonical, old_key, "eastmoney", json.dumps(_article_dict(article), ensure_ascii=False),
        _iso(article.published_at), _iso(article.last_seen_at),
    ))
    conn.execute("INSERT INTO member_baselines VALUES(?,?,?,?)", (
        member.id, "eastmoney", target.key, _iso(NOW-timedelta(minutes=5)),
    ))
    conn.execute("INSERT INTO app_meta VALUES(?,?)", ("quality_history_v1:"+_digest(CORP), "1"))
    conn.execute("INSERT INTO seen_articles VALUES(?,?)", ("synthetic-preserved-seen", _iso(NOW)))
    # These tables did not exist in the old installation. Dropping only new
    # empty tables leaves the genuine v4 decisions, report hashes and marker.
    for table in (
        "notification_acceptances", "business_event_fact_evidence",
        "business_event_evidence", "business_event_current", "business_event_versions",
        "business_event_article_bindings", "business_event_facts", "business_events",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='business_events'").fetchone() is None
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='notification_acceptances'").fetchone() is None
    # Upgrade must be safe for an existing version-4 installation, regardless
    # of whether schema setup is invoked once or repeatedly during deployment.
    conn.execute("PRAGMA user_version=4")
    conn.execute("UPDATE app_meta SET value='4' WHERE key='schema_version'")
    yield {
        "path": path, "conn": conn, "member": member, "target": target,
        "article": article, "old_key": old_key,
        "config": tuple(conn.execute("SELECT * FROM member_configs WHERE member_id=?", (member.id,)).fetchone()),
        "global": tuple(conn.execute("SELECT * FROM config_state").fetchone()),
    }
    conn.close()


def decision(state, outcome):
    state["conn"].execute("INSERT INTO member_evaluations VALUES(?,?,?,?,?)", (
        state["member"].id, state["old_key"], state["target"].key,
        _iso(NOW-timedelta(minutes=5)), outcome,
    ))


def scan(state, article):
    analyzer = FakeAnalyzer()
    service = SubscriptionMonitorService(
        state["path"], Fetcher(article), analyzer, corp_id=CORP,
        environ={"MONITOR_ENABLED": "false"},
    )
    return service.run_once(now=NOW, force=True), analyzer


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_existing_v4_marker_preserves_old_decision_without_replaying(legacy_state, outcome):
    state = legacy_state
    decision(state, outcome)
    init_db(state["conn"])
    init_db(state["conn"])
    result, analyzer = scan(state, state["article"])
    assert result.status == "succeeded", result.errors
    assert result.analyzed == result.queued == 0
    assert analyzer.calls == []
    assert state["conn"].execute("SELECT count(*) FROM notification_candidates").fetchone()[0] == 0
    assert state["conn"].execute("SELECT count(*) FROM notification_acceptances").fetchone()[0] == 0
    new_key = article_version(state["article"], "eastmoney")[0]
    migrated = state["conn"].execute(
        "SELECT outcome FROM member_evaluations WHERE member_id=? AND article_key=? AND target_key=?",
        (state["member"].id, new_key, state["target"].key),
    ).fetchone()
    assert migrated is not None and migrated["outcome"] == outcome
    assert state["conn"].execute(
        "SELECT article_key FROM quality_article_key_map WHERE old_article_key=?", (state["old_key"],),
    ).fetchone()[0] == new_key
    assert tuple(state["conn"].execute("SELECT * FROM member_configs WHERE member_id=?", (state["member"].id,)).fetchone()) == state["config"]
    assert tuple(state["conn"].execute("SELECT * FROM config_state").fetchone()) == state["global"]
    assert state["conn"].execute("SELECT article_key FROM seen_articles").fetchone()[0] == "synthetic-preserved-seen"
    assert state["conn"].execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # The independent migration marker must make a restart idempotent too.
    before = list(map(tuple, state["conn"].execute("SELECT * FROM member_evaluations ORDER BY article_key")))
    again, again_analyzer = scan(state, state["article"])
    assert again.status == "succeeded" and again.analyzed == again.queued == 0
    assert again_analyzer.calls == []
    assert list(map(tuple, state["conn"].execute("SELECT * FROM member_evaluations ORDER BY article_key"))) == before


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_upgrade_does_not_move_old_decision_onto_genuinely_added_metric(legacy_state, outcome):
    state = legacy_state
    decision(state, outcome)
    init_db(state["conn"])
    added = replace(state["article"], summary="营业收入100亿元。毛利率为30%。",
                    published_at=NOW, source_published_at=NOW)
    result, analyzer = scan(state, added)
    assert result.status == "succeeded", result.errors
    assert result.analysis_tasks == result.analyzed == result.queued == 1
    assert len(analyzer.calls) == 1
    old_mapped_key = article_version(state["article"], "eastmoney")[0]
    added_key = article_version(added, "eastmoney")[0]
    assert added_key != old_mapped_key
    rows = {row["article_key"]: row["outcome"] for row in state["conn"].execute(
        "SELECT article_key,outcome FROM member_evaluations WHERE member_id=?", (state["member"].id,),
    )}
    assert rows[old_mapped_key] == outcome
    assert rows[added_key] == "evaluated"
    payload = json.loads(state["conn"].execute("SELECT payload_json FROM notification_candidates").fetchone()[0])
    assert any(fact["metric"] == "gross_margin" and fact["value"] == "30"
               for link in payload["event_links"] for fact in link["event_facts"])
    assert state["conn"].execute("SELECT count(*) FROM notification_acceptances").fetchone()[0] == 0


def test_verified_time_can_replace_unknown_serialized_article_without_content_change(tmp_path):
    path = tmp_path / "verified-time.sqlite"
    conn = connect(path)
    init_db(conn)
    try:
        member = MemberRegistry(conn, CORP).add("synthetic-time-member")
        store = MemberConfigStore(conn, member.id, clock=lambda: NOW-timedelta(hours=1))
        store.apply_operation(
            {"op": "add_stock", "symbol": "123456.SH", "name": "合成公司", "focus": "营收"},
            actor=member.user_id,
        )
        target = NewsTarget.from_value(store.snapshot().targets[0])
        article = NewsArticle(
            key="synthetic-time", raw_id="synthetic-time",
            title="合成公司公告2026年前三季度业绩", summary="营业收入100亿元。",
            url="https://example.invalid/verified-time", published_at=NOW,
            target_keys=(target.key,),
        )
        state = {"path": path}
        first, first_analyzer = scan(state, replace(article, time_status="unknown"))
        assert first.status == "succeeded" and first.analysis_tasks == first.queued == 0
        assert first.deferred >= 1 and first_analyzer.calls == []
        assert conn.execute("SELECT count(*) FROM member_evaluations").fetchone()[0] == 0
        verified, analyzer = scan(state, article)
        assert verified.status == "succeeded", verified.errors
        assert verified.analysis_tasks == verified.queued == 1
        assert len(analyzer.calls) == 1
        stored = json.loads(conn.execute("SELECT article_json FROM subscription_articles ORDER BY fetched_at DESC LIMIT 1").fetchone()[0])
        assert stored["time_status"] == "trusted"
        assert datetime.fromisoformat(stored["source_published_at"]) == NOW
    finally:
        conn.close()
