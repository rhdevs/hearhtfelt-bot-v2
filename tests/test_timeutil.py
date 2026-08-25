#!/usr/bin/env python3
"""
Tests for src/timeutil.py -- the aware-UTC convention the rest of the codebase
depends on. Run directly: `python tests/test_timeutil.py`.
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.timeutil import UTC, ensure_aware_utc, format_duration_minutes, format_hhmm, utcnow


def test_utcnow_is_aware_utc():
    now = utcnow()
    assert now.tzinfo is not None, "utcnow() must be timezone-aware"
    assert now.utcoffset() == datetime.timedelta(0), "utcnow() must be UTC"


def test_ensure_aware_utc_naive_is_treated_as_utc():
    naive = datetime.datetime(2026, 3, 1, 14, 30, 0)
    aware = ensure_aware_utc(naive)
    assert aware.tzinfo is not None
    assert aware.utcoffset() == datetime.timedelta(0)
    # Same wall clock, now labelled UTC -- no shifting.
    assert (aware.year, aware.month, aware.day, aware.hour, aware.minute) == (2026, 3, 1, 14, 30)


def test_ensure_aware_utc_converts_other_zones():
    sgt = datetime.timezone(datetime.timedelta(hours=8))
    value = datetime.datetime(2026, 3, 1, 14, 30, 0, tzinfo=sgt)
    aware = ensure_aware_utc(value)
    assert aware.utcoffset() == datetime.timedelta(0)
    assert aware.hour == 6 and aware.minute == 30, "14:30 SGT is 06:30 UTC"
    assert aware == value, "the instant must be preserved"


def test_ensure_aware_utc_already_utc_is_unchanged():
    value = datetime.datetime(2026, 3, 1, 14, 30, tzinfo=UTC)
    assert ensure_aware_utc(value) == value


def test_ensure_aware_utc_rejects_non_datetimes():
    assert ensure_aware_utc(None) is None
    assert ensure_aware_utc("garbage") is None
    assert ensure_aware_utc(42) is None
    assert ensure_aware_utc(datetime.date(2026, 3, 1)) is None, "a bare date is not a datetime"


def test_format_duration_minutes():
    cases = {
        0: "less than a minute",
        -5: "less than a minute",
        1: "1 minute",
        5: "5 minutes",
        59: "59 minutes",
        60: "1 hour",
        90: "1 hour 30 minutes",
        120: "2 hours",
        1440: "24 hours",
    }
    for minutes, expected in cases.items():
        got = format_duration_minutes(minutes)
        assert got == expected, f"format_duration_minutes({minutes}) == {got!r}, expected {expected!r}"


def test_format_hhmm():
    assert format_hhmm(None) == "--:--"
    assert format_hhmm("not a datetime") == "--:--"
    assert format_hhmm(datetime.datetime(2026, 3, 1, 9, 5, tzinfo=UTC)) == "09:05"
    # Naive is read as UTC, so the wall clock is preserved.
    assert format_hhmm(datetime.datetime(2026, 3, 1, 9, 5)) == "09:05"
    sgt = datetime.timezone(datetime.timedelta(hours=8))
    assert format_hhmm(datetime.datetime(2026, 3, 1, 9, 5, tzinfo=sgt)) == "01:05"


def test_aware_and_naive_are_not_comparable():
    """The reason we chose aware over naive: a missed site fails loudly."""
    try:
        _ = utcnow() > datetime.datetime(2026, 1, 1)
    except TypeError:
        return
    raise AssertionError("comparing aware and naive datetimes should raise TypeError")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\nAll {len(tests)} timeutil tests passed!")
