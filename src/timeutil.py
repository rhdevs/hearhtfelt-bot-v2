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
    """Render a timestamp as HH:MM for channel posts, never raising.

    A missing or unusable timestamp renders as "--:--" rather than blowing up
    mid-post and leaving a request unannounced.
    """
    aware = ensure_aware_utc(value)
    if aware is None:
        return "--:--"
    return aware.strftime("%H:%M")


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
