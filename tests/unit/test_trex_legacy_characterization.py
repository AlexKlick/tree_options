"""Legacy characterization: the live put-spread book, pinned BEFORE the
multi-leg generalization (desk lane E1/E2) and required to stay green
through it and after.

Two surfaces the live book (plans/2026-09-22.toml, managed by trex-monitor)
depends on:

1. ``engine.decide()`` for a PutSpread: the entry ladder, touch, take-profit,
   time stop, expiry safety and the exit pricing ladder, as a table. Every
   expected value is a literal worked out by hand from the rules in the
   engine's docstring (mid = cents((bid+ask)/2) half-up, entry at
   min(mid, cap) then min(ask, cap) after 4 cycles, exits at mid then bid
   after 3 cycles, bid when forced), never computed by the implementation.
2. What ``IbkrTrex`` puts on the wire for a PutSpread: the option legs it
   qualifies, the BAG (legs, actions, ratios, exchange, every ComboLeg
   field), the limit order, the NBBO-from-legs combo quote, and the order
   adoption / fill-evidence lookups. Driven through the real adapter over
   tests/unit/trex_fakes.FakeGateway; expected contracts are built
   independently with ib_async's own constructors.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.unit.trex_fakes import FakeGateway, fill_row, position_row
from tree_options.trex.clock import EntryWindow
from tree_options.trex.engine import (
    AbortEntry,
    Action,
    ComboQuote,
    EngineConfig,
    ExitOrder,
    NoAction,
    PlaceEntry,
    Snapshot,
    configure,
    decide,
)
from tree_options.trex.ibkr import IbkrTrex
from tree_options.trex.plan import PutSpread
from tree_options.trex.state import Status, StructureState

ET = ZoneInfo("America/New_York")
ENTRY = date(2026, 9, 18)
DEADLINE = date(2026, 10, 9)
EXPIRY = date(2026, 10, 16)
Q = ("0.40", "0.48")  # mid 0.44


@pytest.fixture(autouse=True)
def _engine() -> None:
    # the live runners' config: window 09:45-12:00 from the plan, the
    # EngineConfig defaults for everything else
    configure(EngineConfig(entry_window=EntryWindow(time(9, 45), time(12, 0))))


def _spread(**overrides: Any) -> PutSpread:
    base: dict[str, Any] = {
        "id": "nvda-oct",
        "underlying": "NVDA",
        "entry_date": ENTRY,
        "expiry": EXPIRY,
        "long_strike": "185",
        "short_strike": "150",
        "quantity": 5,
        "limit_cap": "0.50",
        "exit_deadline": DEADLINE,
    }
    base.update(overrides)
    return PutSpread(**base)


def _norm(action: Action) -> tuple[str | None, ...]:
    match action:
        case NoAction(reason):
            return ("no_action", reason)
        case PlaceEntry(limit):
            return ("place_entry", str(limit))
        case AbortEntry(reason):
            return ("abort_entry", reason)
        case ExitOrder(reason, limit):
            return ("exit", reason.value, None if limit is None else str(limit))
    raise AssertionError(f"unknown action {action!r}")


TP = {"take_profit_frac": "0.45"}  # threshold cents(0.45 * 35) = 15.75
OPEN = {"status": Status.OPEN}
TP_Q = ("15.70", "15.80")  # mid 15.75


def _exiting(reason: str | None, cycles: int = 0) -> dict[str, Any]:
    return {"status": Status.EXIT_WORKING, "exit_reason": reason, "exit_cycles": cycles}


# (id, spread overrides, state kwargs, (day, hh, mm), spot, (bid, ask) | None, expected)
CASES: list[
    tuple[
        str,
        dict[str, Any],
        dict[str, Any],
        tuple[date, int, int],
        str | None,
        tuple[str, str] | None,
        tuple[str | None, ...],
    ]
] = [
    # -- entry ladder ------------------------------------------------------
    (
        "entry/day-before",
        {},
        {},
        (date(2026, 9, 17), 10, 0),
        "200",
        Q,
        ("no_action", "before entry date"),
    ),
    ("entry/before-window", {}, {}, (ENTRY, 9, 44), "200", Q, ("no_action", "before entry window")),
    ("entry/window-start-inclusive", {}, {}, (ENTRY, 9, 45), "200", Q, ("place_entry", "0.44")),
    ("entry/mid-under-cap", {}, {}, (ENTRY, 10, 0), "200", Q, ("place_entry", "0.44")),
    (
        "entry/mid-above-cap-pays-cap",
        {},
        {},
        (ENTRY, 10, 0),
        "200",
        ("0.55", "0.60"),
        ("place_entry", "0.50"),
    ),
    (
        "entry/mid-rounds-half-up",
        {},
        {},
        (ENTRY, 10, 0),
        "200",
        ("0.40", "0.41"),
        ("place_entry", "0.41"),
    ),
    (
        "entry/cycle-3-still-mid",
        {},
        {"entry_cycles": 3},
        (ENTRY, 10, 0),
        "200",
        Q,
        ("place_entry", "0.44"),
    ),
    (
        "entry/cycle-4-crosses-to-ask",
        {},
        {"entry_cycles": 4},
        (ENTRY, 10, 0),
        "200",
        Q,
        ("place_entry", "0.48"),
    ),
    (
        "entry/cycle-4-ask-capped",
        {},
        {"entry_cycles": 4},
        (ENTRY, 10, 0),
        "200",
        ("0.45", "0.62"),
        ("place_entry", "0.50"),
    ),
    ("entry/window-end-inclusive", {}, {}, (ENTRY, 12, 0), "200", Q, ("place_entry", "0.44")),
    (
        "entry/window-closed",
        {},
        {},
        (ENTRY, 12, 1),
        "200",
        Q,
        ("abort_entry", "entry window closed"),
    ),
    (
        "entry/date-passed",
        {},
        {},
        (date(2026, 9, 21), 10, 0),
        "200",
        Q,
        ("abort_entry", "entry date passed"),
    ),
    ("entry/no-quote", {}, {}, (ENTRY, 10, 0), "200", None, ("no_action", "no quote")),
    (
        "entry/working",
        {},
        {"status": Status.ENTER_WORKING},
        (ENTRY, 10, 0),
        "200",
        Q,
        ("no_action", "entry working"),
    ),
    (
        "entry/working-window-closed",
        {},
        {"status": Status.ENTER_WORKING},
        (ENTRY, 12, 1),
        "200",
        Q,
        ("abort_entry", "entry window closed"),
    ),
    (
        "entry/working-date-passed",
        {},
        {"status": Status.ENTER_WORKING},
        (date(2026, 9, 21), 10, 0),
        "200",
        Q,
        ("abort_entry", "entry date passed"),
    ),
    # -- open: touch, take-profit, time stop, expiry safety ----------------
    ("open/hold", {}, OPEN, (ENTRY, 13, 0), "200", Q, ("no_action", "hold")),
    ("open/no-spot-no-touch", {}, OPEN, (ENTRY, 13, 0), None, Q, ("no_action", "hold")),
    ("open/touch-below", {}, OPEN, (ENTRY, 13, 0), "184.99", Q, ("exit", "touch", "0.44")),
    ("open/touch-exact-strike", {}, OPEN, (ENTRY, 13, 0), "185", Q, ("exit", "touch", "0.44")),
    ("open/touch-no-quote-pending", {}, OPEN, (ENTRY, 13, 0), "180", None, ("exit", "touch", None)),
    (
        "open/touch-after-2-cycles-mid",
        {},
        {**OPEN, "exit_cycles": 2},
        (ENTRY, 13, 0),
        "180",
        Q,
        ("exit", "touch", "0.44"),
    ),
    (
        "open/touch-after-3-cycles-bid",
        {},
        {**OPEN, "exit_cycles": 3},
        (ENTRY, 13, 0),
        "180",
        Q,
        ("exit", "touch", "0.40"),
    ),
    (
        "open/tp-unset-never-fires",
        {},
        OPEN,
        (ENTRY, 13, 0),
        "200",
        ("30", "31"),
        ("no_action", "hold"),
    ),
    (
        "open/tp-at-threshold",
        TP,
        OPEN,
        (ENTRY, 13, 0),
        "200",
        TP_Q,
        ("exit", "take_profit", "15.75"),
    ),
    (
        "open/tp-below-threshold",
        TP,
        OPEN,
        (ENTRY, 13, 0),
        "200",
        ("15.60", "15.80"),
        ("no_action", "hold"),
    ),
    ("open/tp-no-quote", TP, OPEN, (ENTRY, 13, 0), "200", None, ("no_action", "hold")),
    ("open/touch-beats-tp", TP, OPEN, (ENTRY, 13, 0), "180", TP_Q, ("exit", "touch", "15.75")),
    (
        "open/deadline-before-time-stop",
        {},
        OPEN,
        (DEADLINE, 9, 44),
        "200",
        Q,
        ("no_action", "hold"),
    ),
    (
        "open/deadline-time-stop",
        {},
        OPEN,
        (DEADLINE, 9, 45),
        "200",
        Q,
        ("exit", "time_stop", "0.44"),
    ),
    (
        "open/after-deadline-time-stop",
        {},
        OPEN,
        (date(2026, 10, 12), 10, 0),
        "200",
        Q,
        ("exit", "time_stop", "0.44"),
    ),
    (
        "open/tp-beats-time-stop",
        TP,
        OPEN,
        (DEADLINE, 10, 0),
        "200",
        TP_Q,
        ("exit", "take_profit", "15.75"),
    ),
    (
        "open/touch-beats-time-stop",
        {},
        OPEN,
        (DEADLINE, 10, 0),
        "180",
        Q,
        ("exit", "touch", "0.44"),
    ),
    (
        "open/time-stop-no-quote",
        {},
        OPEN,
        (DEADLINE, 10, 0),
        "200",
        None,
        ("exit", "time_stop", None),
    ),
    (
        "open/time-stop-escalated",
        {},
        {**OPEN, "exit_cycles": 3},
        (DEADLINE, 10, 0),
        "200",
        Q,
        ("exit", "time_stop", "0.40"),
    ),
    (
        "open/expiry-safety-forces-bid",
        {},
        OPEN,
        (EXPIRY, 9, 45),
        "200",
        ("2", "4"),
        ("exit", "expiry_safety", "2.00"),
    ),
    (
        "open/expiry-beats-touch",
        {},
        OPEN,
        (EXPIRY, 10, 0),
        "100",
        Q,
        ("exit", "expiry_safety", "0.40"),
    ),
    (
        "open/expiry-no-quote",
        {},
        OPEN,
        (EXPIRY, 10, 0),
        "200",
        None,
        ("exit", "expiry_safety", None),
    ),
    (
        "open/after-expiry",
        {},
        OPEN,
        (date(2026, 10, 19), 10, 0),
        "200",
        Q,
        ("exit", "expiry_safety", "0.40"),
    ),
    # -- exit working: the pricing ladder ----------------------------------
    (
        "exiting/mid-first",
        {},
        _exiting("touch"),
        (ENTRY, 13, 0),
        "200",
        Q,
        ("exit", "touch", "0.44"),
    ),
    (
        "exiting/cycle-2-mid",
        {},
        _exiting("touch", 2),
        (ENTRY, 13, 0),
        "200",
        Q,
        ("exit", "touch", "0.44"),
    ),
    (
        "exiting/cycle-3-bid",
        {},
        _exiting("touch", 3),
        (ENTRY, 13, 0),
        "200",
        Q,
        ("exit", "touch", "0.40"),
    ),
    (
        "exiting/reason-kept",
        {},
        _exiting("take_profit"),
        (ENTRY, 13, 0),
        "200",
        Q,
        ("exit", "take_profit", "0.44"),
    ),
    (
        "exiting/flatten-reason-kept",
        {},
        _exiting("flatten"),
        (ENTRY, 13, 0),
        "200",
        Q,
        ("exit", "flatten", "0.44"),
    ),
    (
        "exiting/no-reason-is-time-stop",
        {},
        _exiting(None),
        (ENTRY, 13, 0),
        "200",
        Q,
        ("exit", "time_stop", "0.44"),
    ),
    (
        "exiting/deadline-before-force",
        {},
        _exiting("time_stop"),
        (DEADLINE, 15, 44),
        "200",
        Q,
        ("exit", "time_stop", "0.44"),
    ),
    (
        "exiting/deadline-force-bid",
        {},
        _exiting("time_stop"),
        (DEADLINE, 15, 45),
        "200",
        Q,
        ("exit", "time_stop", "0.40"),
    ),
    (
        "exiting/force-only-from-deadline",
        {},
        _exiting("touch"),
        (date(2026, 9, 25), 15, 50),
        "200",
        Q,
        ("exit", "touch", "0.44"),
    ),
    (
        "exiting/after-deadline-force",
        {},
        _exiting("time_stop"),
        (date(2026, 10, 12), 15, 45),
        "200",
        Q,
        ("exit", "time_stop", "0.40"),
    ),
    (
        "exiting/no-quote",
        {},
        _exiting("touch"),
        (ENTRY, 13, 0),
        "200",
        None,
        ("exit", "touch", None),
    ),
    (
        "exiting/expiry-overrides-reason",
        {},
        _exiting("touch"),
        (EXPIRY, 10, 0),
        "200",
        Q,
        ("exit", "expiry_safety", "0.40"),
    ),
    # -- terminal ------------------------------------------------------------
    ("closed", {}, {"status": Status.CLOSED}, (ENTRY, 13, 0), "180", Q, ("no_action", "closed")),
    (
        "closed-at-expiry",
        {},
        {"status": Status.CLOSED},
        (EXPIRY, 10, 0),
        "180",
        Q,
        ("no_action", "closed"),
    ),
]


@pytest.mark.parametrize(
    ("overrides", "state", "at", "spot", "quote", "expected"),
    [c[1:] for c in CASES],
    ids=[c[0] for c in CASES],
)
def test_legacy_decide_table(
    overrides: dict[str, Any],
    state: dict[str, Any],
    at: tuple[date, int, int],
    spot: str | None,
    quote: tuple[str, str] | None,
    expected: tuple[str | None, ...],
) -> None:
    day, hh, mm = at
    snap = Snapshot(
        ts=datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET),
        spots={} if spot is None else {"NVDA": Decimal(spot)},
        quotes={
            "nvda-oct": None if quote is None else ComboQuote(Decimal(quote[0]), Decimal(quote[1]))
        },
    )
    assert _norm(decide(_spread(**overrides), StructureState(**state), snap)) == expected


def test_case_ids_are_unique() -> None:
    ids = [c[0] for c in CASES]
    assert len(ids) == len(set(ids))


# -- the broker adapter ------------------------------------------------------

try:  # the optional `trex` group; the shared .venv carries it
    import ib_async
except ImportError:  # pragma: no cover - only without the trex group
    ib_async = None
needs_ib = pytest.mark.skipif(ib_async is None, reason="ib_async (trex group) not installed")

NVDA_OCT = _spread()
QQQ_NOV = _spread(
    id="qqq-nov",
    underlying="QQQ",
    expiry=date(2026, 11, 20),
    long_strike="600",
    short_strike="475",
    quantity=4,
    limit_cap="2.40",
    exit_deadline=date(2026, 11, 6),
)
CON = {
    ("OPT", "NVDA", "20261016", 185.0, "P"): 11,
    ("OPT", "NVDA", "20261016", 150.0, "P"): 12,
    ("OPT", "QQQ", "20261120", 600.0, "P"): 21,
    ("OPT", "QQQ", "20261120", 475.0, "P"): 22,
    ("STK", "NVDA", "", 0.0, ""): 1,
    ("STK", "QQQ", "", 0.0, ""): 2,
}


def _prepared(client_id: int = 71) -> tuple[IbkrTrex, FakeGateway]:
    gw = FakeGateway(con_ids=CON)
    ib = IbkrTrex(client_id=client_id)
    ib._ib = gw
    ib.prepare([NVDA_OCT, QQQ_NOV])
    return ib, gw


def _bag(symbol: str, long_con: int, short_con: int) -> Any:
    return ib_async.Contract(
        symbol=symbol,
        secType="BAG",
        exchange="SMART",
        currency="USD",
        tradingClass=symbol,
        comboLegs=[
            ib_async.ComboLeg(conId=long_con, ratio=1, action="BUY", exchange="SMART"),
            ib_async.ComboLeg(conId=short_con, ratio=1, action="SELL", exchange="SMART"),
        ],
    )


@needs_ib
class TestLegacyWire:
    def test_qualifies_put_legs_then_spot_stocks_in_one_call(self) -> None:
        _, gw = _prepared()
        assert len(gw.qualify_calls) == 1
        sent = gw.qualify_calls[0]
        expected = [
            ib_async.Option(
                symbol=sym,
                lastTradeDateOrContractMonth=exp,
                strike=k,
                right="P",
                exchange="SMART",
                tradingClass=sym,
                conId=con,
            )
            for sym, exp, k, con in [
                ("NVDA", "20261016", 185.0, 11),
                ("NVDA", "20261016", 150.0, 12),
                ("QQQ", "20261120", 600.0, 21),
                ("QQQ", "20261120", 475.0, 22),
            ]
        ] + [
            ib_async.Stock("NVDA", "SMART", "USD", conId=1),
            ib_async.Stock("QQQ", "SMART", "USD", conId=2),
        ]
        assert [type(c) for c in sent] == [type(c) for c in expected]
        assert [dataclasses.asdict(c) for c in sent] == [dataclasses.asdict(c) for c in expected]

    def test_subscribes_every_leg_and_spot(self) -> None:
        _, gw = _prepared()
        assert sorted(c.conId for c in gw.subscribed) == [1, 2, 11, 12, 21, 22]

    @pytest.mark.parametrize("side", ["BUY", "SELL"])
    def test_bag_and_order_are_byte_identical(self, side: str) -> None:
        ib, gw = _prepared()
        ref = ib.place_combo(NVDA_OCT, side, 5, Decimal("0.44"))
        trade = gw.trades[-1]
        assert ref.trade is trade
        assert (ref.structure_id, ref.side, ref.qty, ref.limit) == (
            "nvda-oct",
            side,
            5,
            Decimal("0.44"),
        )
        assert type(trade.contract) is ib_async.Contract
        assert dataclasses.asdict(trade.contract) == dataclasses.asdict(_bag("NVDA", 11, 12))
        assert [
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
            for leg in trade.contract.comboLegs
        ] == [(11, 1, "BUY", "SMART", 0, 0, "", -1), (12, 1, "SELL", "SMART", 0, 0, "", -1)]
        expected_order = ib_async.LimitOrder(side, 5, 0.44, tif="DAY", transmit=True)
        got = dataclasses.asdict(trade.order)
        want = dataclasses.asdict(expected_order)
        got.pop("orderId")
        want.pop("orderId")
        assert got == want
        assert type(trade.order) is ib_async.LimitOrder
        assert trade.order.orderRef == ""

    def test_second_structure_gets_its_own_bag(self) -> None:
        ib, gw = _prepared()
        ib.place_combo(QQQ_NOV, "BUY", 4, Decimal("2.40"))
        assert dataclasses.asdict(gw.trades[-1].contract) == dataclasses.asdict(_bag("QQQ", 21, 22))

    def test_combo_quote_is_nbbo_from_legs(self) -> None:
        ib, gw = _prepared()
        gw.quote(11, 1.10, 1.25)
        gw.quote(12, 0.60, 0.70)
        q = ib.combo_quote("nvda-oct")
        assert q is not None
        # bid = long_bid - short_ask, ask = long_ask - short_bid, exact decimals
        assert (str(q.bid), str(q.ask)) == ("0.4", "0.65")
        snap = ib.snapshot([NVDA_OCT, QQQ_NOV], datetime(2026, 9, 18, 10, 0, tzinfo=ET))
        assert snap.spots == {}
        assert snap.quotes == {"nvda-oct": q, "qqq-nov": None}

    @pytest.mark.parametrize(
        ("long_q", "short_q"),
        [((None, 1.0), (0.5, 0.6)), ((1.0, 1.2), (0.5, float("nan"))), ((1.0, 1.2), (None, None))],
    )
    def test_missing_or_nan_leg_means_no_quote(
        self, long_q: tuple[Any, Any], short_q: tuple[Any, Any]
    ) -> None:
        ib, gw = _prepared()
        gw.quote(11, *long_q)
        gw.quote(12, *short_q)
        assert ib.combo_quote("nvda-oct") is None

    def test_unknown_structure_has_no_quote(self) -> None:
        ib, _ = _prepared()
        assert ib.combo_quote("nope") is None

    def test_unqualified_leg_refuses_to_prepare(self) -> None:
        gw = FakeGateway(con_ids=CON, unknown={("OPT", "NVDA", "20261016", 150.0, "P")})
        ib = IbkrTrex()
        ib._ib = gw
        with pytest.raises(RuntimeError, match="unqualified contracts"):
            ib.prepare([NVDA_OCT])

    def test_adoption_matches_bags_by_leg_conids(self) -> None:
        ib, gw = _prepared()
        ib.place_combo(NVDA_OCT, "SELL", 5, Decimal("0.44"))
        gw.placeOrder(
            ib_async.Option("NVDA", "20261016", 185.0, "P", "SMART", conId=11),
            ib_async.LimitOrder("SELL", 1, 1.0),
        )
        working = ib.open_combo_trades()
        assert [t.contract.secType for t in working] == ["BAG"]
        assert ib.structure_for_bag(working[0].contract) == "nvda-oct"
        assert ib.structure_for_bag(_bag("QQQ", 21, 22)) == "qqq-nov"
        assert ib.structure_for_bag(_bag("NVDA", 11, 99)) is None
        assert ib.structure_for_bag(ib_async.Stock("NVDA", "SMART", "USD")) is None


@needs_ib
class TestLegacyFillEvidence:
    """entry_fill_evidence over the prepared legs (185P = 11, 150P = 12)."""

    def _ib(self, fills: list[Any], positions: list[Any]) -> IbkrTrex:
        ib, gw = _prepared(client_id=72)
        gw.fill_rows = fills
        gw.position_rows = positions
        return ib

    def test_blended_debit_from_our_executions(self) -> None:
        fills = [
            fill_row(11, 3, 1.00, 201, 72),
            fill_row(11, 2, 1.10, 201, 72),
            fill_row(12, 5, 0.60, 201, 72),
            fill_row(11, 9, 9.99, 202, 72),
        ]
        got = self._ib(fills, []).entry_fill_evidence("nvda-oct", "201")
        # (3 * 1.00 + 2 * 1.10 - 5 * 0.60) / 5 = 0.44
        assert got is not None and got[0] == 5 and got[1] == Decimal("0.44")

    def test_other_clients_order_is_not_ours(self) -> None:
        fills = [fill_row(11, 5, 1.00, 201, 71), fill_row(12, 5, 0.60, 201, 71)]
        ib = self._ib(fills, [position_row(11, 5), position_row(12, -5)])
        assert ib.entry_fill_evidence("nvda-oct", "201") is None

    def test_flat_legs_prove_no_fill(self) -> None:
        ib = self._ib([], [position_row(21, 4), position_row(99, 1)])
        assert ib.entry_fill_evidence("nvda-oct", "201") == (0, None)

    def test_held_leg_without_executions_is_inconclusive(self) -> None:
        assert self._ib([], [position_row(12, -5)]).entry_fill_evidence("nvda-oct", "201") is None

    def test_disagreeing_legs_are_inconclusive(self) -> None:
        fills = [fill_row(11, 5, 1.00, 201, 72), fill_row(12, 3, 0.60, 201, 72)]
        assert self._ib(fills, []).entry_fill_evidence("nvda-oct", "201") is None

    def test_one_sided_execution_is_inconclusive(self) -> None:
        fills = [fill_row(11, 5, 1.00, 201, 72)]
        assert self._ib(fills, []).entry_fill_evidence("nvda-oct", "201") is None

    def test_fractional_quantity_is_inconclusive(self) -> None:
        fills = [fill_row(11, 2.5, 1.00, 201, 72), fill_row(12, 2.5, 0.60, 201, 72)]
        assert self._ib(fills, []).entry_fill_evidence("nvda-oct", "201") is None

    def test_no_order_id_falls_back_to_positions(self) -> None:
        fills = [fill_row(11, 5, 1.00, 201, 72), fill_row(12, 5, 0.60, 201, 72)]
        assert self._ib(fills, []).entry_fill_evidence("nvda-oct", None) == (0, None)
        assert self._ib(fills, [position_row(11, 5)]).entry_fill_evidence("nvda-oct", None) is None

    def test_unknown_structure_is_inconclusive(self) -> None:
        assert self._ib([], []).entry_fill_evidence("other", "201") is None
