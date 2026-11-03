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
CREATE TABLE IF NOT EXISTS notification_acceptances (
 corp_id TEXT NOT NULL, member_id INTEGER NOT NULL,
 event_id TEXT NOT NULL, event_version TEXT NOT NULL,
 member_generation INTEGER NOT NULL,
 outbox_id INTEGER NOT NULL,
 first_accepted_at TEXT NOT NULL, last_accepted_at TEXT NOT NULL,
 complete INTEGER NOT NULL DEFAULT 0 CHECK(complete IN (0,1)),
 facts_json TEXT NOT NULL DEFAULT '[]', changes_json TEXT NOT NULL DEFAULT '[]',
 PRIMARY KEY(corp_id,member_id,event_id,event_version)
);
CREATE INDEX IF NOT EXISTS ix_notification_acceptances_event
 ON notification_acceptances(corp_id,member_id,event_id,complete);
"""


def _fact_ids(link):
    return {str(fact['fact_id']) for fact in link.get('event_facts', ())
            if isinstance(fact, dict) and fact.get('fact_id')
            and not fact.get('stale') and not fact.get('disputed')
            and fact.get('kind') not in {'conflict', 'opinion', 'rumor', 'additional_evidence'}}


def _event_links(payload):
    links = payload.get('event_links')
    if links is None:
        links = [payload] if payload.get('event_id') and payload.get('event_version') else []
    return [link for link in links if isinstance(link, dict)
            and link.get('event_id') and link.get('event_version')]


def receipt_event_links(conn, payload, outbox_id):
    """Read additive legacy metadata without changing accepted/rendered text.

    A candidate may describe another item in the same digest. Scope plus the
    original report's canonical identity AND revision must both match before
    its business facts can be associated with a provider acceptance.
    """
    if payload.get('event_links'):
        return _event_links(payload)
    from .quality import canonical_identity, revision_key
    from .subscription_monitor import _read_article

    def identity(value):
        try:
            article = _read_article(json.dumps(value['article']))
            return canonical_identity(article), revision_key(article)
        except (KeyError, ValueError, TypeError):
            return None

    signature = identity(payload)
    if signature is None:
        return _event_links(payload)
    found = {}
    for row in conn.execute('SELECT payload_json FROM notification_candidates WHERE outbox_id=?', (outbox_id,)):
        try:
            candidate = json.loads(row['payload_json'])
        except (ValueError, TypeError):
            continue
        if (any(candidate.get(key) != payload.get(key) for key in
                ('corp_id', 'member_id', 'member_generation', 'touser'))
                or identity(candidate) != signature):
            continue
        targets = set(payload.get('matched_target_ids', ()))
        for link in candidate.get('event_links', ()):
            if not isinstance(link, dict) or not link.get('event_id') or not link.get('event_version'):
                continue
            link = dict(link)
            if targets and link.get('target_ids'):
                retained = targets.intersection(link['target_ids'])
                if not retained:
                    continue
                link['target_ids'] = sorted(retained)
            key = str(link['event_id']), str(link['event_version'])
            found[key] = link
    return list(found.values()) if found else _event_links(payload)


def already_notified(conn, payload, event_id, event_version):
    """Any definite acceptance reserves this exact change across generations."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='notification_acceptances'").fetchone():
        return False
    return conn.execute('''SELECT 1 FROM notification_acceptances
        WHERE corp_id=? AND member_id=? AND event_id=? AND event_version=?''',
        (payload.get('corp_id'), payload.get('member_id'), str(event_id), str(event_version))).fetchone() is not None


def should_notify_event(conn, corp_id, member_id, link):
    """Compare with accepted evidence, never the latest research/candidate state.

    A partial direct message reserves only its exact version. A complete message
    also proves its fact IDs, so reprints containing subsets cannot replay them.
    """
    if link.get('notify_eligible') is False:
        return False
    if link.get('event_facts') and not _fact_ids(link):
        return False
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='notification_acceptances'").fetchone():
        return True
    if already_notified(conn, {'corp_id': corp_id, 'member_id': member_id},
                        link['event_id'], link['event_version']):
        return False
    wanted = _fact_ids(link)
    if not wanted:
        return True
    accepted = set()
    for row in conn.execute('''SELECT facts_json FROM notification_acceptances
            WHERE corp_id=? AND member_id=? AND event_id=? AND complete=1''',
            (corp_id, member_id, str(link['event_id']))):
        accepted.update(_fact_ids({'event_facts': json.loads(row['facts_json'])}))
    return not wanted <= accepted


def member_fact_changes(conn, corp_id, member_id, link):
    """Freeze a recipient's actual prior facts, which may lag research state."""
    latest, accepted_ids = {}, set()
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='notification_acceptances'").fetchone():
        for row in conn.execute('''SELECT facts_json FROM notification_acceptances
                WHERE corp_id=? AND member_id=? AND event_id=? AND complete=1
                ORDER BY last_accepted_at DESC,first_accepted_at DESC,event_version''',
                (corp_id, member_id, str(link['event_id']))):
            for fact in json.loads(row['facts_json']):
                if not _fact_ids({'event_facts': [fact]}):
                    continue
                accepted_ids.add(fact['fact_id'])
                latest.setdefault(fact.get('key', fact['fact_id']), fact)
    changes = []
    source_changes = {change.get('fact_id'): change for change in link.get('fact_changes', ())}
    for fact in link.get('event_facts', ()):
        if fact.get('stale') or fact.get('kind') in {'opinion', 'rumor', 'additional_evidence'}:
            continue
        if fact['fact_id'] in accepted_ids:
            continue
        before = latest.get(fact.get('key', fact['fact_id']))
        kind = fact.get('kind', 'new_fact')
        if before is None and kind not in {'conflict', 'rumor', 'opinion', 'correction', 'progress', 'confirmation'}:
            kind = 'new_fact'
        changes.append({'kind': kind, 'fact_id': fact['fact_id'], 'before': before, 'after': fact,
                        'source_before': source_changes.get(fact['fact_id'], {}).get('before')})
    return changes


def record_delivery_acceptance(conn, payload, *, outbox_id, now, complete=False):
    """Called only alongside an owned outbox checkpoint, after remote acceptance.

    Arbitrarily split digests do not reveal which items a partial segment covers;
    do not claim any constituent was fully notified until the whole digest was
    accepted. The outbox's accepted_chunks still freezes its original text.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='notification_acceptances'").fetchone():
        return 0
    if payload.get('event') == 'subscription.digest':
        if not complete:
            return 0
        items = payload.get('items', ())
        if any(any(child.get(key) != payload.get(key) for key in
                   ('corp_id', 'member_id', 'member_generation', 'touser')) for child in items):
            return 0
    elif payload.get('event') == 'subscription.news':
        items = [payload]
    else:
        return 0
    recorded = 0
    stamp = _iso(now)
    for item in items:
        if not item.get('corp_id') or not item.get('member_id'):
            continue
        for link in receipt_event_links(conn, item, outbox_id):
            # Partial text is acceptance evidence, not proof of all factual detail.
            facts = link.get('event_facts', ()) if complete else ()
            changes = link.get('fact_changes', ()) if complete else ()
            conn.execute('''INSERT INTO notification_acceptances(
                corp_id,member_id,event_id,event_version,member_generation,outbox_id,
                first_accepted_at,last_accepted_at,complete,facts_json,changes_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(corp_id,member_id,event_id,event_version) DO UPDATE SET
                  last_accepted_at=excluded.last_accepted_at,
                  complete=MAX(notification_acceptances.complete,excluded.complete),
                  facts_json=CASE WHEN notification_acceptances.complete=0 AND excluded.complete=1
                    THEN excluded.facts_json ELSE notification_acceptances.facts_json END,
                  changes_json=CASE WHEN notification_acceptances.complete=0 AND excluded.complete=1
                    THEN excluded.changes_json ELSE notification_acceptances.changes_json END''',
                (item['corp_id'], item['member_id'], str(link['event_id']), str(link['event_version']),
                 item.get('member_generation', 1), outbox_id, stamp, stamp, int(complete),
                 json.dumps(list(facts), ensure_ascii=False), json.dumps(list(changes), ensure_ascii=False)))
            recorded += 1
    return recorded


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
    if 'event_links' in old or 'event_links' in new:
        links = {(str(link['event_id']), str(link['event_version'])): dict(link)
                 for link in _event_links(old)}
        for link in _event_links(new):
            key = (str(link['event_id']), str(link['event_version']))
            if key in links and ('target_ids' in link or 'target_ids' in links[key]):
                link = dict(link, target_ids=sorted(set(link.get('target_ids', ()))
                                                  | set(links[key].get('target_ids', ()))))
            links[key] = dict(link)
        result['event_links'] = list(links.values())
    return result


def _editable_candidate(conn, row):
    if row['status'] == 'pending':
        return True
    if row['status'] != 'queued' or not row['outbox_id']:
        return False
    queued = conn.execute('SELECT status,payload_json FROM outbox WHERE id=?', (row['outbox_id'],)).fetchone()
    return bool(queued and queued['status'] == 'pending'
                and not json.loads(queued['payload_json']).get('accepted_chunks', 0))


def _cancel_unaccepted(conn, row):
    """Remove only this candidate from an unsent digest; keep audit records."""
    if not _editable_candidate(conn, row):
        return False
    if row['status'] == 'queued':
        queued = json.loads(conn.execute('SELECT payload_json FROM outbox WHERE id=?',
                                        (row['outbox_id'],)).fetchone()[0])
        if queued.get('event') == 'subscription.digest':
            queued['items'] = [item for item in queued.get('items', ())
                               if not (str(item.get('event_id')) == str(row['event_id'])
                                       and str(item.get('event_version')) == str(row['event_version']))]
            queued.pop('delivery_chunks', None)
            status = 'pending' if queued['items'] else 'cancelled'
        else:
            status = 'cancelled'
        conn.execute("UPDATE outbox SET payload_json=?,status=?,last_error=? WHERE id=? AND status='pending'",
                     (json.dumps(queued, ensure_ascii=False), status,
                      'superseded_fact_version' if status == 'cancelled' else None, row['outbox_id']))
    conn.execute("UPDATE notification_candidates SET status='cancelled' WHERE id=?", (row['id'],))
    return True


_RESERVED_UNION = object()


def _pending_cover(conn, payload, links, now):
    """Locate a live/frozen superset, including several reserved observations.

    Frozen reservations prevent replay; they do not assert every digest item was
    accepted. Cancelled/expired/unaccepted work cannot reserve facts forever.
    """
    if not links or any(not _fact_ids(link) for link in links):
        return None
    reserved = {}
    rows = conn.execute('''SELECT * FROM notification_candidates WHERE corp_id=?
        AND member_id=? ORDER BY id''',
        (payload['corp_id'], payload['member_id'])).fetchall()
    for row in rows:
        old = json.loads(row['payload_json'])
        frozen = False
        if row['outbox_id']:
            outbox = conn.execute('SELECT status,payload_json FROM outbox WHERE id=?', (row['outbox_id'],)).fetchone()
            if not outbox:
                continue
            queued = json.loads(outbox['payload_json'])
            if queued.get('event') == 'subscription.digest':
                old = next((item for item in queued.get('items', ())
                            if str(item.get('event_id')) == str(row['event_id'])
                            and str(item.get('event_version')) == str(row['event_version'])), None)
                if old is None:
                    continue
            else:
                old = queued
            frozen = bool(queued.get('accepted_chunks', 0) or outbox['status'] == 'sent')
            if not frozen and (row['status'] != 'queued' or outbox['status'] not in {'pending', 'sending'}):
                continue
        elif row['status'] != 'pending':
            continue
        if any(old.get(key) != payload.get(key) for key in ('corp_id', 'member_id', 'touser')):
            continue
        if not frozen and (row['expires_at'] <= _iso(now)
                           or row['member_generation'] != payload['member_generation']
                           or old.get('member_generation') != payload['member_generation']):
            continue
        old_links = _event_links(old)
        if all(any(str(new['event_id']) == str(prior['event_id'])
                       and _fact_ids(new) <= _fact_ids(prior) for prior in old_links) for new in links):
            return row
        for link in old_links:
            reserved.setdefault(str(link['event_id']), set()).update(_fact_ids(link))
    if all(_fact_ids(link) <= reserved.get(str(link['event_id']), set()) for link in links):
        return _RESERVED_UNION
    return None


def enqueue_candidate(conn, payload, *, article_key, event_id, event_version,
                      mode, expires_at, now):
    """Insert in the caller's evaluation transaction; never extend expiry."""
    if mode not in {'realtime', 'morning', 'urgent', 'backfill'}:
        return 0
    expiry = _iso(parse_published_at(expires_at))
    stamp = _iso(now)
    payload = dict(payload, event_id=str(event_id), event_version=str(event_version))
    links = _event_links(payload)
    if links and all(not should_notify_event(conn, payload['corp_id'], payload['member_id'], link)
                     for link in links):
        return 0
    if 'event_links' in payload:
        payload['event_links'] = [dict(link, member_fact_changes=member_fact_changes(
            conn, payload['corp_id'], payload['member_id'], link)) for link in links]
        links = payload['event_links']
    scope = (payload['corp_id'], payload['member_id'], str(event_id), str(event_version))
    old = conn.execute('SELECT * FROM notification_candidates WHERE corp_id=? AND member_id=? AND event_id=? AND event_version=?', scope).fetchone()
    if old is None:
        cover = _pending_cover(conn, payload, links, now)
        if cover is not None:
            if cover is not _RESERVED_UNION and _editable_candidate(conn, cover):
                # Keep the covering observation and fact IDs. Only add target
                # assessments; a shortened reprint must not remove known facts.
                previous = json.loads(cover['payload_json'])
                enrichment = dict(payload)
                for field in ('event_links', 'event_facts', 'fact_changes'):
                    if field in previous:
                        enrichment[field] = previous[field]
                enqueue_candidate(conn, enrichment, article_key=cover['article_key'],
                                  event_id=cover['event_id'], event_version=cover['event_version'],
                                  mode=mode, expires_at=cover['expires_at'], now=now)
            return 0
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
    # Replace only proven supersets/corrections. Disjoint new facts must not
    # silently erase an earlier unnotified fact from the candidate pool.
    references = set()
    for previous in conn.execute('''SELECT * FROM notification_candidates
            WHERE corp_id=? AND member_id=? AND event_id=? AND status IN ('pending','queued')''', scope[:3]).fetchall():
        old_payload = json.loads(previous['payload_json'])
        old_ids = set().union(*(_fact_ids(link) for link in _event_links(old_payload)))
        new_ids = set().union(*(_fact_ids(link) for link in links))
        superseded = set()
        for link in links:
            for fact in link.get('event_facts', ()):
                prior = fact.get('supersedes') if isinstance(fact, dict) else None
                superseded.update([prior] if isinstance(prior, str) else prior or ())
        replaces = not old_ids or (old_ids <= (new_ids | superseded))
        if replaces and _cancel_unaccepted(conn, previous):
            # A true new metric/correction has its own public time and lifetime.
            # Only legacy/no-new-fact replacements inherit the old deadline.
            if not old_ids or not (new_ids - old_ids):
                expiry = min(expiry, previous['expires_at'])
        elif not _editable_candidate(conn, previous):
            references.add(previous['event_version'])
    value = dict(payload, event_id=str(event_id), event_version=str(event_version),
                 mode=mode, expires_at=expiry)
    references.update(row['event_version'] for row in conn.execute('''SELECT event_version
        FROM notification_acceptances WHERE corp_id=? AND member_id=? AND event_id=?''', scope[:3]))
    if references:
        value['previous_event_versions'] = sorted(references)
    conn.execute('''INSERT INTO notification_candidates(corp_id,member_id,member_generation,
        article_key,event_id,event_version,mode,expires_at,payload_json,status,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
        (*scope[:2], payload['member_generation'], article_key, *scope[2:], mode,
         expiry, json.dumps(value, ensure_ascii=False), 'pending' if expiry > stamp else 'expired', stamp))
    return int(expiry > stamp)


def seed_sent(conn, payload, *, article_key, event_id, event_version, outbox_id, now):
    """Preserve migration evidence so membership changes cannot replay facts."""
    source = conn.execute('SELECT status,payload_json FROM outbox WHERE id=?', (outbox_id,)).fetchone()
    if source is None:
        return 0
    queued = json.loads(source['payload_json'])
    complete = source['status'] == 'sent'
    if not complete and (not queued.get('accepted_chunks', 0)
                         or queued.get('event') == 'subscription.digest'):
        return 0
    if any(queued.get(key) != payload.get(key) for key in ('corp_id', 'member_id')):
        return 0
    value = dict(payload, event_id=str(event_id), event_version=str(event_version))
    record_delivery_acceptance(conn, value, outbox_id=outbox_id, now=now, complete=complete)
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
                links = _event_links(payload)
                if links and all(not should_notify_event(conn, row['corp_id'], row['member_id'], link) for link in links):
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
