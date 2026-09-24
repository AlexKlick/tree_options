"""Desk D4 pooled log-HAR (FORECAST-001): math against hand oracles and the
point-in-time guard (future poison).

Oracles are worked by hand (means of literal series) or by the textbook
normal equations computed here, never by calling the implementation.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tree_options.desk import har
from tree_options.time.calendar import StaticSessionCalendar

REPO = Path(__file__).resolve().parents[2]
TREX_CAL = REPO / "data" / "calendar" / "trex" / "nyse_sessions_2018_01_02_2028_12_29.json"


@pytest.fixture(scope="module")
def cal() -> StaticSessionCalendar:
    return StaticSessionCalendar(TREX_CAL, TREX_CAL.with_suffix(".sha256"))


def _series(
    v: list[float | None],
    *,
    reporter: bool = False,
    pairs: tuple[tuple[int, int], ...] = (),
    name: str = "X",
) -> har.NameSeries:
    return har.NameSeries.from_proxy(name, v, reporter=reporter, pairs=pairs)


# ---------------------------------------------------------------- rows


def test_regressors_and_target_hand_values() -> None:
    v: list[float | None] = [(i + 1) * 1e-4 for i in range(40)]
    ns = _series(v)
    # t = 30: v_t = 31e-4 ; mean(v[26..30]) = mean(27..31)e-4 = 29e-4 ;
    # mean(v[9..30]) = mean(10..31)e-4 = 20.5e-4
    x = har.regressors(ns, 30)
    assert x == pytest.approx((math.log(31e-4), math.log(29e-4), math.log(20.5e-4)))
    # h = 5: RV = sum(v[31..35]) = (32+...+36)e-4 = 170e-4 ; y = ln(170e-4/5)
    assert har.target(ns, 30, 5) == pytest.approx(math.log(34e-4))
    assert har.regressors(ns, 20) is None  # needs 22 sessions of history
    assert har.target(ns, 36, 5) is None  # window runs past the series


def test_missing_value_voids_windows_touching_it() -> None:
    v: list[float | None] = [1e-4] * 60
    v[25] = None
    ns = _series(v)
    assert har.regressors(ns, 30) is None  # 25 lies in the 22-window 9..30
    assert har.regressors(ns, 46) is None  # window 25..46
    assert har.regressors(ns, 47) is not None  # window 26..47
    assert har.target(ns, 22, 5) is None  # 23..27 contains 25
    assert har.target(ns, 19, 5) is not None  # 20..24


def test_cleaning_replaces_event_sessions_with_prior_five_mean() -> None:
    v: list[float | None] = [(i + 1) * 1e-4 for i in range(10)]
    ns = _series(v, reporter=True, pairs=((6, 7),))
    # v*[6] = mean(v[1..5]) = mean(2..6)e-4 = 4e-4
    # v*[7] = mean(v*[2..6]) = mean(3,4,5,6,4)e-4 = 4.4e-4
    assert ns.v_clean[6] == pytest.approx(4e-4)
    assert ns.v_clean[7] == pytest.approx(4.4e-4)
    assert ns.v_clean[8] == pytest.approx(9e-4)
    assert ns.v[6] == pytest.approx(7e-4)  # the raw proxy (targets) is untouched


def test_n_earn_counts_pairs_intersecting_the_forward_window() -> None:
    ns = _series([1e-4] * 40, reporter=True, pairs=((10, 11), (20, 21)))
    assert har.n_earn(ns, 9, 1) == 1  # (9, 10]
    assert har.n_earn(ns, 10, 1) == 1  # (10, 11] holds the pair's second session
    assert har.n_earn(ns, 11, 5) == 0  # (11, 16]
    assert har.n_earn(ns, 5, 20) == 2  # (5, 25]
    assert har.n_earn(ns, 21, 5) == 0


def test_coverage_rule_imputes_quarterly_mean_before_coverage() -> None:
    rep = _series([1e-4] * 40, reporter=True, pairs=((30, 31),))
    etf = _series([1e-4] * 40, reporter=False)
    coverage = 25  # grid index of the first covered session
    # uncovered reporter row (t+1 < 25): h/63
    assert har.n_earn_used(rep, 22, 5, coverage) == pytest.approx(5 / 63)
    # covered reporter row (t+1 >= 25): the observed count
    assert har.n_earn_used(rep, 24, 5, coverage) == 0.0
    assert har.n_earn_used(rep, 28, 5, coverage) == 1.0
    assert har.n_earn_used(etf, 22, 5, coverage) == 0.0


# ----------------------------------------------------------------- fit


def _rowset(
    names: list[str],
    codes: list[int],
    t: list[int],
    x: np.ndarray,
    y: np.ndarray,
    n_used: np.ndarray,
    event: np.ndarray,
    h: int,
) -> har.RowSet:
    return har.RowSet(
        h=h,
        names=tuple(names),
        name=np.array(codes),
        t=np.array(t),
        x=x,
        y=y,
        n_used=n_used,
        event=event,
    )


def test_fit_recovers_exact_coefficients_without_noise() -> None:
    rng = np.random.default_rng(7)
    n = 300
    codes = [i % 3 for i in range(n)]
    x = rng.normal(size=(n, 3))
    nn = rng.integers(0, 3, size=n).astype(float)
    b0 = np.array([-9.0, -8.5, -8.0])
    y = b0[codes] + x @ np.array([0.3, 0.4, 0.2]) + 0.1 * nn
    rows = _rowset(["A", "B", "C"], codes, list(range(n)), x, y, nn, nn >= 1, h=1)
    fit = har.fit_har(rows, through=10_000, min_event_rows=1)
    assert fit is not None
    assert (fit.bd, fit.bw, fit.bm) == pytest.approx((0.3, 0.4, 0.2), abs=1e-10)
    assert fit.be == pytest.approx(0.1, abs=1e-10)
    assert fit.intercepts == pytest.approx({"A": -9.0, "B": -8.5, "C": -8.0}, abs=1e-10)
    assert fit.s2 == pytest.approx(0.0, abs=1e-20)


def test_fit_matches_normal_equations_and_drops_be_without_events() -> None:
    rng = np.random.default_rng(11)
    n = 40
    codes = [i % 2 for i in range(n)]
    x = rng.normal(size=(n, 3))
    y = rng.normal(size=n)
    nn = np.zeros(n)
    rows = _rowset(["A", "B"], codes, list(range(n)), x, y, nn, nn >= 1, h=1)
    fit = har.fit_har(rows, through=10_000, min_event_rows=1)
    assert fit is not None and fit.be is None  # no event rows: N column dropped
    design = np.column_stack(
        [np.array([c == 0 for c in codes], float), np.array([c == 1 for c in codes], float), x]
    )
    beta = np.linalg.solve(design.T @ design, design.T @ y)
    resid = y - design @ beta
    assert fit.intercepts["A"] == pytest.approx(beta[0], abs=1e-10)
    assert fit.intercepts["B"] == pytest.approx(beta[1], abs=1e-10)
    assert (fit.bd, fit.bw, fit.bm) == pytest.approx(tuple(beta[2:]), abs=1e-10)
    assert fit.s2 == pytest.approx(float(resid @ resid) / (n - 5), rel=1e-10)
    assert (fit.n, fit.k) == (40, 5)


def test_be_needs_the_minimum_number_of_event_rows() -> None:
    rng = np.random.default_rng(3)
    n = 200
    codes = [0] * n
    x = rng.normal(size=(n, 3))
    nn = np.array([1.0 if i % 10 == 0 else 0.0 for i in range(n)])  # 20 event rows
    y = -9.0 + x @ np.array([0.3, 0.4, 0.2]) + 0.5 * nn
    rows = _rowset(["A"], codes, list(range(n)), x, y, nn, nn >= 1, h=1)
    assert har.fit_har(rows, through=10_000, min_event_rows=21).be is None  # type: ignore[union-attr]
    assert har.fit_har(rows, through=10_000, min_event_rows=20).be == pytest.approx(0.5)  # type: ignore[union-attr]


def test_fit_uses_only_rows_whose_target_is_realized() -> None:
    n = 30
    codes = [0] * n
    x = np.column_stack([np.arange(n, dtype=float), np.ones(n), np.ones(n) * 2]) * 0
    x[:, 0] = np.arange(n, dtype=float) % 7
    x[:, 1] = (np.arange(n, dtype=float) * 3) % 5
    x[:, 2] = (np.arange(n, dtype=float) * 5) % 11
    y = 1.0 + x @ np.array([0.1, 0.2, 0.3])
    y_poisoned = y.copy()
    y_poisoned[15:] += 100.0  # rows with t + h > 19 are unrealized at through=19
    nn = np.zeros(n)
    a = har.fit_har(_rowset(["A"], codes, list(range(n)), x, y, nn, nn >= 1, h=5), through=19)
    b = har.fit_har(
        _rowset(["A"], codes, list(range(n)), x, y_poisoned, nn, nn >= 1, h=5), through=19
    )
    assert a is not None and b is not None
    assert a.n == 15  # t + 5 <= 19  ->  t <= 14
    assert (a.bd, a.bw, a.bm, a.intercepts["A"]) == (b.bd, b.bw, b.bm, b.intercepts["A"])


def test_bias_corrected_forecast_hand_value() -> None:
    fit = har.HarFit(
        h=20,
        through=0,
        intercepts={"A": -1.0},
        bd=0.2,
        bw=0.3,
        bm=0.4,
        be=0.5,
        s2=0.09,
        n=100,
        k=5,
        n_event_rows=150,
    )
    # yhat = -1 + 0.2*(-9) + 0.3*(-8) + 0.4*(-8.5) + 0.5*1 = -8.1
    # F = 20 * exp(-8.1 + 0.045)
    got = har.predict(fit, "A", (-9.0, -8.0, -8.5), 1.0)
    assert got == pytest.approx(20.0 * math.exp(-8.055), rel=1e-12)
    assert har.predict(fit, "UNKNOWN", (-9.0, -8.0, -8.5), 1.0) is None
    no_be = dataclasses.replace(fit, be=None)
    assert har.predict(no_be, "A", (-9.0, -8.0, -8.5), 3.0) == pytest.approx(
        20.0 * math.exp(-8.6 + 0.045), rel=1e-12
    )


# --------------------------------------------------- panel + point in time


def _synthetic_panel(cal: StaticSessionCalendar, seed: int) -> dict[str, dict[str, Any]]:
    """Three names on real NYSE sessions 2023-01-03..2025-06-30, with a
    log-AR(1) vol and occasional jumps (numbers only exercise the code)."""
    rng = np.random.default_rng(seed)
    sessions = [s for s in cal.sessions() if date(2023, 1, 3) <= s <= date(2025, 6, 30)]
    panel: dict[str, dict[str, Any]] = {}
    for name in ("AAA", "SPY", "QQQ"):
        close = 100.0
        lv = math.log(0.015)
        bars: dict[str, Any] = {}
        for s in sessions:
            lv = math.log(0.015) + 0.95 * (lv - math.log(0.015)) + 0.2 * rng.normal()
            sig = math.exp(lv)
            o = close * math.exp(0.4 * sig * rng.normal())
            c = o * math.exp(sig * rng.normal())
            hi = max(o, c) * math.exp(abs(rng.normal()) * sig / 3)
            lo = min(o, c) * math.exp(-abs(rng.normal()) * sig / 3)
            bars[s.isoformat()] = {
                "open": f"{o:.4f}",
                "high": f"{hi:.4f}",
                "low": f"{lo:.4f}",
                "close": f"{c:.4f}",
                "volume": 1000,
            }
            close = float(f"{c:.4f}")
        panel[name] = bars
    return panel


EARNINGS = {
    "AAA": [
        "2023-04-27",
        "2023-07-27",
        "2023-10-26",
        "2024-01-25",
        "2024-04-25",
        "2024-07-25",
        "2024-10-24",
        "2025-01-23",
        "2025-04-24",
        "2025-07-24",
    ],
    "SPY": [],
}


def _poison_after(panel: dict[str, dict[str, Any]], cut: str) -> dict[str, dict[str, Any]]:
    rng = np.random.default_rng(99)
    out: dict[str, dict[str, Any]] = {}
    for name, bars in panel.items():
        out[name] = {}
        for d, bar in bars.items():
            if d <= cut:
                out[name][d] = dict(bar)
                continue
            f = float(np.exp(rng.normal() * 0.3))
            out[name][d] = {
                k: (f"{float(v) * f:.4f}" if k != "volume" else v) for k, v in bar.items()
            }
    return out


def test_har_data_marks_etfs_and_event_pairs(cal: StaticSessionCalendar) -> None:
    panel = _synthetic_panel(cal, 1)
    data = har.build_har_data(panel, EARNINGS, cal, ("AAA", "SPY", "QQQ"))
    assert data.names["AAA"].reporter and not data.names["SPY"].reporter
    assert not data.names["QQQ"].reporter  # QQQ is an ETF even without a calendar row
    grid = data.sessions
    first, second = data.names["AAA"].pairs[0]
    assert (grid[first], grid[second]) == (date(2023, 4, 27), date(2023, 4, 28))
    assert grid[data.coverage_idx] == date(2024, 9, 3)  # first session >= 2024-09-01


def _drop_after(
    panel: dict[str, dict[str, Any]], cut: str, seed: int, share: float
) -> dict[str, dict[str, Any]]:
    """Remove a random share of the bars dated after ``cut``, and every
    name's bar on some dates (a vendor-wide outage)."""
    rng = np.random.default_rng(seed)
    outage = {d for d in panel["SPY"] if d > cut and rng.random() < share / 2}
    return {
        name: {
            d: dict(b)
            for d, b in bars.items()
            if d <= cut or (d not in outage and rng.random() >= share)
        }
        for name, bars in panel.items()
    }


def _variants(panel: dict[str, dict[str, Any]], cut: str, mode: str) -> tuple[Any, Any]:
    """(base, altered) panels identical up to ``cut``: future prices
    changed, future bars dropped, or future bars added."""
    if mode == "values":
        return panel, _poison_after(panel, cut)
    if mode == "drop":
        return panel, _drop_after(panel, cut, 3, 0.2)
    return _drop_after(panel, cut, 4, 0.2), _poison_after(panel, cut)  # add (and alter)


@pytest.mark.parametrize("mode", ["values", "drop", "add"])
@pytest.mark.parametrize("h", har.HORIZONS)
def test_future_poison_does_not_change_point_in_time_forecasts(
    cal: StaticSessionCalendar, h: int, mode: str
) -> None:
    """Altering anything dated after t (prices, or which future bars
    exist) must leave every forecast at origins <= t byte-identical
    (walk-forward HAR, RV22 and EWMA), at every horizon."""
    panel = _synthetic_panel(cal, 5)
    cut = "2024-11-14"
    base, altered = _variants(panel, cut, mode)
    names = ("AAA", "SPY", "QQQ")
    outs = []
    for p in (base, altered):
        data = har.build_har_data(p, EARNINGS, cal, names)
        wf = har.walk_forward(data, h, start=date(2024, 9, 1), min_event_rows=1)
        outs.append((data, wf))
    (d0, wf0), (d1, wf1) = outs
    cut_idx = d0.index(date.fromisoformat(cut))
    assert d0.sessions[: cut_idx + 1] == d1.sessions[: cut_idx + 1]
    checked = 0
    for name in names:
        early0 = {t: f for t, f in wf0.forecasts[name].items() if t <= cut_idx}
        early1 = {t: f for t, f in wf1.forecasts[name].items() if t <= cut_idx}
        assert early0 == early1
        checked += len(early0)
        for t in range(cut_idx - 30, cut_idx + 1):
            assert har.rv22_forecast(d0.names[name], t, h) == har.rv22_forecast(
                d1.names[name], t, h
            )
            assert d0.names[name].ewma[t] == d1.names[name].ewma[t]
    assert checked > 100  # the test really compared forecasts
    # and the future really differs: some later forecast moved or vanished
    late0 = {t: f for t, f in wf0.forecasts["SPY"].items() if t > cut_idx}
    late1 = {t: f for t, f in wf1.forecasts["SPY"].items() if t > cut_idx}
    assert late0 != late1


@pytest.mark.parametrize("mode", ["values", "drop", "add"])
def test_forecast_at_truncates_the_panel(cal: StaticSessionCalendar, mode: str) -> None:
    panel = _synthetic_panel(cal, 8)
    d = date(2025, 3, 12)
    base, altered = _variants(panel, d.isoformat(), mode)
    a = har.forecast_at(base, EARNINGS, cal, ("AAA", "SPY", "QQQ"), "AAA", d, 20)
    b = har.forecast_at(altered, EARNINGS, cal, ("AAA", "SPY", "QQQ"), "AAA", d, 20)
    assert a is not None and a == b
    # the fit is the month's: through the last session before 2025-03-03
    assert a.fit_through == date(2025, 2, 28)
    assert a.forecast > 0.0 and a.rv22 is not None and a.rv22 > 0.0


# --------------------------------------------- raw mask, empty names (P2)


def test_cleaning_never_fills_a_missing_raw_observation() -> None:
    """A report-session bar missing from the panel leaves raw v missing;
    cleaning replaces v* there, but no regressor window may contain the
    missing raw value (FORECAST-001: every raw v_{t-21..t+h} must exist)."""
    v: list[float | None] = [1e-4] * 70
    v[30] = None
    ns = _series(v, reporter=True, pairs=((30, 31),))
    assert ns.v_clean[30] is not None  # the cleaned value exists...
    # ...but every origin whose 22-window holds session 30 has no regressors
    assert all(har.regressors(ns, t) is None for t in range(30, 52))
    assert har.regressors(ns, 29) is not None
    assert har.regressors(ns, 52) is not None
    rows = har.build_rows(
        har.HarData(
            sessions=tuple(range(70)),
            names={"X": ns},  # type: ignore[arg-type]
            coverage_idx=0,
        ),
        5,
    )
    assert not any(25 <= t <= 51 for t in rows.t.tolist())  # targets or regressors hit 30


def test_one_empty_name_does_not_disable_the_others(cal: StaticSessionCalendar) -> None:
    panel = _synthetic_panel(cal, 2)
    panel["NEW"] = {}  # no bars at all
    names = ("AAA", "SPY", "QQQ", "NEW")
    data = har.build_har_data(panel, EARNINGS, cal, names)
    assert all(x is None for x in data.names["NEW"].v)
    got = har.forecasts_at(panel, EARNINGS, cal, names, date(2025, 3, 12), 20)
    assert set(got) == {"AAA", "SPY", "QQQ"}
    # a name listed only after the as-of is skipped, not fatal
    late = {**panel, "LATE": {"2025-06-02": dict(panel["SPY"]["2025-06-02"])}}
    got2 = har.forecasts_at(late, EARNINGS, cal, (*names, "LATE"), date(2025, 3, 12), 20)
    assert set(got2) == {"AAA", "SPY", "QQQ"}


# ---------------------------------------------- earnings schedule (P1-3)


def test_schedule_status_requires_a_known_report_beyond_the_window(
    cal: StaticSessionCalendar,
) -> None:
    t, end = date(2026, 9, 22), date(2026, 10, 20)
    # the supplied calendar ends in July: nothing is known about October
    assert har.schedule_status(["2026-04-30", "2026-07-30"], t, end, cal)[0] == "incomplete"
    # a report known beyond the window (estimated dates count): complete
    ok = har.schedule_status(["2026-04-30", "2026-07-30", "2026-10-29"], t, end, cal)
    assert ok == ("complete", "")
    # one inside the window and one beyond: complete
    assert har.schedule_status(["2026-07-30", "2026-10-15", "2027-01-28"], t, end, cal)[0] == (
        "complete"
    )
    # a quarter missing between the last known report and the next one
    gap = har.schedule_status(["2026-03-01", "2026-12-01"], t, end, cal)
    assert gap[0] == "incomplete" and "gap" in gap[1]
    assert har.schedule_status([], t, end, cal)[0] == "unavailable"


def test_forward_schedule_sets_n_earn_and_status(cal: StaticSessionCalendar) -> None:
    panel = _synthetic_panel(cal, 6)
    names = ("AAA", "SPY", "QQQ")
    d = date(2025, 3, 12)
    # the sealed calendar (fit) ends 2025-04-24: the 20-session window after
    # d ends 2025-04-09, so the sealed schedule alone is complete
    got = har.forecasts_at(panel, EARNINGS, cal, names, d, 20)
    assert got["AAA"].schedule == "complete" and got["AAA"].n_earn == 0.0
    assert got["SPY"].schedule == "n/a"
    # a forward schedule without anything beyond the window: incomplete
    fwd = {"AAA": ["2025-01-23"]}
    got2 = har.forecasts_at(panel, EARNINGS, cal, names, d, 20, forward_schedule=fwd)
    assert got2["AAA"].schedule == "incomplete" and got2["AAA"].schedule_reason
    # an estimated report inside the window counts for variance purposes
    fwd3 = {"AAA": ["2025-01-23", "2025-03-27", "2025-07-24"]}
    got3 = har.forecasts_at(panel, EARNINGS, cal, names, d, 20, forward_schedule=fwd3)
    assert got3["AAA"].schedule == "complete" and got3["AAA"].n_earn == 1.0
    # the fit is the sealed calendar's either way (same coefficients)
    assert got3["AAA"].fit_n == got["AAA"].fit_n
