"""Declared observation calendar for funded-account replays.

Handoff §4 (Comparability contract): "Different strategies should share
the same evaluation calendar where possible. A no-trade day is not a
missing-data day." The funded engine therefore iterates a DECLARED
session calendar — never the set of dates on which executions happened
to occur.

The calendar is the repo's pinned XNYS session list
(``data/calendar/nyse_sessions_2018_01_02_2026_12_31.json``,
``exchange-calendars==4.5.2``), sha-pinned alongside it. Tests may pass
their own explicit session tuples to ``run_funded_account``; production
callers resolve sessions through ``load_sessions``/``sessions_between``
so every comparison run names the calendar it used.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

_CALENDAR_RELATIVE = Path("data/calendar/nyse_sessions_2018_01_02_2026_12_31.json")


def calendar_path() -> Path:
    """Path to the pinned XNYS session calendar (repo-relative).

    ``calendar.py`` lives at ``src/tree_options/research/comparison/`` —
    four levels below the repo root (unlike ``research/paths.py``, which
    is three levels deep and uses ``parents[3]``).
    """
    return Path(__file__).resolve().parents[4] / _CALENDAR_RELATIVE


def calendar_sha256() -> str:
    """sha256 of the pinned calendar bytes — bound into run receipts so
    a rerun on a different calendar cannot masquerade as the old one."""
    return hashlib.sha256(calendar_path().read_bytes()).hexdigest()


def load_sessions() -> tuple[date, ...]:
    """All declared sessions, ascending. Dates outside the pinned range
    (the file covers 2018-01-02..2026-12-31) are simply absent — callers
    that need a wider window must supply a new pinned calendar file, not
    synthesize sessions."""
    doc = json.loads(calendar_path().read_text())
    return tuple(date.fromisoformat(s) for s in doc["sessions"])


def sessions_between(start: date | None, end: date | None) -> tuple[date, ...]:
    """Declared sessions in ``[start, end]``. ``None`` bounds are open
    (the pinned calendar's own first/last session)."""
    sessions = load_sessions()
    if start is not None:
        sessions = tuple(s for s in sessions if s >= start)
    if end is not None:
        sessions = tuple(s for s in sessions if s <= end)
    return sessions


__all__ = ["calendar_path", "calendar_sha256", "load_sessions", "sessions_between"]
