"""ET session helpers for trex.

trex keeps all market logic in US/Eastern (the exchange's clock) and renders
local time only for humans. Session days come from a checksummed static
NYSE calendar — never naive weekday arithmetic (the protocol bans that
outside ``time/``, and holidays matter: deadlines land on real sessions).

trex reads its OWN execution calendar (``data/calendar/trex/``, generated
by ``scripts/gen_trex_calendar.py`` through 2028), not the protocol's: that
one ends 2026-12-31 and is sealed for research, and past its last session
every tick is a non-session, i.e. no exits at all. The two agree on every
session they share (tests/unit/test_trex_calendar.py). ``TREX_CALENDAR``
overrides the path; the monitor's health warns inside the last
CALENDAR_HORIZON_WARN_SESSIONS sessions.
"""

from __future__ import annotations

import bisect
import os
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from tree_options.time.calendar import StaticSessionCalendar

ET = ZoneInfo("America/New_York")

_CALENDAR_REL = Path("data/calendar/trex/nyse_sessions_2018_01_02_2028_12_29.json")
CALENDAR_HORIZON_WARN_SESSIONS = 60  # about three months of sessions

_calendar: StaticSessionCalendar | None = None


def now_et() -> datetime:
    """Current ET wall-clock time (aware)."""
    return datetime.now(ET)


def session_calendar() -> StaticSessionCalendar:
    """The repo's checksummed NYSE calendar (cached per process)."""
    global _calendar
    if _calendar is None:
        env = os.environ.get("TREX_CALENDAR")
        base = Path(env) if env else Path(__file__).resolve().parents[3] / _CALENDAR_REL
        _calendar = StaticSessionCalendar(base, base.with_suffix(".sha256"))
    return _calendar


def is_session(dt: datetime) -> bool:
    """True if dt's date is an NYSE session per the static calendar."""
    return session_calendar().is_session(dt.date())


def calendar_last_session() -> date:
    """The last session the calendar knows: past it, trex sees no sessions."""
    return session_calendar().sessions()[-1]


def calendar_sessions_left(d: date) -> int:
    """Sessions strictly after ``d`` that the calendar still covers."""
    sessions = session_calendar().sessions()
    return len(sessions) - bisect.bisect_right(sessions, d)


def calendar_horizon_warn(d: date) -> bool:
    """True inside the last CALENDAR_HORIZON_WARN_SESSIONS sessions:
    regenerate with scripts/gen_trex_calendar.py before it runs out."""
    return calendar_sessions_left(d) < CALENDAR_HORIZON_WARN_SESSIONS


class EntryWindow:
    """The ET time window in which new entries may be placed.

    ``start`` skips the opening auction noise (quarterly OpEx Friday in
    particular); ``end`` stops chasing before midday flows.
    """

    __slots__ = ("end", "start")

    def __init__(self, start: time, end: time) -> None:
        if start >= end:
            raise ValueError(f"entry window start {start} must precede end {end}")
        self.start = start
        self.end = end

    def before(self, dt: datetime) -> bool:
        return dt.time() < self.start

    def after(self, dt: datetime) -> bool:
        return dt.time() > self.end

    def inside(self, dt: datetime) -> bool:
        return self.start <= dt.time() <= self.end
