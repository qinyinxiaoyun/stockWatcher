"""One monitoring cycle, durable deduplication and transactional outbox."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol
import uuid

from .analyzer import AnalysisResult, Analyzer
from .news import NewsArticle, NewsTarget
from .scheduler import SHANGHAI, as_shanghai, slot_for_time


DbSource = sqlite3.Connection | str | Path | Callable[[], sqlite3.Connection]
LOGGER = logging.getLogger(__name__)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=SHANGHAI)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


@contextmanager
def open_connection(source: DbSource) -> Iterator[sqlite3.Connection]:
    """Open a connection while preserving caller-owned connection lifetime."""

    owned = not isinstance(source, sqlite3.Connection)
    if isinstance(source, sqlite3.Connection):
        conn = source
    elif callable(source):
        conn = source()
    else:
        conn = sqlite3.connect(str(source), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        if owned:
            conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Use an explicit short write transaction.

    Callers must perform all HTTP/model work before entering this context.
    """

    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def ensure_monitor_schema(conn: sqlite3.Connection) -> None:
    """Create phase-1 tables if the central initializer has not done so yet."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS monitor_runs (
            slot TEXT PRIMARY KEY,
            lease_owner TEXT NOT NULL,
            lease_until TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            error TEXT
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
        """
    )
    # ``init_db`` may have created an earlier version of these tables.  SQLite
    # has no ``ADD COLUMN IF NOT EXISTS``, so migrate only missing columns.
    run_columns = {row[1] for row in conn.execute("PRAGMA table_info(monitor_runs)")}
    if "error" not in run_columns:
        conn.execute("ALTER TABLE monitor_runs ADD COLUMN error TEXT")
    outbox_columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
    if "lease_owner" not in outbox_columns:
        conn.execute("ALTER TABLE outbox ADD COLUMN lease_owner TEXT")
    conn.commit()


def _as_mapping(snapshot: Any) -> Mapping[str, Any]:
    if snapshot is None:
        return {}
    if isinstance(snapshot, Mapping):
        return snapshot
    if is_dataclass(snapshot):
        return asdict(snapshot)
    for name in ("data", "values", "settings"):
        value = getattr(snapshot, name, None)
        if isinstance(value, Mapping):
            return value
    try:
        return vars(snapshot)
    except TypeError as exc:
        raise TypeError("configuration snapshot must be mapping-like") from exc


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"invalid boolean setting: {value!r}")


@dataclass(frozen=True, slots=True)
class MonitorSettings:
    enabled: bool = False
    targets: tuple[NewsTarget, ...] = ()
    config_version: int = 0
    push_levels: tuple[str, ...] = ("重大", "重要")
    max_articles: int = 10
    lookback_hours: int = 24
    future_tolerance_hours: int = 1
    seen_retention_days: int = 15
    run_lease_seconds: int = 1_200
    run_timeout_seconds: int = 1_200

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Any,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "MonitorSettings":
        data = _as_mapping(snapshot)
        raw_targets = data.get("targets", data.get("monitor_targets", data.get("watch_targets", ())))
        if raw_targets is None:
            raw_targets = ()
        if isinstance(raw_targets, (str, Mapping)):
            raw_targets = [raw_targets]
        targets: list[NewsTarget] = []
        for item in raw_targets:
            if isinstance(item, NewsTarget):
                target = item
            elif isinstance(item, Mapping) or isinstance(item, str):
                target = NewsTarget.from_value(item)
            else:
                target = NewsTarget.from_value(vars(item))
            if target.key not in {existing.key for existing in targets}:
                targets.append(target)
        enabled = _bool(data.get("monitor_enabled", data.get("enabled")), False)
        env = os.environ if environ is None else environ
        if "MONITOR_ENABLED" in env:
            enabled = _bool(env["MONITOR_ENABLED"])
        max_articles = min(10, max(1, int(data.get("max_articles", 10))))
        raw_levels = data.get("push_levels", ("重大", "重要"))
        if isinstance(raw_levels, str):
            raw_levels = [raw_levels]
        push_levels = tuple(dict.fromkeys(str(level).strip() for level in raw_levels))
        if not set(push_levels).issubset({"重大", "重要", "普通"}):
            raise ValueError("push_levels may contain only 重大/重要/普通")
        return cls(
            enabled=enabled,
            targets=tuple(targets),
            config_version=int(data.get("config_version", data.get("version", 0))),
            push_levels=push_levels,
            max_articles=max_articles,
            lookback_hours=max(1, int(data.get("lookback_hours", 24))),
            future_tolerance_hours=max(0, int(data.get("future_tolerance_hours", 1))),
            seen_retention_days=max(1, int(data.get("seen_retention_days", 15))),
            run_lease_seconds=max(300, int(data.get("run_lease_seconds", 1_200))),
            run_timeout_seconds=max(60, int(data.get("run_timeout_seconds", 1_200))),
        )


def merge_articles(articles: Iterable[NewsArticle]) -> list[NewsArticle]:
    """Merge one article returned for several targets into one record."""

    merged: dict[str, NewsArticle] = {}
    for article in articles:
        identity = article.url.strip().lower() or article.raw_id or article.key
        current = merged.get(identity)
        if current is None:
            merged[identity] = article.with_targets(article.target_keys)
            continue
        # Prefer the richer text while retaining a stable first identity.
        merged[identity] = replace(
            current,
            title=current.title if len(current.title) >= len(article.title) else article.title,
            summary=current.summary if len(current.summary) >= len(article.summary) else article.summary,
            target_keys=tuple(sorted(set(current.target_keys).union(article.target_keys))),
            legacy_keys=tuple(sorted(set(current.legacy_keys).union(article.legacy_keys))),
        )
    return list(merged.values())


def eligible_articles(
    articles: Iterable[NewsArticle],
    *,
    now: datetime,
    lookback_hours: int = 24,
    future_tolerance_hours: int = 1,
    limit: int | None = 10,
) -> list[NewsArticle]:
    """Filter the 24-hour/+1-hour window, oldest first, with an optional cap."""

    current = as_shanghai(now)
    earliest = current - timedelta(hours=lookback_hours)
    latest = current + timedelta(hours=future_tolerance_hours)
    result = [
        article
        for article in merge_articles(articles)
        if earliest <= as_shanghai(article.published_at) <= latest
    ]
    result.sort(key=lambda article: as_shanghai(article.published_at))
    return result if limit is None else result[: max(0, limit)]


def apply_effective_at(
    articles: Iterable[NewsArticle], targets: Iterable[NewsTarget]
) -> list[NewsArticle]:
    """Drop target matches published before that target was added/resumed."""

    target_map = {target.key: target for target in targets}
    result: list[NewsArticle] = []
    for article in articles:
        published = as_shanghai(article.published_at)
        active_keys = tuple(
            key
            for key in article.target_keys
            if key in target_map
            and (
                target_map[key].effective_at is None
                or published >= as_shanghai(target_map[key].effective_at)
            )
        )
        if active_keys:
            result.append(replace(article, target_keys=active_keys))
    return result


class NewsFetcher(Protocol):
    def fetch_target(self, target: NewsTarget, *, now: datetime | None = None) -> list[NewsArticle]: ...


@dataclass(frozen=True, slots=True)
class MonitorRunResult:
    status: str
    slot: str | None
    reason: str | None = None
    fetched: int = 0
    eligible: int = 0
    analyzed: int = 0
    queued: int = 0
    baseline_seen: int = 0
    errors: tuple[str, ...] = ()
    dry_run: bool = False
    config_version: int = 0
    deduplicated: int = 0
    duration_seconds: float = 0.0


class RunLeaseLost(RuntimeError):
    """Another worker owns the singleton monitor lease."""


def _default_config_provider(conn: sqlite3.Connection) -> Any:
    try:
        from .config import ConfigStore  # imported lazily to keep this module independently testable
    except ImportError:
        return {}
    return ConfigStore(conn).snapshot()


def _baseline_key(target_key: str) -> str:
    digest = hashlib.sha256(target_key.encode("utf-8")).hexdigest()
    return f"baseline:{digest}"


def _baseline_keys(target: NewsTarget) -> tuple[str, ...]:
    # Imported legacy state used the configured company/symbol rather than the
    # new stable target id.  Accept both during the 15-day migration window.
    return tuple(
        dict.fromkeys(_baseline_key(value) for value in (target.key, *target.queries) if value)
    )


class MonitorService:
    """Coordinates a single scheduled scan without holding remote I/O locks."""

    def __init__(
        self,
        db: DbSource,
        fetcher: NewsFetcher,
        analyzer: Analyzer,
        *,
        config_provider: Callable[[sqlite3.Connection], Any] | None = None,
        environ: Mapping[str, str] | None = None,
        owner: str | None = None,
    ) -> None:
        self.db = db
        self.fetcher = fetcher
        self.analyzer = analyzer
        self.config_provider = config_provider or _default_config_provider
        self.environ = environ
        self.owner = owner or f"worker-{uuid.uuid4().hex}"
        self.last_settings: MonitorSettings | None = None

    def settings(self) -> MonitorSettings:
        with open_connection(self.db) as conn:
            snapshot = self.config_provider(conn)
        return MonitorSettings.from_snapshot(snapshot, environ=self.environ)

    def _result(
        self,
        started: float,
        settings: MonitorSettings,
        **values: Any,
    ) -> MonitorRunResult:
        values.setdefault("config_version", settings.config_version)
        values.setdefault("duration_seconds", max(0.0, time.monotonic() - started))
        result = MonitorRunResult(**values)
        log = LOGGER.debug if result.status == "skipped" else LOGGER.info
        log(
            "monitor_cycle task_id=%s config_version=%d status=%s duration_seconds=%.3f fetched=%d deduplicated=%d analyzed=%d queued=%d error_types=%s",
            result.slot or self.owner, result.config_version, result.status,
            result.duration_seconds, result.fetched, result.deduplicated,
            result.analyzed, result.queued,
            tuple(error.rsplit(":", 1)[-1].strip() for error in result.errors),
            extra={
                "task_id": result.slot or self.owner,
                "config_version": result.config_version,
                "status": result.status,
                "duration_seconds": round(result.duration_seconds, 3),
                "fetched_count": result.fetched,
                "deduplicated_count": result.deduplicated,
                "analyzed_count": result.analyzed,
                "queued_count": result.queued,
                "error_types": tuple(error.rsplit(":", 1)[-1] for error in result.errors),
            },
        )
        return result

    def _claim(self, slot: str, now: datetime, lease_seconds: int) -> bool:
        lease_until = _iso(now + timedelta(seconds=lease_seconds))
        with open_connection(self.db) as conn:
            ensure_monitor_schema(conn)
            with write_transaction(conn):
                existing = conn.execute(
                    "SELECT status, lease_until FROM monitor_runs WHERE slot=?", (slot,)
                ).fetchone()
                if existing is not None and (
                    existing["status"] in {"succeeded", "partial"}
                    or (existing["lease_until"] and existing["lease_until"] > _iso(now))
                ):
                    return False
                lease = conn.execute(
                    "SELECT lease_owner, lease_until FROM monitor_leases WHERE name='monitor'"
                ).fetchone()
                if lease is not None and lease["lease_until"] > _iso(now):
                    return False
                conn.execute(
                    """INSERT INTO monitor_leases(
                           name, lease_owner, lease_until, run_slot, updated_at
                       ) VALUES ('monitor', ?, ?, ?, ?)
                       ON CONFLICT(name) DO UPDATE SET
                           lease_owner=excluded.lease_owner,
                           lease_until=excluded.lease_until,
                           run_slot=excluded.run_slot,
                           updated_at=excluded.updated_at""",
                    (self.owner, lease_until, slot, _iso(now)),
                )
                cursor = conn.execute(
                    """
                    INSERT INTO monitor_runs(
                        slot, lease_owner, lease_until, status, started_at, finished_at, error
                    ) VALUES (?, ?, ?, 'running', ?, NULL, NULL)
                    ON CONFLICT(slot) DO UPDATE SET
                        lease_owner=excluded.lease_owner,
                        lease_until=excluded.lease_until,
                        status='running',
                        started_at=excluded.started_at,
                        finished_at=NULL,
                        error=NULL
                    WHERE monitor_runs.status NOT IN ('succeeded', 'partial')
                      AND COALESCE(monitor_runs.lease_until, '') <= excluded.started_at
                    """,
                    (slot, self.owner, lease_until, _iso(now)),
                )
                if cursor.rowcount != 1:
                    conn.execute(
                        "DELETE FROM monitor_leases WHERE name='monitor' AND lease_owner=? AND run_slot=?",
                        (self.owner, slot),
                    )
                return cursor.rowcount == 1

    def _renew(self, slot: str, now: datetime, lease_seconds: int) -> None:
        lease_until = _iso(now + timedelta(seconds=lease_seconds))
        with open_connection(self.db) as conn:
            with write_transaction(conn):
                lease_cursor = conn.execute(
                    """UPDATE monitor_leases
                       SET lease_until=?, updated_at=?
                       WHERE name='monitor' AND lease_owner=? AND run_slot=?""",
                    (lease_until, _iso(now), self.owner, slot),
                )
                run_cursor = conn.execute(
                    """UPDATE monitor_runs SET lease_until=?
                       WHERE slot=? AND lease_owner=? AND status='running'""",
                    (lease_until, slot, self.owner),
                )
                if lease_cursor.rowcount != 1 or run_cursor.rowcount != 1:
                    raise RunLeaseLost("monitor lease ownership was lost")

    def _assert_lease(self, conn: sqlite3.Connection, slot: str, now: datetime) -> None:
        row = conn.execute(
            """SELECT 1 FROM monitor_leases AS lease
               JOIN monitor_runs AS run
                 ON run.slot=lease.run_slot AND run.lease_owner=lease.lease_owner
               WHERE lease.name='monitor' AND lease.lease_owner=? AND lease.run_slot=?
                 AND lease.lease_until>? AND run.status='running'""",
            (self.owner, slot, _iso(now)),
        ).fetchone()
        if row is None:
            raise RunLeaseLost("monitor lease expired or ownership changed")

    def _finish(self, slot: str, now: datetime, status: str, error: str | None = None) -> None:
        with open_connection(self.db) as conn:
            with write_transaction(conn):
                cursor = conn.execute(
                    """UPDATE monitor_runs
                       SET status=?, finished_at=?, lease_until=?, error=?
                       WHERE slot=? AND lease_owner=?""",
                    (status, _iso(now), _iso(now), (error or "")[:1000] or None, slot, self.owner),
                )
                if cursor.rowcount == 1:
                    conn.execute(
                        "DELETE FROM monitor_leases WHERE name='monitor' AND lease_owner=? AND run_slot=?",
                        (self.owner, slot),
                    )

    def _baseline_targets(self, conn: sqlite3.Connection, targets: Iterable[NewsTarget]) -> set[str]:
        target_list = list(targets)
        keys = [key for target in target_list for key in _baseline_keys(target)]
        if not keys:
            return set()
        placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            f"SELECT key FROM monitor_state WHERE key IN ({placeholders})", keys
        ).fetchall()
        present = {row["key"] for row in rows}
        return {
            target.key
            for target in target_list
            if present.intersection(_baseline_keys(target))
        }

    def _seen_keys(self, conn: sqlite3.Connection, articles: Iterable[NewsArticle]) -> set[str]:
        article_list = list(articles)
        keys = list(
            dict.fromkeys(
                key
                for article in article_list
                for key in (article.key, *article.legacy_keys)
            )
        )
        if not keys:
            return set()
        placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            f"SELECT article_key FROM seen_articles WHERE article_key IN ({placeholders})", keys
        ).fetchall()
        stored = {row["article_key"] for row in rows}
        return {
            article.key
            for article in article_list
            if stored.intersection((article.key, *article.legacy_keys))
        }

    def _record_baseline(
        self,
        *,
        now: datetime,
        slot: str,
        settings: MonitorSettings,
        targets: Iterable[NewsTarget],
        articles: Iterable[NewsArticle],
    ) -> int:
        article_list = list(articles)
        target_list = list(targets)
        with open_connection(self.db) as conn:
            with write_transaction(conn):
                self._assert_lease(conn, slot, now)
                conn.executemany(
                    """INSERT INTO seen_articles(article_key, seen_at) VALUES (?, ?)
                       ON CONFLICT(article_key) DO UPDATE SET seen_at=excluded.seen_at""",
                    [(article.key, _iso(now)) for article in article_list],
                )
                conn.executemany(
                    """INSERT INTO monitor_state(key, value, updated_at) VALUES (?, '1', ?)
                       ON CONFLICT(key) DO UPDATE SET value='1', updated_at=excluded.updated_at""",
                    [(_baseline_key(target.key), _iso(now)) for target in target_list],
                )
                conn.execute(
                    "DELETE FROM seen_articles WHERE seen_at < ?",
                    (_iso(now - timedelta(days=settings.seen_retention_days)),),
                )
        return len(article_list)

    def _persist_analysis(
        self,
        article: NewsArticle,
        analysis: AnalysisResult,
        settings: MonitorSettings,
        now: datetime,
        slot: str,
    ) -> bool:
        payload = {
            "event": "news.analysis",
            "idempotency_key": article.key,
            "config_version": settings.config_version,
            "article": {
                "key": article.key,
                "title": article.title,
                "url": article.url,
                "published_at": article.published_at.isoformat(),
                "summary": article.summary,
                "source": article.source,
                "target_keys": list(article.target_keys),
            },
            "analysis": analysis.to_dict(),
            "target_names": [target.name for target in settings.targets if target.key in article.target_keys],
        }
        with open_connection(self.db) as conn:
            with write_transaction(conn):
                self._assert_lease(conn, slot, now)
                conn.execute(
                    """INSERT INTO seen_articles(article_key, seen_at) VALUES (?, ?)
                       ON CONFLICT(article_key) DO UPDATE SET seen_at=excluded.seen_at""",
                    (article.key, _iso(now)),
                )
                if analysis.level not in settings.push_levels:
                    return False
                cursor = conn.execute(
                    """INSERT INTO outbox(
                           event_key, payload_json, status, attempts, available_at, created_at
                       ) VALUES (?, ?, 'pending', 0, ?, ?)
                       ON CONFLICT(event_key) DO NOTHING""",
                    (
                        article.key,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        _iso(now),
                        _iso(now),
                    ),
                )
                return cursor.rowcount == 1

    def run_once(
        self,
        *,
        now: datetime | None = None,
        force: bool = False,
        dry_run: bool = False,
        backfill_hours: int | None = None,
        backfill_target: str | None = None,
    ) -> MonitorRunResult:
        started = time.monotonic()
        current = as_shanghai(now or datetime.now(tz=SHANGHAI))
        def checkpoint():
            return current + timedelta(seconds=time.monotonic() - started)
        settings = self.settings()
        if backfill_hours is not None:
            if not force or not 1 <= backfill_hours <= 24 or not backfill_target:
                raise ValueError("backfill requires an explicit manual target and 1..24 hours")
            selected = [target for target in settings.targets if backfill_target in {target.key, target.name, *target.queries}]
            if len(selected) != 1:
                raise ValueError("backfill target must uniquely identify one enabled target")
            settings = replace(settings, targets=(replace(selected[0], effective_at=current-timedelta(hours=backfill_hours)),))
        self.last_settings = settings
        if not settings.enabled and not force:
            return self._result(
                started,
                settings,
                status="skipped", slot=None, reason="monitor_disabled", dry_run=dry_run
            )
        if not settings.targets:
            return self._result(
                started,
                settings,
                status="skipped",
                slot=None,
                reason="no_targets",
                dry_run=dry_run,
            )

        if force:
            # Manual runs do not consume scheduled slots. The singleton lease
            # prevents overlap with every other manual or scheduled run.
            slot = f"manual:{current.isoformat(timespec='seconds')}:{uuid.uuid4().hex[:8]}"
        else:
            slot = slot_for_time(current)
            if slot is None:
                return self._result(
                    started,
                    settings,
                    status="skipped", slot=None, reason="outside_schedule", dry_run=dry_run
                )
        if not dry_run and not self._claim(slot, current, settings.run_lease_seconds):
            return self._result(
                started,
                settings,
                status="skipped", slot=slot, reason="slot_or_monitor_already_claimed"
            )

        fetched: list[NewsArticle] = []
        successful_targets: list[NewsTarget] = []
        errors: list[str] = []
        # All network access occurs after the claim transaction has committed.
        for target in settings.targets:
            if time.monotonic() - started >= settings.run_timeout_seconds:
                errors.append("fetch deadline: TimeoutError")
                break
            try:
                fetched.extend(self.fetcher.fetch_target(target, now=current))
                successful_targets.append(target)
            except Exception as exc:  # isolate one upstream target; never include response bodies/secrets
                errors.append(f"fetch {target.key}: {type(exc).__name__}")
            if not dry_run:
                try:
                    self._renew(slot, checkpoint(), settings.run_lease_seconds)
                except RunLeaseLost as exc:
                    return self._result(
                        started,
                        settings,
                        status="failed",
                        slot=slot,
                        reason="lease_lost",
                        fetched=len(fetched),
                        errors=(*errors, f"lease: {type(exc).__name__}"),
                    )
        if not successful_targets:
            message = "; ".join(errors) or "all target fetches failed"
            if not dry_run:
                self._finish(slot, checkpoint(), "failed", message)
            return self._result(
                started,
                settings,
                status="failed", slot=slot, fetched=0, errors=tuple(errors), dry_run=dry_run
            )

        effective_articles = apply_effective_at(fetched, settings.targets)
        merged_articles = merge_articles(effective_articles)
        merged_duplicates = max(0, len(effective_articles) - len(merged_articles))
        all_eligible = eligible_articles(
            merged_articles,
            now=current,
            lookback_hours=settings.lookback_hours,
            future_tolerance_hours=settings.future_tolerance_hours,
            limit=None,
        )
        if dry_run or backfill_hours is not None:
            # Dry-run is diagnostic: exercise analysis on the current window,
            # but do not create baseline/seen/run/outbox records.
            established = {target.key for target in successful_targets}
        else:
            with open_connection(self.db) as conn:
                established = self._baseline_targets(conn, successful_targets)

        new_targets = [target for target in successful_targets if target.key not in established]
        historical_baseline_keys = {
            target.key
            for target in new_targets
            if target.effective_at is None
            or as_shanghai(target.effective_at)
            <= datetime(1970, 1, 2, tzinfo=timezone.utc)
        }
        live_new_keys = {target.key for target in new_targets} - historical_baseline_keys
        baseline_only = [
            article
            for article in all_eligible
            if article.target_keys
            and set(article.target_keys).issubset(historical_baseline_keys)
        ]
        baseline_count = 0
        if new_targets and not dry_run:
            # Mark every in-window baseline row, not just the display cap, so a
            # first run cannot leak older rows over subsequent cycles.
            try:
                baseline_count = self._record_baseline(
                    now=checkpoint(),
                    slot=slot,
                    settings=settings,
                    targets=new_targets,
                    articles=baseline_only,
                )
            except RunLeaseLost as exc:
                return self._result(
                    started,
                    settings,
                    status="failed",
                    slot=slot,
                    reason="lease_lost",
                    fetched=len(fetched),
                    eligible=len(all_eligible),
                    errors=(*errors, f"lease: {type(exc).__name__}"),
                )

        with open_connection(self.db) as conn:
            if not dry_run:
                ensure_monitor_schema(conn)
            try:
                seen = self._seen_keys(conn, all_eligible)
            except sqlite3.OperationalError:
                if not dry_run:
                    raise
                seen = set()
        if backfill_hours is not None:
            # Explicitly acknowledged replay may analyze baseline items again.
            # Existing outbox event keys still prevent re-queuing old alerts.
            seen = set()
        unseen_candidates = [
            article
            for article in all_eligible
            if article.key not in seen
            and any(
                target_key in established or target_key in live_new_keys
                for target_key in article.target_keys
            )
        ]
        candidates = unseen_candidates[: settings.max_articles]
        deduplicated = merged_duplicates + len(seen)

        analyzed = 0
        queued = 0
        by_key = {target.key: target for target in settings.targets}
        for article in candidates:
            if time.monotonic() - started >= settings.run_timeout_seconds:
                errors.append("analyze deadline: TimeoutError")
                break
            selected = [by_key[key] for key in article.target_keys if key in by_key]
            try:
                analysis = self.analyzer.analyze(article, selected)
                analyzed += 1
                if dry_run:
                    queued += int(analysis.level in settings.push_levels)
                else:
                    self._renew(slot, checkpoint(), settings.run_lease_seconds)
                    queued += int(
                        self._persist_analysis(article, analysis, settings, checkpoint(), slot)
                    )
            except RunLeaseLost as exc:
                return self._result(
                    started,
                    settings,
                    status="failed",
                    slot=slot,
                    reason="lease_lost",
                    fetched=len(fetched),
                    eligible=len(all_eligible),
                    analyzed=analyzed,
                    queued=queued,
                    baseline_seen=baseline_count,
                    errors=(*errors, f"lease: {type(exc).__name__}"),
                    dry_run=dry_run,
                    deduplicated=deduplicated,
                )
            except Exception as exc:
                errors.append(f"analyze {article.key[:12]}: {type(exc).__name__}")

        if not dry_run:
            try:
                with open_connection(self.db) as conn:
                    with write_transaction(conn):
                        self._assert_lease(conn, slot, checkpoint())
                        conn.execute(
                            "DELETE FROM seen_articles WHERE seen_at < ?",
                            (_iso(current - timedelta(days=settings.seen_retention_days)),),
                        )
            except RunLeaseLost as exc:
                return self._result(
                    started,
                    settings,
                    status="failed",
                    slot=slot,
                    reason="lease_lost",
                    fetched=len(fetched),
                    eligible=len(all_eligible),
                    analyzed=analyzed,
                    queued=queued,
                    baseline_seen=baseline_count,
                    errors=(*errors, f"lease: {type(exc).__name__}"),
                    deduplicated=deduplicated,
                )
            db_status = "partial" if errors else "succeeded"
            self._finish(slot, checkpoint(), db_status, "; ".join(errors) or None)
        result_status = "partial" if errors else "succeeded"
        return self._result(
            started,
            settings,
            status=result_status,
            slot=slot,
            fetched=len(fetched),
            eligible=len(all_eligible),
            analyzed=analyzed,
            queued=queued,
            baseline_seen=baseline_count,
            errors=tuple(errors),
            dry_run=dry_run,
            deduplicated=deduplicated,
        )


@dataclass(frozen=True, slots=True)
class OutboxItem:
    id: int
    event_key: str
    payload: Mapping[str, Any]
    attempts: int


class OutboxStore:
    """Lease-based at-least-once delivery queue backed by SQLite."""

    def __init__(self, db: DbSource) -> None:
        self.db = db

    def claim_due(
        self,
        *,
        now: datetime,
        owner: str,
        limit: int = 10,
        lease_seconds: int = 60,
    ) -> list[OutboxItem]:
        current = _iso(now)
        locked_until = _iso(now + timedelta(seconds=max(10, lease_seconds)))
        with open_connection(self.db) as conn:
            ensure_monitor_schema(conn)
            with write_transaction(conn):
                rows = conn.execute(
                    """SELECT id, event_key, payload_json, attempts
                       FROM outbox
                       WHERE available_at <= ?
                         AND (status='pending' OR (status='sending' AND locked_until <= ?))
                       ORDER BY id
                       LIMIT ?""",
                    (current, current, max(1, limit)),
                ).fetchall()
                ids = [row["id"] for row in rows]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    conn.execute(
                        f"""UPDATE outbox
                            SET status='sending', lease_owner=?, locked_until=?, attempts=attempts+1
                            WHERE id IN ({placeholders})""",
                        (owner, locked_until, *ids),
                    )
        return [
            OutboxItem(
                id=row["id"],
                event_key=row["event_key"],
                payload=json.loads(row["payload_json"]),
                attempts=row["attempts"] + 1,
            )
            for row in rows
        ]

    def mark_sent(self, item_id: int, *, owner: str, now: datetime) -> bool:
        with open_connection(self.db) as conn:
            with write_transaction(conn):
                cursor = conn.execute(
                    """UPDATE outbox
                       SET status='sent', sent_at=?, locked_until=NULL, lease_owner=NULL, last_error=NULL
                       WHERE id=? AND status='sending' AND lease_owner=?""",
                    (_iso(now), item_id, owner),
                )
                return cursor.rowcount == 1

    def mark_failed(
        self,
        item_id: int,
        *,
        owner: str,
        now: datetime,
        error: str,
        attempts: int,
        max_attempts: int = 5,
    ) -> bool:
        dead = attempts >= max_attempts
        delay = min(3600, 30 * (2 ** max(0, attempts - 1)))
        with open_connection(self.db) as conn:
            with write_transaction(conn):
                cursor = conn.execute(
                    """UPDATE outbox
                       SET status=?, available_at=?, locked_until=NULL, lease_owner=NULL, last_error=?
                       WHERE id=? AND status='sending' AND lease_owner=?""",
                    (
                        "dead" if dead else "pending",
                        _iso(now + timedelta(seconds=delay)),
                        error[:1000],
                        item_id,
                        owner,
                    ),
                )
                return cursor.rowcount == 1

    def retry_dead(
        self,
        *,
        now: datetime,
        event_key: str | None = None,
    ) -> int:
        """Explicitly requeue dead records for an operator retry command."""

        with open_connection(self.db) as conn:
            ensure_monitor_schema(conn)
            with write_transaction(conn):
                if event_key is None:
                    cursor = conn.execute(
                        """UPDATE outbox
                           SET status='pending', attempts=0, available_at=?, locked_until=NULL,
                               lease_owner=NULL, last_error=NULL
                           WHERE status='dead'""",
                        (_iso(now),),
                    )
                else:
                    cursor = conn.execute(
                        """UPDATE outbox
                           SET status='pending', attempts=0, available_at=?, locked_until=NULL,
                               lease_owner=NULL, last_error=NULL
                           WHERE status='dead' AND event_key=?""",
                        (_iso(now), event_key),
                    )
                return cursor.rowcount


__all__ = [
    "DbSource",
    "MonitorRunResult",
    "MonitorService",
    "MonitorSettings",
    "OutboxItem",
    "OutboxStore",
    "RunLeaseLost",
    "apply_effective_at",
    "eligible_articles",
    "ensure_monitor_schema",
    "merge_articles",
    "open_connection",
    "write_transaction",
]
