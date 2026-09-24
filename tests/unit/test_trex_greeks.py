"""Structure greeks (desk lane E4, greeks half).

Oracles are independent of trex.greeks: textbook Black-Scholes literals
(S = K = 100, r = 5%, sigma = 20%, T = 1y: call delta 0.63683, gamma
0.018762, vega 37.524 per 1.00 of vol, theta -6.41403 per year), an
analytic Black-Scholes written out here with math.erf, and finite
differences of the shared synth_options pricer for the units. Leg quotes
are built from the oracle's own prices, so the solved IV is the one planted.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from tree_options.synth_options.greeks import bs_price
from tree_options.trex import greeks
from tree_options.trex.greeks import StructureGreeks, structure_greeks
from tree_options.trex.plan import LegStructure

AS_OF = date(2026, 9, 24)
FRONT = date(2026, 10, 16)  # 22 calendar days
BACK = date(2026, 11, 20)  # 57 calendar days
YEAR_OUT = date(2027, 9, 24)  # 365 calendar days


def _leg(right: str, action: str, strike: str, expiry: date = FRONT) -> dict[str, Any]:
    return {"right": right, "action": action, "strike": strike, "expiry": expiry}


def _spec(kind: str, legs: list[dict[str, Any]], limit: str, qty: int = 1) -> LegStructure:
    return LegStructure(
        id="s",
        underlying="SPY",
        kind=kind,
        legs=legs,
        quantity=qty,
        entry_date=date(2026, 9, 23),
        exit_deadline=date(2026, 10, 9),
        limit=limit,
        exits={"touch": False, "breach": False},
    )


def _n(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs(s: float, k: float, days: int, r: float, sigma: float, right: str) -> dict[str, float]:
    """Price and per-share greeks (vega per 1.00 of sigma, theta per YEAR), q = 0."""
    t = days / 365.0
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    disc = math.exp(-r * t)
    decay = -s * _n(d1) * sigma / (2.0 * math.sqrt(t))
    if right == "C":
        price = s * _cdf(d1) - k * disc * _cdf(d2)
        delta = _cdf(d1)
        theta = decay - r * k * disc * _cdf(d2)
    else:
        price = k * disc * _cdf(-d2) - s * _cdf(-d1)
        delta = _cdf(d1) - 1.0
        theta = decay + r * k * disc * _cdf(-d2)
    return {
        "price": price,
        "delta": delta,
        "gamma": _n(d1) / (s * sigma * math.sqrt(t)),
        "vega": s * _n(d1) * math.sqrt(t),
        "theta": theta,
    }


def _quote(price: float) -> tuple[Decimal, Decimal]:
    """A leg quote whose mid is the oracle price (exactly; locked, so even
    a far wing worth a fraction of a cent keeps a non-negative bid)."""
    p = Decimal(repr(price))
    return p, p


def _oracle(
    spec: LegStructure, spot: float, r: float, vols: list[float]
) -> tuple[list[tuple[Decimal, Decimal]], StructureGreeks]:
    """Leg quotes priced at ``vols`` and the position's greeks, by hand."""
    quotes, delta, gamma, vega, theta = [], 0.0, 0.0, 0.0, 0.0
    for leg, vol in zip(spec.legs, vols, strict=True):
        days = (leg.expiry - AS_OF).days
        g = _bs(spot, float(leg.strike), days, r, vol, leg.right)
        quotes.append(_quote(g["price"]))
        units = (1 if leg.action == "BUY" else -1) * 100 * spec.quantity
        delta += units * g["delta"]
        gamma += units * g["gamma"]
        vega += units * g["vega"] * 0.01
        theta += units * g["theta"] / 365.0
    return quotes, StructureGreeks(delta, gamma, vega, theta)


def _close(got: StructureGreeks | None, want: StructureGreeks, rel: float = 1e-6) -> None:
    assert got is not None
    for f in dataclasses.fields(StructureGreeks):
        assert getattr(got, f.name) == pytest.approx(getattr(want, f.name), rel=rel, abs=1e-9)


class TestInterface:
    def test_frozen_fields_and_units(self) -> None:
        assert [f.name for f in dataclasses.fields(StructureGreeks)] == [
            "delta_shares",
            "gamma_shares_per_usd",
            "vega_usd_per_volpt",
            "theta_usd_per_day",
        ]
        g = StructureGreeks(1.0, 2.0, 3.0, 4.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            g.delta_shares = 5.0  # type: ignore[misc]

    def test_dividend_yield_is_declared_zero(self) -> None:
        assert greeks.__doc__ is not None and "q = 0" in greeks.__doc__


class TestTextbook:
    def test_one_long_call(self) -> None:
        spec = _spec("long_single", [_leg("C", "BUY", "100", YEAR_OUT)], "11.00")
        # the textbook price 10.4506 as the leg mid
        quotes = [(Decimal("10.4406"), Decimal("10.4606"))]
        got = structure_greeks(spec, quotes, Decimal("100"), 0.05, AS_OF)
        assert got is not None
        assert got.delta_shares == pytest.approx(63.683, rel=1e-4)
        assert got.gamma_shares_per_usd == pytest.approx(1.8762, rel=1e-4)
        assert got.vega_usd_per_volpt == pytest.approx(37.524, rel=1e-4)
        assert got.theta_usd_per_day == pytest.approx(-6.41403 / 365 * 100, rel=1e-4)

    def test_one_long_put(self) -> None:
        # put-call parity: 10.4506 - 100 + 100 e^-0.05 = 5.5735
        spec = _spec("long_single", [_leg("P", "BUY", "100", YEAR_OUT)], "6.00")
        quotes = [(Decimal("5.5635"), Decimal("5.5835"))]
        got = structure_greeks(spec, quotes, Decimal("100"), 0.05, AS_OF)
        assert got is not None
        assert got.delta_shares == pytest.approx(-36.317, rel=1e-4)
        assert got.gamma_shares_per_usd == pytest.approx(1.8762, rel=1e-4)
        assert got.theta_usd_per_day == pytest.approx(-1.65788 / 365 * 100, rel=1e-4)


CREDIT_PUT = _spec(
    "credit_vertical", [_leg("P", "SELL", "95"), _leg("P", "BUY", "90")], "1.00", qty=3
)
CONDOR = _spec(
    "iron_condor",
    [
        _leg("P", "BUY", "92"),
        _leg("P", "SELL", "96"),
        _leg("C", "SELL", "104"),
        _leg("C", "BUY", "108"),
    ],
    "1.00",
    qty=2,
)
CALENDAR = _spec(
    "calendar", [_leg("C", "SELL", "100", FRONT), _leg("C", "BUY", "100", BACK)], "1.50", qty=4
)
DEBIT_CALL = _spec("debit_vertical", [_leg("C", "BUY", "100"), _leg("C", "SELL", "110")], "4.00")


class TestPositions:
    @pytest.mark.parametrize(
        ("spec", "vols"),
        [
            (CREDIT_PUT, [0.25, 0.28]),  # skew: the lower strike richer
            (CONDOR, [0.31, 0.27, 0.19, 0.18]),
            (CALENDAR, [0.22, 0.20]),  # two expiries, two DTEs
            (DEBIT_CALL, [0.21, 0.19]),
        ],
        ids=["credit_put", "condor", "calendar", "debit_call"],
    )
    def test_signed_sum_of_legs(self, spec: LegStructure, vols: list[float]) -> None:
        quotes, want = _oracle(spec, 100.0, 0.03, vols)
        got = structure_greeks(spec, quotes, Decimal("100"), 0.03, AS_OF)
        _close(got, want)

    def test_a_short_leg_flips_the_sign(self) -> None:
        # the credit put is short the 95 (richer) put: net long delta, short vega
        quotes, _ = _oracle(CREDIT_PUT, 100.0, 0.03, [0.25, 0.28])
        got = structure_greeks(CREDIT_PUT, quotes, Decimal("100"), 0.03, AS_OF)
        assert got is not None and got.delta_shares > 0 and got.vega_usd_per_volpt < 0
        assert got.theta_usd_per_day > 0

    def test_units_by_finite_difference(self) -> None:
        # the shared pricer, bumped: the condor's dollar value moves by the
        # greeks' own units (x100 multiplier x quantity, vol points, days)
        vols = [0.31, 0.27, 0.19, 0.18]
        quotes, _ = _oracle(CONDOR, 100.0, 0.03, vols)
        got = structure_greeks(CONDOR, quotes, Decimal("100"), 0.03, AS_OF)
        assert got is not None

        def value(spot: float, bump: float = 0.0, days: int = 22) -> float:
            total = 0.0
            for leg, vol in zip(CONDOR.legs, vols, strict=True):
                p = bs_price(
                    spot=spot,
                    strike=float(leg.strike),
                    dte_calendar_days=days,
                    iv=vol + bump,
                    risk_free=0.03,
                    dividend_yield=0.0,
                    call_put=leg.right,
                )
                total += (1 if leg.action == "BUY" else -1) * p * 100 * CONDOR.quantity
            return total

        h = 0.01
        assert (value(100 + h) - value(100 - h)) / (2 * h) == pytest.approx(
            got.delta_shares, rel=1e-3
        )
        assert (value(100, 0.0001) - value(100, -0.0001)) / 0.02 == pytest.approx(
            got.vega_usd_per_volpt, rel=1e-3
        )
        # one calendar day of decay, first order
        assert value(100, days=21) - value(100) == pytest.approx(got.theta_usd_per_day, rel=0.05)


class TestNotEvaluable:
    def _quotes(self) -> list[tuple[Decimal, Decimal] | None]:
        quotes, _ = _oracle(CREDIT_PUT, 100.0, 0.03, [0.25, 0.28])
        return list(quotes)

    def test_a_missing_leg_quote(self) -> None:
        quotes = self._quotes()
        quotes[1] = None
        assert structure_greeks(CREDIT_PUT, quotes, Decimal("100"), 0.03, AS_OF) is None

    @pytest.mark.parametrize(
        "quote",
        [
            (Decimal("0"), Decimal("0")),  # no premium: no vol prices it
            (Decimal("0.30"), Decimal("0.20")),  # crossed
            (Decimal("-0.10"), Decimal("0.10")),  # negative bid
            (Decimal("0.01"), Decimal("0.03")),  # 95 put at spot 80: below intrinsic
        ],
    )
    def test_an_unsolvable_leg(self, quote: tuple[Decimal, Decimal]) -> None:
        quotes = self._quotes()
        quotes[0] = quote
        spot = Decimal("80") if quote[1] == Decimal("0.03") else Decimal("100")
        assert structure_greeks(CREDIT_PUT, quotes, spot, 0.03, AS_OF) is None

    def test_after_an_expiry(self) -> None:
        assert (
            structure_greeks(CREDIT_PUT, self._quotes(), Decimal("100"), 0.03, date(2026, 10, 17))
            is None
        )

    @pytest.mark.parametrize("spot", ["0", "-1", "NaN", "Infinity"])
    def test_no_usable_spot(self, spot: str) -> None:
        assert structure_greeks(CREDIT_PUT, self._quotes(), Decimal(spot), 0.03, AS_OF) is None

    def test_quotes_must_match_the_legs(self) -> None:
        with pytest.raises(ValueError, match="leg"):
            structure_greeks(CREDIT_PUT, self._quotes()[:1], Decimal("100"), 0.03, AS_OF)
