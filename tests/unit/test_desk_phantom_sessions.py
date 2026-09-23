"""Phantom sessions: a calendar session on which NO name has a bar.

Both static calendars list 2025-01-09 as a session, but the NYSE was closed
(national day of mourning) and no panel name has a bar for it. The desk's
econometrics must treat such a day as a non-session: no missing
observation, no excluded windows, no shifted HAR lags or train/test split,
and identical results whether the calendar lists the day or not (a
regenerated trex calendar drops it).
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tree_options.desk import har, ivhist, surface
from tree_options.desk.sessions import FilteredCalendar, phantom_sessions
from tree_options.time.calendar import StaticSessionCalendar

REPO = Path(__file__).resolve().parents[2]
TREX_CAL = REPO / "data" / "calendar" / "trex" / "nyse_sessions_2018_01_02_2028_12_29.json"
PHANTOM = date(2025, 1, 9)
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


def _real_sessions(cal: StaticSessionCalendar) -> list[date]:
    """2023-06-01..2025-06-30 on the calendar, minus the phantom day (the
    panel never has a bar for it)."""
    return [
        s for s in cal.sessions() if date(2023, 6, 1) <= s <= date(2025, 6, 30) and s != PHANTOM
    ]


def test_phantom_detection_and_filtered_calendar(cal: StaticSessionCalendar) -> None:
    panel = _panel(_real_sessions(cal))
    found = phantom_sessions(cal, panel.values())
    if cal.is_session(PHANTOM):
        assert found == frozenset({PHANTOM})
    else:  # a regenerated calendar already dropped it
        assert found == frozenset()
    eff = FilteredCalendar(cal, found)
    assert not eff.is_session(PHANTOM)
    assert eff.nth_after(date(2025, 1, 8), 1) == date(2025, 1, 10)
    assert eff.ordinal(date(2025, 1, 10)) == eff.ordinal(date(2025, 1, 8)) + 1
    # outside the observed span nothing is dropped (no data is not evidence)
    assert eff.is_session(date(2026, 9, 22))


def test_har_ignores_the_phantom_day(cal: StaticSessionCalendar) -> None:
    panel = _panel(_real_sessions(cal))
    data = har.build_har_data(panel, EARNINGS, cal, NAMES)
    assert PHANTOM not in data.sessions
    i = data.index(date(2025, 1, 10))
    assert data.sessions[i - 1] == date(2025, 1, 8)
    ns = data.names["SPY"]
    assert ns.v[i] is not None  # 01-10 uses the 01-08 close: no missing observation
    # no window is excluded around it: every origin near it has regressors
    assert all(har.regressors(ns, t) is not None for t in range(i - 25, i + 25))


def test_results_identical_with_or_without_the_phantom_in_the_calendar(
    cal: StaticSessionCalendar,
) -> None:
    panel = _panel(_real_sessions(cal))
    without = FilteredCalendar(cal, frozenset({PHANTOM}))
    outs = []
    for c in (cal, without):
        data = har.build_har_data(panel, EARNINGS, c, NAMES)
        wf = har.walk_forward(data, 20, start=date(2024, 9, 1), min_event_rows=1)
        by_date = {
            name: {data.sessions[t]: f for t, f in wf.forecasts[name].items()} for name in NAMES
        }
        pairs = {
            n: [
                (data.sessions[a], data.sessions[b])
                for a, b in data.names[n].pairs
                if 0 <= a and b < len(data.sessions)
            ]
            for n in NAMES
        }
        outs.append((data.sessions, by_date, pairs, data.coverage_idx))
    assert outs[0] == outs[1]
    assert outs[0][1]["SPY"]  # forecasts exist
    point = [
        har.forecast_at(panel, EARNINGS, c, NAMES, "AAPL", date(2025, 1, 21), 20)
        for c in (cal, without)
    ]
    assert point[0] is not None and point[0] == point[1]


def test_ivhist_history_skips_the_phantom_day(cal: StaticSessionCalendar) -> None:
    scan = ivhist.CacheScan(source="t")
    real = [date(2025, 1, 8), date(2025, 1, 10)]
    for d in real:
        scan.spot.setdefault("SPY", {})[d] = 100.0
        scan.options.setdefault("SPY", {})[d] = [
            ivhist.OptionBar(date(2025, 2, 21), "C", 100.0, 3.0),
        ]
    sessions = [date(2025, 1, 8), PHANTOM, date(2025, 1, 10)]
    doc = ivhist.build_history(scan, ("SPY",), sessions, ivhist.RateSource.constant(0.04))
    assert sorted(doc["names"]["SPY"]["sessions"]) == ["2025-01-08", "2025-01-10"]
    doc2 = ivhist.build_history(
        scan, ("SPY",), [date(2025, 1, 8), date(2025, 1, 10)], ivhist.RateSource.constant(0.04)
    )
    assert doc == doc2


def test_iv_rank_window_counts_real_sessions_only(cal: StaticSessionCalendar) -> None:
    without = FilteredCalendar(cal, frozenset({PHANTOM}))
    d = date(2025, 2, 3)
    hist = {s: 0.2 + 0.0005 * i for i, s in enumerate(without.sessions()) if s < d}
    assert surface.iv_rank(0.25, hist, d, without) == surface.iv_rank(
        0.25,
        hist,
        d,
        FilteredCalendar(cal, phantom_sessions(cal, [{s.isoformat(): 1 for s in hist}])),
    )
