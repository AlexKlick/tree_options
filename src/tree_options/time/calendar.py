"""Trading-calendar abstraction.

`SessionCalendar` is the production interface: all session arithmetic flows
through `ordinal`/`nth_after` on an injected calendar. `ordinal()` raises
`NotASessionError` for unknown dates — there is no fallback, no guessing, and
no naive business-day arithmetic anywhere in production paths (enforced by an
AST scan test).
"""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from tree_options.time.sessions import (
    SESSION_CLOSE,
    SESSION_OPEN,
    SESSION_TIMEZONE,
    early_close_instant,
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


# Domain separation for calendar content identity (the same pattern as every
# other hash in this repo): the digest names WHAT it is a digest of.
CALENDAR_CONTENT_DOMAIN = b"tree-options-calendar-content-v1"


def calendar_content_sha256(calendar: SessionCalendar) -> str:
    """The COMPLETE content identity of a calendar (R2-P1-a/R2-P1-b, Codex
    round 2): a domain-separated sha256 over the FULL session tuple AND the
    early-close map.

    The concrete API for early closes is `early_close_sessions()` — every
    production calendar implements it (`StaticSessionCalendar`,
    `RealSessionCalendar`, and `MassiveDerivedSessionCalendar` by
    inheritance). A calendar that does not disclose its early-close set has
    no COMPLETE identity: hashing it anyway is exactly the INV-14 stamping
    this function exists to make impossible, so it refuses loudly instead.

    Deliberately NOT hashed: the calendar's `name` (display metadata, not
    semantics — two differently-named calendars over the same sessions and
    early closes are the same authority) and the fixture's file bytes (the
    `.sha256` sidecar pins those; content identity pins the SEMANTICS, so a
    cosmetic re-serialization of the same calendar is not an identity fork).
    """

    sessions = calendar.sessions()
    discloses = getattr(calendar, "early_close_sessions", None)
    if not callable(discloses):
        raise CalendarIntegrityError(
            f"{type(calendar).__name__} does not disclose its early-close set"
            " (no early_close_sessions()): its content identity would be"
            " incomplete, and an incomplete identity must refuse — never"
            " hash half the calendar's semantics"
        )
    payload = json.dumps(
        {
            "n_sessions": len(sessions),
            "sessions": [session.isoformat() for session in sessions],
            "early_close_sessions": [session.isoformat() for session in sorted(discloses())],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(CALENDAR_CONTENT_DOMAIN + payload).hexdigest()


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

        sessions: tuple[date, ...] = tuple(date.fromisoformat(s) for s in payload["sessions"])
        if not sessions:
            raise CalendarIntegrityError("calendar fixture has no sessions")
        if any(b <= a for a, b in itertools.pairwise(sessions)):
            raise CalendarIntegrityError("calendar sessions not strictly increasing")
        if len(set(sessions)) != len(sessions):
            raise CalendarIntegrityError("calendar sessions contain duplicates")

        early_closes: tuple[date, ...] = tuple(
            date.fromisoformat(s) for s in payload.get("early_close_sessions", [])
        )
        unknown_early = [d for d in early_closes if d not in set(sessions)]
        if unknown_early:
            raise CalendarIntegrityError(f"early-close sessions not sessions: {unknown_early[:3]}")

        self._sessions = sessions
        self._ordinals = {d: i for i, d in enumerate(sessions)}
        self._early_closes = frozenset(early_closes)
        self.name = payload["calendar"]

    def sessions(self) -> tuple[date, ...]:
        return self._sessions

    def early_close_sessions(self) -> tuple[date, ...]:
        """The fixture's early-close sessions, sorted (the disclosure
        `calendar_content_sha256` hashes — the calendar's complete
        semantics, never just its session list)."""
        return tuple(sorted(self._early_closes))

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
            raise NotASessionError(f"only {len(self._sessions) - start} sessions remain after {d}")
        return self._sessions[idx]

    def session_open(self, d: date) -> datetime:
        self.ordinal(d)  # fail closed on unknown session
        return session_open_instant(d)

    def session_close(self, d: date) -> datetime:
        self.ordinal(d)  # fail closed on unknown session
        if d in self._early_closes:
            return early_close_instant(d)
        return session_close_instant(d)

    def contains_instant(self, d: date, ts: datetime) -> bool:
        ts = require_utc(ts, what="execution timestamp")
        return self.session_open(d) <= ts <= self.session_close(d)
