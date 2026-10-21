"""Deterministic Asia/Shanghai monitor schedule.

This module calculates due slots only.  It intentionally does not start
threads, cron jobs or background schedulers; :mod:`stock_watch.worker` is the
single runtime entry point for background work.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
OPEN_TIME = time(9, 0)
LAST_SLOT_TIME = time(15, 0)
CUTOFF_TIME = time(15, 5)
SLOT_MINUTES = 5


def shanghai_now() -> datetime:
    return datetime.now(tz=SHANGHAI)


def as_shanghai(value: datetime) -> datetime:
    """Normalize an instant; naive values are treated as Shanghai wall time."""

    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI)


def is_weekday(value: datetime) -> bool:
    return as_shanghai(value).weekday() < 5


def slot_datetime(value: datetime | None = None) -> datetime | None:
    """Return the most recent due five-minute slot, if still runnable.

    The five-minute grace means a worker waking at 10:03 can still claim the
    10:00 slot.  The last slot is 15:00 and is unavailable from 15:05 onward.
    Public-holiday handling is deliberately out of scope; weekends are skipped.
    """

    local = as_shanghai(value or shanghai_now())
    wall = local.timetz().replace(tzinfo=None)
    if local.weekday() >= 5 or wall < OPEN_TIME or wall >= CUTOFF_TIME:
        return None
    floored_minute = local.minute - local.minute % SLOT_MINUTES
    slot = local.replace(minute=floored_minute, second=0, microsecond=0)
    if slot.timetz().replace(tzinfo=None) > LAST_SLOT_TIME:
        return None
    return slot


def slot_for_time(value: datetime | None = None) -> str | None:
    slot = slot_datetime(value)
    return slot.strftime("%Y-%m-%dT%H:%M%z") if slot else None


def next_slot_at(value: datetime | None = None) -> datetime:
    """Return the next exact scheduled slot at or after ``value``."""

    local = as_shanghai(value or shanghai_now())
    candidate = local.replace(second=0, microsecond=0)
    if local.second or local.microsecond or candidate.minute % SLOT_MINUTES:
        minutes = (SLOT_MINUTES - candidate.minute % SLOT_MINUTES) % SLOT_MINUTES
        if not minutes and (local.second or local.microsecond):
            minutes = SLOT_MINUTES
        candidate += timedelta(minutes=minutes)
        candidate = candidate.replace(second=0, microsecond=0)

    for _ in range(8 * 24 * 12):  # more than one full week of five-minute ticks
        wall = candidate.timetz().replace(tzinfo=None)
        if candidate.weekday() < 5 and OPEN_TIME <= wall <= LAST_SLOT_TIME:
            return candidate
        candidate += timedelta(minutes=SLOT_MINUTES)
    raise RuntimeError("could not find the next weekday schedule slot")


def seconds_until_next_slot(value: datetime | None = None) -> float:
    local = as_shanghai(value or shanghai_now())
    return max(0.0, (next_slot_at(local) - local).total_seconds())


__all__ = [
    "CUTOFF_TIME",
    "LAST_SLOT_TIME",
    "OPEN_TIME",
    "SHANGHAI",
    "SLOT_MINUTES",
    "as_shanghai",
    "is_weekday",
    "next_slot_at",
    "seconds_until_next_slot",
    "shanghai_now",
    "slot_datetime",
    "slot_for_time",
]
