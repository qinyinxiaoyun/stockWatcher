"""SQLite connection and schema management for stock-watch.

The application deliberately keeps schema setup small and repeatable.  Callers
should open a connection with :func:`connect` and invoke :func:`init_db` before
constructing repositories.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal


SCHEMA_VERSION = 3


class SchemaVersionError(RuntimeError):
    """Raised when a database was created by a newer, unsupported schema."""


def connect(
    path: str | Path,
    *,
    busy_timeout_ms: int = 5_000,
    journal_mode: Literal["DELETE", "WAL"] = "DELETE",
) -> sqlite3.Connection:
    """Open an application SQLite connection with production-safe pragmas.

    ``isolation_level=None`` leaves transaction boundaries explicit.  This is
    important for configuration updates, which use ``BEGIN IMMEDIATE`` and must
    never accidentally include unrelated work.
    """

    if busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be non-negative")
    mode = journal_mode.upper()
    if mode not in {"DELETE", "WAL"}:
        raise ValueError("journal_mode must be DELETE or WAL")

    database = str(path)
    if database != ":memory:" and not database.startswith("file:"):
        Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        database,
        timeout=busy_timeout_ms / 1_000,
        isolation_level=None,
        uri=database.startswith("file:"),
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    # DELETE is conservative for the bundled SQLite 3.50.x runtime. WAL stays
    # available as an explicit opt-in after an operator verifies their build.
    conn.execute(f"PRAGMA journal_mode = {mode}")
    conn.execute(
        "PRAGMA synchronous = FULL" if mode == "DELETE" else "PRAGMA synchronous = NORMAL"
    )
    return conn




_EPOCH = "1970-01-01T00:00:00Z"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def init_db(conn: sqlite3.Connection) -> None:
    """Create/migrate all local tables and seed defaults exactly once.

    The singleton ``config_state`` row is the initialization marker.  Keeping
    that row when all targets are deleted prevents a later restart from
    silently restoring the three migration defaults.
    """

    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"database schema {current} is newer than supported schema {SCHEMA_VERSION}"
        )
    from .subscription_monitor import SCHEMA_SQL as subscription_schema_sql

    conn.executescript(
        """
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS config_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            config_version INTEGER NOT NULL CHECK (config_version >= 0),
            push_levels_json TEXT NOT NULL DEFAULT '["重大","重要"]',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stocks (
            id TEXT PRIMARY KEY,
            market TEXT NOT NULL,
            code TEXT NOT NULL,
            symbol TEXT NOT NULL UNIQUE COLLATE NOCASE,
            name TEXT NOT NULL,
            focus TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
            direction TEXT NOT NULL DEFAULT 'both',
            push_level TEXT NOT NULL DEFAULT 'important',
            effective_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(market, code)
        );
        CREATE TABLE IF NOT EXISTS stock_aliases (
            stock_id TEXT NOT NULL REFERENCES stocks(id) ON DELETE CASCADE,
            alias TEXT NOT NULL,
            normalized_alias TEXT NOT NULL UNIQUE,
            PRIMARY KEY (stock_id, normalized_alias)
        );
        CREATE TABLE IF NOT EXISTS stock_topics (
            stock_id TEXT NOT NULL REFERENCES stocks(id) ON DELETE CASCADE,
            topic TEXT NOT NULL,
            normalized_topic TEXT NOT NULL,
            PRIMARY KEY(stock_id, normalized_topic)
        );
        CREATE TABLE IF NOT EXISTS themes (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            normalized_name TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            effective_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS theme_keywords (
            theme_id TEXT NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
            keyword TEXT NOT NULL,
            normalized_keyword TEXT NOT NULL,
            PRIMARY KEY (theme_id, normalized_keyword)
        );
        CREATE TABLE IF NOT EXISTS config_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            source TEXT NOT NULL,
            source_message_id TEXT,
            action TEXT NOT NULL,
            target_id TEXT,
            before_json TEXT,
            after_json TEXT,
            from_version INTEGER NOT NULL,
            to_version INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_config_audit_version
            ON config_audit(to_version, id);
        CREATE TABLE IF NOT EXISTS confirmations (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            config_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_confirmations_lookup
            ON confirmations(user_id, action, expires_at);
        CREATE TABLE IF NOT EXISTS context (
            user_id TEXT PRIMARY KEY,
            context_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            expires_at TEXT
        );
        CREATE TABLE IF NOT EXISTS inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            user_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            processed_at TEXT,
            error TEXT
        );
        CREATE TABLE IF NOT EXISTS async_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_key TEXT NOT NULL UNIQUE,
            task_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            locked_until TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS monitor_runs (
            slot TEXT PRIMARY KEY,
            lease_owner TEXT,
            lease_until TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            started_at TEXT,
            finished_at TEXT,
            error TEXT,
            detail_json TEXT
        );
        CREATE TABLE IF NOT EXISTS monitor_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS monitor_leases (
            name TEXT PRIMARY KEY,
            lease_owner TEXT NOT NULL,
            lease_until TEXT NOT NULL,
            run_slot TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS seen_articles (
            article_key TEXT PRIMARY KEY,
            seen_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_seen_articles_seen_at ON seen_articles(seen_at);
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            locked_until TEXT,
            lease_owner TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_outbox_due
            ON outbox(status, available_at, locked_until);
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            corp_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','subscriber')),
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            generation INTEGER NOT NULL DEFAULT 1 CHECK(generation > 0),
            analysis_private INTEGER NOT NULL DEFAULT 0 CHECK(analysis_private IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(corp_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS member_configs (
            member_id INTEGER PRIMARY KEY REFERENCES members(id),
            config_version INTEGER NOT NULL CHECK(config_version >= 0),
            snapshot_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS member_config_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(id),
            actor TEXT NOT NULL,
            source TEXT NOT NULL,
            source_message_id TEXT,
            action TEXT NOT NULL,
            target_id TEXT,
            before_json TEXT,
            after_json TEXT,
            from_version INTEGER NOT NULL,
            to_version INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_member_config_audit
            ON member_config_audit(member_id,to_version,id);
        CREATE TABLE IF NOT EXISTS member_confirmations (
            token TEXT PRIMARY KEY,
            member_id INTEGER NOT NULL REFERENCES members(id),
            member_generation INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            config_version INTEGER NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_member_confirmations
            ON member_confirmations(member_id,user_id,expires_at);
        CREATE TABLE IF NOT EXISTS member_registry_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES members(id),
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
        + subscription_schema_sql
        + "\nCOMMIT;"
    )

    conn.execute("BEGIN IMMEDIATE")
    try:
        _add_column(conn, "config_state", "push_levels_json TEXT NOT NULL DEFAULT '[\"重大\",\"重要\"]'")
        _add_column(conn, "stocks", "market TEXT NOT NULL DEFAULT ''")
        _add_column(conn, "stocks", "code TEXT NOT NULL DEFAULT ''")
        had_enabled = "enabled" in {row[1] for row in conn.execute("PRAGMA table_info(stocks)")}
        _add_column(conn, "stocks", "enabled INTEGER NOT NULL DEFAULT 1")
        if not had_enabled:
            conn.execute("UPDATE stocks SET enabled=CASE WHEN paused=1 THEN 0 ELSE 1 END")
        _add_column(conn, "stocks", f"effective_at TEXT NOT NULL DEFAULT '{_EPOCH}'")
        _add_column(conn, "config_audit", "source_message_id TEXT")
        _add_column(conn, "monitor_runs", "error TEXT")
        _add_column(conn, "monitor_runs", "detail_json TEXT")
        _add_column(conn, "outbox", "lease_owner TEXT")

        for row in conn.execute("SELECT id,symbol,market,code FROM stocks").fetchall():
            if row["market"] and row["code"]:
                continue
            code, separator, market = str(row["symbol"]).upper().partition(".")
            if separator:
                conn.execute(
                    "UPDATE stocks SET market=?,code=? WHERE id=?", (market, code, row["id"])
                )

        initialized = conn.execute(
            "SELECT 1 FROM config_state WHERE singleton=1"
        ).fetchone()
        if initialized is None:
            conn.executemany(
                """
                INSERT INTO stocks(
                    id,market,code,symbol,name,focus,enabled,paused,direction,push_level,
                    effective_at,created_at,updated_at
                ) VALUES (?,?,?,?,?,'',1,0,'both','important',?,?,?)
                """,
                [
                    ("stk_7a754c32f6c25dc4", "SH", "603986", "603986.SH", "兆易创新", _EPOCH, _EPOCH, _EPOCH),
                    ("stk_263899307d615441", "SH", "688981", "688981.SH", "中芯国际", _EPOCH, _EPOCH, _EPOCH),
                    ("stk_a4982c601744566e", "HK", "9988", "9988.HK", "阿里巴巴-W", _EPOCH, _EPOCH, _EPOCH),
                ],
            )
            conn.execute(
                "INSERT INTO config_state(singleton,config_version,push_levels_json,updated_at) "
                "VALUES (1,1,?,?)",
                ('["重大","重要"]', _EPOCH),
            )
        if conn.execute("SELECT 1 FROM app_meta WHERE key='legacy_aliases_seeded'").fetchone() is None:
            version = conn.execute("SELECT config_version FROM config_state WHERE singleton=1").fetchone()[0]
            if version == 1:
                defaults = {
                    'stk_7a754c32f6c25dc4': ('兆易创新', 'GigaDevice', '603986'),
                    'stk_263899307d615441': ('中芯国际', 'SMIC', '688981'),
                    'stk_a4982c601744566e': ('阿里巴巴', '阿里云', 'Alibaba', 'BABA'),
                }
                for stock_id, aliases in defaults.items():
                    if conn.execute("SELECT 1 FROM stocks WHERE id=?", (stock_id,)).fetchone():
                        conn.executemany("INSERT OR IGNORE INTO stock_aliases(stock_id,alias,normalized_alias) VALUES(?,?,?)",
                            [(stock_id, alias, alias.casefold()) for alias in aliases])
            conn.execute("INSERT INTO app_meta(key,value) VALUES('legacy_aliases_seeded','1')")
        conn.execute(
            "INSERT INTO app_meta(key,value) VALUES ('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


__all__ = ["SCHEMA_VERSION", "SchemaVersionError", "connect", "init_db"]
