"""Session instant helpers and timezone constants.

All instants are tz-aware UTC datetimes. Session wall times live in the
exchange timezone (America/New_York); converting wall time to an instant is
DST-correct via zoneinfo and happens ONLY in this package.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

SESSION_TIMEZONE = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)


def session_open_instant(session_date) -> datetime:
    """09:30 America/New_York on the session date, as a UTC instant."""
    local = datetime.combine(session_date, SESSION_OPEN, tzinfo=SESSION_TIMEZONE)
    return local.astimezone(UTC)


def session_close_instant(session_date) -> datetime:
    """16:00 America/New_York on the session date, as a UTC instant."""
    local = datetime.combine(session_date, SESSION_CLOSE, tzinfo=SESSION_TIMEZONE)
    return local.astimezone(UTC)


def require_utc(ts: datetime, *, what: str = "timestamp") -> datetime:
    """Reject naive timestamps outright; normalize aware ones to UTC."""
    if ts.tzinfo is None:
        raise ValueError(f"naive {what} rejected: {ts!r}; tz-aware UTC required")
    return ts.astimezone(UTC)
