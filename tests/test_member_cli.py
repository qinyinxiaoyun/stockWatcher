"""Offline operator entry points and a long-lived worker's mode switch."""
import json

from stock_watch.analyzer import FakeAnalyzer
from stock_watch.cli import FakeFetcher, main, monitor_service
from stock_watch.config import ConfigStore
from stock_watch.db import connect, init_db
from stock_watch.monitor import MonitorService
from stock_watch.settings import Settings
from stock_watch.subscriptions import MemberConfigStore, MemberRegistry


def test_member_cli_activation_preserves_owner_and_isolates_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WECOM_CORP_ID", "ww-offline")
    monkeypatch.setenv("MONITOR_ENABLED", "false")
    database = tmp_path / "subscriptions.db"
    prefix = ["--database", str(database)]
    assert main(prefix + ["init-db"]) == 0
    capsys.readouterr()
    for user in ("alice", "bob"):
        assert main(prefix + ["members", "add", user]) == 0
        assert json.loads(capsys.readouterr().out)["user_id"] == user
    assert main(prefix + ["members", "assign-legacy-owner", "alice"]) == 0
    capsys.readouterr()
    assert main(prefix + ["config", "--member", "alice"]) == 0
    owner = json.loads(capsys.readouterr().out)
    assert len(owner["stocks"]) == 3 and owner["config_version"] == 1
    assert main(prefix + ["config", "--member", "bob"]) == 0
    assert not json.loads(capsys.readouterr().out)["stocks"]
    assert main(prefix + ["command", "添加方向 工业机器人", "--member", "bob"]) == 0
    assert "已保存" in capsys.readouterr().out
    assert main(prefix + ["config"]) == 1
    assert "--member" in capsys.readouterr().err
    assert main(prefix + ["command", "添加方向 不得落入全局"]) == 1
    capsys.readouterr()
    conn = connect(database)
    registry = MemberRegistry(conn, "ww-offline")
    assert not ConfigStore(conn).snapshot().themes
    assert not MemberConfigStore(conn, registry.get("alice").id).snapshot().themes
    assert MemberConfigStore(conn, registry.get("bob").id).snapshot().themes[0].name == "工业机器人"
    conn.close()
    assert main(prefix + ["members", "analysis", "bob", "--private"]) == 0
    assert json.loads(capsys.readouterr().out)["analysis_private"] is True
    assert main(prefix + ["members", "revoke", "bob"]) == 0
    capsys.readouterr()
    assert main(prefix + ["members", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["independent_subscriptions"]
    assert next(m for m in listed["members"] if m["user_id"] == "bob")["active"] is False
    assert main(prefix + ["command", "查看监控", "--member", "bob"]) == 1


def test_worker_factory_rereads_mode_and_legacy_claim_is_fenced(tmp_path):
    database = tmp_path / "subscriptions.db"
    conn = connect(database)
    init_db(conn)
    settings = Settings(data_dir=tmp_path, database_path=database, wecom_corp_id="ww-offline")
    service = monitor_service(settings, fake=True)
    assert service.run_once(force=True).baseline_seen > 0
    registry = MemberRegistry(conn, settings.wecom_corp_id)
    registry.add("alice")
    registry.assign_legacy_owner("alice")
    result = service.run_once(force=True)
    assert service.member_service is not None
    assert result.queued == 0  # Owner inherited the legacy article baseline.
    legacy = MonitorService(database, FakeFetcher(), FakeAnalyzer())
    late = legacy.run_once(force=True)
    assert late.status == "skipped" and not legacy.fetcher.queries
    conn.close()


def test_member_backfill_requires_explicit_owner_and_preview_preserves_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WECOM_CORP_ID", "ww-offline")
    database = tmp_path / "subscriptions.db"
    conn = connect(database)
    init_db(conn)
    registry = MemberRegistry(conn, "ww-offline")
    registry.add("alice")
    registry.assign_legacy_owner("alice")
    before = list(conn.execute("SELECT event_key FROM outbox"))
    prefix = ["--database", str(database)]
    assert main(prefix + ["backfill", "--target", "603986.SH"]) == 1
    capsys.readouterr()
    assert main(prefix + ["backfill", "--target", "603986.SH", "--member", "alice"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] and not result["delivery"]
    assert list(conn.execute("SELECT event_key FROM outbox")) == before
    assert not conn.execute("SELECT 1 FROM subscription_analysis").fetchone()
    conn.close()


def test_member_mode_wrong_organization_never_falls_back_to_global(tmp_path, monkeypatch, capsys):
    database = tmp_path / "subscriptions.db"
    conn = connect(database)
    init_db(conn)
    registry = MemberRegistry(conn, "ww-offline")
    registry.add("alice")
    registry.assign_legacy_owner("alice")
    before = ConfigStore(conn).snapshot().to_dict()
    monkeypatch.setenv("WECOM_CORP_ID", "ww-other")
    assert main(["--database", str(database), "command", "添加方向 不可写入", "--user", "alice"]) == 1
    assert ConfigStore(conn).snapshot().to_dict() == before
    capsys.readouterr()
    conn.close()


def test_monitor_schema_check_does_not_commit_callers_transaction(tmp_path):
    from stock_watch.monitor import ensure_monitor_schema

    conn = connect(tmp_path / "subscriptions.db")
    init_db(conn)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO monitor_state VALUES('uncommitted','value','2026-10-22')")
    ensure_monitor_schema(conn)
    assert conn.in_transaction
    conn.rollback()
    assert not conn.execute("SELECT 1 FROM monitor_state WHERE key='uncommitted'").fetchone()
    conn.close()
