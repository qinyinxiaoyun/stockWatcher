"""Worker-cycle composition; durable delivery lives in ``delivery.py``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import threading
from typing import Callable, Protocol

from .monitor import MonitorRunResult, MonitorService
from .scheduler import SHANGHAI, as_shanghai


class DeliveryDispatcher(Protocol):
    """The narrow interface implemented by ``delivery.DeliveryDispatcher``."""

    def deliver_one(self, now: datetime | None = None) -> bool: ...


@dataclass(frozen=True, slots=True)
class WorkerCycleResult:
    monitor: MonitorRunResult | None = None
    delivery_processed: bool = False
    management_processed: bool = False


def run_worker_once(
    service: MonitorService | None,
    dispatcher: DeliveryDispatcher | None = None,
    *,
    now: datetime | None = None,
    force: bool = False,
    dry_run: bool = False,
    callback_service=None,
) -> WorkerCycleResult:
    """Run one monitor/dispatch cycle without enabling monitoring implicitly."""

    # MONITOR_ENABLED gates only automatic scans. Management and durable
    # receipts must continue to work while automatic monitoring is paused.
    management_processed = bool(callback_service.run_one()) if callback_service and not dry_run else False
    # Candidates can become due while automatic scans are disabled or outside
    # trading slots. The worker remains the only background composition point.
    database = getattr(getattr(dispatcher, 'settings', None), 'database_path', None)
    if database is None:
        database = getattr(service, 'db', None)
    if database is not None and not dry_run:
        from .notifications import flush_notifications
        flush_notifications(database, now=now)
    delivery_processed = bool(dispatcher.deliver_one(now=now)) if dispatcher and not dry_run else False
    # Re-read wall clock at the scan boundary. A long management/delivery task
    # must not carry a stale 15:04 timestamp across the 15:05 cutoff.
    monitor_result = service.run_once(now=now, force=force, dry_run=dry_run) if service else None
    return WorkerCycleResult(
        monitor=monitor_result, delivery_processed=delivery_processed, management_processed=management_processed
    )


def run_worker_loop(
    service: MonitorService,
    dispatcher: DeliveryDispatcher | None = None,
    *,
    stop_event: threading.Event | None = None,
    poll_seconds: float = 15.0,
    clock: Callable[[], datetime] | None = None,
    on_cycle: Callable[[WorkerCycleResult], None] | None = None,
) -> None:
    """Poll until stopped; never starts automatically on import or in HTTP."""

    stopper = stop_event or threading.Event()
    delay = max(0.1, poll_seconds)
    while not stopper.is_set():
        result = run_worker_once(service, dispatcher, now=clock() if clock else None)
        if on_cycle is not None:
            on_cycle(result)
        stopper.wait(delay)


worker_once = run_worker_once
worker_loop = run_worker_loop


__all__ = [
    "DeliveryDispatcher",
    "WorkerCycleResult",
    "run_worker_loop",
    "run_worker_once",
    "worker_loop",
    "worker_once",
]
