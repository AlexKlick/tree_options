"""Property tests for trex multi-leg structures (desk lane E1).

The oracle is expiry intrinsic value, computed here from first principles
(never through the implementation):

* max loss: for the same-expiry kinds (long single, verticals, condor) the
  structure's max_loss_per_package() equals the worst expiry P&L over a
  dense spot grid (plus every strike, where the piecewise-linear payoff
  kinks), with the entry filled at the plan limit, the worst allowed price:
  the cap paid for debit kinds, only the floor received for credit kinds.
* package value >= 0 in debit orientation: the package's intrinsic value
  is never negative, so every limit price the adapter sends is positive.
  For calendars and diagonals this is the value at the FRONT expiry with
  the back leg at its intrinsic (a lower bound of its price): same strike
  or a protective strike keeps it non-negative.
* any uncovered SELL (per right, more SELL legs than BUY legs) is refused.
* fuzz: arbitrary leg sets that DO validate satisfy the same invariants,
  so a validation hole shows up as a counterexample.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from tree_options.trex.plan import LegStructure, validate_package_order

ENTRY = date(2026, 9, 24)
DEADLINE = date(2026, 10, 9)
FRONT = date(2026, 10, 16)
BACK = date(2026, 11, 20)
SAME_EXPIRY_KINDS = {"long_single", "debit_vertical", "credit_vertical", "iron_condor"}
CREDIT_KINDS = {"credit_vertical", "iron_condor"}
ZERO = Decimal(0)

strikes = st.integers(2, 800).map(lambda n: Decimal(n) / 2)  # 1.0 .. 400.0 in 0.50 steps
rights = st.sampled_from(["C", "P"])


def _leg(right: str, action: str, strike: Decimal, expiry: date = FRONT) -> dict[str, Any]:
    return {"right": right, "action": action, "strike": strike, "expiry": expiry}


def _build(kind: str, legs: list[dict[str, Any]], limit: Decimal, qty: int = 1) -> LegStructure:
    return LegStructure(
        id="p",
        underlying="SPY",
        kind=kind,
        legs=legs,
        quantity=qty,
        entry_date=ENTRY,
        exit_deadline=DEADLINE,
        limit=limit,
        exits={"touch": False, "breach": False},
    )


def _intrinsic(right: str, strike: Decimal, spot: Decimal) -> Decimal:
    return max(ZERO, spot - strike) if right == "C" else max(ZERO, strike - spot)


def _value(legs: list[tuple[str, str, Decimal]], spot: Decimal) -> Decimal:
    """Signed intrinsic of (right, action, strike) legs: BUY +, SELL -."""
    return sum(
        ((1 if action == "BUY" else -1) * _intrinsic(right, k, spot) for right, action, k in legs),
        ZERO,
    )


def _grid(legs: list[tuple[str, str, Decimal]]) -> list[Decimal]:
    ks = [k for _, _, k in legs]
    top = 2 * max(ks) + 10
    dense = [top * i / 400 for i in range(401)]
    return sorted({ZERO, *ks, *dense, top * 10})


def _authored(s: LegStructure) -> list[tuple[str, str, Decimal]]:
    return [(g.right, g.action, g.strike) for g in s.legs]


def _check_same_expiry_max_loss(s: LegStructure, kind: str, limit: Decimal) -> None:
    legs = _authored(s)
    credit = kind in CREDIT_KINDS
    # entry at the worst allowed price: pay the cap / receive only the floor
    worst = max(-(_value(legs, x) + (limit if credit else -limit)) for x in _grid(legs))
    assert s.max_loss_per_package() >= worst
    assert s.max_loss_per_package() == worst  # and never overstated
    assert s.max_loss() == worst * 100 * s.quantity


def _check_package_non_negative(s: LegStructure) -> None:
    package = [(g.right, g.action, g.strike) for g in s.package_legs()]
    assert all(_value(package, x) >= 0 for x in _grid(package))


@st.composite
def same_expiry_structures(draw: st.DrawFn) -> tuple[str, list[dict[str, Any]], Decimal]:
    kind = draw(st.sampled_from(sorted(SAME_EXPIRY_KINDS)))
    if kind == "long_single":
        limit = Decimal(draw(st.integers(1, 5000))) / 100
        return kind, [_leg(draw(rights), "BUY", draw(strikes))], limit
    if kind == "iron_condor":
        a, b, c, d = sorted(draw(st.lists(strikes, min_size=4, max_size=4, unique=True)))
        legs = [
            _leg("P", "BUY", a),
            _leg("P", "SELL", b),
            _leg("C", "SELL", c),
            _leg("C", "BUY", d),
        ]
        wing = max(b - a, d - c)
    else:
        lo, hi = sorted(draw(st.lists(strikes, min_size=2, max_size=2, unique=True)))
        right = draw(rights)
        # debit: buy the strike nearer the money side of the view (put high / call low)
        buy_hi = (right == "P") == (kind == "debit_vertical")
        legs = [
            _leg(right, "BUY", hi if buy_hi else lo),
            _leg(right, "SELL", lo if buy_hi else hi),
        ]
        wing = hi - lo
    limit = Decimal(draw(st.integers(1, int(wing * 100) - 1))) / 100
    return kind, draw(st.permutations(legs)), limit


@st.composite
def two_expiry_structures(draw: st.DrawFn) -> tuple[str, list[dict[str, Any]], Decimal]:
    kind = draw(st.sampled_from(["calendar", "diagonal"]))
    right = draw(rights)
    if kind == "calendar":
        k = draw(strikes)
        k_buy, k_sell = k, k
    else:
        lo, hi = sorted(draw(st.lists(strikes, min_size=2, max_size=2, unique=True)))
        # protective: the long (back) call strike at or below the short's,
        # the long put strike at or above it
        k_buy, k_sell = (lo, hi) if right == "C" else (hi, lo)
    legs = [_leg(right, "SELL", k_sell, FRONT), _leg(right, "BUY", k_buy, BACK)]
    limit = Decimal(draw(st.integers(1, 5000))) / 100
    return kind, draw(st.permutations(legs)), limit


@settings(max_examples=300, deadline=None)
@given(same_expiry_structures(), st.integers(1, 7))
def test_max_loss_is_the_worst_expiry_loss(
    case: tuple[str, list[dict[str, Any]], Decimal], qty: int
) -> None:
    kind, legs, limit = case
    s = _build(kind, legs, limit, qty)
    _check_same_expiry_max_loss(s, kind, limit)


@settings(max_examples=300, deadline=None)
@given(st.one_of(same_expiry_structures(), two_expiry_structures()))
def test_package_value_is_never_negative(case: tuple[str, list[dict[str, Any]], Decimal]) -> None:
    kind, legs, limit = case
    _check_package_non_negative(_build(kind, legs, limit))


@settings(max_examples=300, deadline=None)
@given(
    st.sampled_from(
        ["long_single", "debit_vertical", "credit_vertical", "iron_condor", "calendar", "diagonal"]
    ),
    rights,
    st.integers(1, 3),
    st.data(),
)
def test_any_uncovered_sell_is_refused(
    kind: str, right: str, sells: int, data: st.DataObject
) -> None:
    buys = data.draw(st.integers(0, sells - 1))
    other = data.draw(st.lists(st.sampled_from(["BUY", "SELL"]), max_size=2))
    other_right = "C" if right == "P" else "P"
    n = sells + buys + len(other)
    ks = data.draw(st.lists(strikes, min_size=n, max_size=n, unique=True))
    expiries = data.draw(st.lists(st.sampled_from([FRONT, BACK]), min_size=n, max_size=n))
    actions = ["SELL"] * sells + ["BUY"] * buys
    legs = [_leg(right, a, ks[i], expiries[i]) for i, a in enumerate(actions)]
    legs += [
        _leg(other_right, a, ks[len(actions) + i], expiries[len(actions) + i])
        for i, a in enumerate(other)
    ]
    limit = Decimal(data.draw(st.integers(1, 5000))) / 100
    try:
        _build(kind, data.draw(st.permutations(legs)), limit)
    except ValidationError as err:
        assert any("uncovered" in str(e["msg"]) for e in err.errors())
    else:
        raise AssertionError(f"uncovered SELL accepted: {kind} {legs}")


@settings(max_examples=500, deadline=None)
@given(
    st.sampled_from(
        ["long_single", "debit_vertical", "credit_vertical", "iron_condor", "calendar", "diagonal"]
    ),
    st.lists(
        st.tuples(
            rights,
            st.sampled_from(["BUY", "SELL"]),
            st.integers(180, 220).map(Decimal),
            st.sampled_from([FRONT, BACK]),
        ),
        min_size=1,
        max_size=4,
    ),
    st.integers(1, 3000),
)
def test_whatever_validates_is_defined_risk(
    kind: str, raw: list[tuple[str, str, Decimal, date]], limit_cents: int
) -> None:
    limit = Decimal(limit_cents) / 100
    try:
        s = _build(kind, [_leg(*r) for r in raw], limit)
    except ValidationError:
        return
    _check_package_non_negative(s)
    if kind in SAME_EXPIRY_KINDS:
        _check_same_expiry_max_loss(s, kind, limit)


@settings(max_examples=400, deadline=None)
@given(st.one_of(same_expiry_structures(), two_expiry_structures()), st.data())
def test_accepted_orders_never_lose_more_than_the_worst_case(
    case: tuple[str, list[dict[str, Any]], Decimal], data: st.DataObject
) -> None:
    """Any open price and close price validate_package_order accepts realize
    at most the worst-case loss: the worst expiry loss at the limit (from the
    intrinsic oracle) for same-expiry kinds, the debit cap otherwise."""
    kind, legs, limit = case
    s = _build(kind, legs, limit)
    authored = _authored(s)
    top = 2 * max(k for _, _, k in authored) + 10
    p_open = Decimal(data.draw(st.integers(1, int(limit * 200)))) / 100
    p_close = Decimal(data.draw(st.integers(1, int(top * 100)))) / 100
    try:
        validate_package_order(s, s.open_side, 1, p_open)
        validate_package_order(s, s.close_side, 1, p_close)
    except ValueError:
        return
    credit = kind in CREDIT_KINDS
    realized_loss = p_close - p_open if credit else p_open - p_close
    if kind in SAME_EXPIRY_KINDS:
        bound = max(-(_value(authored, x) + (limit if credit else -limit)) for x in _grid(authored))
    else:
        bound = limit
    assert realized_loss <= bound
