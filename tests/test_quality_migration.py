"""Offline schema-3 history migration, scoped replay protection and rollback.

The base schema is unchanged. Suppressing only additive schema-4 scripts builds
a database with no new tables; serialization and hashing reproduce schema 3.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

import pytest

from stock_watch import analysis_jobs, model_usage, notifications, quality
from stock_watch.analyzer import FakeAnalyzer
from stock_watch.db import connect, init_db
from stock_watch.monitor import write_transaction
from stock_watch.news import NewsArticle, NewsTarget
from stock_watch.quality import canonical_identity, event_identity, revision_key
from stock_watch.subscription_monitor import SubscriptionMonitorService, article_version, _read_article
from stock_watch.subscription_scan import _migrate, prepare_history
from stock_watch.subscriptions import MemberConfigStore, MemberRegistry


NOW = datetime(2026, 10, 23, 2, tzinfo=timezone.utc)
CORP = "synthetic-migration-corp"


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def old_json(article):
    # Schema 3 has no source-time status or first/last seen fields.
    return {"key": article.key, "title": article.title, "summary": article.summary,
            "url": article.url, "published_at": article.published_at.isoformat(),
            "source": article.source, "target_keys": list(article.target_keys),
            "raw_id": article.raw_id, "legacy_keys": list(article.legacy_keys)}


def old_version(article):
    identity = digest(["eastmoney", article.raw_id or article.key])
    content = digest([article.title, article.summary, article.url, article.source, stamp(article.published_at)])
    return digest([identity, content]), identity, content


class Fetcher:
    def __init__(self, articles=()):
        self.articles = tuple(articles)

    def fetch_target(self, target, **kwargs):
        return [replace(article, target_keys=(target.key,)) for article in self.articles]


def service(path, articles=(), analyzer=None, corp=CORP):
    return SubscriptionMonitorService(path, Fetcher(articles), analyzer or FakeAnalyzer(),
        corp_id=corp, environ={"MONITOR_ENABLED": "false"})


@dataclass
class LegacyState:
    path: object
    conn: sqlite3.Connection
    alice: object
    bob: object
    target: NewsTarget
    articles: tuple[NewsArticle, ...]
    old_keys: tuple[str, ...]
    outbox_payload: str
    config_rows: tuple


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    path = tmp_path / "schema3.db"
    conn = connect(path)
    with monkeypatch.context() as old_schema:
        old_schema.setattr(quality, "QUALITY_SCHEMA_SQL", "")
        for module in (analysis_jobs, model_usage, notifications):
            old_schema.setattr(module, "SCHEMA_SQL", "")
        init_db(conn)
    conn.execute("PRAGMA user_version=3")
    conn.execute("UPDATE app_meta SET value='3' WHERE key='schema_version'")
    registry = MemberRegistry(conn, CORP)
    alice, bob = registry.add("synthetic-alice"), registry.add("synthetic-bob")
    for member in (alice, bob):
        MemberConfigStore(conn, member.id, clock=lambda: NOW-timedelta(hours=2)).apply_operation(
            {"op": "add_stock", "symbol": "123456.SH", "name": "示例公司", "focus": "订单"}, actor=member.user_id)
    snapshot = MemberConfigStore(conn, alice.id).snapshot()
    target = NewsTarget.from_value(snapshot.targets[0])
    raw_id = "synthetic-source-id"
    original = NewsArticle(hashlib.sha256(raw_id.encode()).hexdigest(), "示例公司公告新增10亿元订单",
        "https://migration.invalid/news/123", NOW-timedelta(minutes=20),
        summary="公司新增订单，与客户签订合同。", source="合成媒体甲", raw_id=raw_id,
        target_keys=(target.key,))
    variant = replace(original, title="示例 公司公告新增１０亿元订单",
        summary="另一查询摘录展示订单的不同段落。", source="合成媒体乙",
        url=original.url+"?utm_source=synthetic#highlight")
    articles, keys = (original, variant), []
    for index, article in enumerate(articles):
        key, identity, content = old_version(article)
        keys.append(key)
        conn.execute("INSERT INTO subscription_articles VALUES(?,?,?,?,?,?,?)", (
            key, identity, content, "eastmoney", json.dumps(old_json(article), ensure_ascii=False),
            stamp(article.published_at), stamp(NOW-timedelta(minutes=15-index))))
        conn.execute("INSERT INTO member_evaluations VALUES(?,?,?,?,?)",
            (alice.id, key, target.key, stamp(NOW-timedelta(minutes=10)), "evaluated"))
    for member in (alice, bob):
        conn.execute("INSERT INTO member_baselines VALUES(?,?,?,?)",
            (member.id, "eastmoney", target.key, stamp(NOW-timedelta(hours=2))))
    analysis = {"level": "重要", "impact": "利好", "reason": "新增订单形成经营收入。", "followup": "核实履约进度。",
                "matched_targets": [target.key], "should_notify": True}
    payload = {"event": "subscription.news", "member_id": alice.id, "member_generation": alice.generation,
        "corp_id": CORP, "touser": alice.user_id, "config_version": snapshot.config_version,
        "matched_target_ids": [target.key], "subscription_effective_at": {target.key: snapshot.targets[0]["effective_at"]},
        "analysis_focus": {target.key: list(target.focus)}, "article": old_json(original),
        "analysis": analysis, "target_names": [target.name],
        "target_analyses": [{"target_id": target.key, "name": target.name, "analysis": analysis}],
        "accepted_chunks": 1, "delivery_chunks": ["合成已接受分段"]}
    raw_payload = json.dumps(payload, ensure_ascii=False)
    conn.execute("""INSERT INTO outbox(event_key,payload_json,status,available_at,created_at,sent_at)
        VALUES(?,?,'sent',?,?,?)""", (f"member:{alice.id}:article:{keys[0]}", raw_payload,
        stamp(NOW-timedelta(minutes=10)), stamp(NOW-timedelta(minutes=10)), stamp(NOW-timedelta(minutes=9))))
    conn.execute("INSERT INTO seen_articles VALUES('synthetic-legacy-seen',?)", (stamp(NOW-timedelta(days=1)),))
    config_rows = tuple(tuple(row) for row in conn.execute("SELECT * FROM member_configs ORDER BY member_id"))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='quality_articles'").fetchone() is None
    assert len(set(keys)) == 2
    assert "first_seen_at" not in old_json(original)
    yield LegacyState(path, conn, alice, bob, target, articles, tuple(keys), raw_payload, config_rows)
    conn.close()


def migrate(state, corp=CORP):
    init_db(state.conn)
    with write_transaction(state.conn):
        _migrate(service(state.path, corp=corp), state.conn, NOW)


def test_upgrade_preserves_configs_raw_history_and_maps_format_variants(legacy):
    state, conn = legacy, legacy.conn
    migrate(state)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert tuple(tuple(row) for row in conn.execute("SELECT * FROM member_configs ORDER BY member_id")) == state.config_rows
    assert conn.execute("SELECT article_key FROM seen_articles").fetchone()[0] == "synthetic-legacy-seen"
    mapped = dict(conn.execute("SELECT * FROM quality_article_key_map"))
    assert set(mapped) == set(state.old_keys) and len(set(mapped.values())) == 1
    canonical = canonical_identity(state.articles[0])
    assert canonical == conn.execute("SELECT canonical_id FROM quality_articles").fetchone()[0]
    new_key = next(iter(mapped.values()))
    assert new_key == article_version(state.articles[0], "eastmoney")[0]
    assert conn.execute("SELECT count(*) FROM subscription_articles").fetchone()[0] == 3
    for old_key, article in zip(state.old_keys, state.articles):
        assert json.loads(conn.execute("SELECT article_json FROM subscription_articles WHERE article_key=?", (old_key,)).fetchone()[0]) == old_json(article)
    assert {row[0] for row in conn.execute("SELECT article_key FROM member_evaluations WHERE member_id=?", (state.alice.id,))} == {*state.old_keys, new_key}
    assert conn.execute("SELECT count(*) FROM member_evaluations WHERE member_id=?", (state.bob.id,)).fetchone()[0] == 0
    assert tuple(conn.execute("SELECT status,payload_json,sent_at FROM outbox").fetchone()) == ("sent", state.outbox_payload, stamp(NOW-timedelta(minutes=9)))
    assert tuple(conn.execute("SELECT * FROM quality_member_articles").fetchone())[:3] == (state.alice.id, canonical, revision_key(state.articles[0]))


def test_sent_article_and_event_reprints_do_not_replay_or_suppress_another_member(legacy):
    state, conn = legacy, legacy.conn
    migrate(state)
    analyzer = FakeAnalyzer()
    first = service(state.path, (state.articles[1],), analyzer).run_once(now=NOW, force=True)
    assert first.status == "succeeded" and first.analyzed == first.queued == 1
    candidates = [json.loads(row[0]) for row in conn.execute("SELECT payload_json FROM notification_candidates ORDER BY id")]
    assert [value["touser"] for value in candidates] == [state.alice.user_id, state.bob.user_id]
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    reprint = replace(state.articles[0], key="synthetic-new-key", raw_id="synthetic-reprint-id",
                      url="https://reprint.invalid/another", source="合成转载媒体")
    second = service(state.path, (reprint,), analyzer).run_once(now=NOW+timedelta(minutes=1), force=True)
    assert second.analyzed == second.queued == 0 and len(analyzer.calls) == 1
    assert conn.execute("SELECT count(*) FROM notification_candidates").fetchone()[0] == 2
    assert conn.execute("SELECT payload_json FROM outbox").fetchone()[0] == state.outbox_payload


def test_marker_is_idempotent_without_growing_history(legacy):
    state, conn = legacy, legacy.conn
    migrate(state)
    tables = ("subscription_articles", "member_evaluations", "quality_article_key_map", "quality_articles",
              "quality_article_observations", "quality_article_revisions", "quality_member_articles",
              "quality_member_events", "notification_candidates", "outbox")
    before = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}
    migrate(state)
    assert before == {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}
    assert conn.execute("SELECT count(*) FROM app_meta WHERE key LIKE 'quality_history_v1:%'").fetchone()[0] == 1
    assert not conn.in_transaction


def test_caller_transaction_rolls_back_marker_and_all_new_evidence(legacy):
    state, conn = legacy, legacy.conn
    init_db(conn)
    conn.execute("""CREATE TRIGGER synthetic_migration_failure BEFORE INSERT ON quality_member_events
        BEGIN SELECT RAISE(ABORT, 'synthetic migration failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic migration failure"):
        with write_transaction(conn):
            _migrate(service(state.path), conn, NOW)
    for table in ("quality_article_key_map", "quality_articles", "quality_article_observations",
                  "quality_article_revisions", "quality_member_articles", "quality_member_events", "notification_candidates"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM app_meta WHERE key LIKE 'quality_history_v1:%'").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM subscription_articles").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM member_evaluations").fetchone()[0] == 2
    assert conn.execute("SELECT payload_json FROM outbox").fetchone()[0] == state.outbox_payload
    conn.execute("DROP TRIGGER synthetic_migration_failure")
    migrate(state)
    assert conn.execute("SELECT count(*) FROM quality_member_events").fetchone()[0] == 1


def test_markers_and_notification_evidence_are_organization_scoped(legacy):
    state, conn = legacy, legacy.conn
    other = MemberRegistry(conn, "synthetic-other-corp").add("synthetic-other-member")
    foreign = dict(json.loads(state.outbox_payload), corp_id="synthetic-other-corp", member_id=other.id,
                   member_generation=other.generation, touser=other.user_id)
    conn.execute("INSERT INTO outbox(event_key,payload_json,status,available_at,created_at,sent_at) VALUES(?,?,'sent',?,?,?)",
        ("synthetic-other-outbox", json.dumps(foreign, ensure_ascii=False), stamp(NOW), stamp(NOW), stamp(NOW)))
    migrate(state)
    assert {row[0] for row in conn.execute("SELECT member_id FROM quality_member_articles")} == {state.alice.id}
    assert {row[0] for row in conn.execute("SELECT corp_id FROM notification_candidates")} == {CORP}
    migrate(state, corp="synthetic-other-corp")
    assert {row[0] for row in conn.execute("SELECT member_id FROM quality_member_articles")} == {state.alice.id, other.id}
    assert {row[0] for row in conn.execute("SELECT corp_id FROM notification_candidates")} == {CORP, "synthetic-other-corp"}
    assert conn.execute("SELECT count(*) FROM app_meta WHERE key LIKE 'quality_history_v1:%'").fetchone()[0] == 2


def test_event_date_uses_beijing_midnight_not_utc_midnight():
    target = NewsTarget("synthetic", "示例公司", "示例公司")
    base = NewsArticle("one", "示例公司公告新增10亿元订单", "https://date.invalid/one",
        datetime(2026, 10, 22, 15, 30, tzinfo=timezone.utc), raw_id="one")
    beijing_next_day = replace(base, key="two", raw_id="two", url="https://date.invalid/two",
        published_at=datetime(2026, 10, 22, 16, 30, tzinfo=timezone.utc))
    utc_next_day = replace(base, key="three", raw_id="three", url="https://date.invalid/three",
        published_at=datetime(2026, 10, 23, 0, 30, tzinfo=timezone.utc))
    assert event_identity(base, target)[0] != event_identity(beijing_next_day, target)[0]
    assert event_identity(beijing_next_day, target) == event_identity(utc_next_day, target)


def test_explicit_history_preparation_runs_outside_schedule_without_a_scan(legacy, monkeypatch):
    state, conn = legacy, legacy.conn
    init_db(conn)
    assert conn.execute("SELECT count(*) FROM quality_article_key_map").fetchone()[0] == 0
    # Ordinary connection initialization performs no business-history migration.
    prepare_history(conn, NOW)
    assert conn.execute("SELECT count(*) FROM quality_article_key_map").fetchone()[0] == 0
    conn.execute("INSERT INTO app_meta VALUES('independent_subscriptions',?)", (CORP,))
    def no_scan(*args, **kwargs):
        pytest.fail("database initialization attempted an analysis/scan")
    monkeypatch.setattr(SubscriptionMonitorService, "run_once", no_scan)
    monkeypatch.setattr(FakeAnalyzer, "analyze", no_scan)
    after_hours = NOW.replace(hour=14)  # 22:00 Beijing, outside every scan window.
    prepare_history(conn, after_hours)
    assert conn.execute("SELECT count(*) FROM quality_article_key_map").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM quality_member_events").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM monitor_runs").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    before = conn.execute("SELECT count(*) FROM quality_article_observations").fetchone()[0]
    prepare_history(conn, after_hours+timedelta(days=2))
    assert conn.execute("SELECT count(*) FROM quality_article_observations").fetchone()[0] == before
    assert not conn.in_transaction


def test_init_db_cli_upgrades_history_before_any_scheduled_worker(legacy, monkeypatch, capsys):
    from stock_watch.cli import main
    from stock_watch.settings import Settings
    state, conn = legacy, legacy.conn
    conn.execute("INSERT INTO app_meta VALUES('independent_subscriptions',?)", (CORP,))
    # The corporation must come from the persisted independent-subscription mode,
    # not a shell account or a deployment process's unrelated runtime identity.
    settings = Settings(data_dir=state.path.parent, database_path=state.path,
                        wecom_corp_id="synthetic-unrelated-runtime", monitor_enabled=False)
    monkeypatch.setattr(Settings, "from_env", lambda: settings)
    def no_scan(*args, **kwargs):
        pytest.fail("explicit init-db attempted a scheduled scan")
    monkeypatch.setattr(SubscriptionMonitorService, "run_once", no_scan)
    assert main(["--database", str(state.path), "init-db"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == 4 and output["monitor_enabled"] is False
    assert conn.execute("SELECT count(*) FROM quality_article_key_map").fetchone()[0] == 2
    assert {row[0] for row in conn.execute("SELECT corp_id FROM notification_candidates")} == {CORP}
    assert conn.execute("SELECT count(*) FROM app_meta WHERE key LIKE 'quality_history_v1:%'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM monitor_runs").fetchone()[0] == 0
    assert tuple(tuple(row) for row in conn.execute("SELECT * FROM member_configs ORDER BY member_id")) == state.config_rows
    assert conn.execute("SELECT payload_json FROM outbox").fetchone()[0] == state.outbox_payload


@pytest.mark.parametrize('correction_first', [False, True])
def test_old_evaluations_and_sent_payload_cannot_claim_an_unsent_correction(legacy, correction_first):
    state, conn = legacy, legacy.conn
    original = state.articles[0]
    corrected = replace(original, summary='公司公告更正订单金额为8亿元。',
                        published_at=NOW-timedelta(minutes=10))
    old_corrected_key, identity, content = old_version(corrected)
    conn.execute('INSERT INTO subscription_articles VALUES(?,?,?,?,?,?,?)', (
        old_corrected_key, identity, content, 'eastmoney', json.dumps(old_json(corrected), ensure_ascii=False),
        stamp(corrected.published_at), stamp(NOW-timedelta(minutes=12))))
    # Upstream queries may return a stale excerpt after the corrected excerpt.
    # Both are preserved in schema-3 storage, but only the old 10 was evaluated
    # and sent. Arrival order must not turn that evidence into an 8 notification.
    sequence = [old_corrected_key, *state.old_keys] if correction_first else [*state.old_keys, old_corrected_key]
    for position, key in enumerate(sequence):
        conn.execute('UPDATE subscription_articles SET fetched_at=? WHERE article_key=?',
                     (stamp(NOW-timedelta(minutes=5)+timedelta(seconds=position)), key))
    migrate(state)
    original_key = article_version(original, 'eastmoney')[0]
    corrected_key = article_version(corrected, 'eastmoney')[0]
    mapping = dict(conn.execute('SELECT * FROM quality_article_key_map'))
    assert {mapping[key] for key in state.old_keys} == {original_key}
    assert mapping[old_corrected_key] == corrected_key
    assert conn.execute('SELECT revision_key FROM quality_articles').fetchone()[0] == revision_key(corrected)
    assert conn.execute('SELECT 1 FROM member_evaluations WHERE member_id=? AND article_key=?',
                        (state.alice.id, corrected_key)).fetchone() is None
    sent_revisions = {row[0] for row in conn.execute('SELECT revision_key FROM quality_member_articles WHERE member_id=?', (state.alice.id,))}
    assert sent_revisions == {revision_key(original)}
    assert conn.execute('SELECT event_version FROM notification_candidates WHERE member_id=?',
                        (state.alice.id,)).fetchone()[0] == revision_key(original)
    historical = json.loads(conn.execute('SELECT article_json FROM subscription_articles WHERE article_key=?', (original_key,)).fetchone()[0])
    assert revision_key(_read_article(json.dumps(historical))) == revision_key(original)
    assert datetime.fromisoformat(historical['source_published_at']) == original.published_at

    # The current pool JOIN selects the correction, whose real disclosure date
    # remains recent. Alice and Bob both deserve the newly disclosed version.
    analyzer = FakeAnalyzer()
    result = service(state.path, analyzer=analyzer).run_once(now=NOW, force=True)
    assert result.status == 'succeeded' and result.analyzed == 1 and result.queued == 2
    alice_versions = {row[0] for row in conn.execute('SELECT event_version FROM notification_candidates WHERE member_id=?', (state.alice.id,))}
    assert revision_key(original) in alice_versions
    alice_new = [json.loads(row[0]) for row in conn.execute(
        "SELECT payload_json FROM notification_candidates WHERE member_id=? AND status='pending'", (state.alice.id,))]
    assert len(alice_new) == 1
    assert revision_key(_read_article(json.dumps(alice_new[0]['article']))) == revision_key(corrected)
    assert notifications.flush_notifications(state.path, NOW+timedelta(minutes=30)) == 2
    assert conn.execute("SELECT count(*) FROM outbox WHERE event_key LIKE 'notification:%'").fetchone()[0] == 2
    assert conn.execute('SELECT payload_json FROM outbox WHERE id=1').fetchone()[0] == state.outbox_payload
