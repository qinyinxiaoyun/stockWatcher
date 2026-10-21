"""Schedule calculations are independent of the worker host's timezone."""

from datetime import datetime, timezone

import pytest

from stock_watch.scheduler import (
    SHANGHAI,
    as_shanghai,
    next_slot_at,
    seconds_until_next_slot,
    slot_datetime,
    slot_for_time,
)


@pytest.mark.parametrize(
    ("utc_time", "expected"),
    [
        ("2026-10-21T00:59:59+00:00", None),
        ("2026-10-21T01:00:00+00:00", "2026-10-21T09:00+0800"),
        ("2026-10-21T01:04:59+00:00", "2026-10-21T09:00+0800"),
        ("2026-10-21T06:55:00+00:00", "2026-10-21T14:55+0800"),
        ("2026-10-21T06:59:59+00:00", "2026-10-21T14:55+0800"),
        ("2026-10-21T07:00:00+00:00", "2026-10-21T15:00+0800"),
        ("2026-10-21T07:04:59+00:00", "2026-10-21T15:00+0800"),
        ("2026-10-21T07:05:00+00:00", None),
        ("2026-10-24T01:00:00+00:00", None),
        ("2026-10-25T06:55:00+00:00", None),
        ("2026-10-26T01:00:00+00:00", "2026-10-26T09:00+0800"),
    ],
)
def test_due_slots_use_shanghai_wall_time_on_utc_host(utc_time, expected):
    instant = datetime.fromisoformat(utc_time)
    assert instant.tzinfo == timezone.utc
    assert slot_for_time(instant) == expected
    slot = slot_datetime(instant)
    if slot is not None:
        assert slot.tzinfo == SHANGHAI
        assert slot.second == slot.microsecond == 0


@pytest.mark.parametrize(
    ("local_time", "expected"),
    [
        ("2026-10-21T08:59:00", "2026-10-21T09:00:00"),
        ("2026-10-21T09:00:00", "2026-10-21T09:00:00"),
        ("2026-10-21T14:55:01", "2026-10-21T15:00:00"),
        ("2026-10-21T15:00:00", "2026-10-21T15:00:00"),
        ("2026-10-21T15:00:00.000001", "2026-10-22T09:00:00"),
        ("2026-10-23T15:05:00", "2026-10-26T09:00:00"),
        ("2026-10-24T09:00:00", "2026-10-26T09:00:00"),
    ],
)
def test_next_slot_rounds_forward_and_crosses_weekend(local_time, expected):
    local = datetime.fromisoformat(local_time).replace(tzinfo=SHANGHAI)
    wanted = datetime.fromisoformat(expected).replace(tzinfo=SHANGHAI)
    assert next_slot_at(local.astimezone(timezone.utc)) == wanted
    assert seconds_until_next_slot(local) == (wanted - local).total_seconds()


def test_naive_input_is_shanghai_and_timezone_conversion_preserves_instant():
    naive = datetime(2026, 10, 21, 9)
    assert as_shanghai(naive) == naive.replace(tzinfo=SHANGHAI)
    assert slot_for_time(naive) == slot_for_time(
        datetime(2026, 10, 21, 1, tzinfo=timezone.utc)
    )
