"""IbkrTrex multi-leg surface (desk lane E2), driven through the real
adapter over tests/unit/trex_fakes.FakeGateway.

Every BAG is built in DEBIT ORIENTATION (plan.LegStructure.package_legs), so
every limit price is positive: debit kinds open with BUY, credit kinds open
with SELL at a positive price. Long singles trade as plain OPT orders.
Expected contracts and quotes are literals / ib_async constructions made
here, never read back from the adapter.
"""

from __future__ import annotations

import dataclasses
import inspect
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.unit.trex_fakes import FakeDeskBroker, FakeGateway, fill_row, position_row
from tree_options.trex.ibkr import BrokerPosition, IbkrTrex
from tree_options.trex.plan import LegStructure, PutSpread

ib_async = pytest.importorskip("ib_async")

ET = ZoneInfo("America/New_York")
FRONT = date(2026, 10, 16)
BACK = date(2026, 11, 20)
F, B = "20261016", "20261120"


def _leg(right: str, action: str, strike: str, expiry: date = FRONT) -> dict[str, Any]:
    return {"right": right, "action": action, "strike": strike, "expiry": expiry}


def _spec(sid: str, kind: str, legs: list[dict[str, Any]], limit: str = "1.00") -> LegStructure:
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


CONDOR = _spec(
    "ic",
    "iron_condor",
    [
        _leg("P", "BUY", "90"),
        _leg("P", "SELL", "95"),
        _leg("C", "SELL", "105"),
        _leg("C", "BUY", "115"),
    ],
    "2.00",
)
CREDIT_PUT = _spec("cv", "credit_vertical", [_leg("P", "SELL", "100"), _leg("P", "BUY", "95")])
DEBIT_PUT = _spec("dv", "debit_vertical", [_leg("P", "BUY", "100"), _leg("P", "SELL", "95")])
CALENDAR = _spec(
    "cal", "calendar", [_leg("C", "SELL", "100", FRONT), _leg("C", "BUY", "100", BACK)]
)
LONG_CALL = _spec("lc", "long_single", [_leg("C", "BUY", "105")], "3.00")
LONG_CALL_TWIN = _spec("lc2", "long_single", [_leg("C", "BUY", "105")], "3.00")

CON = {
    ("OPT", "SPY", F, 90.0, "P"): 90,
    ("OPT", "SPY", F, 95.0, "P"): 95,
    ("OPT", "SPY", F, 100.0, "P"): 100,
    ("OPT", "SPY", F, 105.0, "C"): 105,
    ("OPT", "SPY", F, 115.0, "C"): 115,
    ("OPT", "SPY", F, 100.0, "C"): 200,
    ("OPT", "SPY", B, 100.0, "C"): 300,
    ("STK", "SPY", "", 0.0, ""): 1,
    ("OPT", "NVDA", F, 185.0, "P"): 11,
    ("OPT", "NVDA", F, 150.0, "P"): 12,
    ("STK", "NVDA", "", 0.0, ""): 2,
}


def _adapter(*structures: Any) -> tuple[IbkrTrex, FakeGateway]:
    gw = FakeGateway(con_ids=CON)
    ib = IbkrTrex(client_id=81)
    ib._ib = gw
    ib.prepare(list(structures))
    return ib, gw


def _combo(legs: list[tuple[int, str]]) -> list[tuple[Any, ...]]:
    return [(con, 1, action, "SMART", 0, 0, "", -1) for con, action in legs]


def _wire_legs(contract: Any) -> list[tuple[Any, ...]]:
    return [
        (
            leg.conId,
            leg.ratio,
            leg.action,
            leg.exchange,
            leg.openClose,
            leg.shortSaleSlot,
            leg.designatedLocation,
            leg.exemptCode,
        )
        for leg in contract.comboLegs
    ]


class TestBags:
    def test_condor_bag_is_debit_oriented(self) -> None:
        ib, gw = _adapter(CONDOR)
        ref = ib.place(CONDOR, "SELL", 2, Decimal("1.85"))
        bag = gw.trades[-1].contract
        assert (bag.symbol, bag.secType, bag.exchange, bag.currency, bag.tradingClass) == (
            "SPY",
            "BAG",
            "SMART",
            "USD",
            "SPY",
        )
        # opened BUY 90P / SELL 95P / SELL 105C / BUY 115C -> every action flipped
        assert _wire_legs(bag) == _combo([(90, "SELL"), (95, "BUY"), (105, "BUY"), (115, "SELL")])
        assert (ref.structure_id, ref.side, ref.qty, ref.limit) == (
            "ic",
            "SELL",
            2,
            Decimal("1.85"),
        )

    def test_credit_vertical_opens_as_a_positive_sell(self) -> None:
        ib, gw = _adapter(CREDIT_PUT)
        ib.place(CREDIT_PUT, CREDIT_PUT.open_side, 2, Decimal("1.40"))
        trade = gw.trades[-1]
        assert _wire_legs(trade.contract) == _combo([(100, "BUY"), (95, "SELL")])
        order = trade.order
        assert (order.action, order.totalQuantity, order.lmtPrice) == ("SELL", 2, 1.40)
        assert (order.orderType, order.tif, order.transmit) == ("LMT", "DAY", True)
        assert order.orderRef == "trex:cv"

    def test_calendar_legs_carry_their_own_expiries(self) -> None:
        ib, gw = _adapter(CALENDAR)
        opts = [c for c in gw.qualify_calls[0] if c.secType == "OPT"]
        assert [(o.lastTradeDateOrContractMonth, o.strike, o.right) for o in opts] == [
            (F, 100.0, "C"),
            (B, 100.0, "C"),
        ]
        ib.place(CALENDAR, "BUY", 1, Decimal("1.10"))
        assert _wire_legs(gw.trades[-1].contract) == _combo([(200, "SELL"), (300, "BUY")])

    def test_long_single_is_a_plain_opt_order(self) -> None:
        ib, gw = _adapter(LONG_CALL)
        ib.place(LONG_CALL, "BUY", 2, Decimal("3.00"))
        ib.place(LONG_CALL, "SELL", 2, Decimal("4.10"))
        assert all(t.contract.secType == "OPT" for t in gw.trades)
        expected = ib_async.Option(
            symbol="SPY",
            lastTradeDateOrContractMonth=F,
            strike=105.0,
            right="C",
            exchange="SMART",
            tradingClass="SPY",
            conId=105,
        )
        assert dataclasses.asdict(gw.trades[0].contract) == dataclasses.asdict(expected)
        assert [t.order.action for t in gw.trades] == ["BUY", "SELL"]

    def test_legacy_spec_through_place_rides_the_same_bag(self) -> None:
        spread = PutSpread(
            id="nvda-oct",
            underlying="NVDA",
            entry_date=date(2026, 9, 22),
            expiry=FRONT,
            long_strike="185",
            short_strike="150",
            quantity=5,
            limit_cap="0.50",
            exit_deadline=date(2026, 10, 9),
        )
        ib, gw = _adapter(spread)
        ib.place_combo(spread, "SELL", 5, Decimal("0.44"))
        ib.place(spread.as_spec(), "SELL", 5, Decimal("0.44"))
        legacy, generic = gw.trades
        assert dataclasses.asdict(generic.contract) == dataclasses.asdict(legacy.contract)
        assert _wire_legs(generic.contract) == _combo([(11, "BUY"), (12, "SELL")])
        assert legacy.order.orderRef == "" and generic.order.orderRef == "trex:nvda-oct"

    @pytest.mark.parametrize(
        ("side", "qty", "limit"),
        [("BUY", 1, "0"), ("SELL", 1, "-0.25"), ("HOLD", 1, "1"), ("BUY", 0, "1")],
    )
    def test_refuses_nonpositive_limits_and_bad_orders(
        self, side: str, qty: int, limit: str
    ) -> None:
        ib, gw = _adapter(CREDIT_PUT)
        with pytest.raises(ValueError):
            ib.place(CREDIT_PUT, side, qty, Decimal(limit))
        with pytest.raises(ValueError):
            ib.whatif(CREDIT_PUT, side, qty, Decimal(limit))
        assert gw.trades == [] and gw.whatif_calls == []

    def test_refuses_an_unprepared_or_changed_structure(self) -> None:
        ib, gw = _adapter(CREDIT_PUT)
        with pytest.raises(ValueError, match="not prepared"):
            ib.place(DEBIT_PUT, "BUY", 1, Decimal("1"))
        changed = CREDIT_PUT.model_copy(update={"legs": DEBIT_PUT.legs, "kind": "debit_vertical"})
        with pytest.raises(ValueError, match="differs"):
            ib.place(changed, "BUY", 1, Decimal("1"))
        assert gw.trades == []


class TestPrepare:
    def test_reprepare_is_idempotent_and_a_changed_spec_is_refused(self) -> None:
        ib, gw = _adapter(CONDOR)
        ib.prepare([CONDOR])
        assert len(gw.qualify_calls) == 1
        with pytest.raises(ValueError, match="already prepared"):
            ib.prepare([CONDOR.model_copy(update={"legs": CONDOR.legs[:1]})])

    def test_failed_qualification_registers_nothing(self) -> None:
        gw = FakeGateway(con_ids=CON, unknown={("OPT", "SPY", F, 115.0, "C")})
        ib = IbkrTrex()
        ib._ib = gw
        with pytest.raises(RuntimeError, match="unqualified"):
            ib.prepare([CONDOR])
        assert ib.package_quote("ic") is None
        assert gw.subscribed == []
        with pytest.raises(ValueError, match="not prepared"):
            ib.place(CONDOR, "SELL", 1, Decimal("1"))

    def test_incremental_prepare(self) -> None:
        ib, gw = _adapter(CONDOR)
        ib.prepare([LONG_CALL])  # a later structure on the same session
        ib.place(LONG_CALL, "BUY", 1, Decimal("3.00"))
        assert gw.trades[-1].contract.conId == 105


class TestPackageQuote:
    def test_condor_sums_debit_oriented_legs(self) -> None:
        ib, gw = _adapter(CONDOR)
        gw.quote(90, 0.20, 0.25)  # SELL in debit orientation
        gw.quote(95, 0.50, 0.60)  # BUY
        gw.quote(105, 0.70, 0.80)  # BUY
        gw.quote(115, 0.10, 0.15)  # SELL
        q = ib.package_quote("ic")
        assert q is not None
        # bid = (0.50 + 0.70) - (0.25 + 0.15); ask = (0.60 + 0.80) - (0.20 + 0.10)
        assert (q.bid, q.ask) == (Decimal("0.80"), Decimal("1.10"))
        snap = ib.snapshot([CONDOR], datetime(2026, 9, 24, 10, 0, tzinfo=ET))
        assert snap.quotes == {"ic": q} and snap.spots == {}

    def test_long_single_is_the_leg_nbbo(self) -> None:
        ib, gw = _adapter(LONG_CALL)
        gw.quote(105, 2.95, 3.05)
        q = ib.package_quote("lc")
        assert q is not None and (q.bid, q.ask) == (Decimal("2.95"), Decimal("3.05"))

    def test_any_missing_leg_means_no_quote(self) -> None:
        ib, gw = _adapter(CONDOR)
        gw.quote(90, 0.20, 0.25)
        gw.quote(95, 0.50, 0.60)
        gw.quote(105, 0.70, float("nan"))
        gw.quote(115, 0.10, 0.15)
        assert ib.package_quote("ic") is None
        assert ib.package_quote("unknown") is None

    def test_fake_desk_broker_prices_like_the_adapter(self) -> None:
        ib, gw = _adapter(CONDOR)
        fake = FakeDeskBroker()
        fake.prepare([CONDOR])
        for i, (con, bid, ask) in enumerate(
            [
                (90, "0.20", "0.25"),
                (95, "0.50", "0.60"),
                (105, "0.70", "0.80"),
                (115, "0.10", "0.15"),
            ]
        ):
            gw.quote(con, float(bid), float(ask))
            fake.set_leg_quote("ic", i, bid, ask)
        assert fake.package_quote("ic") == ib.package_quote("ic")


class TestAdoption:
    def test_working_trades_are_bag_and_opt_only(self) -> None:
        ib, gw = _adapter(CONDOR, LONG_CALL)
        ib.place(CONDOR, "SELL", 1, Decimal("1.90"))
        ib.place(LONG_CALL, "BUY", 1, Decimal("3.00"))
        gw.placeOrder(ib_async.Stock("SPY", "SMART", "USD"), ib_async.LimitOrder("BUY", 1, 1.0))
        done = ib.place(LONG_CALL, "SELL", 1, Decimal("3.50"))
        done.trade.orderStatus.status = "Filled"
        assert [t.contract.secType for t in ib.working_trades()] == ["BAG", "OPT"]

    def test_structure_for_contract(self) -> None:
        ib, gw = _adapter(CONDOR, LONG_CALL, DEBIT_PUT)
        ib.place(CONDOR, "SELL", 1, Decimal("1.90"))
        ib.place(LONG_CALL, "BUY", 1, Decimal("3.00"))
        bag, opt = gw.trades[0].contract, gw.trades[1].contract
        assert ib.structure_for_contract(bag) == "ic"
        assert ib.structure_for_contract(opt) == "lc"
        # same conIds, other orientation: a different package, not ours
        flipped = ib_async.Contract(
            symbol="SPY",
            secType="BAG",
            exchange="SMART",
            currency="USD",
            comboLegs=[
                ib_async.ComboLeg(conId=90, ratio=1, action="BUY", exchange="SMART"),
                ib_async.ComboLeg(conId=95, ratio=1, action="SELL", exchange="SMART"),
                ib_async.ComboLeg(conId=105, ratio=1, action="SELL", exchange="SMART"),
                ib_async.ComboLeg(conId=115, ratio=1, action="BUY", exchange="SMART"),
            ],
        )
        assert ib.structure_for_contract(flipped) is None
        # a multi-leg structure's leg traded alone is not a structure order
        leg95 = ib_async.Option("SPY", F, 95.0, "P", "SMART", conId=95)
        assert ib.structure_for_contract(leg95) is None
        assert ib.structure_for_contract(ib_async.Stock("SPY", "SMART", "USD", conId=1)) is None

    def test_ambiguous_contracts_are_never_guessed(self) -> None:
        # a debit and a credit put vertical on the same strikes share one
        # debit-oriented BAG; two long singles share one option
        ib, gw = _adapter(DEBIT_PUT, CREDIT_PUT, LONG_CALL, LONG_CALL_TWIN)
        ib.place(DEBIT_PUT, "BUY", 1, Decimal("1.00"))
        ib.place(LONG_CALL, "BUY", 1, Decimal("3.00"))
        bag, opt = gw.trades[0].contract, gw.trades[1].contract
        assert ib.structure_for_contract(bag) is None
        assert ib.structure_for_contract(opt) is None
        # the order's trex tag resolves them
        assert ib.structure_for_trade(gw.trades[0]) == "dv"
        assert ib.structure_for_trade(gw.trades[1]) == "lc"

    def test_structure_for_trade_checks_the_tag_against_the_contract(self) -> None:
        ib, gw = _adapter(CONDOR, LONG_CALL)
        ib.place(CONDOR, "SELL", 1, Decimal("1.90"))
        trade = gw.trades[0]
        trade.order.orderRef = "trex:lc"  # tag names a structure the contract isn't
        assert ib.structure_for_trade(trade) is None
        trade.order.orderRef = "trex:elsewhere"  # another runner's structure
        assert ib.structure_for_trade(trade) is None
        trade.order.orderRef = ""  # untagged (placed by hand): by contract
        assert ib.structure_for_trade(trade) == "ic"


class TestPositionsAndRelease:
    def test_positions(self) -> None:
        ib, gw = _adapter(CONDOR)
        gw.position_rows = [
            position_row(
                95,
                -2.0,
                avg_cost=55.5,
                symbol="SPY",
                right="P",
                strike=95.0,
                lastTradeDateOrContractMonth=F,
            ),
            position_row(1, 10.0, avg_cost=501.25, secType="STK", symbol="SPY"),
        ]
        assert ib.positions() == [
            BrokerPosition(
                con_id=95,
                sec_type="OPT",
                symbol="SPY",
                right="P",
                strike=Decimal("95.0"),
                expiry=F,
                qty=Decimal("-2.0"),
                avg_cost=Decimal("55.5"),
            ),
            BrokerPosition(
                con_id=1,
                sec_type="STK",
                symbol="SPY",
                right="",
                strike=Decimal("0.0"),
                expiry="",
                qty=Decimal("10.0"),
                avg_cost=Decimal("501.25"),
            ),
        ]
        assert ib.leg_con_ids("ic") == (90, 95, 105, 115)
        assert ib.leg_con_ids("nope") == ()

    def test_release_cancels_only_unshared_market_data(self) -> None:
        ib, gw = _adapter(CONDOR, LONG_CALL)  # both use the 105 call
        ib.release("ic")
        assert sorted(gw.mkt_cancelled) == [90, 95, 115]  # 105 still quotes "lc"
        assert ib.package_quote("ic") is None
        with pytest.raises(ValueError, match="not prepared"):
            ib.place(CONDOR, "SELL", 1, Decimal("1"))
        ib.release("lc")  # the last SPY structure: its leg and the spot go too
        assert sorted(gw.mkt_cancelled) == [1, 90, 95, 105, 115]
        ib.release("lc")  # idempotent
        ib.release("never")
        assert sorted(gw.mkt_cancelled) == [1, 90, 95, 105, 115]


class TestWhatIf:
    def test_returns_the_initial_margin_change_without_placing(self) -> None:
        ib, gw = _adapter(CONDOR)
        gw.whatif_margin = "812.40"
        margin = ib.whatif(CONDOR, "SELL", 2, Decimal("1.85"))
        assert margin == Decimal("812.40")
        assert gw.trades == []
        contract, order = gw.whatif_calls[0]
        assert _wire_legs(contract) == _combo(
            [(90, "SELL"), (95, "BUY"), (105, "BUY"), (115, "SELL")]
        )
        assert (order.action, order.totalQuantity, order.lmtPrice) == ("SELL", 2, 1.85)

    def test_long_single_whatif_uses_the_option(self) -> None:
        ib, gw = _adapter(LONG_CALL)
        gw.whatif_margin = "0"
        assert ib.whatif(LONG_CALL, "BUY", 1, Decimal("3.00")) == Decimal("0")
        assert gw.whatif_calls[0][0].secType == "OPT"

    @pytest.mark.parametrize("raw", ["", "1.7976931348623157E308", "junk", None, "nan"])
    def test_no_number_is_none(self, raw: Any) -> None:
        ib, gw = _adapter(CONDOR)
        gw.whatif_margin = raw
        assert ib.whatif(CONDOR, "SELL", 2, Decimal("1.85")) is None

    def test_an_error_reply_is_none(self) -> None:
        ib, gw = _adapter(CONDOR)
        gw.whatIfOrder = lambda contract, order: []  # type: ignore[method-assign]
        assert ib.whatif(CONDOR, "SELL", 2, Decimal("1.85")) is None


class TestFillEvidence:
    """entry_fill_evidence over N legs: per-leg executed quantities must
    agree; the package price is the debit-oriented signed sum."""

    def test_condor_credit_from_leg_executions(self) -> None:
        ib, gw = _adapter(CONDOR)
        gw.fill_rows = [
            fill_row(90, 2, 0.22, 301, 81),
            fill_row(95, 2, 0.55, 301, 81),
            fill_row(105, 2, 0.75, 301, 81),
            fill_row(115, 2, 0.12, 301, 81),
        ]
        # (0.55 + 0.75) - (0.22 + 0.12) = 0.96 per package
        assert ib.entry_fill_evidence("ic", "301") == (2, Decimal("0.96"))

    def test_disagreeing_legs_are_inconclusive(self) -> None:
        ib, gw = _adapter(CONDOR)
        gw.fill_rows = [
            fill_row(90, 2, 0.22, 301, 81),
            fill_row(95, 2, 0.55, 301, 81),
            fill_row(105, 1, 0.75, 301, 81),
            fill_row(115, 2, 0.12, 301, 81),
        ]
        assert ib.entry_fill_evidence("ic", "301") is None

    def test_long_single(self) -> None:
        ib, gw = _adapter(LONG_CALL)
        gw.fill_rows = [fill_row(105, 1, 2.9, 302, 81), fill_row(105, 1, 3.0, 302, 81)]
        assert ib.entry_fill_evidence("lc", "302") == (2, Decimal("2.95"))
        gw.fill_rows = []
        gw.position_rows = [position_row(105, 2)]
        assert ib.entry_fill_evidence("lc", "302") is None


class TestFakeDeskBrokerConformance:
    """The multi-leg test double must not drift from the adapter it stands for."""

    @pytest.mark.parametrize(
        "name",
        [
            "prepare",
            "package_quote",
            "snapshot",
            "place",
            "working_trades",
            "structure_for_contract",
            "structure_for_trade",
            "order_status",
            "cancel",
            "positions",
            "release",
            "whatif",
            "sleep",
        ],
    )
    def test_same_parameters(self, name: str) -> None:
        real = inspect.signature(getattr(IbkrTrex, name))
        fake = inspect.signature(getattr(FakeDeskBroker, name))
        assert list(real.parameters) == list(fake.parameters)
