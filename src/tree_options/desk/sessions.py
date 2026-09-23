"""Session arithmetic for the desk jobs, on the injected NYSE calendar.

Everything slices ``cal.sessions()`` (bisect on the sorted tuple); there is
no weekday or timedelta arithmetic here (the repo's AST guard bans it
outside ``time/``). Calendar-day distances use epoch math.
"""

from __future__ import annotations

import bisect
from datetime import UTC, date, datetime, time
from typing import Protocol

from tree_options.trex.clock import ET

# A session's data counts as published once its close has settled: the
# trex monitor's session end, 15 minutes after the regular close.
CUTOFF = time(16, 15)
DAY_S = 86_400


class Calendar(Protocol):
    def sessions(self) -> tuple[date, ...]: ...
    def is_session(self, d: date) -> bool: ...
    def ordinal(self, d: date) -> int: ...
    def nth_after(self, d: date, n: int) -> date: ...


def cutoff_instant(d: date) -> datetime:
    """16:15 America/New_York on ``d`` (aware; DST-correct via zoneinfo)."""
    return datetime.combine(d, CUTOFF, tzinfo=ET)


def latest_completed_session(now: datetime, cal: Calendar) -> date:
    """The latest session whose 16:15 ET cutoff is at or before ``now``."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    sessions = cal.sessions()
    today = now.astimezone(ET).date()
    i = bisect.bisect_right(sessions, today) - 1
    while i >= 0 and cutoff_instant(sessions[i]) > now:
        i -= 1
    if i < 0:
        raise ValueError(f"no completed session in the calendar before {now.isoformat()}")
    return sessions[i]


def previous_session(d: date, cal: Calendar) -> date | None:
    sessions = cal.sessions()
    i = bisect.bisect_left(sessions, d) - 1
    return sessions[i] if i >= 0 else None


def first_session_after(d: date, cal: Calendar) -> date | None:
    """The first session strictly after ``d`` (``d`` need not be a session)."""
    sessions = cal.sessions()
    i = bisect.bisect_right(sessions, d)
    return sessions[i] if i < len(sessions) else None


def sessions_after(after: date, through: date, cal: Calendar) -> list[date]:
    """Sessions in ``(after, through]``."""
    sessions = cal.sessions()
    lo = bisect.bisect_right(sessions, after)
    hi = bisect.bisect_right(sessions, through)
    return list(sessions[lo:hi])


def is_first_session_of_month(d: date, cal: Calendar) -> bool:
    if not cal.is_session(d):
        return False
    prev = previous_session(d, cal)
    return prev is None or (prev.year, prev.month) != (d.year, d.month)


def _epoch_day(iso: str) -> float:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp()


def calendar_days_between(a: str, b: str) -> float:
    """Calendar days from ISO date ``a`` to ``b`` (epoch math)."""
    return (_epoch_day(b) - _epoch_day(a)) / DAY_S
