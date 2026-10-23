"""Member-local notification candidates and durable digest windows (no I/O)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .monitor import _iso, open_connection, write_transaction
from .news import parse_published_at
from .scheduler import as_shanghai


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notification_candidates (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 corp_id TEXT NOT NULL, member_id INTEGER NOT NULL,
 member_generation INTEGER NOT NULL,
 article_key TEXT NOT NULL, event_id TEXT NOT NULL, event_version TEXT NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('realtime','morning','urgent','backfill')),
 expires_at TEXT NOT NULL, payload_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','queued','expired','cancelled')),
 created_at TEXT NOT NULL, queued_at TEXT, outbox_id INTEGER,
 UNIQUE(corp_id,member_id,event_id,event_version)
);
CREATE INDEX IF NOT EXISTS ix_notification_candidates_pending
 ON notification_candidates(status,created_at,id);
CREATE TABLE IF NOT EXISTS notification_windows (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 corp_id TEXT NOT NULL, member_id INTEGER NOT NULL,
 kind TEXT NOT NULL, window_key TEXT NOT NULL,
 outbox_id INTEGER NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(corp_id,member_id,kind,window_key)
);
"""


def _merge_payload(old, new):
    """Enrich unsent facts with independent target impacts, never recipients."""
    result = dict(old)
    if any(old.get(key) != new.get(key) for key in
           ('corp_id', 'member_id', 'member_generation', 'touser')):
        return result
    analyses = {item['target_id']: item for item in old.get('target_analyses', ())}
    analyses.update({item['target_id']: item for item in new.get('target_analyses', ())})
    result['target_analyses'] = list(analyses.values())
    result['matched_target_ids'] = sorted(analyses)
    result['target_names'] = [item['name'] for item in analyses.values()]
    if analyses:
        result['analysis'] = max((item['analysis'] for item in analyses.values()),
                                 key=lambda value: {'普通': 0, '重要': 1, '重大': 2}[value['level']])
    result['config_version'] = new.get('config_version', old.get('config_version'))
    for field in ('analysis_focus', 'subscription_effective_at'):
        result[field] = {**old.get(field, {}), **new.get(field, {})}
    return result


def enqueue_candidate(conn, payload, *, article_key, event_id, event_version,
                      mode, expires_at, now):
    """Insert in the caller's evaluation transaction; never extend expiry."""
    if mode not in {'realtime', 'morning', 'urgent', 'backfill'}:
        return 0
    expiry = _iso(parse_published_at(expires_at))
    stamp = _iso(now)
    scope = (payload['corp_id'], payload['member_id'], str(event_id), str(event_version))
    old = conn.execute('SELECT * FROM notification_candidates WHERE corp_id=? AND member_id=? AND event_id=? AND event_version=?', scope).fetchone()
    if old:
        if old['status'] == 'pending':
            merged = _merge_payload(json.loads(old['payload_json']), payload)
            promote = mode == 'urgent' and old['mode'] == 'realtime'
            if promote:
                merged['mode'] = 'urgent'
            # A resumed membership cannot inherit a previous generation's news.
            conn.execute('UPDATE notification_candidates SET payload_json=?,mode=? WHERE id=?',
                         (json.dumps(merged, ensure_ascii=False), 'urgent' if promote else old['mode'], old['id']))
        elif old['status'] == 'queued' and old['outbox_id']:
            row = conn.execute('SELECT status,payload_json FROM outbox WHERE id=?', (old['outbox_id'],)).fetchone()
            if row and row['status'] == 'pending':
                queued = json.loads(row['payload_json'])
                if not queued.get('accepted_chunks', 0):
                    if queued.get('event') == 'subscription.digest':
                        queued['items'] = [_merge_payload(item, payload) if str(item.get('event_id')) == str(event_id) and str(item.get('event_version')) == str(event_version) else item for item in queued['items']]
                        if mode == 'urgent' and old['mode'] == 'realtime':
                            promoted = next(item for item in queued['items'] if str(item.get('event_id')) == str(event_id) and str(item.get('event_version')) == str(event_version))
                            promoted['mode'] = 'urgent'
                            queued['items'] = [item for item in queued['items'] if item is not promoted]
                            queued.pop('delivery_chunks', None)
                            conn.execute('UPDATE outbox SET payload_json=?,status=?,last_error=? WHERE id=? AND status=\'pending\'',
                                (json.dumps(queued, ensure_ascii=False), 'pending' if queued['items'] else 'cancelled',
                                 None if queued['items'] else 'promoted_to_urgent', old['outbox_id']))
                            encoded = json.dumps(promoted, ensure_ascii=False)
                            conn.execute("UPDATE notification_candidates SET mode='urgent',payload_json=? WHERE id=?", (encoded, old['id']))
                            _enqueue_outbox(conn, [dict(old, payload_json=encoded)], now)
                            return 0
                    else:
                        queued = _merge_payload(queued, payload)
                    queued.pop('delivery_chunks', None)
                    conn.execute('UPDATE outbox SET payload_json=? WHERE id=? AND status=\'pending\'', (json.dumps(queued, ensure_ascii=False), old['outbox_id']))
        return 0
    # A later factual correction replaces an unsent version in the same
    # candidate pool, leaving the previous observation and status auditable.
    conn.execute("""UPDATE notification_candidates SET status='cancelled'
                    WHERE corp_id=? AND member_id=? AND event_id=? AND status='pending'""", scope[:3])
    value = dict(payload, event_id=str(event_id), event_version=str(event_version),
                 mode=mode, expires_at=expiry)
    conn.execute('''INSERT INTO notification_candidates(corp_id,member_id,member_generation,
        article_key,event_id,event_version,mode,expires_at,payload_json,status,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        (*scope[:2], payload['member_generation'], article_key, *scope[2:], mode,
         expiry, json.dumps(value, ensure_ascii=False), 'pending' if expiry > stamp else 'expired', stamp))
    return int(expiry > stamp)


def seed_sent(conn, payload, *, article_key, event_id, event_version, outbox_id, now):
    """Preserve migration evidence so membership changes cannot replay facts."""
    value = dict(payload, event_id=str(event_id), event_version=str(event_version))
    return conn.execute('''INSERT OR IGNORE INTO notification_candidates(
        corp_id,member_id,member_generation,article_key,event_id,event_version,mode,
        expires_at,payload_json,status,created_at,queued_at,outbox_id)
        VALUES(?,?,?,?,?,?,'realtime',?,?,'queued',?,?,?)''',
        (payload['corp_id'], payload['member_id'], payload.get('member_generation', 1),
         article_key, str(event_id), str(event_version), payload.get('expires_at') or _iso(now),
         json.dumps(value, ensure_ascii=False), _iso(now), _iso(now), outbox_id)).rowcount


def _enqueue_outbox(conn, rows, now, *, kind=None, window_key=None):
    items = [json.loads(row['payload_json']) for row in rows]
    first = items[0]
    if kind:
        payload = {key: first[key] for key in ('corp_id','member_id','member_generation','touser')}
        payload.update(event='subscription.digest', mode=kind, items=items,
                       quality_policy_version=first.get('quality_policy_version'),
                       config_version=first.get('config_version'))
        identity = (first['corp_id'], first['member_id'], kind, window_key)
    else:
        payload = first
        identity = (first['corp_id'], first['member_id'], first['event_id'], first['event_version'])
    key = 'notification:' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    stamp = _iso(now)
    conn.execute('''INSERT OR IGNORE INTO outbox(event_key,payload_json,available_at,created_at)
                    VALUES(?,?,?,?)''', (key, json.dumps(payload, ensure_ascii=False), stamp, stamp))
    outbox_id = conn.execute('SELECT id FROM outbox WHERE event_key=?', (key,)).fetchone()[0]
    if kind:
        conn.execute('''INSERT INTO notification_windows(corp_id,member_id,kind,window_key,outbox_id,created_at)
                        VALUES(?,?,?,?,?,?)''', (*identity, outbox_id, stamp))
    conn.executemany("UPDATE notification_candidates SET status='queued',queued_at=?,outbox_id=? WHERE id=?", [(stamp, outbox_id, row['id']) for row in rows])


def flush_notifications(db, now=None):
    """Queue due digests fairly within fixed member windows; no remote calls."""
    current = now or datetime.now(timezone.utc)
    local = as_shanghai(current)
    queued = 0
    with open_connection(db) as conn:
        # Compatibility with monitor-only fixtures and schema-3 dry fixtures.
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='notification_candidates'").fetchone():
            return 0
        with write_transaction(conn):
            conn.execute("UPDATE notification_candidates SET status='expired' WHERE status='pending' AND expires_at<=?", (_iso(current),))
            rows = conn.execute("SELECT * FROM notification_candidates WHERE status='pending' ORDER BY created_at,id").fetchall()
            groups = {}
            for row in rows:
                payload = json.loads(row['payload_json'])
                member = conn.execute('SELECT * FROM members WHERE id=? AND corp_id=?', (row['member_id'], row['corp_id'])).fetchone()
                if (not member or not member['active'] or member['generation'] != row['member_generation']
                        or member['user_id'] != payload.get('touser')
                        or payload.get('corp_id') != row['corp_id']
                        or payload.get('member_id') != row['member_id']
                        or payload.get('member_generation') != row['member_generation']):
                    conn.execute("UPDATE notification_candidates SET status='cancelled' WHERE id=?", (row['id'],))
                    continue
                if row['mode'] in {'urgent','backfill'}:
                    _enqueue_outbox(conn, [row], current)
                    queued += 1
                    continue
                if row['mode'] == 'morning':
                    if not (local.hour == 9 and 10 <= local.minute < 15):
                        continue
                    kind, window = 'morning', local.date().isoformat()
                else:
                    kind = 'realtime'
                    boundary = local.replace(minute=(local.minute // 30) * 30, second=0, microsecond=0)
                    if row['created_at'] >= _iso(boundary):
                        continue
                    window = boundary.isoformat()
                # Never combine different membership generations or recipients.
                group = (row['corp_id'],row['member_id'],row['member_generation'],payload['touser'],kind,window)
                groups.setdefault(group, []).append(row)
            for (corp, member, generation, recipient, kind, window), pending in groups.items():
                if conn.execute('SELECT 1 FROM notification_windows WHERE corp_id=? AND member_id=? AND kind=? AND window_key=?', (corp,member,kind,window)).fetchone():
                    continue
                _enqueue_outbox(conn, pending[:3], current, kind=kind, window_key=window)
                queued += 1
    return queued
