"""Quality-first member scans; remote analysis never holds a write lock."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import time
import uuid
from types import SimpleNamespace

from . import analysis_jobs as jobs
from .analyzer import DeepSeekAnalyzer, validate_analysis
from .model_usage import record_usage, reserve_budget, settle_budget
from .monitor import RunLeaseLost, _iso, open_connection, write_transaction
from .news import EastmoneyNewsClient, NewsTarget, parse_published_at
from .notifications import (enqueue_candidate, flush_notifications, seed_sent,
                            should_notify_event, record_delivery_acceptance)
from .events import register_event
from .quality import (QUALITY_RULE_VERSION, QualityStore, canonicalize_article,
                      canonical_identity, classify_article, event_identity, freshness, revision_key)
from .query_policy import select_queries
from .scheduler import SHANGHAI, as_shanghai, slot_for_time
from .subscription_monitor import _article_dict, _digest, _read_article, article_version


def _fetch_target(service, target, now):
    # A security-associated source already supplies the relationship. Its own
    # explicitly unsupported/failed-source fallback handles keyword queries.
    if getattr(service.fetcher, 'security_associated', False) and target.kind == 'stock':
        return service.fetcher.fetch_target(target, now=now)
    selected = select_queries(target, now) if isinstance(service.fetcher, EastmoneyNewsClient) else target
    if selected == target:
        return service.fetcher.fetch_target(target, now=now)
    try:
        articles = service.fetcher.fetch_target(selected, now=now)
        if articles:
            return articles
    except Exception:
        # The complete configured query set remains the fallback. Only the
        # final failure is reported by the scan, with no upstream text logged.
        pass
    return service.fetcher.fetch_target(target, now=now)


def _combine(first, second):
    """One report can have different snippets and independent related targets."""
    best = first if len(first.summary) >= len(second.summary) else second
    full = {'full_text', 'full_text_truncated'}
    first_full = getattr(first, 'evidence_status', '') in full
    second_full = getattr(second, 'evidence_status', '') in full
    if first_full != second_full:
        best = first if first_full else second
    elif first_full and second_full:
        # A shorter verified refresh may contain a correction. Length alone
        # must not permanently preserve superseded facts.
        previous = getattr(first, 'evidence_checked_at', '')
        incoming = getattr(second, 'evidence_checked_at', '')
        if incoming and (not previous or parse_published_at(incoming) >= parse_published_at(previous)):
            best = second
    return replace(best, published_at=min(first.published_at, second.published_at),
                   target_keys=tuple(sorted(set(first.target_keys) | set(second.target_keys))),
                   legacy_keys=tuple(sorted(set(first.legacy_keys) | set(second.legacy_keys))))


def _save_report(conn, article, source, now):
    key, identity, content = article_version(article, source)
    old = conn.execute("SELECT article_json FROM subscription_articles WHERE article_key=?", (key,)).fetchone()
    if old:
        incoming = article
        article = replace(_combine(_read_article(old[0]), incoming),
                          published_at=incoming.published_at,
                          source_published_at=incoming.source_published_at,
                          time_status=incoming.time_status,
                          first_seen_at=incoming.first_seen_at,
                          last_seen_at=incoming.last_seen_at)
    conn.execute("""INSERT INTO subscription_articles VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(article_key) DO UPDATE SET article_json=excluded.article_json,
        published_at=excluded.published_at,fetched_at=excluded.fetched_at""",
        (key, identity, content, source, json.dumps(_article_dict(article), ensure_ascii=False),
         _iso(article.published_at), _iso(now)))
    return key, article


def _task_key(service, article, target, member, observation=None):
    # Exact factual events can have several report URLs. The quality helper
    # keeps unproven similarities report-scoped; distinct targets/focus/privacy
    # still enter the existing analysis specification independently.
    identity = (observation.event_id, observation.change_id) if observation else event_identity(article, target)
    context = getattr(article, 'research_context', None) or {}
    specification = [service.source_namespace, *identity]
    if context.get('fingerprint'):
        specification.append(context['fingerprint'])
    content = _digest(specification)
    return service._cache_key(content, target, member)


def _notification_history(conn, scan, now):
    """Read member-local fact evidence and whether its message can be enriched."""
    rows = conn.execute("""SELECT c.*,o.status AS outbox_status,o.payload_json AS outbox_payload
        FROM notification_candidates c LEFT JOIN outbox o ON o.id=c.outbox_id
        WHERE c.corp_id=? AND c.member_id=? ORDER BY c.id""",
        (scan.member.corp_id, scan.member.id)).fetchall()
    history = []
    for source in rows:
        row = dict(source)
        value = json.loads(row['payload_json'])
        accepted = 0
        if row['outbox_payload']:
            queued = json.loads(row['outbox_payload'])
            accepted = queued.get('accepted_chunks', 0)
            if queued.get('event') == 'subscription.digest':
                value = next((item for item in queued.get('items', ())
                              if item.get('event_id') == row['event_id']
                              and item.get('event_version') == row['event_version']), value)
            else:
                value = queued
        if value.get('corp_id') != scan.member.corp_id or value.get('member_id') != scan.member.id:
            continue
        row['_notification'] = value
        row['_editable'] = (row['member_generation'] == scan.member.generation
                            and value.get('member_generation') == scan.member.generation
                            and value.get('touser') == scan.member.user_id
                            and row['expires_at'] > _iso(now)
                            and (row['status'] == 'pending' or row['status'] == 'queued'
                                 and row['outbox_status'] == 'pending' and not accepted))
        history.append(row)
    return history


def _matching_candidate(history, article, target, *, observation=None, additional_only=False):
    """Match only a report identity or exact facts for a known affected subject."""
    matches = []
    for row in history:
        value = row['_notification']
        if additional_only and target.key in value.get('matched_target_ids', ()):
            continue
        previous = _read_article(json.dumps(value['article']))
        same_facts = False
        if observation is not None:
            incoming = set(observation.as_link().get('fact_ids', ()))
            for link in value.get('event_links', ()):
                prior_facts = {fact['fact_id'] for fact in link.get('event_facts', link.get('facts', ())) if 'fact_id' in fact}
                if link.get('event_id') == observation.event_id and incoming and incoming <= prior_facts:
                    same_facts = True
        same_article = article.key == previous.key and revision_key(article) == revision_key(previous)
        same_event = any(event_identity(article, NewsTarget(item['target_id'], item['name'], item['name']))
                         == event_identity(previous, NewsTarget(item['target_id'], item['name'], item['name']))
                         for item in value.get('target_analyses', ()))
        if same_facts or same_article or same_event:
            matches.append(row)
    return next((row for row in matches if row['_editable']), matches[0] if matches else None)


def _fact_article(article, observation):
    """A reprint's date cannot renew the lifetime of its known facts."""
    if observation is None or not observation.strong_identity or observation.first_public_at is None:
        return article
    return replace(article, published_at=observation.first_public_at,
                   source_published_at=observation.first_public_at)


def _analysis_article(article, observation, research_context=None):
    if research_context is not None:
        article = replace(article, research_context=research_context)
    if observation is None or not observation.strong_identity or not observation.facts:
        return article
    fields = ('metric', 'period', 'basis', 'unit', 'relation', 'value', 'status')
    def compact(fact):
        return {field: fact.get(field) for field in fields} if fact else None
    changes = [dict(kind=item['kind'], before=compact(item.get('before')), after=compact(item.get('after')))
               for item in observation.changes if item['kind'] != 'duplicate']
    context = dict(event_id=observation.event_id, event_version=observation.change_id,
                   changes=changes,
                   facts=[compact(fact) for fact in observation.facts if not fact.get('stale')])
    return replace(article, fact_context=context)


def _migrate(service, conn, now):
    """Map old evaluations/accepted notifications without replaying history."""
    marker = "quality_history_v1:" + _digest(service.corp_id)
    if conn.execute("SELECT 1 FROM app_meta WHERE key=?", (marker,)).fetchone():
        _migrate_report_versions(service, conn, now)
        _migrate_event_receipts(service, conn, now)
        return
    store = QualityStore(conn)

    def historical_article(article, namespace):
        # Observation maintains the current trusted representative and aliases.
        # Historical evaluations and sent payloads prove only their own facts;
        # they must never inherit a more recent correction from that store.
        original = canonicalize_article(article)
        observed = store.observe(article, now=now, namespace=namespace)
        return replace(original, key=observed.key,
                       first_seen_at=observed.first_seen_at,
                       last_seen_at=observed.last_seen_at,
                       source_published_at=original.source_published_at or (
                           original.published_at if original.time_status != 'unknown' else None))

    for row in conn.execute("SELECT * FROM subscription_articles ORDER BY fetched_at,article_key").fetchall():
        article = _read_article(row["article_json"])
        article = replace(article, legacy_keys=tuple(set((*article.legacy_keys, article.key))))
        article = historical_article(article, row["source"])
        new_key, article = _save_report(conn, article, row["source"], now)
        conn.execute("INSERT OR IGNORE INTO quality_article_key_map VALUES (?,?)", (row["article_key"], new_key))
        conn.execute("""INSERT OR IGNORE INTO member_evaluations
            SELECT member_id,?,target_key,evaluated_at,outcome FROM member_evaluations WHERE article_key=?""",
            (new_key, row["article_key"]))
    for row in conn.execute("SELECT id,payload_json,created_at FROM outbox").fetchall():
        payload = json.loads(row["payload_json"])
        if payload.get("event") != "subscription.news" or payload.get("corp_id") != service.corp_id:
            continue
        article = _read_article(json.dumps(payload["article"]))
        article = historical_article(article, "eastmoney")
        key, canonical, _ = article_version(article, "eastmoney")
        revision = revision_key(article)
        stamp = row["created_at"]
        conn.execute("INSERT OR IGNORE INTO quality_member_articles VALUES (?,?,?,?)",
                     (payload["member_id"], canonical, revision, stamp))
        for item in payload.get("target_analyses", ()):
            target = NewsTarget(item["target_id"], item["name"], item["name"])
            event, version = event_identity(article, target)
            conn.execute("INSERT OR IGNORE INTO quality_member_events VALUES (?,?,?,?)",
                         (payload["member_id"], event, version, stamp))
        seed_sent(conn, payload, article_key=key, event_id="article:" + canonical,
                  event_version=revision, outbox_id=row["id"], now=now)
    conn.execute("INSERT INTO app_meta VALUES (?,?)", (marker, "1"))
    _migrate_report_versions(service, conn, now)
    _migrate_event_receipts(service, conn, now)


def _migrate_report_versions(service, conn, now):
    """Map old revision keys without lending old evaluations new facts."""
    marker = "business_report_revisions_v1:" + _digest(service.corp_id)
    if conn.execute("SELECT 1 FROM app_meta WHERE key=?", (marker,)).fetchone():
        return
    store = QualityStore(conn)
    for row in conn.execute("SELECT * FROM subscription_articles ORDER BY published_at,fetched_at,article_key").fetchall():
        original = canonicalize_article(_read_article(row['article_json']))
        observed = store.observe(original, now, namespace=row['source'])
        historical = replace(original, key=observed.key, first_seen_at=observed.first_seen_at,
                             last_seen_at=observed.last_seen_at,
                             source_published_at=original.source_published_at or (
                                 original.published_at if original.time_status == 'trusted' else None))
        new_key, _ = _save_report(conn, historical, row['source'], now)
        if new_key != row['article_key']:
            conn.execute("""INSERT INTO quality_article_key_map VALUES (?,?)
                ON CONFLICT(old_article_key) DO UPDATE SET article_key=excluded.article_key""",
                (row['article_key'], new_key))
            conn.execute("""INSERT OR IGNORE INTO member_evaluations
                SELECT e.member_id,?,e.target_key,e.evaluated_at,e.outcome
                FROM member_evaluations e JOIN members m ON m.id=e.member_id
                WHERE e.article_key=? AND m.corp_id=?""", (new_key, row['article_key'], service.corp_id))
    conn.execute("INSERT INTO app_meta VALUES (?,?)", (marker, "1"))


def _migrate_event_receipts(service, conn, now):
    """Reconstruct acceptance only from outbox evidence, never evaluation flags.

    This additive migration leaves frozen original payloads and subscriptions
    unchanged. The legacy marker cannot prove the new ledger was populated.
    """
    marker = "business_event_receipts_v1:" + _digest(service.corp_id)
    if conn.execute("SELECT 1 FROM app_meta WHERE key=?", (marker,)).fetchone():
        return
    rows = conn.execute("SELECT id,status,payload_json,sent_at,created_at FROM outbox ORDER BY id").fetchall()
    for row in rows:
        payload = json.loads(row['payload_json'])
        if (payload.get('event') not in {'subscription.news', 'subscription.digest'}
                or payload.get('corp_id') != service.corp_id):
            continue
        value = dict(payload)
        items = value.get('items', ()) if value['event'] == 'subscription.digest' else (value,)
        if value['event'] == 'subscription.digest':
            value['items'] = [dict(item) for item in items]
        for item in (value['items'] if value['event'] == 'subscription.digest' else (value,)):
            if (item.get('corp_id') != service.corp_id or not item.get('article')
                    or item.get('member_id') != value.get('member_id')
                    or item.get('touser') != value.get('touser')):
                continue
            article = canonicalize_article(_read_article(json.dumps(item['article'])))
            links = []
            for target in item.get('target_analyses', ()):
                observed = register_event(conn, article,
                    NewsTarget(target['target_id'], target['name'], target['name']), now)
                link = dict(observed.as_link(), target_ids=[target['target_id']])
                links.append(link)
            if links and (row['status'] in {'pending', 'sending', 'sent'} or payload.get('accepted_chunks', 0)):
                _seed_candidate_links(conn, row, item, article, links, now)
            unrecorded = []
            for link in links:
                accepted = conn.execute("""SELECT complete FROM notification_acceptances
                    WHERE corp_id=? AND member_id=? AND event_id=? AND event_version=?""",
                    (service.corp_id, item['member_id'], link['event_id'], link['event_version'])).fetchone()
                if accepted is None or (row['status'] == 'sent' and not accepted['complete']):
                    unrecorded.append(link)
            if unrecorded:
                item['event_links'] = unrecorded
                item.setdefault('event_id', unrecorded[0]['event_id'])
                item.setdefault('event_version', unrecorded[0]['event_version'])
            else:
                item['event_links'] = []
        if row['status'] == 'sent' or payload.get('accepted_chunks', 0):
            record_delivery_acceptance(conn, value, outbox_id=row['id'],
                                       now=parse_published_at(row['sent_at']) if row['sent_at'] else now,
                                       complete=row['status'] == 'sent')
    conn.execute("INSERT INTO app_meta VALUES (?,?)", (marker, "1"))


def _seed_candidate_links(conn, outbox, item, article, links, now):
    """Attach proven event metadata to old reservations without changing text."""
    for row in conn.execute("""SELECT * FROM notification_candidates
        WHERE outbox_id=? AND corp_id=? AND member_id=?""",
        (outbox['id'], item['corp_id'], item['member_id'])).fetchall():
        value = json.loads(row['payload_json'])
        if any(value.get(key) != item.get(key) for key in ('corp_id', 'member_id', 'member_generation', 'touser')):
            continue
        previous = _read_article(json.dumps(value['article']))
        if canonical_identity(previous) == canonical_identity(article) and revision_key(previous) == revision_key(article):
            value['event_links'] = links
            conn.execute('UPDATE notification_candidates SET payload_json=? WHERE id=?',
                         (json.dumps(value, ensure_ascii=False), row['id']))
            return
    event_id = item.get('event_id') or links[0]['event_id']
    version = item.get('event_version') or links[0]['event_version']
    timing = freshness(article, now)
    expiry = item.get('expires_at') or _iso(timing.expires_at or article.published_at + timedelta(hours=2))
    mode = item.get('mode', 'realtime')
    if mode not in {'realtime', 'morning', 'urgent', 'backfill'}:
        mode = 'realtime'
    value = dict(item, event_id=event_id, event_version=version, event_links=links, mode=mode, expires_at=expiry)
    conn.execute("""INSERT OR IGNORE INTO notification_candidates(corp_id,member_id,member_generation,
        article_key,event_id,event_version,mode,expires_at,payload_json,status,created_at,queued_at,outbox_id)
        VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?,?)""",
        (item['corp_id'], item['member_id'], item.get('member_generation', 1),
         article_version(article, 'eastmoney')[0], event_id, version, mode, expiry,
         json.dumps(value, ensure_ascii=False), outbox['created_at'], outbox['created_at'], outbox['id']))


def prepare_history(conn, now):
    """Explicit init-db migration, including outside scheduled scan hours."""
    mode = conn.execute("SELECT value FROM app_meta WHERE key='independent_subscriptions'").fetchone()
    if mode is None:
        return
    if conn.in_transaction:
        raise RuntimeError('initialize quality history outside a caller transaction')
    with write_transaction(conn):
        _migrate(SimpleNamespace(corp_id=mode[0]), conn, now)


def run_subscription_scan(service, *, now=None, force=False, dry_run=False, slot=None,
                          backfill_hours=None, backfill_target=None, member_id=None):
    started = time.monotonic()
    current = as_shanghai(now or datetime.now(SHANGHAI))
    checkpoint = lambda: current + timedelta(seconds=time.monotonic() - started)
    settings = service.settings()
    scans, errors = service._members()
    cursor_key = "subscription_member_cursor:" + _digest(service.corp_id)
    with open_connection(service.db) as conn:
        cursor = conn.execute("SELECT value FROM monitor_state WHERE key=?", (cursor_key,)).fetchone()
    if cursor:
        after = next((i + 1 for i, scan in enumerate(scans) if str(scan.member.id) == cursor[0]), 0)
        scans = scans[after:] + scans[:after]
    manual = backfill_hours is not None
    if manual:
        if member_id is None or not force or not 1 <= backfill_hours <= 24 or not backfill_target:
            raise ValueError("backfill requires an explicit member, manual target and 1..24 hours")
        scans = [scan for scan in scans if scan.member.id == member_id]
        found = False
        for scan in scans:
            targets = [t for t in scan.targets.values() if backfill_target in {t.key, t.name, *t.queries}]
            if len(targets) > 1:
                raise ValueError("backfill target must uniquely identify a target")
            scan.targets = {t.key: replace(t, effective_at=current - timedelta(hours=backfill_hours)) for t in targets}
            found |= bool(targets)
        if not found:
            raise ValueError("backfill target must identify an enabled target")
    metrics = dict(analysis_tasks=0, analysis_succeeded=0, filtered=0, expired=0,
                   cache_hits=0, deferred=0, notifications_queued=0)
    counts = dict(fetched=0, eligible=0, analyzed=0, queued=0, baseline_seen=0, deduplicated=0)

    def result(**values):
        values.setdefault("errors", tuple(errors))
        values.setdefault("dry_run", dry_run)
        values.setdefault("member_config_versions", tuple((s.member.id, s.settings.config_version) for s in scans))
        return service._result(started, settings, **counts, **metrics, **values)

    if not settings.enabled and not force:
        return result(status="skipped", slot=None, reason="monitor_disabled")
    if not any(scan.targets for scan in scans):
        return result(status="skipped", slot=None, reason="no_targets")
    slot = slot or (f"manual:{current.isoformat()}:{uuid.uuid4().hex[:8]}" if force else slot_for_time(current))
    if slot is None:
        return result(status="skipped", slot=None, reason="outside_schedule")
    if not dry_run and not service._claim(slot, current, settings.run_lease_seconds):
        return result(status="skipped", slot=slot, reason="slot_or_monitor_already_claimed")

    def transaction(action):
        with open_connection(service.db) as conn:
            with write_transaction(conn):
                service._assert_lease(conn, slot, checkpoint())
                value = action(conn)
                until = _iso(checkpoint() + timedelta(seconds=settings.run_lease_seconds))
                conn.execute("UPDATE monitor_leases SET lease_until=? WHERE name='monitor' AND lease_owner=?", (until, service.owner))
                conn.execute("UPDATE monitor_runs SET lease_until=? WHERE slot=? AND lease_owner=?", (until, slot, service.owner))
                return value

    def audit(conn, scan, key, reason):
        conn.execute("INSERT INTO quality_policy_audit(run_slot,member_id,article_key,reason_code,created_at) VALUES (?,?,?,?,?)",
                     (slot, scan.member.id, key, reason, _iso(checkpoint())))

    queries, raw, succeeded = {}, [], set()
    subject_names = {}
    event_observations = {}
    research_contexts, analysis_keys = {}, {}
    def event_target(article, target_id):
        target = queries[target_id]
        text = article.title + '。' + article.summary
        matching = sorted((name for name in subject_names[target_id] if name in text), key=lambda name: (-len(name), name))
        return replace(target, name=matching[0]) if matching else target
    def provider_for(member):
        return _digest([service.corp_id, member.id if member.analysis_private else "public",
                        getattr(getattr(service.analyzer, "transport", None), "endpoint", "offline")])
    last_member = None
    try:
        begin_cycle = getattr(service.fetcher, 'begin_cycle', None)
        if begin_cycle is not None:
            begin_cycle()
        if not dry_run:
            transaction(lambda conn: _migrate(service, conn, checkpoint()))
        for scan in scans:
            for target in scan.targets.values():
                subject_names.setdefault(target.key, set()).add(target.name)
                previous = queries.get(target.key)
                union = tuple(dict.fromkeys((*(previous.queries if previous else ()), *(target.queries or (target.query,)))))
                if previous:
                    representative = min((previous, target), key=lambda value: (value.kind, value.name, value.query))
                    union = tuple(sorted(set((*union, previous.name, target.name))))
                else:
                    representative = target
                queries[target.key] = replace(representative, queries=union, focus=(), effective_at=None)
        for key, target in queries.items():
            if time.monotonic() - started >= settings.run_timeout_seconds:
                errors.append("fetch deadline: TimeoutError")
                break
            try:
                articles = _fetch_target(service, target, current)
                counts["fetched"] += len(articles)
                succeeded.add(key)
                raw.extend(replace(a.with_targets((key,)), legacy_keys=tuple(set((*a.legacy_keys, a.key)))) for a in articles)
                source_status = getattr(service.fetcher, 'last_fetch_status', {}).get(target.key, ())
                for code in ([source_status] if isinstance(source_status, str) else source_status):
                    # Source diagnostics are fixed codes, never response prose.
                    if code in {'fallback_unsupported_security', 'fallback_associated_unavailable',
                                'partial_associated_page_error', 'partial_associated_invalid_record',
                                'partial_associated_page_limit'}:
                        errors.append('fetch: ' + code)
            except Exception as exc:
                errors.append("fetch: " + type(exc).__name__)
        pool = {}
        if dry_run:
            for item in raw:
                article = canonicalize_article(item)
                key, _, _ = article_version(article, service.source_namespace)
                pool[key] = (_combine(pool[key][0], article) if key in pool else article, service.source_namespace)
        else:
            def save(conn):
                store = QualityStore(conn)
                for item in sorted(raw, key=lambda value: value.source_published_at or value.published_at):
                    article = store.observe(item, checkpoint(), namespace=service.source_namespace)
                    _save_report(conn, article, service.source_namespace, checkpoint())
                    store.record_decision(article, classify_article(article), freshness(article, checkpoint()), checkpoint())
                    # Archive observed facts even when their alert window has
                    # closed. Research evidence is independent of notification.
                    for target_id in article.target_keys:
                        if target_id in queries:
                            register_event(conn, article, event_target(article, target_id), checkpoint())
                acknowledge = getattr(service.fetcher, 'acknowledge', None)
                if acknowledge is not None:
                    acknowledge(conn, checkpoint())
            transaction(save)
            with open_connection(service.db) as conn:
                rows = conn.execute("""SELECT a.*,q.revision_key AS current_revision FROM subscription_articles a
                    JOIN quality_articles q ON q.canonical_id=a.identity_key
                    WHERE a.published_at>=? AND a.published_at<=?
                    AND NOT EXISTS (SELECT 1 FROM quality_article_key_map m WHERE m.old_article_key=a.article_key AND m.article_key!=a.article_key)
                    ORDER BY a.published_at DESC,a.article_key""",
                    (_iso(current - timedelta(hours=settings.lookback_hours)), _iso(current + timedelta(minutes=5)))).fetchall()
                pool = {}
                for row in rows:
                    article = _read_article(row["article_json"])
                    if revision_key(article) == row["current_revision"]:
                        pool[row["article_key"]] = (article, row["source"])
            def observe_pool(conn):
                for key, (article, _) in sorted(pool.items(), key=lambda item: item[1][0].published_at):
                    for target_id in article.target_keys:
                        if target_id in queries:
                            event_observations[key, target_id] = register_event(conn, article, event_target(article, target_id), checkpoint())
            transaction(observe_pool)
        candidates = [[] for _ in scans]
        for position, scan in enumerate(scans):
            with open_connection(service.db) as conn:
                seen = {(r[0], r[1]) for r in conn.execute("SELECT article_key,target_key FROM member_evaluations WHERE member_id=?", (scan.member.id,))}
                legacy = {r[0] for r in conn.execute("SELECT article_key FROM member_legacy_seen WHERE member_id=?", (scan.member.id,))}
                baselines = {(r[0], r[1]) for r in conn.execute("SELECT source,target_key FROM member_baselines WHERE member_id=?", (scan.member.id,))}
                notification_history = _notification_history(conn, scan, checkpoint())
                notified = {pair: observed.notify_eligible and not should_notify_event(conn, service.corp_id, scan.member.id, observed.as_link())
                            for pair, observed in event_observations.items()}
            evaluations, consumed, policy_skips = [], set(), []
            for key, (article, source) in pool.items():
                decision = classify_article(article)
                timing = freshness(article, checkpoint())
                targets = []
                for target in scan.targets.values():
                    if target.key not in article.target_keys:
                        continue
                    observation = event_observations.get((key, target.key))
                    fact_article = _fact_article(article, observation)
                    target_timing = freshness(fact_article, checkpoint())
                    if not manual and (key, target.key) in seen:
                        counts["deduplicated"] += 1
                        continue
                    inherited = legacy.intersection((article.key, *article.legacy_keys)) if source == "eastmoney" else set()
                    if not manual and inherited:
                        evaluations.append((key, target.key, "baseline"))
                        consumed.update(inherited)
                        counts["deduplicated"] += 1
                        counts["baseline_seen"] += 1
                        continue
                    if target.effective_at and fact_article.published_at < target.effective_at:
                        continue
                    established = (source, target.key) in baselines
                    new_source = not established and any(t == target.key for _, t in baselines)
                    old = target.effective_at is None or target.effective_at <= datetime(1970, 1, 2, tzinfo=timezone.utc)
                    if not manual and not dry_run and not established and (old or new_source):
                        evaluations.append((key, target.key, "baseline"))
                        counts["baseline_seen"] += 1
                        continue
                    if not manual and notified.get((key, target.key), False) and _matching_candidate(notification_history, article, target, observation=observation, additional_only=True) is None:
                        evaluations.append((key, target.key, "duplicate_event"))
                        counts["deduplicated"] += 1
                        continue
                    if decision.kind == "market_noise":
                        evaluations.append((key, target.key, "filtered:v" + QUALITY_RULE_VERSION))
                        metrics["filtered"] += 1
                        policy_skips.append((key, decision.reason))
                        continue
                    if not manual and target_timing.mode not in {"realtime", "morning", "urgent"}:
                        if target_timing.mode == "expired":
                            evaluations.append((key, target.key, "expired:v" + QUALITY_RULE_VERSION))
                            metrics["expired"] += 1
                        else:
                            metrics["deferred"] += 1
                        policy_skips.append((key, target_timing.reason))
                        continue
                    targets.append(target)
                if targets:
                    candidates[position].append((key, article, targets, decision, timing))
            if not dry_run:
                def save_evaluations(conn):
                    if service._current_member(conn, scan) is None:
                        return
                    conn.executemany("INSERT OR IGNORE INTO member_baselines VALUES (?,?,?,?)",
                        [(scan.member.id, service.source_namespace, t.key, _iso(checkpoint())) for t in scan.targets.values() if t.key in succeeded])
                    conn.executemany("INSERT OR IGNORE INTO member_evaluations VALUES (?,?,?,?,?)",
                        [(scan.member.id, key, target, _iso(checkpoint()), outcome) for key, target, outcome in evaluations])
                    conn.executemany("DELETE FROM member_legacy_seen WHERE member_id=? AND article_key=?", [(scan.member.id, key) for key in consumed])
                    for key, reason in set(policy_skips):
                        audit(conn, scan, key, reason)
                transaction(save_evaluations)
            candidates[position].sort(key=lambda item: (not item[3].urgent_hint, -item[1].published_at.timestamp(), item[0]))
        counts["eligible"] = sum(map(len, candidates))
        ordered = []
        for offset in range(max((len(c) for c in candidates), default=0)):
            ordered.extend((scan, rows[offset]) for scan, rows in zip(scans, candidates) if offset < len(rows))
        needed, cache = {}, {}
        from .research_context import select_context
        with open_connection(service.db) as conn:
            for scan, (key, article, targets, decision, timing) in ordered:
                for target in targets:
                    pair = (key, target.key)
                    if pair not in research_contexts:
                        research_contexts[pair] = select_context(
                            conn, getattr(target, 'symbol', ''), article, checkpoint())
                    analyzed_article = _analysis_article(article, event_observations.get(pair), research_contexts[pair])
                    cache_key = _task_key(service, analyzed_article, target, scan.member, event_observations.get(pair))
                    analysis_keys[(scan.member.id, *pair)] = cache_key
                    needed.setdefault(cache_key, []).append((scan, analyzed_article, target, decision))
        if needed:
            with open_connection(service.db) as conn:
                keys = list(needed)
                for start in range(0, len(keys), 500):
                    batch = keys[start:start + 500]
                    for row in conn.execute(f"SELECT cache_key,result_json FROM subscription_analysis WHERE cache_key IN ({','.join('?' for _ in batch)}) AND created_at>=?", (*batch, _iso(current - timedelta(days=15)))):
                        try:
                            cache[row[0]] = validate_analysis(json.loads(row[1]), allowed_target_keys=(needed[row[0]][0][2].key,))
                        except (TypeError, ValueError):
                            pass
        metrics["cache_hits"] = len(cache)
        allowance = {scan.member.id: max(1, 10 // max(1, len(scans))) for scan in scans}
        used = {scan.member.id: 0 for scan in scans}
        tasks = []
        for cache_key, uses in needed.items():
            if cache_key in cache:
                continue
            if dry_run and isinstance(service.analyzer, DeepSeekAnalyzer):
                # A preview may use existing results, but never spend untracked
                # provider tokens or mutate the operator's real budget.
                metrics["deferred"] += 1
                continue
            owner = None
            for use in uses:
                with open_connection(service.db) as conn:
                    if service._current_member(conn, use[0]) is not None and used[use[0].member.id] < allowance[use[0].member.id]:
                        owner = use
                        break
            if owner is None or len(tasks) >= 10:
                metrics["deferred"] += 1
                continue
            if not dry_run and not transaction(lambda conn: jobs.claim(conn, cache_key, provider_for(owner[0].member), checkpoint(), suspected_major=owner[3].urgent_hint)):
                metrics["deferred"] += 1
                continue
            used[owner[0].member.id] += 1
            tasks.append((cache_key, owner))
            last_member = owner[0].member.id
        groups = {}
        for cache_key, owner in tasks:
            scope = owner[0].member.id if owner[0].member.analysis_private else "public"
            groups.setdefault((scope, bool(owner[3].urgent_hint)), []).append((cache_key, owner))
        for group in groups.values():
            size = 5 if isinstance(service.analyzer, DeepSeekAnalyzer) else 1
            pending = list(group)
            while pending:
                chunk, pending = pending[:size], pending[size:]
                active = []
                for cache_key, owner in chunk:
                    with open_connection(service.db) as conn:
                        valid = service._current_member(conn, owner[0]) is not None
                        breaker = conn.execute("SELECT blocked_until FROM analysis_provider_state WHERE provider_key=?", (provider_for(owner[0].member),)).fetchone()
                        valid = valid and not (breaker and breaker[0] and breaker[0] > _iso(checkpoint()))
                    if valid:
                        active.append((cache_key, owner))
                    elif not dry_run:
                        metrics["deferred"] += 1
                        transaction(lambda conn: jobs.release(conn, cache_key, checkpoint()))
                if not active:
                    continue
                if time.monotonic() - started >= settings.run_timeout_seconds:
                    metrics["deferred"] += len(active)
                    if not dry_run:
                        transaction(lambda conn: [jobs.release(conn, key, checkpoint()) for key, _ in active])
                    continue
                reservation = None
                if isinstance(service.analyzer, DeepSeekAnalyzer) and not dry_run:
                    # A five-item request can exceed either the input ceiling
                    # or today's remaining capacity while its individual tasks
                    # still fit. Split before any HTTP request; moved tasks
                    # retain their original claim and count only when sent.
                    while active:
                        batch = [(key, replace(owner[1], target_keys=(owner[2].key,)), (owner[2],)) for key, owner in active]
                        reason = "budget_deferred"
                        try:
                            estimated = service.analyzer.estimate_batch_tokens(batch)
                        except ValueError:
                            reason = "input_limit_exceeded"
                        else:
                            reservation = reserve_budget(service.db, estimated_tokens=estimated,
                                suspected_major=all(owner[3].urgent_hint for _, owner in active), now=checkpoint(),
                                **service.budget_limits)
                            if reservation is not None:
                                break
                        if len(active) > 1:
                            pending.insert(0, active.pop())
                            continue
                        cache_key, owner = active[0]
                        metrics["deferred"] += 1
                        def release_unsent(conn):
                            jobs.release(conn, cache_key, checkpoint())
                            if reason == "input_limit_exceeded":
                                conn.execute("UPDATE analysis_attempts SET last_error_code=? WHERE cache_key=?",
                                             (reason, cache_key))
                                audit(conn, owner[0], article_version(owner[1], service.source_namespace)[0], reason)
                        transaction(release_unsent)
                        active = []
                    if not active:
                        continue
                batch = [(key, replace(owner[1], target_keys=(owner[2].key,)), (owner[2],)) for key, owner in active]
                counts["analyzed"] += 1
                metrics["analysis_tasks"] += len(active)
                try:
                    if isinstance(service.analyzer, DeepSeekAnalyzer):
                        outcomes = service.analyzer.analyze_batch(batch)
                    else:
                        key, article, targets = batch[0]
                        outcomes = {key: service.analyzer.analyze(article, targets)}
                except Exception as exc:
                    outcomes = {key: exc for key, _ in active}
                if isinstance(service.analyzer, DeepSeekAnalyzer):
                    records = service.analyzer.drain_usage_records()
                    if not dry_run:
                        if reservation:
                            first = records.pop(0) if records else {"category": "news", "status": "failure", "total_tokens": None, "logical_tasks": len(active)}
                            settle_budget(service.db, reservation, first, now=checkpoint())
                        for record in records:
                            record_usage(service.db, record, now=checkpoint())
                for index, (cache_key, owner) in enumerate(active):
                    scan, article, target, _ = owner
                    provider = provider_for(scan.member)
                    outcome = outcomes.get(cache_key, ValueError("missing_analysis_task"))
                    try:
                        if isinstance(outcome, Exception):
                            raise outcome
                        outcome = validate_analysis(outcome.to_dict(), allowed_target_keys=(target.key,))
                    except Exception as exc:
                        errors.append(f"analyze member {scan.member.id}: {jobs.safe_error_code(exc)}")
                        if not dry_run:
                            transaction(lambda conn: jobs.fail(conn, cache_key, provider, exc, checkpoint(), provider_failure=index == 0))
                        continue
                    if not dry_run:
                        def save_analysis(conn):
                            if service._current_member(conn, scan) is None:
                                jobs.release(conn, cache_key, checkpoint())
                                return False
                            conn.execute("INSERT INTO subscription_analysis VALUES (?,?,?) ON CONFLICT(cache_key) DO UPDATE SET result_json=excluded.result_json,created_at=excluded.created_at",
                                (cache_key, json.dumps(outcome.to_dict(), ensure_ascii=False), _iso(checkpoint())))
                            # Preserve the exact public research excerpt even
                            # when the conclusion is not eligible to notify.
                            # Personal focus remains in its existing isolated
                            # analysis key, never copied to shared research.
                            context = getattr(article, 'research_context', None)
                            if context is not None:
                                conn.execute("INSERT INTO monitor_state VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                                    ('analysis-context-v1:' + cache_key,
                                     json.dumps(context, ensure_ascii=False, sort_keys=True), _iso(checkpoint())))
                            jobs.succeed(conn, cache_key, provider, checkpoint())
                            return True
                        if not transaction(save_analysis):
                            metrics["deferred"] += 1
                            continue
                    cache[cache_key] = outcome
                    metrics["analysis_succeeded"] += 1
        for scan, (key, article, targets, decision, timing) in ordered:
            results = [(target, cache.get(analysis_keys[(scan.member.id, key, target.key)])) for target in targets]
            results = [(target, analysis) for target, analysis in results if analysis is not None]
            if not results:
                continue
            matches = [(target, analysis) for target, analysis in results if service._notify(scan, target, analysis)]
            if dry_run:
                counts["queued"] += int(bool(matches))
                continue
            def evaluate(conn):
                member = service._current_member(conn, scan)
                if member is None:
                    return 0
                conn.executemany("INSERT OR IGNORE INTO member_evaluations VALUES (?,?,?,?, 'evaluated')",
                    [(member.id, key, target.key, _iso(checkpoint())) for target, _ in results])
                if not matches:
                    return 0
                history = _notification_history(conn, scan, checkpoint())
                novel = []
                representatives = []
                for target, analysis in matches:
                    observation = event_observations.get((key, target.key))
                    if observation is not None and not should_notify_event(conn, service.corp_id, member.id, observation.as_link()):
                        continue
                    prior = _matching_candidate(history, article, target, observation=observation)
                    if prior:
                        if not prior['_editable'] or target.key in prior['_notification'].get('matched_target_ids', ()):
                            continue
                        representatives.append(prior)
                    novel.append((target, analysis))
                if not novel:
                    return 0
                # Keep source deadlines fixed; model significance cannot make an
                # expired routine story newly current.
                checked_times = [freshness(_fact_article(article, event_observations.get((key, target.key))), checkpoint())
                                 for target, _ in novel]
                checked = checked_times[0]
                mode, expiry = checked.mode, min((item.expires_at for item in checked_times if item.expires_at), default=None)
                if manual:
                    mode, expiry = "backfill", checkpoint() + timedelta(hours=1)
                elif any(item.mode not in {"realtime", "morning", "urgent"} for item in checked_times):
                    audit(conn, scan, key, "expired_after_analysis")
                    return 0
                strongest = max(novel, key=lambda item: {"普通": 0, "重要": 1, "重大": 2}[item[1].level])[1]
                if strongest.level == "重大" and mode == "realtime":
                    mode = "urgent"
                effective = {t["key"]: t.get("effective_at") for t in scan.snapshot.targets}
                payload = {"event": "subscription.news", "corp_id": service.corp_id,
                    "member_id": member.id, "member_generation": member.generation,
                    "touser": member.user_id, "config_version": scan.settings.config_version,
                    "quality_policy_version": QUALITY_RULE_VERSION,
                    "matched_target_ids": [t.key for t, _ in novel],
                    "subscription_effective_at": {t.key: effective[t.key] for t, _ in novel},
                    "analysis_focus": {t.key: list(t.focus) for t, _ in novel},
                    "article": _article_dict(article), "analysis": strongest.to_dict(),
                    "target_names": [t.name for t, _ in novel],
                    "target_analyses": [{"target_id": t.key, "name": t.name, "analysis": a.to_dict(),
                                         "research_context": research_contexts.get((key, t.key))} for t, a in novel]}
                links = {}
                for target, _ in novel:
                    observation = event_observations.get((key, target.key))
                    if observation is None:
                        continue
                    link = observation.as_link()
                    pair = link['event_id'], link['event_version']
                    if pair not in links:
                        links[pair] = dict(link, target_ids=[])
                    links[pair]['target_ids'].append(target.key)
                payload['event_links'] = list(links.values())
                primary = payload['event_links'][0] if payload['event_links'] else None
                representative = representatives[0] if representatives else None
                inserted = enqueue_candidate(conn, payload,
                    article_key=representative['article_key'] if representative else key,
                    event_id=representative['event_id'] if representative else primary['event_id'] if primary else "article:" + article.key,
                    event_version=representative['event_version'] if representative else primary['event_version'] if primary else revision_key(article),
                    mode='urgent' if mode == 'urgent' else representative['mode'] if representative else mode,
                    expires_at=representative['expires_at'] if representative else expiry, now=checkpoint())
                # Legacy tables retain evaluation evidence for compatibility;
                # only notification_acceptances proves API acceptance.
                conn.execute("INSERT OR IGNORE INTO quality_member_articles VALUES (?,?,?,?)",
                    (member.id, article.key, revision_key(article), _iso(checkpoint())))
                for target, analysis in novel:
                    event, version = event_identity(article, target, analysis)
                    conn.execute("INSERT OR IGNORE INTO quality_member_events VALUES (?,?,?,?)", (member.id, event, version, _iso(checkpoint())))
                    conn.execute("INSERT INTO quality_events VALUES (?,?,?,NULL,?,?) ON CONFLICT(event_id,event_version) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                        (event, version, getattr(analysis, "event_type", "unknown"), _iso(checkpoint()), _iso(checkpoint())))
                return inserted
            counts["queued"] += transaction(evaluate)
        if not dry_run:
            if last_member is not None:
                transaction(lambda conn: conn.execute("INSERT INTO monitor_state VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (cursor_key, str(last_member), _iso(checkpoint()))))
            metrics["notifications_queued"] = flush_notifications(service.db, checkpoint())
            def prune(conn):
                cutoff = _iso(current - timedelta(days=15))
                for table, column in (("member_evaluations", "evaluated_at"), ("subscription_articles", "published_at"),
                                      ("subscription_analysis", "created_at"), ("member_legacy_seen", "seen_at")):
                    conn.execute(f"DELETE FROM {table} WHERE {column}<?", (cutoff,))
                conn.execute("DELETE FROM monitor_state WHERE key LIKE 'analysis-context-v1:%' AND updated_at<?", (cutoff,))
                history = _iso(current - timedelta(days=30))
                conn.execute("DELETE FROM quality_policy_audit WHERE created_at<?", (history,))
                conn.execute("DELETE FROM quality_article_observations WHERE observed_at<?", (history,))
                conn.execute("DELETE FROM quality_decisions WHERE decided_at<?", (history,))
            transaction(prune)
            status = "partial" if errors else "succeeded"
            service._finish(slot, checkpoint(), status, "; ".join(errors))
            transaction_detail = {**counts, **metrics, "member_config_versions": [(s.member.id, s.settings.config_version) for s in scans]}
            with open_connection(service.db) as conn:
                with write_transaction(conn):
                    conn.execute("UPDATE monitor_runs SET detail_json=? WHERE slot=? AND lease_owner=?",
                                 (json.dumps(transaction_detail), slot, service.owner))
        return result(status="partial" if errors else "succeeded", slot=slot)
    except RunLeaseLost:
        return result(status="failed", slot=slot, reason="lease_lost")
    except Exception as exc:
        errors.append("scan: " + type(exc).__name__)
        if not dry_run:
            service._finish(slot, checkpoint(), "failed", type(exc).__name__)
        return result(status="failed", slot=slot)
