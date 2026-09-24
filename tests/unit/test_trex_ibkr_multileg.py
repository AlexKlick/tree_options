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
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.unit.trex_fakes import (
    SDK_DONE_STATES,
    FakeDeskBroker,
    FakeGateway,
    fill_row,
    position_row,
)
from tree_options.trex.ibkr import DONE_STATES, BrokerPosition, IbkrTrex
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
        ref = ib.place(CONDOR, "SELL", 2, Decimal("2.15"))
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
            Decimal("2.15"),
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
        ib.place(CALENDAR, "BUY", 1, Decimal("0.95"))
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
        [
            ("BUY", 1, "0"),
            ("SELL", 1, "-0.25"),
            ("HOLD", 1, "1"),
            ("BUY", 0, "1"),
            ("SELL", 1, "Infinity"),  # positive, not finite
            ("SELL", 1, "NaN"),
            ("SELL", 2, "0.10"),  # below the 1.00 credit floor
            ("BUY", 1, "5.01"),  # BUY-to-close above the 5-wide width
            ("SELL", 3, "1.20"),  # more packages than the structure's 2
        ],
    )
    def test_refuses_out_of_bound_and_bad_orders(self, side: str, qty: int, limit: str) -> None:
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
            ib.place(CONDOR, "SELL", 1, Decimal("2.10"))

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

    def test_empty_book_leg_is_no_quote_not_a_zero_market(self) -> None:
        # 2026-09-24: for ~16 min after the open the gateway reported legs
        # 0.0/0.0 (no MM book yet); those quotes flowed through as mids of
        # $0.00 and marked the whole book at exactly its committed debit
        ib, gw = _adapter(CONDOR)
        gw.quote(90, 0.20, 0.25)
        gw.quote(95, 0.50, 0.60)
        gw.quote(105, 0.0, 0.0)  # empty book
        gw.quote(115, 0.10, 0.15)
        assert ib.package_quote("ic") is None

    def test_zero_bid_with_an_ask_is_still_a_market(self) -> None:
        ib, gw = _adapter(LONG_CALL)
        gw.quote(105, 0.0, 0.05)  # genuinely near-worthless, but two-sided
        q = ib.package_quote("lc")
        assert q is not None and (q.bid, q.ask) == (Decimal("0"), Decimal("0.05"))

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
        ib.place(CONDOR, "SELL", 1, Decimal("2.10"))
        ib.place(LONG_CALL, "BUY", 1, Decimal("3.00"))
        gw.placeOrder(ib_async.Stock("SPY", "SMART", "USD"), ib_async.LimitOrder("BUY", 1, 1.0))
        done = ib.place(LONG_CALL, "SELL", 1, Decimal("3.50"))
        done.trade.orderStatus.status = "Filled"
        assert [t.contract.secType for t in ib.working_trades()] == ["BAG", "OPT"]

    def test_structure_for_contract(self) -> None:
        ib, gw = _adapter(CONDOR, LONG_CALL, DEBIT_PUT)
        ib.place(CONDOR, "SELL", 1, Decimal("2.10"))
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
        ib.place(CONDOR, "SELL", 1, Decimal("2.10"))
        trade = gw.trades[0]
        trade.order.orderRef = "trex:lc"  # tag names a structure the contract isn't
        assert ib.structure_for_trade(trade) is None
        trade.order.orderRef = "trex:elsewhere"  # another runner's structure
        assert ib.structure_for_trade(trade) is None
        trade.order.orderRef = ""  # untagged: never adopted on contract alone
        assert ib.structure_for_trade(trade) is None
        trade.order.orderRef = "manual-7"  # someone else's reference
        assert ib.structure_for_trade(trade) is None
        trade.order.orderRef = "trex:ic"
        assert ib.structure_for_trade(trade) == "ic"

    def test_legacy_untagged_adoption_stays_on_the_legacy_path(self) -> None:
        # Codex P1-3: only a credit vertical is prepared; an old untagged
        # SELL closing a DEBIT vertical on the same strikes carries the same
        # debit-oriented BAG. The contract alone must not make it ours.
        ib, gw = _adapter(CREDIT_PUT)
        ib.place(CREDIT_PUT, "SELL", 1, Decimal("1.20"))
        foreign = gw.trades[0]
        foreign.order.orderRef = ""
        assert ib.structure_for_trade(foreign) is None
        # the legacy lookup (structure_for_bag, conId set) is unchanged
        assert ib.structure_for_bag(foreign.contract) == "cv"


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
                account="DU0000000",
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
                account="DU0000000",
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

    def test_positions_across_accounts_need_a_selected_account(self) -> None:
        # Codex P2-5: a long leg in one account and its short in another is
        # two naked exposures, not one covered package
        ib, gw = _adapter(CONDOR)
        gw.position_rows = [
            position_row(90, 1.0, account="DU0000001"),
            position_row(95, -1.0, account="DU0000002"),
        ]
        with pytest.raises(ValueError, match="accounts"):
            ib.positions()
        assert [(p.account, p.con_id) for p in ib.positions("DU0000002")] == [("DU0000002", 95)]
        assert ib.positions("DU0000009") == []

    def test_release_cancels_only_unshared_market_data(self) -> None:
        ib, gw = _adapter(CONDOR, LONG_CALL)  # both use the 105 call
        ib.release("ic")
        assert sorted(gw.mkt_cancelled) == [90, 95, 115]  # 105 still quotes "lc"
        assert ib.package_quote("ic") is None
        with pytest.raises(ValueError, match="not prepared"):
            ib.place(CONDOR, "SELL", 1, Decimal("2.10"))
        ib.release("lc")  # the last SPY structure: its leg and the spot go too
        assert sorted(gw.mkt_cancelled) == [1, 90, 95, 105, 115]
        ib.release("lc")  # idempotent
        ib.release("never")
        assert sorted(gw.mkt_cancelled) == [1, 90, 95, 105, 115]


class TestMarketDataSubscriptions:
    """Codex P2-4: ib_async keeps ONE reqId per ticker, so a second
    reqMktData for a shared leg orphans the first request (cancelMktData
    cancels only the latest). One subscription per conId, counted."""

    def test_a_shared_leg_is_subscribed_once(self) -> None:
        ib, gw = _adapter(CONDOR, LONG_CALL, LONG_CALL_TWIN)
        counts: dict[int, int] = {}
        for c in gw.subscribed:
            counts[c.conId] = counts.get(c.conId, 0) + 1
        assert counts == {90: 1, 95: 1, 105: 1, 115: 1, 1: 1}
        gw.quote(105, 2.95, 3.05)  # the one ticker quotes all three owners
        assert ib.package_quote("lc") == ib.package_quote("lc2")

    def test_releasing_every_owner_leaves_no_stream(self) -> None:
        ib, gw = _adapter(CONDOR, LONG_CALL)
        ib.prepare([LONG_CALL_TWIN])  # a later owner of the same call
        for sid in ("lc", "ic", "lc2"):
            ib.release(sid)
        assert gw.md_live == set()

    def test_prepare_release_cycles_do_not_accumulate(self) -> None:
        ib, gw = _adapter(CONDOR)
        for _ in range(3):
            ib.prepare([LONG_CALL])
            ib.release("lc")
        assert len(gw.md_live) == 5  # the condor's 4 legs + the SPY spot
        ib.release("ic")
        assert gw.md_live == set()


class TestWhatIf:
    def test_returns_the_initial_margin_change_without_placing(self) -> None:
        ib, gw = _adapter(CONDOR)
        gw.whatif_margin = "812.40"
        margin = ib.whatif(CONDOR, "SELL", 2, Decimal("2.15"))
        assert margin == Decimal("812.40")
        assert gw.trades == []
        contract, order = gw.whatif_calls[0]
        assert _wire_legs(contract) == _combo(
            [(90, "SELL"), (95, "BUY"), (105, "BUY"), (115, "SELL")]
        )
        assert (order.action, order.totalQuantity, order.lmtPrice) == ("SELL", 2, 2.15)
        # bounded: the gateway's reply may never come
        assert gw.run_timeouts == [ib.whatif_timeout] and 0 < ib.whatif_timeout <= 30

    def test_long_single_whatif_uses_the_option(self) -> None:
        ib, gw = _adapter(LONG_CALL)
        gw.whatif_margin = "0"
        assert ib.whatif(LONG_CALL, "BUY", 1, Decimal("3.00")) == Decimal("0")
        assert gw.whatif_calls[0][0].secType == "OPT"

    @pytest.mark.parametrize("raw", ["", "1.7976931348623157E308", "junk", None, "nan"])
    def test_no_number_is_none(self, raw: Any) -> None:
        ib, gw = _adapter(CONDOR)
        gw.whatif_margin = raw
        assert ib.whatif(CONDOR, "SELL", 2, Decimal("2.15")) is None

    def test_an_unanswered_request_times_out_to_none(self) -> None:
        # Codex P1-1: ib_async 2.1.0 ends a what-if request only when
        # initMarginChange != UNSET_DOUBLE, and RequestTimeout defaults to 0
        ib, gw = _adapter(CONDOR)
        ib.whatif_timeout = 0.05
        gw.whatif_pending = True
        assert ib.whatif(CONDOR, "SELL", 2, Decimal("2.15")) is None
        assert gw.run_timeouts == [0.05]

    def test_a_failed_request_is_none(self) -> None:
        ib, gw = _adapter(CONDOR)
        gw.whatif_reply = []  # RaiseRequestErrors off: the request ends with []
        assert ib.whatif(CONDOR, "SELL", 2, Decimal("2.15")) is None
        gw.whatif_reply = None
        gw.whatif_error = ConnectionError("gateway went away")
        assert ib.whatif(CONDOR, "SELL", 2, Decimal("2.15")) is None
        gw.whatif_error = ib_async.RequestError(7, 201, "Order rejected")
        assert ib.whatif(CONDOR, "SELL", 2, Decimal("2.15")) is None


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

    def test_done_states_match_the_sdk(self) -> None:
        assert DONE_STATES == SDK_DONE_STATES == ib_async.OrderStatus.DoneStates


@dataclass
class _Harness:
    """One broker implementation plus how to drive its market data."""

    broker: Any
    quote: Any  # (sid, leg index, bid, ask) -> None
    set_positions: Any  # [(account, conId, qty)] -> None


def _adapter_harness() -> _Harness:
    gw = FakeGateway(con_ids=CON)
    ib = IbkrTrex(client_id=81)
    ib._ib = gw

    def quote(sid: str, index: int, bid: str, ask: str) -> None:
        gw.quote(ib.leg_con_ids(sid)[index], float(bid), float(ask))

    def set_positions(rows: list[tuple[str, int, float]]) -> None:
        gw.position_rows = [position_row(con, qty, account=acct) for acct, con, qty in rows]

    return _Harness(ib, quote, set_positions)


def _fake_harness() -> _Harness:
    fake = FakeDeskBroker()

    def set_positions(rows: list[tuple[str, int, float]]) -> None:
        fake.position_rows = [
            BrokerPosition(
                account=acct,
                con_id=con,
                sec_type="OPT",
                symbol="SPY",
                right="",
                strike=Decimal(0),
                expiry="",
                qty=Decimal(str(qty)),
                avg_cost=Decimal(0),
            )
            for acct, con, qty in rows
        ]

    return _Harness(fake, fake.set_leg_quote, set_positions)


@pytest.fixture(params=["adapter", "fake"])
def harness(request: pytest.FixtureRequest) -> _Harness:
    return _adapter_harness() if request.param == "adapter" else _fake_harness()


class TestSharedBehaviour:
    """Codex P2-6: the same behavioural cases against IbkrTrex (over
    FakeGateway) and FakeDeskBroker, so the runner-level double cannot
    drift from the adapter's ownership, bounds and release rules."""

    def test_identical_contracts_resolve_only_by_tag(self, harness: _Harness) -> None:
        b = harness.broker
        b.prepare([DEBIT_PUT, CREDIT_PUT])  # one debit-oriented BAG for both
        dv = b.place(DEBIT_PUT, "BUY", 1, Decimal("1.00"))
        cv = b.place(CREDIT_PUT, "SELL", 1, Decimal("1.20"))
        assert b.structure_for_contract(dv.trade.contract) is None
        assert b.structure_for_contract(cv.trade.contract) is None
        assert b.structure_for_trade(dv.trade) == "dv"
        assert b.structure_for_trade(cv.trade) == "cv"

    @pytest.mark.parametrize("tag", ["", "manual-7", "trex:", "trex:dv", "trex:lc", "TREX:cv"])
    def test_missing_foreign_or_wrong_tags_are_not_adopted(
        self, harness: _Harness, tag: str
    ) -> None:
        b = harness.broker
        b.prepare([CREDIT_PUT, LONG_CALL])
        trade = b.place(CREDIT_PUT, "SELL", 1, Decimal("1.20")).trade
        trade.order.orderRef = tag
        assert b.structure_for_trade(trade) is None

    def test_released_structures_are_gone(self, harness: _Harness) -> None:
        b = harness.broker
        b.prepare([CONDOR])
        trade = b.place(CONDOR, "SELL", 1, Decimal("2.10")).trade
        for i in range(4):
            harness.quote("ic", i, "0.50", "0.60")
        assert b.package_quote("ic") is not None
        b.release("ic")
        assert b.structure_for_trade(trade) is None
        assert b.package_quote("ic") is None
        with pytest.raises(ValueError, match="not prepared"):
            b.place(CONDOR, "SELL", 1, Decimal("2.10"))
        with pytest.raises(ValueError, match="not prepared"):
            b.whatif(CONDOR, "SELL", 1, Decimal("2.10"))

    def test_pending_orders_are_working_until_done(self, harness: _Harness) -> None:
        b = harness.broker
        b.prepare([CONDOR, LONG_CALL])
        refs = [b.place(LONG_CALL, "BUY", 1, Decimal("3.00")) for _ in range(6)]
        states = ["PendingSubmit", "PreSubmitted", "Submitted", "Filled", "Cancelled", "Inactive"]
        for ref, state in zip(refs, states, strict=True):
            ref.trade.orderStatus.status = state
        working = [t.orderStatus.status for t in b.working_trades()]
        assert working == ["PendingSubmit", "PreSubmitted", "Submitted"]
        partial = refs[2]
        partial.trade.orderStatus.filled = 1
        partial.trade.orderStatus.avgFillPrice = 2.95
        assert b.order_status(partial).filled == 1
        assert b.order_status(partial).avg_fill_price == Decimal("2.95")

    @pytest.mark.parametrize(
        ("spec", "side", "qty", "limit"),
        [
            (CREDIT_PUT, "SELL", 1, "0.99"),  # below the credit floor
            (CREDIT_PUT, "BUY", 1, "5.01"),  # BUY-to-close above the width
            (DEBIT_PUT, "BUY", 1, "1.01"),  # above the debit cap
            (DEBIT_PUT, "BUY", 3, "0.90"),  # more than the structure's 2
            (LONG_CALL, "BUY", 1, "Infinity"),
            (LONG_CALL, "SELL", 1, "0"),
        ],
    )
    def test_the_same_orders_are_refused(
        self, harness: _Harness, spec: LegStructure, side: str, qty: int, limit: str
    ) -> None:
        b = harness.broker
        b.prepare([CREDIT_PUT, DEBIT_PUT, LONG_CALL])
        with pytest.raises(ValueError):
            b.place(spec, side, qty, Decimal(limit))
        with pytest.raises(ValueError):
            b.whatif(spec, side, qty, Decimal(limit))
        assert b.working_trades() == []

    def test_the_same_package_quote(self, harness: _Harness) -> None:
        b = harness.broker
        b.prepare([CONDOR, LONG_CALL])
        for i, (bid, ask) in enumerate(
            [("0.20", "0.25"), ("0.50", "0.60"), ("0.70", "0.80"), ("0.10", "0.15")]
        ):
            harness.quote("ic", i, bid, ask)
        q = b.package_quote("ic")
        assert (q.bid, q.ask) == (Decimal("0.80"), Decimal("1.10"))
        # the 105 call is shared: the single sees the condor's quote of it
        single = b.package_quote("lc")
        assert (single.bid, single.ask) == (Decimal("0.70"), Decimal("0.80"))

    def test_positions_need_one_account(self, harness: _Harness) -> None:
        b = harness.broker
        harness.set_positions([("DU0000001", 90, 1.0), ("DU0000002", 95, -1.0)])
        with pytest.raises(ValueError, match="accounts"):
            b.positions()
        assert [(p.account, p.con_id) for p in b.positions("DU0000001")] == [("DU0000001", 90)]
        harness.set_positions([("DU0000001", 90, 1.0), ("DU0000001", 95, -1.0)])
        assert len(b.positions()) == 2

    def test_a_changed_spec_is_refused(self, harness: _Harness) -> None:
        b = harness.broker
        b.prepare([CONDOR])
        with pytest.raises(ValueError, match="already prepared"):
            b.prepare([CONDOR.model_copy(update={"legs": CONDOR.legs[:1]})])
        b.prepare([CONDOR])  # the same spec again is a no-op
