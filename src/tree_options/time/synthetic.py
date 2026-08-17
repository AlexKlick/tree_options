"""Deterministic synthetic calendar for TESTS ONLY.

Contiguous weekdays starting at `start_date`. Holiday structure is irrelevant
for the properties under test; realism comes from the committed NYSE fixture.
No production module may import this file (enforced by test).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from tree_options.time.calendar import NotASessionError
from tree_options.time.sessions import require_utc, session_close_instant, session_open_instant


class SyntheticCalendar:
    def __init__(self, start_date: date, n_sessions: int) -> None:
        if n_sessions < 1:
            raise ValueError("n_sessions must be >= 1")
        # Naive weekday generation is allowed HERE and ONLY here: this class
        # is a test double, never a production calendar.
        sessions: list[date] = []
        cursor = start_date
        while len(sessions) < n_sessions:
            if cursor.weekday() < 5:
                sessions.append(cursor)
            cursor = cursor + timedelta(days=1)
        self._sessions = tuple(sessions)
        self._ordinals = {d: i for i, d in enumerate(sessions)}
        self.name = "SYNTHETIC"

    def sessions(self) -> tuple[date, ...]:
        return self._sessions

    def is_session(self, d: date) -> bool:
        return d in self._ordinals

    def ordinal(self, d: date) -> int:
        try:
            return self._ordinals[d]
        except KeyError:
            raise NotASessionError(f"{d} is not a session in SYNTHETIC") from None

    def nth_after(self, d: date, n: int) -> date:
        if n < 0:
            raise ValueError(f"nth_after requires n >= 0, got {n}")
        idx = self.ordinal(d) + n
        if idx >= len(self._sessions):
            raise NotASessionError(f"no session {n} after {d}")
        return self._sessions[idx]

    def session_open(self, d: date) -> datetime:
        self.ordinal(d)
        return session_open_instant(d)

    def session_close(self, d: date) -> datetime:
        self.ordinal(d)
        return session_close_instant(d)

    def contains_instant(self, d: date, ts: datetime) -> bool:
        ts = require_utc(ts, what="execution timestamp")
        return self.session_open(d) <= ts <= self.session_close(d)
