"""Offline acceptance tests for versioned configuration and explicit commands."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from stock_watch.commands import CommandError, CommandService, UnknownCommand, parse_command
from stock_watch.config import (
    ConfigError,
    ConfigStore,
    ConfigVersionConflict,
    ConfirmationError,
    DuplicateStock,
)
from stock_watch.db import SCHEMA_VERSION, SchemaVersionError, connect, init_db


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds=60):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def config(tmp_path):
    conn = connect(tmp_path / "config.db")
    init_db(conn)
    clock = Clock()
    store = ConfigStore(conn, clock=clock)
    try:
        yield conn, store, CommandService(store), clock
    finally:
        conn.close()


def stock(snapshot, symbol="603986.SH"):
    return next(item for item in snapshot.stocks if item.symbol == symbol)


def test_schema_initialization_is_idempotent_and_has_all_repository_tables(config):
    conn, store, _, _ = config
    initial = store.snapshot().to_dict()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"stocks", "stock_aliases", "stock_topics", "themes", "theme_keywords",
            "config_state", "config_audit", "confirmations", "inbox", "outbox"} <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert len(initial["stocks"]) == 3
    assert initial["monitor_enabled"] is False
    assert "Alibaba" in stock(store.snapshot(), "9988.HK").aliases
    init_db(conn)
    init_db(conn)
    assert store.snapshot().to_dict() == initial
    assert store.audit() == []
    assert conn.in_transaction is False


def test_v1_migration_preserves_user_data_version_and_paused_state(tmp_path):
    conn = connect(tmp_path / "v1.db")
    try:
        conn.executescript("""
            CREATE TABLE config_state (
                singleton INTEGER PRIMARY KEY, config_version INTEGER NOT NULL,
                updated_at TEXT NOT NULL);
            INSERT INTO config_state VALUES(1,7,'2026-10-20T01:00:00Z');
            CREATE TABLE stocks (
                id TEXT PRIMARY KEY, symbol TEXT NOT NULL UNIQUE COLLATE NOCASE,
                name TEXT NOT NULL, focus TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL DEFAULT 'both',
                push_level TEXT NOT NULL DEFAULT 'important',
                paused INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO stocks VALUES(
                'retained-stock-id','000001.SZ','自定义名称','旧关注点','both',
                'important',1,'2026-10-19T01:00:00Z','2026-10-20T01:00:00Z');
            CREATE TABLE config_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
                source TEXT NOT NULL, action TEXT NOT NULL, target_id TEXT,
                before_json TEXT, after_json TEXT, from_version INTEGER NOT NULL,
                to_version INTEGER NOT NULL, created_at TEXT NOT NULL);
            INSERT INTO config_audit(actor,source,action,from_version,to_version,created_at)
                VALUES('old-user','command','set_focus',6,7,'2026-10-20T01:00:00Z');
            PRAGMA user_version=1;
        """)
        init_db(conn)
        store = ConfigStore(conn)
        snapshot = store.snapshot()
        assert snapshot.schema_version == SCHEMA_VERSION
        assert snapshot.config_version == 7
        assert len(snapshot.stocks) == 1
        item = snapshot.stocks[0]
        assert (item.id, item.market, item.code, item.name, item.focus) == (
            "retained-stock-id", "SZ", "000001", "自定义名称", "旧关注点"
        )
        assert item.paused is True
        assert item.enabled is False
        assert snapshot.targets == ()
        assert store.audit()[0]["source_message_id"] is None
        init_db(conn)
        assert store.snapshot().to_dict() == snapshot.to_dict()
        assert len(store.audit()) == 1
    finally:
        conn.close()


def test_future_schema_is_rejected_without_downgrading(config):
    conn, _, _, _ = config
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(SchemaVersionError):
        init_db(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1


def test_cleared_targets_and_explicitly_removed_aliases_do_not_reappear(config):
    conn, store, service, _ = config
    service.execute("设置别名 9988.HK", user_id="admin")
    init_db(conn)
    assert stock(store.snapshot(), "9988.HK").aliases == ()
    service.execute("添加方向 半导体 芯片,晶圆", user_id="admin")
    confirmation = service.execute("清空监控", user_id="admin")
    service.execute(f"确认 {confirmation.confirmation_token}", user_id="admin")
    cleared = store.snapshot().to_dict()
    init_db(conn)
    init_db(conn)
    assert store.snapshot().to_dict() == cleared
    assert cleared["stocks"] == cleared["themes"] == cleared["targets"] == []
    view = service.execute("查看监控", user_id="admin")
    assert "空名单" in view.text
    assert "全部目标暂停" in view.text


@pytest.mark.parametrize("symbol", [
    "000001", "000001.XX", "123.SH", "ABC.SZ", "1234567.BJ",
    "998.HK", "123456.HK", "123.US", "ABCDEFGHIJK.US",
    "600000.SH;DROP", "600000.SH\nDELETE",
])
def test_invalid_market_or_code_never_changes_configuration(config, symbol):
    _, store, _, _ = config
    before = store.snapshot().to_dict()
    with pytest.raises(ConfigError):
        store.apply_operation({"op": "add_stock", "symbol": symbol, "name": "候选公司"}, actor="admin")
    assert store.snapshot().to_dict() == before
    assert store.audit() == []


def test_explicit_add_requires_a_name_and_market_and_normalizes_symbol(config):
    _, store, service, _ = config
    with pytest.raises(CommandError):
        service.execute("添加股票 工商银行", user_id="admin")
    with pytest.raises(ConfigError):
        store.apply_operation({"op": "add_stock", "symbol": "600000.SH"}, actor="admin")
    result = service.execute('添加股票 aapl.us "Apple Inc" Apple,苹果', user_id="admin")
    item = stock(result.snapshot, "AAPL.US")
    assert (item.market, item.code, item.name) == ("US", "AAPL", "Apple Inc")
    assert set(item.aliases) == {"Apple", "苹果"}
    assert "下一次定时任务开始时读取" in result.text
    assert "生效时间" in result.text
    with pytest.raises(DuplicateStock):
        service.execute("添加股票 AAPL.US 苹果公司", user_id="admin")


def test_same_name_candidates_require_a_unique_reference(config):
    _, store, service, _ = config
    service.execute("添加股票 000001.SZ 同名公司", user_id="admin")
    service.execute("添加股票 0001.HK 同名公司", user_id="admin")
    before = store.snapshot().to_dict()
    with pytest.raises(ConfigError) as error:
        service.execute("删除股票 同名公司", user_id="admin")
    assert "000001.SZ" in str(error.value)
    assert "0001.HK" in str(error.value)
    assert store.snapshot().to_dict() == before
    result = service.execute("暂停监控 0001.HK", user_id="admin")
    assert stock(result.snapshot, "0001.HK").paused
    assert stock(result.snapshot, "000001.SZ").enabled


def test_stock_id_survives_name_alias_changes_and_readdition(config):
    _, store, service, _ = config
    added = service.execute("添加股票 000001.SZ 平安银行 PingAn", user_id="admin")
    first_id = stock(added.snapshot, "000001.SZ").id
    store.apply_operations([
        {"op": "set_name", "target": first_id, "name": "展示名称"},
        {"op": "set_aliases", "target": first_id, "aliases": ["别名"]},
    ], actor="admin")
    assert stock(store.snapshot(), "000001.SZ").id == first_id
    service.execute("删除股票 别名", user_id="admin")
    added_again = service.execute("添加股票 000001.sz 重新添加", user_id="admin")
    assert stock(added_again.snapshot, "000001.SZ").id == first_id


def test_focus_update_preserves_seen_state_and_existing_effective_time(config):
    conn, store, service, clock = config
    stamp = "2026-10-21T01:00:00Z"
    conn.execute("INSERT INTO seen_articles(article_key,seen_at) VALUES(?,?)", ("article-key", stamp))
    conn.execute("INSERT INTO monitor_state(key,value,updated_at) VALUES(?,?,?)", ("baseline", "retained", stamp))
    before = stock(store.snapshot())
    clock.advance()
    result = service.execute("设置关注 603986.SH 存储价格、汽车芯片", user_id="admin")
    after = stock(result.snapshot)
    assert after.id == before.id
    assert after.effective_at == before.effective_at
    assert after.updated_at != before.updated_at
    assert after.focus == "存储价格、汽车芯片"
    target = next(target for target in result.snapshot.targets if target["key"] == after.id)
    assert target["focus"] == ["存储价格", "汽车芯片"]
    assert [tuple(row) for row in conn.execute("SELECT * FROM seen_articles")] == [("article-key", stamp)]
    assert tuple(conn.execute("SELECT * FROM monitor_state").fetchone()) == ("baseline", "retained", stamp)


def test_theme_lifecycle_uses_stable_id_and_explicit_search_keywords(config):
    _, store, service, clock = config
    added = service.execute("添加方向 半导体 芯片,晶圆,芯片", user_id="admin")
    theme = added.snapshot.themes[0]
    assert set(theme.keywords) == {"芯片", "晶圆"}
    target = next(target for target in added.snapshot.targets if target["kind"] == "theme")
    assert target["key"] == theme.id
    assert set(target["queries"]) == {"芯片", "晶圆"}
    clock.advance()
    paused = store.apply_operation({"op": "pause_theme", "target": theme.id}, actor="admin")
    assert not any(target["key"] == theme.id for target in paused.targets)
    clock.advance()
    resumed = store.apply_operation({"op": "resume_theme", "target": theme.id}, actor="admin")
    assert resumed.themes[0].effective_at > theme.effective_at
    service.execute("删除方向 半导体", user_id="admin")
    assert store.snapshot().themes == ()
    readded = service.execute("添加方向 半导体", user_id="admin")
    assert readded.snapshot.themes[0].id == theme.id
    assert readded.snapshot.themes[0].keywords == ("半导体",)


def test_stale_configuration_is_rejected_without_audit_or_partial_write(config):
    _, store, service, _ = config
    version = store.snapshot().config_version
    service.execute("设置关注 603986.SH 存储", user_id="first", expected_version=version)
    saved = store.snapshot().to_dict()
    with pytest.raises(ConfigVersionConflict) as error:
        service.execute("删除股票 603986.SH", user_id="second", expected_version=version)
    assert (error.value.expected, error.value.actual) == (version, version + 1)
    assert store.snapshot().to_dict() == saved
    assert len(store.audit()) == 1


def test_successful_multi_operation_batch_increments_once_and_audits_each_change(config):
    conn, store, _, _ = config
    version = store.snapshot().config_version
    saved = store.apply_operations([
        {"op": "set_focus", "target": "603986.SH", "focus": "存储"},
        {"op": "set_push_levels", "levels": ["普通", "重大"]},
        {"op": "add_theme", "name": "人工智能", "keywords": ["算力"]},
    ], actor="admin", expected_version=version, source="wecom-command", source_message_id="batch-100")
    assert saved.config_version == version + 1
    assert saved.push_levels == ("普通", "重大")
    assert len(saved.themes) == 1
    assert len(store.audit()) == 3
    assert {(row["from_version"], row["to_version"], row["source_message_id"]) for row in store.audit()} == {
        (version, version + 1, "batch-100")
    }
    assert conn.in_transaction is False


def test_invalid_later_operation_rolls_back_all_prior_operations_and_audits(config):
    conn, store, _, _ = config
    before = store.snapshot().to_dict()
    with pytest.raises(ConfigError):
        store.apply_operations([
            {"op": "set_focus", "target": "603986.SH", "focus": "不得保存"},
            {"op": "add_theme", "name": "不得创建"},
            {"op": "set_push_levels", "levels": ["不存在的等级"]},
        ], actor="admin", source_message_id="failed-batch")
    assert store.snapshot().to_dict() == before
    assert store.audit() == []
    assert conn.in_transaction is False


def test_cross_stock_alias_collision_rolls_back_the_whole_edit(config):
    _, store, service, _ = config
    before = store.snapshot().to_dict()
    with pytest.raises(ConfigError, match="alias"):
        service.execute("设置别名 603986.SH GigaDevice,Alibaba", user_id="admin")
    assert store.snapshot().to_dict() == before
    assert store.audit() == []


def test_clear_confirmation_displays_scope_is_user_bound_and_single_use(config):
    conn, store, service, _ = config
    before = store.snapshot().to_dict()
    request = service.execute("清空监控", user_id="owner")
    assert request.status == "confirmation_required"
    assert all(item.symbol in request.text for item in store.snapshot().stocks)
    assert store.snapshot().to_dict() == before
    bound = conn.execute("SELECT * FROM confirmations WHERE token=?", (request.confirmation_token,)).fetchone()
    assert bound["config_version"] == before["config_version"]
    assert set(json.loads(bound["payload_json"])["stock_ids"]) == {
        item["id"] for item in before["stocks"]
    }
    with pytest.raises(ConfirmationError, match="user"):
        service.execute(f"确认 {request.confirmation_token}", user_id="another")
    assert store.snapshot().to_dict() == before
    result = service.execute(f"确认 {request.confirmation_token}", user_id="owner", source_message_id="confirm-101")
    assert result.snapshot.stocks == result.snapshot.themes == result.snapshot.targets == ()
    assert result.snapshot.config_version == before["config_version"] + 1
    with pytest.raises(ConfirmationError, match="already"):
        service.execute(f"确认 {request.confirmation_token}", user_id="owner")
    assert len(store.audit()) == 1
    assert store.audit()[0]["source_message_id"] == "confirm-101"


def test_clear_confirmation_is_bound_to_configuration_version(config):
    _, store, service, _ = config
    request = service.execute("清空监控", user_id="admin")
    service.execute("添加方向 新方向", user_id="admin")
    saved = store.snapshot().to_dict()
    with pytest.raises(ConfigVersionConflict):
        service.execute(f"确认 {request.confirmation_token}", user_id="admin")
    assert store.snapshot().to_dict() == saved


def test_clear_confirmation_expires_at_the_exact_deadline(config):
    conn, store, _, clock = config
    service = CommandService(store, confirmation_ttl_seconds=30)
    before = store.snapshot().to_dict()
    request = service.execute("清空监控", user_id="admin")
    clock.advance(30)
    with pytest.raises(ConfirmationError, match="expired"):
        service.execute(f"确认 {request.confirmation_token}", user_id="admin")
    assert store.snapshot().to_dict() == before
    assert conn.execute("SELECT consumed_at FROM confirmations").fetchone()[0] is None


def test_failed_confirmed_batch_rolls_back_changes_and_token_consumption(config):
    conn, store, _, _ = config
    before = store.snapshot().to_dict()
    confirmation = store.request_confirmation(user_id="admin", action="batch", payload={"operations": [
        {"op": "remove_stock", "target": "603986.SH"},
        {"op": "remove_stock", "target": "missing"},
    ]})
    with pytest.raises(ConfigError):
        store.apply_confirmation(confirmation.token, user_id="admin")
    assert store.snapshot().to_dict() == before
    assert store.audit() == []
    assert conn.execute("SELECT consumed_at FROM confirmations WHERE token=?", (confirmation.token,)).fetchone()[0] is None


def test_command_audit_records_message_actor_versions_and_actual_before_after(config):
    _, store, service, _ = config
    result = service.execute("设置关注 603986.SH 汽车芯片", user_id="admin-user", source_message_id="wecom-msg-42")
    row = store.audit()[0]
    assert (row["actor"], row["source"], row["source_message_id"], row["action"]) == (
        "admin-user", "wecom-command", "wecom-msg-42", "set_focus"
    )
    assert row["target_id"] == stock(result.snapshot).id
    assert row["to_version"] == result.snapshot.config_version
    assert row["from_version"] + 1 == row["to_version"]
    assert json.loads(row["before_json"])["focus"] == ""
    assert json.loads(row["after_json"])["focus"] == "汽车芯片"


def test_unknown_prose_and_malformed_known_commands_do_not_mutate(config):
    _, store, service, _ = config
    before = store.snapshot().to_dict()
    assert parse_command("帮我关注一家公司") is None
    with pytest.raises(UnknownCommand):
        service.execute("帮我关注一家公司", user_id="admin")
    for text in ("", "添加股票", "删除股票", '添加方向 "未闭合', "设置推送等级"):
        with pytest.raises(CommandError):
            service.execute(text, user_id="admin")
    assert service.execute("帮助", user_id="admin").status == "help"
    assert store.snapshot().to_dict() == before
    assert store.audit() == []
