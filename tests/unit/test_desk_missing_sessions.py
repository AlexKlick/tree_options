"""Sessions come from the authoritative trex calendar, never from data.

2025-01-09 (national day of mourning; the NYSE was closed) is removed from
the trex calendar by a declared ``closure_overrides`` entry in
``scripts/gen_trex_calendar.py``. The desk no longer infers closures from
missing bars: an ordinary missing bar, even a vendor-wide outage that hits
every name, stays a missing observation (its windows are excluded, never
bridged), and no calendar is ever built from bars dated after an as-of.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tree_options.desk import har, ivhist
from tree_options.time.calendar import StaticSessionCalendar

REPO = Path(__file__).resolve().parents[2]
TREX_CAL = REPO / "data" / "calendar" / "trex" / "nyse_sessions_2018_01_02_2028_12_29.json"
CLOSED = date(2025, 1, 9)
OUTAGE = date(2025, 2, 12)  # a real session: pretend no vendor bar arrived for anyone
NAMES = ("AAPL", "SPY", "QQQ")
EARNINGS = {"AAPL": ["2024-10-31", "2025-01-30", "2025-05-01"]}


@pytest.fixture(scope="module")
def cal() -> StaticSessionCalendar:
    return StaticSessionCalendar(TREX_CAL, TREX_CAL.with_suffix(".sha256"))


def _panel(sessions: list[date]) -> dict[str, dict[str, Any]]:
    rng = np.random.default_rng(21)
    panel: dict[str, dict[str, Any]] = {}
    for name in NAMES:
        close = 100.0
        bars: dict[str, Any] = {}
        for s in sessions:
            o = close * math.exp(0.004 * rng.normal())
            c = o * math.exp(0.012 * rng.normal())
            hi = max(o, c) * math.exp(abs(rng.normal()) * 0.004)
            lo = min(o, c) * math.exp(-abs(rng.normal()) * 0.004)
            bars[s.isoformat()] = {
                "open": f"{o:.4f}",
                "high": f"{hi:.4f}",
                "low": f"{lo:.4f}",
                "close": f"{c:.4f}",
                "volume": 1,
            }
            close = float(f"{c:.4f}")
        panel[name] = bars
    return panel


def _sessions(cal: StaticSessionCalendar) -> list[date]:
    return [s for s in cal.sessions() if date(2023, 6, 1) <= s <= date(2025, 6, 30)]


def test_the_trex_calendar_has_the_closure_removed(cal: StaticSessionCalendar) -> None:
    assert not cal.is_session(CLOSED)
    assert cal.is_session(date(2025, 1, 8)) and cal.is_session(date(2025, 1, 10))
    assert cal.is_session(OUTAGE)


def test_har_grid_is_the_calendar_not_the_data(cal: StaticSessionCalendar) -> None:
    panel = _panel(_sessions(cal))
    data = har.build_har_data(panel, EARNINGS, cal, NAMES)
    assert CLOSED not in data.sessions
    i = data.index(date(2025, 1, 10))
    assert data.sessions[i - 1] == date(2025, 1, 8)
    assert data.names["SPY"].v[i] is not None  # 01-10 against the 01-08 close


def test_an_all_name_outage_stays_a_missing_observation(cal: StaticSessionCalendar) -> None:
    panel = _panel(_sessions(cal))
    for bars in panel.values():
        del bars[OUTAGE.isoformat()]
    data = har.build_har_data(panel, EARNINGS, cal, NAMES)
    assert OUTAGE in data.sessions  # never inferred to be a closure
    i = data.index(OUTAGE)
    ns = data.names["SPY"]
    assert ns.v[i] is None and ns.v[i + 1] is None  # no proxy bridges the gap
    # every regressor window that touches the gap is excluded, not stretched
    assert all(har.regressors(ns, t) is None for t in range(i, i + har.MONTH + 1))
    assert har.regressors(ns, i + har.MONTH + 1) is not None
    assert har.regressors(ns, i - 1) is not None


def test_ivhist_sessions_follow_the_calendar(cal: StaticSessionCalendar) -> None:
    scan = ivhist.CacheScan(source="t")
    for d in (date(2025, 1, 8), date(2025, 1, 10), date(2025, 3, 3)):
        scan.spot.setdefault("SPY", {})[d] = 100.0
        scan.options.setdefault("SPY", {})[d] = [
            ivhist.OptionBar(date(2025, 3, 21), "C", 100.0, 3.0)
        ]
    sessions = [date(2025, 1, 8), date(2025, 1, 10), OUTAGE, date(2025, 3, 3)]
    doc = ivhist.build_history(scan, ("SPY",), sessions, ivhist.RateSource.constant(0.04))
    recs = doc["names"]["SPY"]["sessions"]
    # a session without bars is recorded as NOT_EVALUABLE, never dropped
    assert recs[OUTAGE.isoformat()] == {"status": "NOT_EVALUABLE", "reason": "no option bars"}
