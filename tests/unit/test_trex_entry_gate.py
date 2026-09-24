"""The desk's entry gate (lane E3): before the FIRST entry order of a
structure, the order must pass plan.validate_package_order, IBKR's what-if
must answer, and its initial-margin change must be within
plan.margin_within_max_loss (1.1 x the computed max loss). No answer, or
any error, refuses: fail closed.

Run against both broker implementations the desk runtime can hold: the
real IbkrTrex adapter over FakeGateway and FakeDeskBroker, so the runner
double cannot drift from the adapter on this path either.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from tests.unit.trex_fakes import FakeDeskBroker, FakeGateway
from tree_options.trex.entry_gate import EntryVerdict, entry_gate
from tree_options.trex.ibkr import IbkrTrex
from tree_options.trex.plan import LegStructure

pytest.importorskip("ib_async")

FRONT = date(2026, 10, 16)


def _leg(right: str, action: str, strike: str) -> dict[str, Any]:
    return {"right": right, "action": action, "strike": strike, "expiry": FRONT}


def _spec(sid: str, kind: str, legs: list[dict[str, Any]], limit: str) -> LegStructure:
    return LegStructure(
        id=sid,
        underlying="SPY",
        kind=kind,
        legs=legs,
        quantity=2,
        entry_date=date(2026, 9, 24),
        exit_deadline=date(2026, 10, 9),
        limit=limit,
        exits={"touch": False, "breach": False},
    )


# wings 5 (puts) and 10 (calls), floor 2.00: max loss (10 - 2.00) x 100 = 800
# per package; two packages 1600, bound 1.1 x 1600 = 1760
CONDOR = _spec(
    "ic",
    "iron_condor",
    [
        _leg("P", "BUY", "85"),
        _leg("P", "SELL", "90"),
        _leg("C", "SELL", "110"),
        _leg("C", "BUY", "120"),
    ],
    "2.00",
)
# cap 2.00: max loss 2.00 x 100 = 200 per package; one package bound 220
DEBIT_PUT = _spec(
    "dp", "debit_vertical", [_leg("P", "BUY", "100"), _leg("P", "SELL", "95")], "2.00"
)


@dataclass
class _Broker:
    broker: Any
    set_margin: Callable[[str | None], None]
    whatif_calls: Callable[[], list[tuple[str, int, Decimal]]]  # (side, qty, limit)
    placed: Callable[[], int]


def _adapter() -> _Broker:
    gw = FakeGateway()
    ib = IbkrTrex(client_id=81)
    ib._ib = gw
    ib.prepare([CONDOR, DEBIT_PUT])

    def set_margin(value: str | None) -> None:
        gw.whatif_margin = value

    def calls() -> list[tuple[str, int, Decimal]]:
        return [
            (o.action, int(o.totalQuantity), Decimal(str(o.lmtPrice))) for _c, o in gw.whatif_calls
        ]

    return _Broker(ib, set_margin, calls, lambda: len(gw.trades))


def _fake() -> _Broker:
    fake = FakeDeskBroker()
    fake.prepare([CONDOR, DEBIT_PUT])

    def set_margin(value: str | None) -> None:
        fake.whatif_margin = None if value is None else Decimal(value)

    def calls() -> list[tuple[str, int, Decimal]]:
        return [(side, qty, limit) for _sid, side, qty, limit in fake.whatif_calls]

    return _Broker(fake, set_margin, calls, lambda: len(fake.trades))


@pytest.fixture(params=["adapter", "fake"])
def broker(request: pytest.FixtureRequest) -> _Broker:
    return _adapter() if request.param == "adapter" else _fake()


class TestEntryGate:
    @pytest.mark.parametrize(
        ("margin", "ok"),
        [
            ("1600", True),  # exactly the max loss
            ("1760", True),  # 1.1 x 1600: the bound is inclusive
            ("1760.01", False),
            ("-50", True),  # the package reduces margin
        ],
    )
    def test_margin_bound(self, broker: _Broker, margin: str, ok: bool) -> None:
        broker.set_margin(margin)
        verdict = entry_gate(broker.broker, CONDOR, 2, Decimal("2.15"))
        assert verdict.ok is ok
        assert verdict.reason == ("ok" if ok else "margin_above_max_loss")
        assert verdict.margin == Decimal(margin)
        assert verdict.max_loss == Decimal("1600")
        # asked about exactly the opening order: a credit kind SELLs the package
        assert broker.whatif_calls() == [("SELL", 2, Decimal("2.15"))]
        assert broker.placed() == 0  # a gate never trades

    def test_debit_kind_asks_about_a_buy(self, broker: _Broker) -> None:
        broker.set_margin("200")
        verdict = entry_gate(broker.broker, DEBIT_PUT, 1, Decimal("1.90"))
        assert verdict == EntryVerdict(
            ok=True, reason="ok", margin=Decimal("200"), max_loss=Decimal("200")
        )
        assert broker.whatif_calls() == [("BUY", 1, Decimal("1.90"))]

    def test_no_number_refuses(self, broker: _Broker) -> None:
        broker.set_margin(None)
        verdict = entry_gate(broker.broker, CONDOR, 2, Decimal("2.15"))
        assert (verdict.ok, verdict.reason, verdict.margin) == (False, "whatif_no_answer", None)

    @pytest.mark.parametrize(
        ("spec", "qty", "limit"),
        [
            (CONDOR, 2, "1.99"),  # below the credit floor
            (DEBIT_PUT, 1, "2.01"),  # above the debit cap
            (CONDOR, 3, "2.15"),  # more packages than the structure
            (DEBIT_PUT, 1, "0"),
        ],
    )
    def test_an_out_of_bounds_order_is_refused_before_asking(
        self, broker: _Broker, spec: LegStructure, qty: int, limit: str
    ) -> None:
        broker.set_margin("1")
        verdict = entry_gate(broker.broker, spec, qty, Decimal(limit))
        assert (verdict.ok, verdict.reason) == (False, "order_refused")
        assert broker.whatif_calls() == []

    def test_an_unprepared_structure_is_refused(self, broker: _Broker) -> None:
        broker.set_margin("1")
        other = CONDOR.model_copy(update={"id": "ic-2"})
        verdict = entry_gate(broker.broker, other, 1, Decimal("2.15"))
        assert (verdict.ok, verdict.reason) == (False, "whatif_error")


class TestEntryGateFailsClosed:
    def test_an_unanswered_whatif_refuses(self) -> None:
        b = _adapter()
        b.broker.whatif_timeout = 0.05
        b.broker._ib.whatif_pending = True
        verdict = entry_gate(b.broker, CONDOR, 2, Decimal("2.15"))
        assert (verdict.ok, verdict.reason) == (False, "whatif_no_answer")

    def test_a_disconnected_adapter_refuses(self) -> None:
        ib = IbkrTrex(client_id=81)  # never connected: the adapter asserts
        verdict = entry_gate(ib, CONDOR, 2, Decimal("2.15"))
        assert (verdict.ok, verdict.reason) == (False, "whatif_error")

    @pytest.mark.parametrize(
        "error", [ConnectionError("gone"), TimeoutError(), RuntimeError("boom"), AssertionError()]
    )
    def test_any_broker_error_refuses(self, error: BaseException) -> None:
        class Raising:
            def whatif(self, struct: LegStructure, side: str, qty: int, limit: Decimal) -> Any:
                raise error

        verdict = entry_gate(Raising(), CONDOR, 2, Decimal("2.15"))
        assert (verdict.ok, verdict.reason, verdict.margin) == (False, "whatif_error", None)

    @pytest.mark.parametrize("junk", [Decimal("NaN"), Decimal("Infinity"), 1700.0, "1700"])
    def test_a_non_decimal_or_non_finite_margin_refuses(self, junk: Any) -> None:
        class Junk:
            def whatif(self, struct: LegStructure, side: str, qty: int, limit: Decimal) -> Any:
                return junk

        verdict = entry_gate(Junk(), CONDOR, 2, Decimal("2.15"))
        assert verdict.ok is False
