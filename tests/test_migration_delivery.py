import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from stock_watch.config import ConfigStore
from stock_watch.db import connect, init_db
from stock_watch.migration import backup_database, import_seen
from stock_watch.settings import Settings


def test_seen_import_validates_whole_file_then_is_idempotent(tmp_path):
    now = datetime.now(timezone.utc)
    source = tmp_path / "seen.json"
    source.write_text(json.dumps({"seen": {"a" * 24: now.timestamp(), "b" * 24: (now-timedelta(days=16)).timestamp()}}))
    db = tmp_path / "data.db"
    assert import_seen(db, source, now=now) == {"valid_recent": 1, "inserted": 1, "expired": 1}
    assert import_seen(db, source, now=now)["inserted"] == 0
    source.write_text(json.dumps({"seen": {"c" * 24: now.timestamp(), "bad": "not a timestamp"}}))
    with pytest.raises(ValueError):
        import_seen(db, source, now=now)
    conn = connect(db)
    assert conn.execute("SELECT count(*) FROM seen_articles").fetchone()[0] == 1
    conn.close()


def test_sqlite_backup_restores_configuration_and_refuses_overwrite(tmp_path):
    db = tmp_path / "source.db"
    conn = connect(db)
    init_db(conn)
    ConfigStore(conn).apply_operations([{"op": "pause", "target": "603986.SH"}], actor="local")
    version = ConfigStore(conn).snapshot().config_version
    backup = tmp_path / "backup.db"
    assert backup_database(db, backup)["integrity"] == "ok"
    restored = connect(backup)
    assert ConfigStore(restored).snapshot().config_version == version
    assert next(stock for stock in ConfigStore(restored).snapshot().stocks if stock.symbol == "603986.SH").paused
    restored.close()
    with pytest.raises(ValueError):
        backup_database(db, backup)
    conn.close()


def test_delivery_resumes_from_accepted_segment_and_preserves_user(tmp_path):
    from stock_watch.delivery import DeliveryDispatcher
    from stock_watch.monitor import ensure_monitor_schema
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "data.db", wecom_to_users=("broadcast",))
    conn = connect(settings.database_path)
    init_db(conn)
    ensure_monitor_schema(conn)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {"text": "中文📈" * 1000, "touser": "admin"}
    conn.execute("INSERT INTO outbox(event_key,payload_json,available_at,created_at) VALUES(?,?,?,?)",
                 ("receipt", json.dumps(payload), stamp, stamp))
    class Sender:
        def __init__(self):
            self.calls = []
            self.fail = True
        def send_chunk(self, text, to_user=None):
            if len(self.calls) == 1 and self.fail:
                self.fail = False
                raise TimeoutError()
            self.calls.append((text, to_user))
    sender = Sender()
    dispatcher = DeliveryDispatcher(settings, sender)
    assert dispatcher.deliver_one()
    row = conn.execute("SELECT * FROM outbox").fetchone()
    assert json.loads(row["payload_json"])["accepted_chunks"] == 1
    conn.execute("UPDATE outbox SET available_at=?", (stamp,))
    assert DeliveryDispatcher(settings, sender).deliver_one()
    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == "sent"
    assert sender.calls[0][0].startswith("[1/")
    assert all(user == "admin" for _, user in sender.calls)
    assert len({text for text, _ in sender.calls}) == len(sender.calls)
    conn.close()


def test_old_delivery_claim_cannot_overwrite_reclaimed_progress(tmp_path):
    from stock_watch.delivery import DeliveryDispatcher
    from stock_watch.monitor import ensure_monitor_schema
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "data.db", wecom_to_users=("alerts",))
    conn = connect(settings.database_path)
    init_db(conn)
    ensure_monitor_schema(conn)
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    conn.execute("INSERT INTO outbox(event_key,payload_json,available_at,created_at) VALUES('race',?,?,?)",
        (json.dumps({"text": "one segment"}), stamp, stamp))
    replacement = DeliveryDispatcher(settings, object())
    class Sender:
        def send_chunk(self, text, to_user=None):
            takeover = replacement.store.claim_due(now=now+timedelta(seconds=121), owner=replacement.owner)
            assert len(takeover) == 1
            conn.execute("UPDATE outbox SET payload_json=? WHERE id=?",
                         (json.dumps({"accepted_chunks": 99}), takeover[0].id))
    DeliveryDispatcher(settings, Sender()).deliver_one(now=now)
    row = conn.execute("SELECT * FROM outbox").fetchone()
    assert row["status"] == "sending" and row["lease_owner"] == replacement.owner
    assert json.loads(row["payload_json"])["accepted_chunks"] == 99
    conn.close()


def test_explicit_backfill_reanalyzes_baseline_without_duplicate_alert_queue(tmp_path):
    from stock_watch.analyzer import FakeAnalyzer
    from stock_watch.monitor import MonitorService
    from stock_watch.news import NewsArticle
    db = tmp_path / "backfill.db"
    conn = connect(db)
    init_db(conn)
    now = datetime.now(timezone.utc)
    class Fetcher:
        def fetch_target(self, target, **kwargs):
            if target.name != "兆易创新":
                return []
            return [NewsArticle(key="baseline-article", title="兆易创新订单", url="https://example.invalid/backfill",
                published_at=now-timedelta(hours=4), target_keys=(target.key,))]
    service = MonitorService(db, Fetcher(), FakeAnalyzer(), environ={"MONITOR_ENABLED": "false"})
    assert service.run_once(now=now, force=True).baseline_seen == 1
    replay = service.run_once(now=now, force=True, backfill_hours=24, backfill_target="603986.SH")
    assert replay.analyzed == 1 and replay.queued == 1
    again = service.run_once(now=now, force=True, backfill_hours=24, backfill_target="603986.SH")
    assert again.analyzed == 1 and again.queued == 0
    assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    conn.close()
