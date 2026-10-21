"""旧 COS JSON 导入及 SQLite online-backup 运维操作。"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import connect, init_db


def import_seen(database_path, source_path, *, now=None):
    now = now or datetime.now(timezone.utc)
    path = Path(source_path)
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("seen JSON too large")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or set(value) != {"seen"} or not isinstance(value["seen"], dict):
        raise ValueError("expected {seen: {legacy_id: unix_timestamp}}")
    rows = []
    for key, timestamp in value["seen"].items():
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{24}", key):
            raise ValueError("invalid legacy news id")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            raise ValueError("invalid seen timestamp")
        instant = datetime.fromtimestamp(timestamp, timezone.utc)
        if instant > now + timedelta(hours=1):
            raise ValueError("seen timestamp is in the future")
        if instant >= now - timedelta(days=15):
            rows.append((key, instant.isoformat(timespec="seconds")))
    conn = connect(database_path)
    try:
        init_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO seen_articles(article_key,seen_at) VALUES(?,?)", rows)
        inserted = conn.total_changes - before
        # The original three targets retain their imported baseline status.
        import hashlib
        for row in conn.execute("SELECT symbol FROM stocks WHERE symbol IN ('603986.SH','688981.SH','9988.HK')").fetchall():
            key = "baseline:" + hashlib.sha256(row["symbol"].encode()).hexdigest()
            conn.execute("INSERT OR IGNORE INTO monitor_state(key,value,updated_at) VALUES(?, '1', ?)",
                         (key, now.isoformat(timespec="seconds")))
        conn.commit()
        return {"valid_recent": len(rows), "inserted": inserted, "expired": len(value["seen"]) - len(rows)}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def backup_database(database_path, destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("backup destination already exists")
    if destination == Path(database_path).resolve():
        raise ValueError("backup must have a distinct path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Reserve exclusively so an existing file cannot be overwritten.
    with destination.open("xb"):
        pass
    source = connect(database_path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("backup integrity check failed")
    finally:
        target.close()
        source.close()
    return {"backup": str(destination), "integrity": "ok"}
