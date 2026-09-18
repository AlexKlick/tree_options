"""ET session helpers for trex.

trex keeps all market logic in US/Eastern (the exchange's clock) and renders
local time only for humans. Session days come from the repo's checksummed
static NYSE calendar — never naive weekday arithmetic (the protocol bans
that outside ``time/``, and holidays matter: deadlines land on real
sessions).
"""

from __future__ import annotations

import os
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from tree_options.time.calendar import StaticSessionCalendar

ET = ZoneInfo("America/New_York")

_CALENDAR_REL = Path("data/calendar/nyse_sessions_2018_01_02_2026_12_31.json")

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
