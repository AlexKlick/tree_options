"""Trading-calendar abstraction.

`SessionCalendar` is the production interface: all session arithmetic flows
through `ordinal`/`nth_after` on an injected calendar. `ordinal()` raises
`NotASessionError` for unknown dates — there is no fallback, no guessing, and
no naive business-day arithmetic anywhere in production paths (enforced by an
AST scan test).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from tree_options.time.sessions import (
    SESSION_CLOSE,
    SESSION_OPEN,
    SESSION_TIMEZONE,
    require_utc,
    session_close_instant,
    session_open_instant,
)


class CalendarError(RuntimeError):
    """Base class for calendar failures (fail closed)."""


class NotASessionError(CalendarError):
    """The queried date is not a session in this calendar."""


class CalendarIntegrityError(CalendarError):
    """The calendar fixture itself is defective (checksum, ordering, shape)."""


class SessionCalendar(Protocol):
    def sessions(self) -> tuple[date, ...]: ...
    def is_session(self, d: date) -> bool: ...
    def ordinal(self, d: date) -> int: ...
    def nth_after(self, d: date, n: int) -> date: ...
    def session_open(self, d: date) -> datetime: ...
    def session_close(self, d: date) -> datetime: ...
    def contains_instant(self, d: date, ts: datetime) -> bool: ...


class StaticSessionCalendar:
    """Production calendar over a committed, checksummed session list.

    Fails closed on: checksum mismatch, missing file, non-monotonic sessions,
    duplicate sessions, empty session list, unexpected calendar metadata.
    """

    def __init__(
        self,
        json_path: Path | str,
        checksum_path: Path | str,
        *,
        verify_checksum: bool = True,
    ) -> None:
        json_path = Path(json_path)
        checksum_path = Path(checksum_path)

        if not json_path.is_file():
            raise CalendarIntegrityError(f"calendar fixture missing: {json_path}")
        raw = json_path.read_bytes()

        if verify_checksum:
            if not checksum_path.is_file():
                raise CalendarIntegrityError(f"calendar checksum missing: {checksum_path}")
            expected = checksum_path.read_text().split()[0]
            actual = hashlib.sha256(raw).hexdigest()
            if actual != expected:
                raise CalendarIntegrityError(
                    f"calendar fixture checksum mismatch for {json_path.name}: "
                    f"expected {expected}, got {actual}"
                )

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CalendarIntegrityError(f"calendar fixture is not valid JSON: {exc}") from exc

        for key in ("calendar", "timezone", "open", "close", "sessions"):
            if key not in payload:
                raise CalendarIntegrityError(f"calendar fixture missing key: {key!r}")
        if payload["timezone"] != str(SESSION_TIMEZONE):
            raise CalendarIntegrityError(
                f"calendar timezone {payload['timezone']!r} != expected {SESSION_TIMEZONE!s}"
            )
        if payload["open"] != SESSION_OPEN.strftime("%H:%M"):
            raise CalendarIntegrityError(f"calendar open {payload['open']!r} unexpected")
        if payload["close"] != SESSION_CLOSE.strftime("%H:%M"):
            raise CalendarIntegrityError(f"calendar close {payload['close']!r} unexpected")

        sessions: tuple[date, ...] = tuple(
            date.fromisoformat(s) for s in payload["sessions"]
        )
        if not sessions:
            raise CalendarIntegrityError("calendar fixture has no sessions")
        if any(b <= a for a, b in zip(sessions, sessions[1:])):
            raise CalendarIntegrityError("calendar sessions not strictly increasing")
        if len(set(sessions)) != len(sessions):
            raise CalendarIntegrityError("calendar sessions contain duplicates")

        self._sessions = sessions
        self._ordinals = {d: i for i, d in enumerate(sessions)}
        self.name = payload["calendar"]

    def sessions(self) -> tuple[date, ...]:
        return self._sessions

    def is_session(self, d: date) -> bool:
        return d in self._ordinals

    def ordinal(self, d: date) -> int:
        try:
            return self._ordinals[d]
        except KeyError:
            raise NotASessionError(f"{d} is not a session in {self.name}") from None

    def nth_after(self, d: date, n: int) -> date:
        """The session n positions after d; d itself must be a session.

        `nth_after(d, 0) == d`. n must be >= 0. There is deliberately no
        `nth_before`: folds only roll forward; anything needing backward
        arithmetic must slice `sessions()` explicitly and name its intent.
        """
        if n < 0:
            raise ValueError(f"nth_after requires n >= 0, got {n}")
        start = self.ordinal(d)
        idx = start + n
        if idx >= len(self._sessions):
            raise NotASessionError(
                f"only {len(self._sessions) - start} sessions remain after {d}"
            )
        return self._sessions[idx]

    def session_open(self, d: date) -> datetime:
        self.ordinal(d)  # fail closed on unknown session
        return session_open_instant(d)

    def session_close(self, d: date) -> datetime:
        self.ordinal(d)  # fail closed on unknown session
        return session_close_instant(d)

    def contains_instant(self, d: date, ts: datetime) -> bool:
        ts = require_utc(ts, what="execution timestamp")
        return self.session_open(d) <= ts <= self.session_close(d)
