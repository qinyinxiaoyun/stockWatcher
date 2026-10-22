"""One worker, shared article processing and member-scoped durable fan-out.

Only configured focus strings are shared within an organisation. Members marked
``analysis_private`` receive a separate cache domain; personal background is not
part of the public analysis specification. Thresholds never enter cache keys.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from copy import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
import unicodedata
import uuid

from .analyzer import DeepSeekAnalyzer, validate_analysis
from .monitor import (
    MonitorService, MonitorSettings, RunLeaseLost, _as_mapping, _baseline_keys,
    _iso, open_connection, write_transaction,
)
from .news import NewsArticle, NewsTarget, parse_published_at
from .scheduler import SHANGHAI, as_shanghai, slot_for_time


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS subscription_articles (
 article_key TEXT PRIMARY KEY, identity_key TEXT NOT NULL, content_hash TEXT NOT NULL,
 source TEXT NOT NULL, article_json TEXT NOT NULL, published_at TEXT NOT NULL,
 fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_subscription_articles_time ON subscription_articles(published_at);
CREATE TABLE IF NOT EXISTS subscription_analysis (
 cache_key TEXT PRIMARY KEY, result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS member_evaluations (
 member_id INTEGER NOT NULL, article_key TEXT NOT NULL, target_key TEXT NOT NULL,
 evaluated_at TEXT NOT NULL, outcome TEXT NOT NULL,
 PRIMARY KEY(member_id,article_key,target_key)
);
CREATE TABLE IF NOT EXISTS member_baselines (
 member_id INTEGER NOT NULL, source TEXT NOT NULL, target_key TEXT NOT NULL,
 established_at TEXT NOT NULL, PRIMARY KEY(member_id,source,target_key)
);
CREATE TABLE IF NOT EXISTS member_legacy_seen (
 member_id INTEGER NOT NULL, article_key TEXT NOT NULL, seen_at TEXT NOT NULL,
 PRIMARY KEY(member_id,article_key)
);
"""


def copy_legacy_state(conn, member_id):
    """Copy the explicitly assigned owner's history inside the caller's transaction."""
    from .subscriptions import MemberConfigStore
    conn.execute("INSERT OR IGNORE INTO member_legacy_seen(member_id,article_key,seen_at) SELECT ?,article_key,seen_at FROM seen_articles", (member_id,))
    snapshot = MemberConfigStore(conn, member_id).snapshot()
    present = {r[0] for r in conn.execute("SELECT key FROM monitor_state")}
    for raw in snapshot.targets:
        target = NewsTarget.from_value(raw)
        if present.intersection(_baseline_keys(target)):
            conn.execute("INSERT OR IGNORE INTO member_baselines VALUES (?, 'eastmoney', ?, ?)",
                         (member_id, target.key, snapshot.updated_at))


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _normal(value):
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def article_version(article, namespace=None):
    """Keep upstream namespaces and substantive revisions independent."""
    source = _normal(namespace or article.source)
    identity = _digest([source, article.raw_id or article.key])
    content = _digest([article.title, article.summary, article.url, article.source,
                       _iso(article.published_at)])
    return _digest([identity, content]), identity, content


def _article_dict(article):
    return {"key": article.key, "title": article.title, "summary": article.summary,
            "url": article.url, "published_at": article.published_at.isoformat(),
            "source": article.source, "target_keys": list(article.target_keys),
            "raw_id": article.raw_id, "legacy_keys": list(article.legacy_keys)}


def _read_article(value):
    obj = json.loads(value)
    obj["published_at"] = parse_published_at(obj["published_at"])
    obj["target_keys"] = tuple(obj["target_keys"])
    obj["legacy_keys"] = tuple(obj.get("legacy_keys", ()))
    return NewsArticle(**obj)


@dataclass
class MemberScan:
    member: object
    snapshot: object
    settings: MonitorSettings
    targets: dict
    rules: dict


class SubscriptionMonitorService(MonitorService):
    """Reuse the global monitor lease; never create a per-member scheduler."""
    independent_subscriptions = True

    def __init__(self, *args, corp_id, **kwargs):
        super().__init__(*args, **kwargs)
        if isinstance(self.analyzer, DeepSeekAnalyzer):
            # The service budget counts actual requests, including failures.
            # Retry unfinished work next cycle without spending a hidden retry.
            self.analyzer = copy(self.analyzer)
            self.analyzer.max_attempts = 1
        self.corp_id = corp_id
        self.source_namespace = _normal(getattr(self.fetcher, "source_namespace", "eastmoney"))

    def _members(self):
        from .subscriptions import MemberConfigStore, MemberRegistry
        scans, errors = [], []
        with open_connection(self.db) as conn:
            for member in MemberRegistry(conn, self.corp_id).active():
                try:
                    snapshot = MemberConfigStore(conn, member.id).snapshot()
                    settings = MonitorSettings.from_snapshot(snapshot, environ=self.environ)
                    data = _as_mapping(snapshot)
                    rules = {s["id"]: s for s in data.get("stocks", ())}
                    scans.append(MemberScan(member, snapshot, settings,
                                            {t.key: t for t in settings.targets}, rules))
                except Exception as exc:
                    errors.append(f"member {member.id}: {type(exc).__name__}")
        return scans, errors

    def _cache_key(self, content, target, member):
        return _digest([self.corp_id, member.id if getattr(member, "analysis_private", False) else "public",
                        content, target.key, target.kind, _normal(target.name),
                        sorted({_normal(f) for f in target.focus}),
                        getattr(self.analyzer, "model", type(self.analyzer).__name__),
                        getattr(self.analyzer, "model_revision", ""),
                        getattr(getattr(self.analyzer, "transport", None), "endpoint", ""),
                        getattr(self.analyzer, "prompt_version", "1"),
                        getattr(self.analyzer, "schema_version", "1")])

    def _current_member(self, conn, scan):
        from .subscriptions import MemberConfigStore, MemberRegistry
        member = MemberRegistry(conn, self.corp_id).get(scan.member.user_id)
        if member is None or not member.active or member.generation != scan.member.generation:
            return None
        latest = MemberConfigStore(conn, member.id).snapshot()
        return member if latest.config_version == scan.settings.config_version else None

    @staticmethod
    def _notify(scan, target, analysis):
        rule = scan.rules.get(target.key, {})
        levels = scan.settings.push_levels
        if rule.get("push_level") == "none":
            return False
        if rule.get("push_level") == "all":
            levels = ("重大", "重要", "普通")
        direction = rule.get("direction", "both")
        return (analysis.should_notify and analysis.level in levels and
                (direction == "both" or
                 direction == "positive" and analysis.impact == "利好" or
                 direction == "negative" and analysis.impact == "利空"))

    def run_once(self, *, now=None, force=False, dry_run=False, slot=None,
                 backfill_hours=None, backfill_target=None, member_id=None):
        started = time.monotonic()
        current = as_shanghai(now or datetime.now(SHANGHAI))
        checkpoint = lambda: current + timedelta(seconds=time.monotonic() - started)
        settings = self.settings()
        scans, errors = self._members()
        cursor_key = f"subscription_member_cursor:{_digest(self.corp_id)}"
        with open_connection(self.db) as conn:
            cursor = conn.execute("SELECT value FROM monitor_state WHERE key=?", (cursor_key,)).fetchone()
        if cursor:
            after = next((pos+1 for pos, scan in enumerate(scans) if str(scan.member.id) == cursor[0]), 0)
            scans = scans[after:] + scans[:after]
        if backfill_hours is not None:
            if member_id is None or not force or not 1 <= backfill_hours <= 24 or not backfill_target:
                raise ValueError("backfill requires an explicit member, manual target and 1..24 hours")
            scans = [scan for scan in scans if scan.member.id == member_id]
            found = False
            for scan in scans:
                selected = [t for t in scan.targets.values() if backfill_target in {t.key, t.name, *t.queries}]
                if len(selected) > 1:
                    raise ValueError("backfill target must uniquely identify a target")
                scan.targets = {t.key: replace(t, effective_at=current-timedelta(hours=backfill_hours)) for t in selected}
                found |= bool(selected)
            if not found:
                raise ValueError("backfill target must identify an enabled target")
        self.last_settings = settings
        def result(**values):
            values.setdefault("errors", tuple(errors))
            values.setdefault("dry_run", dry_run)
            return self._result(started, settings, **values)
        if not settings.enabled and not force:
            return result(status="skipped", slot=None, reason="monitor_disabled")
        if not any(s.targets for s in scans):
            return result(status="skipped", slot=None, reason="no_targets")
        slot = slot or (f"manual:{current.isoformat()}:{uuid.uuid4().hex[:8]}" if force else slot_for_time(current))
        if slot is None:
            return result(status="skipped", slot=None, reason="outside_schedule")
        if not dry_run and not self._claim(slot, current, settings.run_lease_seconds):
            return result(status="skipped", slot=slot, reason="slot_or_monitor_already_claimed")

        def transaction(action):
            with open_connection(self.db) as conn:
                with write_transaction(conn):
                    self._assert_lease(conn, slot, checkpoint())
                    value = action(conn)
                    until = _iso(checkpoint()+timedelta(seconds=settings.run_lease_seconds))
                    conn.execute("UPDATE monitor_leases SET lease_until=? WHERE name='monitor' AND lease_owner=?", (until, self.owner))
                    conn.execute("UPDATE monitor_runs SET lease_until=? WHERE slot=? AND lease_owner=?", (until, slot, self.owner))
                    return value

        fetched, pool, pool_sources, succeeded = 0, {}, {}, set()
        queries = {}
        for scan in scans:
            for target in scan.targets.values():
                previous = queries.get(target.key)
                normalized = {_normal(query): query for query in (*(previous.queries if previous else ()), *(target.queries or (target.query,)))}
                union = tuple(normalized.values())
                queries[target.key] = replace(target, queries=union, focus=(), effective_at=None)
        analyzed = queued = baseline_count = deduplicated = 0
        try:
            for query_key, target in queries.items():
                if time.monotonic()-started >= settings.run_timeout_seconds:
                    errors.append("fetch deadline: TimeoutError")
                    break
                try:
                    articles = self.fetcher.fetch_target(target, now=current)
                    fetched += len(articles)
                    succeeded.add(query_key)
                    for article in articles:
                        article = article.with_targets((target.key,))
                        key, identity, content = article_version(article, self.source_namespace)
                        previous = pool.get(key)
                        if previous:
                            article = article.with_targets(previous[0].target_keys)
                        pool[key] = (article, identity, content)
                        pool_sources[key] = self.source_namespace
                except Exception as exc:
                    errors.append(f"fetch {target.key}: {type(exc).__name__}")
            if not dry_run:
                def save_pool(conn):
                    for key, (article, identity, content) in pool.items():
                        old = conn.execute("SELECT article_json FROM subscription_articles WHERE article_key=?", (key,)).fetchone()
                        if old:
                            article = article.with_targets(_read_article(old[0]).target_keys)
                        conn.execute("INSERT INTO subscription_articles VALUES (?,?,?,?,?,?,?) ON CONFLICT(article_key) DO UPDATE SET article_json=excluded.article_json,fetched_at=excluded.fetched_at",
                                     (key, identity, content, self.source_namespace, json.dumps(_article_dict(article), ensure_ascii=False), _iso(article.published_at), _iso(checkpoint())))
                transaction(save_pool)
                with open_connection(self.db) as conn:
                    rows = conn.execute("SELECT * FROM subscription_articles WHERE published_at>=? AND published_at<=? ORDER BY published_at,article_key",
                                        (_iso(current-timedelta(hours=settings.lookback_hours)), _iso(current+timedelta(hours=settings.future_tolerance_hours)))).fetchall()
                    pool = {r["article_key"]: (_read_article(r["article_json"]), r["identity_key"], r["content_hash"]) for r in rows}
                    pool_sources = {r["article_key"]: r["source"] for r in rows}
            candidates = [[] for _ in scans]
            baseline_count = deduplicated = 0
            for pos, scan in enumerate(scans):
                with open_connection(self.db) as conn:
                    seen = {(r[0], r[1]) for r in conn.execute("SELECT e.article_key,e.target_key FROM member_evaluations e JOIN subscription_articles a ON a.article_key=e.article_key WHERE e.member_id=? AND a.published_at>=?", (scan.member.id, _iso(current-timedelta(hours=settings.lookback_hours))))}
                    legacy = {r[0] for r in conn.execute("SELECT article_key FROM member_legacy_seen WHERE member_id=? AND seen_at>=?", (scan.member.id, _iso(current-timedelta(days=15))))}
                    baselines = {(r[0], r[1]) for r in conn.execute("SELECT source,target_key FROM member_baselines WHERE member_id=?", (scan.member.id,))}
                suppressed = []
                consumed_legacy = set()
                new_baselines = {(self.source_namespace, target.key) for target in scan.targets.values() if target.key in succeeded}
                for key, (article, _, content) in pool.items():
                    if not current-timedelta(hours=settings.lookback_hours) <= as_shanghai(article.published_at) <= current+timedelta(hours=settings.future_tolerance_hours):
                        continue
                    matches = []
                    for target in scan.targets.values():
                        if target.key not in article.target_keys:
                            continue
                        source = pool_sources[key]
                        established = (source, target.key) in baselines
                        new_source = not established and any(t == target.key for _, t in baselines)
                        old = (target.effective_at is None or target.effective_at <= datetime(1970, 1, 2, tzinfo=timezone.utc))
                        if not dry_run and backfill_hours is None and (key, target.key) in seen:
                            deduplicated += 1
                            continue
                        inherited = legacy.intersection((article.key, *article.legacy_keys)) if source == "eastmoney" else set()
                        if not dry_run and backfill_hours is None and inherited:
                            # Bind the imported ID-only history to this observed
                            # content version, atomically for every target. The
                            # same version stays silent; a later correction is
                            # an independently eligible event.
                            suppressed.append((key, target.key))
                            consumed_legacy.update(inherited)
                            deduplicated += 1
                            continue
                        if target.effective_at and as_shanghai(article.published_at) < as_shanghai(target.effective_at):
                            continue
                        if not dry_run and backfill_hours is None and not established and (old or new_source):
                            suppressed.append((key, target.key))
                            continue
                        matches.append(target)
                    if matches:
                        candidates[pos].append((key, article, content, matches))
                if not dry_run:
                    def save_baseline(conn):
                        conn.executemany("INSERT OR IGNORE INTO member_baselines VALUES (?,?,?,?)", [(scan.member.id, source, target, _iso(checkpoint())) for source, target in new_baselines])
                        conn.executemany("INSERT OR IGNORE INTO member_evaluations VALUES (?,?,?,?, 'baseline')", [(scan.member.id, key, target, _iso(checkpoint())) for key, target in suppressed])
                        conn.executemany("DELETE FROM member_legacy_seen WHERE member_id=? AND article_key=?", [(scan.member.id, key) for key in consumed_legacy])
                    transaction(save_baseline)
                    baseline_count += len(suppressed)
            # Round-robin candidates prevent a large member from consuming the budget.
            ordered = []
            for offset in range(max((len(c) for c in candidates), default=0)):
                ordered.extend((scan, items[offset]) for scan, items in zip(scans, candidates) if offset < min(len(items), scan.settings.max_articles))
            cache = {}
            if not dry_run:
                needed = list({self._cache_key(content, target, scan.member)
                               for scan, (_, _, content, targets) in ordered for target in targets})
                with open_connection(self.db) as conn:
                    for start in range(0, len(needed), 500):
                        batch = needed[start:start+500]
                        marks = ",".join("?" for _ in batch)
                        for row in conn.execute(f"SELECT cache_key,result_json FROM subscription_analysis WHERE cache_key IN ({marks}) AND created_at>=?", (*batch, _iso(current-timedelta(days=15)))):
                            try:
                                cache[row[0]] = json.loads(row[1])
                            except (TypeError, ValueError):
                                pass  # Invalid persisted cache rows are repaired on demand.
            allowances = {s.member.id: max(1, 10//max(1, len(scans))) for s in scans}
            used = {s.member.id: 0 for s in scans}
            last_attempted_member = None
            for scan, (key, article, content, targets) in ordered:
                results, completed = [], True
                for target in targets:
                    if time.monotonic()-started >= settings.run_timeout_seconds:
                        completed = False
                        errors.append("analyze deadline: TimeoutError")
                        break
                    with open_connection(self.db) as conn:
                        if self._current_member(conn, scan) is None:
                            completed = False
                            break
                    cache_key = self._cache_key(content, target, scan.member)
                    if cache_key in cache:
                        try:
                            analysis = validate_analysis(cache[cache_key], allowed_target_keys=(target.key,))
                        except (TypeError, ValueError):
                            cache.pop(cache_key, None)
                    if cache_key not in cache:
                        if analyzed >= 10 or used[scan.member.id] >= allowances[scan.member.id] or time.monotonic()-started >= settings.run_timeout_seconds:
                            completed = False
                            errors.append("analysis budget: Deferred")
                            break
                        analyzed += 1
                        used[scan.member.id] += 1
                        last_attempted_member = scan.member.id
                        try:
                            analysis = self.analyzer.analyze(replace(article, target_keys=(target.key,)), (target,))
                            analysis = validate_analysis(analysis.to_dict(), allowed_target_keys=(target.key,))
                        except Exception as exc:
                            errors.append(f"analyze member {scan.member.id}: {type(exc).__name__}")
                            completed = False
                            break
                        if not dry_run:
                            def save_analysis(conn):
                                if self._current_member(conn, scan) is None:
                                    return False
                                conn.execute("INSERT INTO subscription_analysis VALUES (?,?,?) ON CONFLICT(cache_key) DO UPDATE SET result_json=excluded.result_json,created_at=excluded.created_at", (cache_key, json.dumps(analysis.to_dict(), ensure_ascii=False), _iso(checkpoint())))
                                return True
                            if not transaction(save_analysis):
                                completed = False
                                break
                        cache[cache_key] = analysis.to_dict()
                    results.append((target, analysis))
                if not completed:
                    continue
                matches = [(t, a) for t, a in results if self._notify(scan, t, a)]
                if dry_run:
                    queued += bool(matches)
                    continue
                def evaluate(conn):
                    member = self._current_member(conn, scan)
                    if member is None:
                        return 0  # Changed subscriptions are reconsidered against the shared cache.
                    conn.executemany("INSERT OR IGNORE INTO member_evaluations VALUES (?,?,?,?, 'evaluated')", [(member.id, key, t.key, _iso(checkpoint())) for t, _ in results])
                    if not matches:
                        return 0
                    event = f"member:{member.id}:article:{key}"
                    strongest = max(matches, key=lambda item: {"普通": 0, "重要": 1, "重大": 2}[item[1].level])[1]
                    # A manual backfill widens only the article window. Keep
                    # the actual subscription activation times for delivery
                    # fencing, otherwise every authorized backfill is cancelled.
                    effective_times = {t["key"]: t.get("effective_at") for t in scan.snapshot.targets}
                    payload = {"event": "subscription.news", "member_id": member.id, "member_generation": member.generation,
                               "corp_id": self.corp_id, "touser": member.user_id, "config_version": scan.settings.config_version,
                               "matched_target_ids": [t.key for t, _ in matches],
                               "subscription_effective_at": {t.key: effective_times[t.key] for t, _ in matches},
                               "analysis_focus": {t.key: list(t.focus) for t, _ in matches},
                               "article": _article_dict(article), "analysis": strongest.to_dict(),
                               "target_names": [t.name for t, _ in matches],
                               "target_analyses": [{"target_id": t.key, "name": t.name, "analysis": a.to_dict()} for t, a in matches]}
                    cursor = conn.execute("INSERT OR IGNORE INTO outbox(event_key,payload_json,available_at,created_at) VALUES (?,?,?,?)", (event, json.dumps(payload, ensure_ascii=False), _iso(checkpoint()), _iso(checkpoint())))
                    return cursor.rowcount
                queued += transaction(evaluate)
            status = "partial" if errors else "succeeded"
            if not dry_run:
                if last_attempted_member is not None:
                    transaction(lambda conn: conn.execute("INSERT INTO monitor_state VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (cursor_key, str(last_attempted_member), _iso(checkpoint()))))
                def prune(conn):
                    cutoff = _iso(current-timedelta(days=15))
                    conn.execute("DELETE FROM member_evaluations WHERE evaluated_at<?", (cutoff,))
                    conn.execute("DELETE FROM subscription_articles WHERE published_at<?", (cutoff,))
                    conn.execute("DELETE FROM subscription_analysis WHERE created_at<?", (cutoff,))
                    conn.execute("DELETE FROM member_legacy_seen WHERE seen_at<?", (cutoff,))
                transaction(prune)
                self._finish(slot, checkpoint(), status, "; ".join(errors))
            return result(status=status, slot=slot, fetched=fetched, eligible=sum(map(len, candidates)),
                          analyzed=analyzed, queued=queued, baseline_seen=baseline_count, deduplicated=deduplicated)
        except RunLeaseLost:
            return result(status="failed", slot=slot, reason="lease_lost", fetched=fetched)
        except Exception as exc:
            errors.append(f"scan: {type(exc).__name__}")
            if not dry_run:
                self._finish(slot, checkpoint(), "failed", type(exc).__name__)
            return result(status="failed", slot=slot, fetched=fetched, analyzed=analyzed,
                          queued=queued, baseline_seen=baseline_count)


__all__ = ["SubscriptionMonitorService", "SCHEMA_SQL", "copy_legacy_state", "article_version"]
