"""Desk D6 structure valuation at the planned exit.

Oracles are computed here, independently of the implementation: a
Black-Scholes written below (erfc form), the total-variance closed form
for the sanity oracle, and plain-python linear interpolation of the
fixture's declared smile and spreads for the deterministic exit checks.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from tests.fixtures import desk_pricing as fx
from tree_options.desk import distribution as dist
from tree_options.desk import pit, pricing
from tree_options.synth_options.greeks import bs_price
from tree_options.time.calendar import StaticSessionCalendar
from tree_options.trex.plan import ExitRules, Leg, LegStructure

D = date(2025, 3, 12)
ENTRY = date(2025, 3, 13)
EXIT = date(2025, 4, 9)  # D + 20 sessions
EXP1 = date(2025, 5, 16)
EXP2 = date(2025, 6, 20)
COMMISSION = 0.65


@pytest.fixture(scope="module")
def cal() -> StaticSessionCalendar:
    return fx.trex_calendar()


# ---------------------------------------------------------------- oracle


def _ncdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _bs(s: float, k: float, days: int, iv: float, r: float, right: str) -> float:
    t = max(days, 0.5) / 365.0
    v = iv * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * iv * iv) * t) / v
    d2 = d1 - v
    if right == "C":
        return s * _ncdf(d1) - k * math.exp(-r * t) * _ncdf(d2)
    return k * math.exp(-r * t) * _ncdf(-d2) - s * _ncdf(-d1)


def _interp(x: float, xs: Sequence[float], ys: Sequence[float]) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for (x0, y0), (x1, y1) in itertools.pairwise(zip(xs, ys, strict=True)):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    raise AssertionError("unreachable")


def _struct(
    kind: str,
    legs: Sequence[tuple[str, str, float, date]],
    *,
    limit: str = "3",
    exit_deadline: date = EXIT,
    qty: int = 1,
    sid: str = "s",
) -> LegStructure:
    return LegStructure(
        id=sid,
        underlying="AAA",
        kind=kind,  # type: ignore[arg-type]
        legs=tuple(
            Leg(right=r, action=a, strike=Decimal(str(k)), expiry=e)  # type: ignore[arg-type]
            for r, a, k, e in legs
        ),
        quantity=qty,
        entry_date=ENTRY,
        exit_deadline=exit_deadline,
        limit=Decimal(limit),
        exits=ExitRules(touch=False, breach=False),
    )


CALL_VERTICAL = [("C", "BUY", 100.0, EXP1), ("C", "SELL", 110.0, EXP1)]
PUT_VERTICAL = [("P", "BUY", 100.0, EXP1), ("P", "SELL", 90.0, EXP1)]
PUT_CREDIT = [("P", "SELL", 100.0, EXP1), ("P", "BUY", 90.0, EXP1)]
CONDOR = [
    ("P", "BUY", 85.0, EXP1),
    ("P", "SELL", 90.0, EXP1),
    ("C", "SELL", 110.0, EXP1),
    ("C", "BUY", 115.0, EXP1),
]
LONG_CALL = [("C", "BUY", 100.0, EXP1)]


def _doc(
    rate: float,
    *,
    iv: Callable[[float], float] = fx.smile,
    half_spread: Callable[[float], float] = fx.half_spread,
    strikes: Sequence[float] = tuple(float(k) for k in range(70, 135, 5)),
) -> dict[str, Any]:
    return fx.chain_doc(
        sym="AAA",
        session=D,
        spot=100.0,
        rate=rate,
        expiries=[EXP1, EXP2],
        strikes=strikes,
        iv=iv,
        half_spread=half_spread,
        source_as_of="2025-03-13T03:49:00+00:00",
    )


def _pool(seed: int, n: int = 20_000) -> np.ndarray:
    return dist.prepare_pool(np.random.default_rng(seed).normal(size=n))


# ---------------------------------------------------------- the pricer


def test_vectorized_pricer_matches_the_pinned_bs_price() -> None:
    spots = np.array([37.0, 50.0, 99.5, 100.0, 150.0, 400.0])
    for strike, dte, iv, r, q, right in itertools.product(
        (80.0, 100.0, 120.0), (0, 1, 30, 400), (0.05, 0.3, 1.5), (0.0, 0.05), (0.0, 0.02), "CP"
    ):
        got = pricing.bs_price_vec(
            spot=spots,
            strike=strike,
            dte_calendar_days=dte,
            iv=np.full(len(spots), iv),
            risk_free=r,
            dividend_yield=q,
            call_put=right,
        )
        for s, g in zip(spots, got, strict=True):
            want = bs_price(
                spot=float(s),
                strike=strike,
                dte_calendar_days=dte,
                iv=iv,
                risk_free=r,
                dividend_yield=q,
                call_put=right,  # type: ignore[arg-type]
            )
            assert abs(g - want) <= 1e-12 * max(1.0, want)


# --------------------------------------------------- sanity oracle (brief #5)


def test_no_view_ev_matches_the_closed_form(cal: StaticSessionCalendar) -> None:
    """Zero drift (r = 0, no view), flat IV, zero spreads: the exit value of
    a European debit vertical over lognormal paths is Black-Scholes on the
    total variance (implied to expiry after the exit + physical over the
    hold), so EV = 100 x (BS(v_tot) - BS(v_entry)) - commissions."""
    iv, phys = 0.30, 0.20
    doc = _doc(0.0, iv=lambda m: iv, half_spread=lambda p: 0.0)
    surf = pricing.EntrySurface(doc, session=D, rate=0.0)
    sigma2 = phys * phys / 252.0
    ps = dist.simulate_paths(
        name="AAA", session=D, exit_session=EXIT, cal=cal, row_id="oracle",
        z_pool=_pool(21, 50_000), daily_var=sigma2, rate=0.0,
    )  # fmt: skip
    assert ps.spec.n_paths == dist.N_PATHS == 20_000
    val = pricing.price_structure(
        _struct("debit_vertical", CALL_VERTICAL), ps, surf, mr=pricing.IvMeanReversion.off()
    )

    def vertical(v: float) -> float:
        def call(k: float) -> float:
            d1 = (math.log(100.0 / k) + v / 2.0) / math.sqrt(v)
            return 100.0 * _ncdf(d1) - k * _ncdf(d1 - math.sqrt(v))

        return call(100.0) - call(110.0)

    v_entry = iv * iv * (EXP1 - D).days / 365.0
    v_total = iv * iv * (EXP1 - EXIT).days / 365.0 + 20 * sigma2
    want = 100.0 * (vertical(v_total) - vertical(v_entry)) - 4 * COMMISSION
    se = float(val.ev_se)
    assert se > 0.0
    assert abs(float(val.ev) - want) < 4.0 * se + 0.01
    # discriminating: the physical/implied gap is many standard errors wide
    assert abs(want - (-4 * COMMISSION)) > 8.0 * se
    assert val.round_trip_cost == Decimal("2.60")  # zero spreads: commissions only


# ------------------------------------------- deterministic exit valuation


def _constant_paths(
    cal: StaticSessionCalendar, s_exit: float, *, signal: dist.SignalDrift | None = None
) -> dist.PathSet:
    ps = dist.simulate_paths(
        name="AAA", session=D, exit_session=EXIT, cal=cal, row_id="c",
        z_pool=_pool(1, 400), daily_var=1e-4, rate=0.03, signal=signal, n_paths=8,
    )  # fmt: skip
    return dataclasses.replace(ps, log_paths=np.full(ps.log_paths.shape, math.log(s_exit / 100.0)))


def _oracle_pnl(
    legs: Sequence[tuple[str, str, float, date]],
    s_exit: float,
    *,
    credit: bool,
    k: float = 0.5,
    iv_mult: float = 1.0,
    mr: float = 1.0,
    width: float | None = None,
    rate: float = 0.03,
    strikes: Sequence[float] = tuple(float(x) for x in range(70, 135, 5)),
) -> float:
    """P&L in dollars of one package: entry at the fixture's quotes, exit on
    the entry smile/spreads at the new moneyness (linear in ln(K/S), flat
    beyond the nodes), IV x mr x iv_mult, fills mid +- k x half-spread."""
    ms = [math.log(x / 100.0) for x in strikes]
    mid0 = hs0 = mid1 = hs1 = 0.0
    for right, action, strike, expiry in legs:
        sign = 1.0 if action == "BUY" else -1.0
        if credit:
            sign = -sign  # debit orientation
        px = [
            _bs(100.0, x, (expiry - D).days, fx.smile(m), rate, right)
            for x, m in zip(strikes, ms, strict=True)
        ]
        hs = [fx.half_spread(p) for p in px]
        i = strikes.index(strike)
        mid0 += sign * px[i]
        hs0 += hs[i]
        m = math.log(strike / s_exit)
        ivx = _interp(m, ms, [fx.smile(x) for x in ms]) * mr * iv_mult
        mid1 += sign * _bs(s_exit, strike, (expiry - EXIT).days, ivx, rate, right)
        hs1 += _interp(m, ms, hs)
    n = len(legs)
    if not credit:
        pnl = max(0.0, mid1 - k * hs1) - (mid0 + k * hs0)
    else:
        assert width is not None
        pnl = (mid0 - k * hs0) - min(width, max(0.0, mid1 + k * hs1))
    return 100.0 * pnl - 2 * n * COMMISSION


@pytest.mark.parametrize("s_exit", [95.0, 104.0])
def test_exit_is_sticky_moneyness_with_mean_reversion_spreads_and_costs(
    cal: StaticSessionCalendar, s_exit: float
) -> None:
    surf = pricing.EntrySurface(_doc(0.03), session=D, rate=0.03)
    mr = pricing.IvMeanReversion(
        factor=1.1, status="applied", reason="test", ratio=1.2, n=200, half_life=30
    )
    signal = dist.SignalDrift(excess_20=0.03, weight=0.5)
    ps = _constant_paths(cal, s_exit, signal=signal)
    val = pricing.price_structure(_struct("debit_vertical", PUT_VERTICAL), ps, surf, mr=mr)

    def o(s: float, **kw: Any) -> float:
        return _oracle_pnl(PUT_VERTICAL, s, credit=False, mr=1.1, **kw)

    assert float(val.ev) == pytest.approx(o(s_exit), abs=0.006)
    assert float(val.ev_fill_stress) == pytest.approx(o(s_exit, k=1.0), abs=0.006)
    assert float(val.ev_iv_up) == pytest.approx(o(s_exit, iv_mult=1.2), abs=0.006)
    assert float(val.ev_iv_down) == pytest.approx(o(s_exit, iv_mult=0.8), abs=0.006)
    assert float(val.ev_crash) == pytest.approx(o(s_exit * 0.98), abs=0.006)
    assert float(val.ev_stress) == pytest.approx(o(s_exit * 0.98, iv_mult=1.2), abs=0.006)
    s_sig = s_exit * math.exp(20 * 0.5 * math.log(1.03) / 20.0)
    assert val.ev_signal is not None
    assert float(val.ev_signal) == pytest.approx(o(s_sig), abs=0.006)
    assert float(val.cvar5) == pytest.approx(o(s_exit), abs=0.006)
    assert val.ev_se == Decimal("0.00")
    assert val.p_profit in (0.0, 1.0) and val.p_profit == (1.0 if o(s_exit) > 0 else 0.0)
    assert val.p_max_loss == 0.0
    # entry fill (debit orientation) and max loss from the structure
    mid0 = _bs(100.0, 100.0, 65, fx.smile(0.0), 0.03, "P") - _bs(
        100.0, 90.0, 65, fx.smile(math.log(0.9)), 0.03, "P"
    )
    assert float(val.entry_mid) == pytest.approx(mid0, abs=1e-4)
    assert val.max_loss == Decimal("300")  # limit 3 x 100 x 1


def test_debit_floor_and_credit_cap_are_max_loss(cal: StaticSessionCalendar) -> None:
    surf = pricing.EntrySurface(_doc(0.03), session=D, rate=0.03)
    off = pricing.IvMeanReversion.off()
    deep = _constant_paths(cal, 50.0)
    debit = pricing.price_structure(_struct("debit_vertical", CALL_VERTICAL), deep, surf, mr=off)
    assert debit.p_max_loss == 1.0 and debit.p_profit == 0.0
    assert float(debit.ev) == pytest.approx(
        _oracle_pnl(CALL_VERTICAL, 50.0, credit=False), abs=0.006
    )
    # proceeds floored at 0: the whole entry fill plus commissions is lost
    assert float(debit.ev) == pytest.approx(-100.0 * float(debit.entry_fill) - 2.6, abs=0.006)
    credit = pricing.price_structure(
        _struct("credit_vertical", PUT_CREDIT, limit="2"), deep, surf, mr=off
    )
    assert credit.p_max_loss == 1.0
    want = _oracle_pnl(PUT_CREDIT, 50.0, credit=True, width=10.0)
    assert float(credit.ev) == pytest.approx(want, abs=0.006)
    # closing cost capped at the width: loss = width - credit + commissions
    assert float(credit.ev) == pytest.approx(
        100.0 * (float(credit.entry_fill) - 10.0) - 2.6, abs=0.006
    )


def test_cvar_probability_and_standard_error(cal: StaticSessionCalendar) -> None:
    surf = pricing.EntrySurface(_doc(0.03), session=D, rate=0.03)
    ps = dist.simulate_paths(
        name="AAA", session=D, exit_session=EXIT, cal=cal, row_id="c",
        z_pool=_pool(1, 400), daily_var=1e-4, rate=0.03, n_paths=100,
    )  # fmt: skip
    terminal = np.array([math.log(1.05)] * 97 + [math.log(0.80)] * 3)
    ps = dataclasses.replace(
        ps, log_paths=np.repeat(terminal[:, None], ps.log_paths.shape[1], axis=1)
    )
    val = pricing.price_structure(
        _struct("debit_vertical", CALL_VERTICAL), ps, surf, mr=pricing.IvMeanReversion.off()
    )
    win = _oracle_pnl(CALL_VERTICAL, 105.0, credit=False)
    loss = _oracle_pnl(CALL_VERTICAL, 80.0, credit=False)
    assert win > 0.0 > loss
    assert val.p_profit == pytest.approx(0.97)
    assert float(val.ev) == pytest.approx((97 * win + 3 * loss) / 100, abs=0.006)
    # the worst ceil(5% x 100) = 5 outcomes: 3 losses and 2 wins
    assert float(val.cvar5) == pytest.approx((3 * loss + 2 * win) / 5, abs=0.006)
    se = float(np.std([win] * 97 + [loss] * 3, ddof=1)) / 10.0
    assert float(val.ev_se) == pytest.approx(se, abs=0.006)


# ------------------------------------------------------ stress directions


@pytest.fixture(scope="module")
def mc(cal: StaticSessionCalendar) -> tuple[pricing.EntrySurface, dist.PathSet, dist.PathSet]:
    surf = pricing.EntrySurface(_doc(0.04), session=D, rate=0.04)
    kw: dict[str, Any] = dict(
        name="AAA", session=D, exit_session=EXIT, cal=cal, row_id="mc",
        z_pool=_pool(8), daily_var=0.3**2 / 252, rate=0.04,
    )  # fmt: skip
    up = dist.simulate_paths(signal=dist.SignalDrift(excess_20=0.05, weight=0.5), **kw)
    down = dist.simulate_paths(signal=dist.SignalDrift(excess_20=-0.05, weight=0.5), **kw)
    return surf, up, down


def test_stress_knobs_move_ev_the_right_way(
    mc: tuple[pricing.EntrySurface, dist.PathSet, dist.PathSet],
) -> None:
    surf, up, down = mc
    off = pricing.IvMeanReversion.off()

    def value(legs: Any, kind: str, ps: dist.PathSet, limit: str = "3") -> pricing.Valuation:
        return pricing.price_structure(_struct(kind, legs, limit=limit), ps, surf, mr=off)

    cv = value(CALL_VERTICAL, "debit_vertical", up)
    assert cv.ev_fill_stress < cv.ev
    assert cv.ev_crash < cv.ev  # long delta
    assert cv.ev_signal is not None and cv.ev_signal > cv.ev  # positive drift view
    cv_down = value(CALL_VERTICAL, "debit_vertical", down)
    assert cv_down.ev == cv.ev  # no-view EV ignores the view
    assert cv_down.ev_signal is not None and cv_down.ev_signal < cv_down.ev
    pv = value(PUT_VERTICAL, "debit_vertical", up)
    assert pv.ev_crash > pv.ev  # short delta
    lc = value(LONG_CALL, "long_single", up, limit="10")
    assert lc.ev_iv_up > lc.ev > lc.ev_iv_down  # long vega
    ic = value(CONDOR, "iron_condor", up, limit="1")
    assert ic.ev_iv_up < ic.ev < ic.ev_iv_down  # short vega
    assert ic.ev_stress < ic.ev
    assert ic.ev_fill_stress < ic.ev
    for v in (cv, pv, lc, ic):
        assert 0.0 <= v.p_max_loss <= 1.0 and 0.0 <= v.p_profit <= 1.0
        assert v.cvar5 <= v.ev


def test_calendar_reprices_both_expiries(
    mc: tuple[pricing.EntrySurface, dist.PathSet, dist.PathSet],
) -> None:
    surf, up, _ = mc
    legs = [("C", "SELL", 100.0, EXP1), ("C", "BUY", 100.0, EXP2)]
    val = pricing.price_structure(
        _struct("calendar", legs), up, surf, mr=pricing.IvMeanReversion.off()
    )
    assert val.ev_iv_up > val.ev > val.ev_iv_down  # a calendar is long vega
    assert val.max_loss == Decimal("300")


# ------------------------------------------------------------- refusals


def test_refusals(cal: StaticSessionCalendar) -> None:
    surf = pricing.EntrySurface(_doc(0.03), session=D, rate=0.03)
    ps = _constant_paths(cal, 100.0)
    off = pricing.IvMeanReversion.off()
    early = date(2025, 4, 4)  # a leg expiring before the planned exit
    s = _struct(
        "debit_vertical",
        [("C", "BUY", 100.0, early), ("C", "SELL", 110.0, early)],
        exit_deadline=date(2025, 4, 3),
    )
    with pytest.raises(pit.NotEvaluable, match="expire"):
        pricing.price_structure(s, ps, surf, mr=off)
    s = _struct("debit_vertical", CALL_VERTICAL, exit_deadline=date(2025, 4, 1))
    with pytest.raises(pit.NotEvaluable, match="exit deadline"):
        pricing.price_structure(s, ps, surf, mr=off)
    s = _struct("debit_vertical", [("C", "BUY", 101.0, EXP1), ("C", "SELL", 110.0, EXP1)])
    with pytest.raises(pit.NotEvaluable, match="quote"):
        pricing.price_structure(s, ps, surf, mr=off)
    other = date(2025, 5, 23)
    s = _struct("debit_vertical", [("C", "BUY", 100.0, other), ("C", "SELL", 110.0, other)])
    with pytest.raises(pit.NotEvaluable, match="quote"):
        pricing.price_structure(s, ps, surf, mr=off)
    s = _struct("debit_vertical", CALL_VERTICAL).model_copy(update={"underlying": "BBB"})
    with pytest.raises(ValueError, match="underlying"):
        pricing.price_structure(s, ps, surf, mr=off)


def test_money_is_decimal_and_serializes_as_strings(cal: StaticSessionCalendar) -> None:
    surf = pricing.EntrySurface(_doc(0.03), session=D, rate=0.03)
    val = pricing.price_structure(
        _struct("debit_vertical", CALL_VERTICAL),
        _constant_paths(cal, 103.0),
        surf,
        mr=pricing.IvMeanReversion.off(),
    )
    for money in (val.ev, val.cvar5, val.ev_stress, val.max_loss, val.round_trip_cost):
        assert isinstance(money, Decimal)
    assert val.ev.as_tuple().exponent == -2
    doc = val.as_dict()
    assert doc["ev"] == str(val.ev) and doc["max_loss"] == "300.00"
    assert isinstance(doc["p_profit"], float)
    assert doc["ev_signal"] is None


# ------------------------------------------------------- IV mean reversion


def _hist(cal: StaticSessionCalendar, values: Sequence[float], end: date) -> dict[date, Any]:
    s = cal.sessions()
    i = s.index(end)
    days = s[i - len(values) + 1 : i + 1]
    return {d: (v, "interpolated") for d, v in zip(days, values, strict=True)}


def test_iv_mean_reversion_hand_values(cal: StaticSessionCalendar) -> None:
    h = _hist(cal, [0.20] * 199 + [0.25], D)
    mr = pricing.iv_mean_reversion(h, D, 30, cal)
    # median 0.20 / current 0.25 = 0.8; half the gap closes in 30 sessions
    assert mr.status == "applied" and mr.n == 200
    assert mr.ratio == pytest.approx(0.8)
    assert mr.factor == pytest.approx(1.0 + (0.8 - 1.0) * 0.5)
    assert pricing.iv_mean_reversion(h, D, 60, cal).factor == pytest.approx(1.0 - 0.2 * 0.75)
    clamped = pricing.iv_mean_reversion(_hist(cal, [0.20] * 199 + [0.05], D), D, 30, cal)
    assert clamped.ratio == 2.0 and clamped.factor == pytest.approx(1.5)
    stale = pricing.iv_mean_reversion(
        _hist(cal, [0.2] * 200, cal.sessions()[cal.sessions().index(D) - 10]), D, 30, cal
    )
    assert stale.status == "unavailable" and stale.factor == 1.0
    short = pricing.iv_mean_reversion(_hist(cal, [0.2] * 100, D), D, 30, cal)
    assert short.status == "unavailable" and short.factor == 1.0
    assert pricing.iv_mean_reversion({}, D, 30, cal).factor == 1.0
    # nothing after D is read
    future = dict(h)
    future.update(_hist(cal, [9.9] * 5, cal.nth_after(D, 5)))
    assert pricing.iv_mean_reversion(future, D, 30, cal) == mr
