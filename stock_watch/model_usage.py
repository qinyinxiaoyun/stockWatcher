"""Content-free model accounting and short-transaction token reservations.

Actual provider usage is nullable. Conservative charges for attempts without
usage are kept separately and never represented as measured tokens.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import re
import sqlite3
import uuid
from zoneinfo import ZoneInfo
from typing import Any, Mapping

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS model_usage (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 created_at TEXT NOT NULL, category TEXT NOT NULL,
 model TEXT NOT NULL DEFAULT '', logical_tasks INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER,
 prompt_cache_hit_tokens INTEGER, prompt_cache_miss_tokens INTEGER,
 total_tokens INTEGER, system_fingerprint TEXT,
 estimated_tokens INTEGER NOT NULL DEFAULT 0,
 budget_charge INTEGER NOT NULL DEFAULT 0,
 reservation_id TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS model_usage_time ON model_usage(category, created_at);
CREATE TABLE IF NOT EXISTS model_budget_reservations (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, category TEXT NOT NULL,
 estimated_tokens INTEGER NOT NULL, suspected_major INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'reserved'
);
CREATE INDEX IF NOT EXISTS model_reservation_time
 ON model_budget_reservations(category, created_at, state);
"""


def _time(now=None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace('Z', '+00:00'))
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _stamp(now=None) -> str:
    return _time(now).isoformat(timespec='seconds')


@contextmanager
def _connection(database):
    owned = not isinstance(database, sqlite3.Connection)
    if owned:
        from .db import connect
        conn = connect(database)
    else:
        conn = database
    try:
        yield conn
    finally:
        if owned:
            conn.close()


def _number(value):
    return value if type(value) is int and value >= 0 else None


def usage_metadata(payload: Any) -> dict[str, Any]:
    """Whitelist only provider counters; no prompt or response content."""
    payload = payload if isinstance(payload, Mapping) else {}
    usage = payload.get('usage')
    usage = usage if isinstance(usage, Mapping) else {}
    result = {key: _number(usage.get(key)) for key in (
        'prompt_tokens', 'completion_tokens', 'prompt_cache_hit_tokens',
        'prompt_cache_miss_tokens', 'total_tokens')}
    fingerprint = payload.get('system_fingerprint')
    result['system_fingerprint'] = (
        fingerprint if isinstance(fingerprint, str) and
        re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', fingerprint) else None)
    return result


def estimate_tokens(prompt: str, *, max_output_tokens: int = 1200) -> int:
    """Explicit conservative budget estimate, not a token measurement."""
    return max_output_tokens + len(prompt.encode('utf-8'))


def _insert(conn, record: Mapping[str, Any], *, reservation_id=None, now=None) -> int:
    category = record.get('category', 'news')
    if category not in {'news', 'intent'}:
        raise ValueError('unsupported usage category')
    # Model/fingerprint are metadata but still reject arbitrary text.
    model = record.get('model', '')
    model = model if isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{0,100}', model) else ''
    status = record.get('status', 'unknown')
    if status not in {'success', 'failure', 'validation_failure', 'truncated', 'unknown', 'interrupted'}:
        status = 'failure'
    estimated = _number(record.get('estimated_tokens')) or 0
    total = _number(record.get('total_tokens'))
    charge = total if total is not None else (_number(record.get('budget_charge')) or estimated)
    fingerprint = usage_metadata({'system_fingerprint': record.get('system_fingerprint')})['system_fingerprint']
    logical = _number(record.get('logical_tasks'))
    logical = 1 if logical is None else min(logical, 5)
    cursor = conn.execute('''INSERT OR IGNORE INTO model_usage
       (created_at,category,model,logical_tasks,status,prompt_tokens,completion_tokens,
        prompt_cache_hit_tokens,prompt_cache_miss_tokens,total_tokens,system_fingerprint,
        estimated_tokens,budget_charge,reservation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
       (_stamp(now), category, model, logical, status,
        *(_number(record.get(key)) for key in ('prompt_tokens','completion_tokens',
          'prompt_cache_hit_tokens','prompt_cache_miss_tokens','total_tokens')),
        fingerprint,estimated,charge,reservation_id))
    return cursor.lastrowid


def record_usage(database, record: Mapping[str, Any] | None = None, *, now=None, **fields) -> int:
    with _connection(database) as conn:
        return _insert(conn, {**(record or {}), **fields}, now=now)


def _status(conn, *, category, now, hour_limit, day_limit, major_reserve_fraction):
    current = _time(now)
    hour = current.replace(minute=0, second=0, microsecond=0).isoformat(timespec='seconds')
    day = current.astimezone(ZoneInfo('Asia/Shanghai')).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat(timespec='seconds')
    values = {}
    for label, cutoff, limit in (('hour', hour, hour_limit), ('day', day, day_limit)):
        used = conn.execute('SELECT COALESCE(SUM(budget_charge),0) FROM model_usage WHERE category=? AND created_at>=?', (category,cutoff)).fetchone()[0]
        reserved = conn.execute("SELECT COALESCE(SUM(estimated_tokens),0) FROM model_budget_reservations WHERE category=? AND state='reserved' AND created_at>=?", (category,cutoff)).fetchone()[0]
        values[label+'_used'] = used
        values[label+'_reserved'] = reserved
        values[label+'_remaining'] = max(0, limit-used-reserved)
        values[label+'_ordinary_remaining'] = max(0, int(limit*(1-major_reserve_fraction))-used-reserved)
    values['unknown_attempts'] = conn.execute('SELECT COUNT(*) FROM model_usage WHERE category=? AND total_tokens IS NULL AND created_at>=?', (category,day)).fetchone()[0]
    return values


def budget_status(database, *, category='news', now=None, hour_limit=60000,
                  day_limit=300000, major_reserve_fraction=.2) -> dict:
    with _connection(database) as conn:
        return _status(conn,category=category,now=now,hour_limit=hour_limit,
                       day_limit=day_limit,major_reserve_fraction=major_reserve_fraction)


def reserve_budget(database, *, estimated_tokens: int, category='news',
                   suspected_major=False, hour_limit=60000, day_limit=300000,
                   major_reserve_fraction=.2, now=None) -> str | None:
    if category not in {'news','intent'} or type(estimated_tokens) is not int or estimated_tokens <= 0:
        raise ValueError('invalid reservation')
    if hour_limit < 0 or day_limit < 0 or not 0 <= major_reserve_fraction < 1:
        raise ValueError('invalid budget limits')
    with _connection(database) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            state = _status(conn,category=category,now=now,hour_limit=hour_limit,
                            day_limit=day_limit,major_reserve_fraction=major_reserve_fraction)
            suffix = '_remaining' if suspected_major else '_ordinary_remaining'
            if any(state[period+suffix] < estimated_tokens for period in ('hour','day')):
                conn.rollback()
                return None
            token = uuid.uuid4().hex
            conn.execute('INSERT INTO model_budget_reservations(id,created_at,category,estimated_tokens,suspected_major) VALUES (?,?,?,?,?)',
                         (token,_stamp(now),category,estimated_tokens,int(suspected_major)))
            conn.commit()
            return token
        except BaseException:
            conn.rollback()
            raise


def settle_budget(database, reservation_id: str, record: Mapping[str, Any], *, now=None) -> None:
    with _connection(database) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT category,estimated_tokens,state FROM model_budget_reservations WHERE id=?', (reservation_id,)).fetchone()
            if row is None:
                raise ValueError('unknown reservation')
            if row[2] == 'reserved':
                _insert(conn,{**record,'category':row[0],'estimated_tokens':row[1]},reservation_id=reservation_id,now=now)
                conn.execute("UPDATE model_budget_reservations SET state='settled' WHERE id=?", (reservation_id,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def recover_reservations(database, *, before=None, now=None) -> int:
    """Charge abandoned requests conservatively; never silently free their cost.

    Call at worker startup with an appropriate stale cutoff, never while another
    active worker can still settle those reservations.
    """
    cutoff = _stamp(before if before is not None else _time(now)-timedelta(minutes=10))
    with _connection(database) as conn:
        rows = list(conn.execute("SELECT id FROM model_budget_reservations WHERE state='reserved' AND created_at<?", (cutoff,)))
    for row in rows:
        settle_budget(database,row[0],{'status':'interrupted','total_tokens':None},now=now)
    return len(rows)
