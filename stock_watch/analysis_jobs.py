"""Durable attempt cooldowns, separate from successful analysis and delivery."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS analysis_attempts (
 cache_key TEXT PRIMARY KEY, provider_key TEXT NOT NULL, attempts INTEGER NOT NULL,
 state TEXT NOT NULL, next_attempt_at TEXT NOT NULL, last_error_code TEXT,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_provider_state (
 provider_key TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
 last_failure_at TEXT, blocked_until TEXT
);
CREATE TABLE IF NOT EXISTS quality_article_key_map (
 old_article_key TEXT PRIMARY KEY, article_key TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quality_member_articles (
 member_id INTEGER NOT NULL, canonical_id TEXT NOT NULL, revision_key TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(member_id,canonical_id,revision_key)
);
CREATE TABLE IF NOT EXISTS quality_member_events (
 member_id INTEGER NOT NULL, event_id TEXT NOT NULL, event_version TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(member_id,event_id,event_version)
);
CREATE TABLE IF NOT EXISTS quality_policy_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_slot TEXT NOT NULL, member_id INTEGER,
 article_key TEXT NOT NULL, reason_code TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def safe_error_code(error: Exception) -> str:
    value = getattr(error, "code", type(error).__name__)
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value) else type(error).__name__


def claim(conn, cache_key: str, provider_key: str, now: datetime, *, suspected_major=False) -> bool:
    """Called within the monitor's short transaction, before any network work."""
    stamp = iso(now)
    provider = conn.execute("SELECT blocked_until FROM analysis_provider_state WHERE provider_key=?", (provider_key,)).fetchone()
    if provider and provider[0] and provider[0] > stamp:
        return False
    row = conn.execute("SELECT * FROM analysis_attempts WHERE cache_key=?", (cache_key,)).fetchone()
    if row and row["state"] != "succeeded" and row["next_attempt_at"] > stamp:
        return False
    # Ordinary tasks get one recovery attempt/hour after three attempts;
    # suspected major tasks remain recoverable with the same finite cooldown.
    attempts = row["attempts"] + 1 if row else 1
    conn.execute("""INSERT INTO analysis_attempts VALUES (?,?,?,'running',?,NULL,?)
        ON CONFLICT(cache_key) DO UPDATE SET attempts=excluded.attempts,state='running',
        next_attempt_at=excluded.next_attempt_at,last_error_code=NULL,updated_at=excluded.updated_at""",
        (cache_key, provider_key, attempts, iso(now + timedelta(minutes=20)), stamp))
    return True


def succeed(conn, cache_key: str, provider_key: str, now: datetime) -> None:
    conn.execute("UPDATE analysis_attempts SET state='succeeded',next_attempt_at=?,last_error_code=NULL,updated_at=? WHERE cache_key=?",
                 (iso(now), iso(now), cache_key))
    conn.execute("DELETE FROM analysis_provider_state WHERE provider_key=?", (provider_key,))


def fail(conn, cache_key: str, provider_key: str, error: Exception, now: datetime, *, provider_failure=True) -> None:
    row = conn.execute("SELECT attempts FROM analysis_attempts WHERE cache_key=?", (cache_key,)).fetchone()
    attempts = row[0] if row else 1
    delay = 5 if attempts == 1 else 15 if attempts == 2 else 60
    conn.execute("UPDATE analysis_attempts SET state='failed',next_attempt_at=?,last_error_code=?,updated_at=? WHERE cache_key=?",
                 (iso(now + timedelta(minutes=delay)), safe_error_code(error), iso(now), cache_key))
    if provider_failure and isinstance(error, (TimeoutError, ConnectionError, OSError, RuntimeError)):
        previous = conn.execute("SELECT failures,last_failure_at FROM analysis_provider_state WHERE provider_key=?", (provider_key,)).fetchone()
        recent = previous and previous[1] and previous[1] >= iso(now - timedelta(minutes=5))
        failures = previous[0] + 1 if recent else 1
        blocked = iso(now + timedelta(minutes=10)) if failures >= 3 else None
        conn.execute("""INSERT INTO analysis_provider_state VALUES (?,?,?,?)
            ON CONFLICT(provider_key) DO UPDATE SET failures=excluded.failures,
            last_failure_at=excluded.last_failure_at,blocked_until=excluded.blocked_until""",
            (provider_key, failures, iso(now), blocked))


def release(conn, cache_key: str, now: datetime) -> None:
    """No provider request occurred (e.g. budget); don't spend an attempt."""
    conn.execute("UPDATE analysis_attempts SET attempts=MAX(0,attempts-1),state='failed',next_attempt_at=?,last_error_code='budget_deferred',updated_at=? WHERE cache_key=?",
                 (iso(now), iso(now), cache_key))
