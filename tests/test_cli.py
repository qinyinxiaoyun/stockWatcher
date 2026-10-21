import json
import sqlite3
from pathlib import Path

from stock_watch.cli import main


def test_dry_run_does_not_create_formal_database(tmp_path, capsys):
    formal = tmp_path / "formal.db"
    assert main(["--database", str(formal), "monitor", "--dry-run", "--fake"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] and not result["delivery"] and not result["external_model"]
    assert not formal.exists()


def test_cli_commands_are_executable_and_preserve_monitor_disabled(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MONITOR_ENABLED", "false")
    formal = tmp_path / "formal.db"
    prefix = ["--database", str(formal)]
    assert main(prefix + ["init-db"]) == 0
    assert json.loads(capsys.readouterr().out)["monitor_enabled"] is False
    assert main(prefix + ["command", "添加方向 半导体设备"]) == 0
    assert "已保存" in capsys.readouterr().out
    assert main(prefix + ["config"]) == 0
    assert any(row["name"] == "半导体设备" for row in json.loads(capsys.readouterr().out)["themes"])
    assert main(prefix + ["test-push", "--to", "offline-user"]) == 0
    assert json.loads(capsys.readouterr().out)["code"] == 0
    backup = tmp_path / "backup.db"
    assert main(prefix + ["backup", str(backup)]) == 0
    assert backup.exists()
    assert main(prefix + ["status"]) == 0


def test_demo_runs_without_formal_configuration(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "must-not-exist.db"))
    assert main(["demo"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["offline"]
    assert any("半导体设备" in query for query in result["queries"])
    assert result["next_scan"]["analyzed"] > 0
    assert result["deliveries"]
    assert not (tmp_path / "must-not-exist.db").exists()


def test_backfill_preview_is_bounded_and_real_requires_explicit_ack(tmp_path, capsys):
    formal = tmp_path / "formal.db"
    prefix = ["--database", str(formal)]
    assert main(prefix + ["backfill", "--target", "603986.SH"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["notification_limit"] == 10 and result["dry_run"]
    assert not formal.exists()
    assert main(prefix + ["backfill", "--target", "603986.SH", "--real"]) == 1
    assert not formal.exists()


def test_fake_monitor_and_worker_preserve_formal_tasks_and_configuration(tmp_path, monkeypatch, capsys):
    from stock_watch.callback_service import CallbackService
    from stock_watch.db import connect, init_db
    from stock_watch.intent import DeepSeekIntentClient
    from stock_watch.settings import Settings
    formal = tmp_path / "formal.db"
    settings = Settings(data_dir=tmp_path, database_path=formal, wecom_admin_userids=("admin",))
    conn = connect(formal)
    init_db(conn)
    conn.close()
    CallbackService(settings).handle({"message_id": "pending-intent", "from_user": "admin",
        "msg_type": "text", "content": "以后多看看兆易创新的订单"})
    monkeypatch.setenv("WECOM_ADMIN_USERIDS", "admin")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "never-use-this-key")
    def forbidden(*_args):
        raise AssertionError("fake worker called real model")
    monkeypatch.setattr(DeepSeekIntentClient, "_request", forbidden)
    conn = connect(formal)
    conn.execute("INSERT INTO outbox(event_key,payload_json,available_at,created_at) VALUES('receipt',?, '2000-01-01','2000-01-01')",
                 (json.dumps({"text": "pending real receipt", "touser": "admin"}),))
    prefix = ["--database", str(formal)]
    assert main(prefix + ["monitor"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"]
    assert main(prefix + ["worker", "--fake", "--once"]) == 0
    assert conn.execute("SELECT status FROM async_tasks").fetchone()[0] == "pending"
    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == "pending"
    assert conn.execute("SELECT count(*) FROM seen_articles").fetchone()[0] == 0
    assert conn.execute("SELECT config_version FROM config_state").fetchone()[0] == 1
    conn.close()
