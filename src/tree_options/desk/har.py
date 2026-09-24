"""Pooled log-HAR realized-variance forecasts (plan D4; pre-registered in
``docs/desk/FORECAST-001.md``, sealed before any fit).

Per horizon h in {5, 20, 63, 126}::

    ln(RV_{t,h}/h) = b0_i + bd ln v*_t + bw ln mean5(v*) + bm ln mean22(v*)
                     + be N_earn(t,h) + e

* v is the overnight + Garman-Klass proxy (:mod:`tree_options.desk.rv`);
  targets use raw v, regressors the earnings-cleaned v* (on a session of
  a report's event pair {s(D), next session}, v* is the mean of the five
  preceding v*);
* N_earn counts the event pairs meeting the forward window (t, t+h] (the
  sealed calendar's dates are treated as known: a schedule assumption);
* the calendar covers reports from 2024-09-01 only: an uncovered reporter
  row gets N_earn = h/63 (a quarterly reporter's mean), ETFs always 0; be
  is estimated only with >= MIN_EVENT_ROWS covered reporter rows with an
  event, else the column is dropped;
* pooled OLS with one intercept per name; forecast
  F = h exp(yhat + s^2/2) (lognormal bias correction);
* refit at the first session of each month on the rows whose target is
  realized by the previous session (strictly point in time; the
  future-poison test in tests/unit/test_desk_har.py pins it).

Floats: variance statistics, not money. Grid indices are positions in
consecutive NYSE sessions (no date arithmetic).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from tree_options.desk import rv, stats
from tree_options.desk.sessions import Calendar, without_phantoms

HORIZONS: tuple[int, ...] = (5, 20, 63, 126)
WEEK = 5
MONTH = 22
CLEAN_BACK = 5
QUARTER_SESSIONS = 63
COVERAGE_START = date(2024, 9, 1)  # earnings-calendar.json covers 2024-09 onward
MIN_EVENT_ROWS = 100
# no earnings reports (N_earn = 0 by definition)
ETF_NAMES: frozenset[str] = frozenset(
    {"SPY", "QQQ", "IWM", "SMH", "SOXX", "XLE", "XLV", "XLF", "GLD", "TQQQ", "SQQQ"}
)


# ------------------------------------------------------------------ series


def _prefix(values: Sequence[float | None]) -> tuple[tuple[float, ...], tuple[int, ...]]:
    """Causal prefix sums and missing counts: window (a, b] is complete iff
    miss[b+1] == miss[a+1]."""
    pre = [0.0]
    miss = [0]
    for x in values:
        pre.append(pre[-1] + (x if x is not None else 0.0))
        miss.append(miss[-1] + (1 if x is None else 0))
    return tuple(pre), tuple(miss)


def clean_proxy(v: Sequence[float | None], events: Iterable[int]) -> list[float | None]:
    """v* : on an event session, the mean of the five preceding v*
    (recursive), None when those are incomplete."""
    marked = set(events)
    out = list(v)
    for i in range(len(out)):
        if i not in marked:
            continue
        if i < CLEAN_BACK:
            out[i] = None
            continue
        prior = [x for x in out[i - CLEAN_BACK : i] if x is not None]
        out[i] = sum(prior) / CLEAN_BACK if len(prior) == CLEAN_BACK else None
    return out


@dataclass(frozen=True)
class NameSeries:
    name: str
    reporter: bool
    v: tuple[float | None, ...]
    v_clean: tuple[float | None, ...]
    pairs: tuple[tuple[int, int], ...]
    ewma: tuple[float | None, ...]
    _pre_v: tuple[float, ...] = field(repr=False, compare=False)
    _miss_v: tuple[int, ...] = field(repr=False, compare=False)
    _pre_c: tuple[float, ...] = field(repr=False, compare=False)
    _miss_c: tuple[int, ...] = field(repr=False, compare=False)

    @classmethod
    def from_proxy(
        cls,
        name: str,
        v: Sequence[float | None],
        *,
        reporter: bool,
        pairs: Sequence[tuple[int, int]] = (),
    ) -> NameSeries:
        events = [i for pair in pairs for i in pair if 0 <= i < len(v)]
        v_clean = clean_proxy(v, events)
        pre_v, miss_v = _prefix(v)
        pre_c, miss_c = _prefix(v_clean)
        return cls(
            name=name,
            reporter=reporter,
            v=tuple(v),
            v_clean=tuple(v_clean),
            pairs=tuple(pairs),
            ewma=tuple(rv.ewma_series(v)),
            _pre_v=pre_v,
            _miss_v=miss_v,
            _pre_c=pre_c,
            _miss_c=miss_c,
        )

    def _mean(self, lo: int, hi: int, *, clean: bool) -> float | None:
        """mean over indices lo..hi inclusive, None if any is missing."""
        if lo < 0 or hi >= len(self.v) or hi < lo:
            return None
        pre, miss = (self._pre_c, self._miss_c) if clean else (self._pre_v, self._miss_v)
        if miss[hi + 1] != miss[lo]:
            return None
        return (pre[hi + 1] - pre[lo]) / (hi - lo + 1)


def regressors(ns: NameSeries, t: int) -> tuple[float, float, float] | None:
    """(ln v*_t, ln mean5 v*, ln mean22 v*) or None if any window is incomplete."""
    d = ns._mean(t, t, clean=True)
    w = ns._mean(t - WEEK + 1, t, clean=True)
    m = ns._mean(t - MONTH + 1, t, clean=True)
    if d is None or w is None or m is None or min(d, w, m) <= 0.0:
        return None
    return (math.log(d), math.log(w), math.log(m))


def target(ns: NameSeries, t: int, h: int) -> float | None:
    """ln(RV_{t,h}/h) from raw v over (t, t+h]."""
    mean = ns._mean(t + 1, t + h, clean=False)
    return math.log(mean) if mean is not None and mean > 0.0 else None


def realized(ns: NameSeries, t: int, h: int) -> float | None:
    """RV_{t,h} = sum of raw v over (t, t+h]."""
    mean = ns._mean(t + 1, t + h, clean=False)
    return mean * h if mean is not None else None


def n_earn(ns: NameSeries, t: int, h: int) -> int:
    """Event pairs meeting (t, t+h]."""
    return sum(1 for a, b in ns.pairs if t < a <= t + h or t < b <= t + h)


def n_earn_used(ns: NameSeries, t: int, h: int, coverage_idx: int) -> float:
    """The N_earn regressor under the coverage rule (FORECAST-001)."""
    if not ns.reporter:
        return 0.0
    if t + 1 >= coverage_idx:
        return float(n_earn(ns, t, h))
    return h / QUARTER_SESSIONS


def rv22_forecast(ns: NameSeries, t: int, h: int) -> float | None:
    mean = ns._mean(t - MONTH + 1, t, clean=False)
    return h * mean if mean is not None else None


def ewma_forecast(ns: NameSeries, t: int, h: int) -> float | None:
    if t < 0 or t >= len(ns.ewma):
        return None
    s2 = ns.ewma[t]
    return h * s2 if s2 is not None else None


# -------------------------------------------------------------------- data


@dataclass(frozen=True)
class HarData:
    sessions: tuple[date, ...]  # consecutive NYSE sessions (the grid)
    names: dict[str, NameSeries]
    coverage_idx: int  # first grid index on or after COVERAGE_START

    def index(self, d: date) -> int:
        i = bisect.bisect_left(self.sessions, d)
        if i == len(self.sessions) or self.sessions[i] != d:
            raise KeyError(f"{d} is not on the grid")
        return i


def event_pairs(reports: Iterable[str], cal: Calendar, grid0: date) -> tuple[tuple[int, int], ...]:
    """(s(D), next session) as grid indices (may fall outside the grid)."""
    sessions = cal.sessions()
    base = bisect.bisect_left(sessions, grid0)
    out = []
    for rep in sorted(set(reports)):
        try:
            d = date.fromisoformat(rep)
        except (TypeError, ValueError):
            continue
        i = bisect.bisect_left(sessions, d)
        if i + 1 >= len(sessions):
            continue
        out.append((i - base, i + 1 - base))
    return tuple(out)


def build_har_data(
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]],
    earnings: Mapping[str, Iterable[str]],
    cal: Calendar,
    names: Sequence[str],
    *,
    through: date | None = None,
) -> HarData:
    """Per-name proxies on the NYSE grid spanning the names' panel bars
    (only bars dated <= ``through`` when given: point-in-time truncation).

    A calendar session on which none of ``names`` has a bar (2025-01-09:
    listed by both static calendars, the NYSE was closed) is a phantom and
    is dropped from the grid and from the event-pair session count, so it
    never voids a window or shifts a lag, and the grid is the same whether
    or not the calendar lists it (whether a day is a phantom depends only
    on bars dated that day: point in time)."""
    cut = through.isoformat() if through is not None else None
    bars: dict[str, dict[str, Mapping[str, Any]]] = {}
    for name in names:
        raw = panel.get(name) or {}
        bars[name] = {d: b for d, b in raw.items() if cut is None or d <= cut}
    dated = [d for b in bars.values() for d in (min(b), max(b)) if b]
    if not dated:
        raise ValueError("no panel bars for the requested names")
    first, last = date.fromisoformat(min(dated)), date.fromisoformat(max(dated))
    cal = without_phantoms(cal, bars.values())
    all_sessions = cal.sessions()
    lo = bisect.bisect_left(all_sessions, first)
    hi = bisect.bisect_right(all_sessions, last)
    grid = tuple(all_sessions[lo:hi])
    iso = [s.isoformat() for s in grid]
    series = {}
    for name in names:
        v = rv.proxy_series(bars[name], iso)
        series[name] = NameSeries.from_proxy(
            name,
            v,
            reporter=name not in ETF_NAMES,
            pairs=event_pairs(earnings.get(name, ()), cal, grid[0]),
        )
    return HarData(
        sessions=grid,
        names=series,
        coverage_idx=bisect.bisect_left(grid, COVERAGE_START),
    )


# --------------------------------------------------------------------- fit


@dataclass(frozen=True)
class RowSet:
    """Estimation rows of one horizon (every row with complete regressors
    and a complete target)."""

    h: int
    names: tuple[str, ...]
    name: np.ndarray  # int codes into names
    t: np.ndarray  # grid index of the origin
    x: np.ndarray  # (n, 3): ln v*, ln mean5, ln mean22
    y: np.ndarray
    n_used: np.ndarray  # N_earn under the coverage rule
    event: np.ndarray  # covered reporter row with N_earn >= 1


def build_rows(data: HarData, h: int) -> RowSet:
    names = tuple(data.names)
    codes: list[int] = []
    ts: list[int] = []
    xs: list[tuple[float, float, float]] = []
    ys: list[float] = []
    ns_used: list[float] = []
    events: list[bool] = []
    for code, name in enumerate(names):
        ns = data.names[name]
        for t in range(len(ns.v) - h):
            y = target(ns, t, h)
            if y is None:
                continue
            x = regressors(ns, t)
            if x is None:
                continue
            used = n_earn_used(ns, t, h, data.coverage_idx)
            codes.append(code)
            ts.append(t)
            xs.append(x)
            ys.append(y)
            ns_used.append(used)
            events.append(ns.reporter and t + 1 >= data.coverage_idx and used >= 1.0)
    return RowSet(
        h=h,
        names=names,
        name=np.array(codes, dtype=np.int64),
        t=np.array(ts, dtype=np.int64),
        x=np.array(xs, dtype=np.float64).reshape(len(xs), 3),
        y=np.array(ys, dtype=np.float64),
        n_used=np.array(ns_used, dtype=np.float64),
        event=np.array(events, dtype=bool),
    )


@dataclass(frozen=True)
class HarFit:
    h: int
    through: int  # grid index of the last session whose data the fit saw
    intercepts: dict[str, float]
    bd: float
    bw: float
    bm: float
    be: float | None  # None: N_earn column dropped (too few event rows)
    s2: float
    n: int
    k: int
    n_event_rows: int


def fit_har(rows: RowSet, *, through: int, min_event_rows: int = MIN_EVENT_ROWS) -> HarFit | None:
    """Pooled OLS on the rows whose target is realized by grid index
    ``through`` (t + h <= through); None when the sample cannot identify
    the model."""
    mask = (rows.t + rows.h) <= through
    n = int(mask.sum())
    if n == 0:
        return None
    codes = rows.name[mask]
    present = sorted(set(codes.tolist()))
    col = {c: j for j, c in enumerate(present)}
    n_events = int((rows.event & mask).sum())
    use_n = n_events >= min_event_rows
    p = len(present)
    k = p + 3 + (1 if use_n else 0)
    if n <= k:
        return None
    design = np.zeros((n, k), dtype=np.float64)
    design[np.arange(n), [col[c] for c in codes.tolist()]] = 1.0
    design[:, p : p + 3] = rows.x[mask]
    if use_n:
        design[:, p + 3] = rows.n_used[mask]
    try:
        beta, resid = stats.ols(design, rows.y[mask])
    except np.linalg.LinAlgError:
        return None
    return HarFit(
        h=rows.h,
        through=through,
        intercepts={rows.names[c]: float(beta[col[c]]) for c in present},
        bd=float(beta[p]),
        bw=float(beta[p + 1]),
        bm=float(beta[p + 2]),
        be=float(beta[p + 3]) if use_n else None,
        s2=float(resid @ resid) / (n - k),
        n=n,
        k=k,
        n_event_rows=n_events,
    )


def predict(
    fit: HarFit, name: str, x: tuple[float, float, float], n_earn_value: float
) -> float | None:
    """Bias-corrected variance forecast over h sessions."""
    b0 = fit.intercepts.get(name)
    if b0 is None:
        return None
    yhat = b0 + fit.bd * x[0] + fit.bw * x[1] + fit.bm * x[2]
    if fit.be is not None:
        yhat += fit.be * n_earn_value
    return fit.h * math.exp(yhat + fit.s2 / 2.0)


# ------------------------------------------------------------ walk forward


def month_starts(sessions: Sequence[date], lo: int, hi: int) -> list[int]:
    """Grid indices in [lo, hi] opening a calendar month (lo always counts)."""
    out = []
    for i in range(max(lo, 0), hi + 1):
        if i == lo or (sessions[i].year, sessions[i].month) != (
            sessions[i - 1].year,
            sessions[i - 1].month,
        ):
            out.append(i)
    return out


@dataclass(frozen=True)
class WalkForward:
    h: int
    fits: dict[int, HarFit | None]  # month-start grid index -> the month's fit
    forecasts: dict[str, dict[int, float]]  # name -> origin index -> F


def walk_forward(
    data: HarData,
    h: int,
    *,
    start: date,
    through_idx: int | None = None,
    min_event_rows: int = MIN_EVENT_ROWS,
    rows: RowSet | None = None,
) -> WalkForward:
    """Monthly expanding-window refits from ``start``'s month; every origin
    t in month M is forecast with M's fit (rows realized by the last
    session before M) and regressors dated <= t."""
    rows = rows if rows is not None else build_rows(data, h)
    last = len(data.sessions) - 1 if through_idx is None else through_idx
    lo = bisect.bisect_left(data.sessions, start)
    starts = month_starts(data.sessions, lo, last)
    fits: dict[int, HarFit | None] = {}
    forecasts: dict[str, dict[int, float]] = {name: {} for name in data.names}
    for j, m0 in enumerate(starts):
        fit = fit_har(rows, through=m0 - 1, min_event_rows=min_event_rows)
        fits[m0] = fit
        if fit is None:
            continue
        end = starts[j + 1] - 1 if j + 1 < len(starts) else last
        for name, ns in data.names.items():
            for t in range(m0, end + 1):
                x = regressors(ns, t)
                if x is None:
                    continue
                f = predict(fit, name, x, n_earn_used(ns, t, h, data.coverage_idx))
                if f is not None:
                    forecasts[name][t] = f
    return WalkForward(h=h, fits=fits, forecasts=forecasts)


# --------------------------------------------------------- point forecasts

# The sealed FORECAST-001 verdict (docs/desk/FORECAST-001-results.md). Only a
# PASS makes the HAR forecast the desk's vol input; anything else (FAIL, or
# PENDING before the run) keeps the naive RV22 benchmark and shows HAR as
# unvalidated (the pre-registered failure rule). PASS as of the 2026-09-23 run
# (h=20 p=5.35e-07, h=63 p=7.79e-07; cutoff 2026-09-23). The forward-monitoring
# demotion rule of the pre-registration can set it back.
FORECAST_001_VERDICT = "PASS"


def forecast_source() -> str:
    return "har" if FORECAST_001_VERDICT == "PASS" else "rv22"


@dataclass(frozen=True)
class PointForecast:
    name: str
    session: date
    h: int
    forecast: float  # HAR variance over the next h sessions
    rv22: float | None  # h * mean22(v): the naive benchmark
    ewma: float | None
    n_earn: float
    fit_through: date
    be_estimated: bool
    fit_n: int


def forecasts_at(
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]],
    earnings: Mapping[str, Iterable[str]],
    cal: Calendar,
    names: Sequence[str],
    session: date,
    h: int,
    *,
    min_event_rows: int = MIN_EVENT_ROWS,
) -> dict[str, PointForecast]:
    """The HAR forecasts a desk run on ``session`` may use: the panel is
    cut at ``session``, the fit is the month's (rows realized by the last
    session before the month's first session), one pooled fit for all."""
    try:
        data = build_har_data(panel, earnings, cal, names, through=session)
        t = data.index(session)
    except (KeyError, ValueError):
        return {}
    m0 = bisect.bisect_left(data.sessions, date(session.year, session.month, 1))
    if m0 < 1:
        return {}
    fit = fit_har(build_rows(data, h), through=m0 - 1, min_event_rows=min_event_rows)
    if fit is None:
        return {}
    out: dict[str, PointForecast] = {}
    for name, ns in data.names.items():
        x = regressors(ns, t)
        if x is None:
            continue
        n_value = n_earn_used(ns, t, h, data.coverage_idx)
        f = predict(fit, name, x, n_value)
        if f is None:
            continue
        out[name] = PointForecast(
            name=name,
            session=session,
            h=h,
            forecast=f,
            rv22=rv22_forecast(ns, t, h),
            ewma=ewma_forecast(ns, t, h),
            n_earn=n_value,
            fit_through=data.sessions[m0 - 1],
            be_estimated=fit.be is not None,
            fit_n=fit.n,
        )
    return out


def forecast_at(
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]],
    earnings: Mapping[str, Iterable[str]],
    cal: Calendar,
    names: Sequence[str],
    name: str,
    session: date,
    h: int,
    *,
    min_event_rows: int = MIN_EVENT_ROWS,
) -> PointForecast | None:
    if name not in names:
        return None
    return forecasts_at(panel, earnings, cal, names, session, h, min_event_rows=min_event_rows).get(
        name
    )
