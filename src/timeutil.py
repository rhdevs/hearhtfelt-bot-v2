"""Time helpers shared across the bot.

Convention: EVERY datetime in this process is timezone-aware UTC. Nothing here
may import `config` or anything under `src.database` -- those import this module,
and a cycle would break at boot.

Why aware rather than naive-UTC: a conversion site we missed raises
`TypeError: can't compare offset-naive and offset-aware datetimes` immediately,
instead of silently skewing by the developer's UTC offset (invisible on the UTC
droplet, wrong on an SGT laptop). It also retires
`datetime.datetime.utcnow()`, deprecated since Python 3.12.
"""

import datetime
from typing import Any, Optional

UTC = datetime.timezone.utc

# What people READ. Everything is stored and compared in UTC; only the displayed
# clock is local. Raffles Hall is in Singapore, which has had no daylight saving
# since 1982, so a fixed +08:00 is exact -- and needs no tzdata in the container.
DISPLAY_TZ = datetime.timezone(datetime.timedelta(hours=8), "SGT")


def utcnow() -> datetime.datetime:
    """The single source of 'now': timezone-aware UTC."""
    return datetime.datetime.now(UTC)


def ensure_aware_utc(value: Any) -> Optional[datetime.datetime]:
    """Coerce a value to an aware UTC datetime, or None if it isn't one.

    A naive datetime is assumed to be UTC: every naive value that can reach this
    function was written by `datetime.datetime.utcnow()` (Mongo documents) or by
    a pre-migration in-memory write. That assumption is correct for the former
    and fail-safe for the latter.
    """
    if value is None:
        return None
    if not isinstance(value, datetime.datetime):
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def format_hhmm(value: Any) -> str:
    """Render a timestamp as HH:MM Singapore time for channel posts, never raising.

    Singapore, not UTC: the droplet runs on UTC, so a request made at 11:10 PM
    used to read "Time: 15:10" -- eight hours behind the people reading it.

    A missing or unusable timestamp renders as "--:--" rather than blowing up
    mid-post and leaving a request unannounced.
    """
    aware = ensure_aware_utc(value)
    if aware is None:
        return "--:--"
    return aware.astimezone(DISPLAY_TZ).strftime("%H:%M")


def format_duration_minutes(minutes: int) -> str:
    """Human-readable duration for requester-facing copy.

    1 -> "1 minute", 90 -> "1 hour 30 minutes", 1440 -> "24 hours".
    """
    try:
        total = int(minutes)
    except (TypeError, ValueError):
        return "less than a minute"

    if total <= 0:
        return "less than a minute"

    hours, mins = divmod(total, 60)
    parts = []
    if hours:
        parts.append("1 hour" if hours == 1 else f"{hours} hours")
    if mins:
        parts.append("1 minute" if mins == 1 else f"{mins} minutes")
    return " ".join(parts)
