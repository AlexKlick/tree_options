"""Wave 2 lane C: the supervised owner (desk runtime, clientId 83) exits
desk-owned long equity positions.

Drives the REAL ``DeskRuntime`` and ``IbkrTrex`` over the shared C2.8
``Wave2Gateway`` and ``SessionClock``: the gateway checks every stock order
against the Wave 2 wire contract (SMART/USD, LMT, DAY, no outside-RTH, the
desk tag on a SELL, an explicit managed account, whole shares, never more
than held net of working sells) and derives broker positions from fills.

Oracles are the frozen fixture effect (2 SPY at 671.50, stop 665.00,
target 680.00, deadline session 2026-10-23, 3 confirm ticks), wire fields,
book states and event names.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.unit.trex_fakes import (
    SPY_CON_ID,
    FillStep,
    SessionClock,
    Wave2Gateway,
    contract_fixture,
    equity_effect,
)
from tree_options.action_graph.proposal import canonical_bytes
from tree_options.trex import exit_watch
from tree_options.trex.contracts import (
    DeskEvent,
    EquityDeskSpecV2,
    SupervisedEquityEffect,
    desk_spec_v2_for,
    model_bytes,
    parse_desk_spec,
)
from tree_options.trex.desk_runtime import DeskPaths, DeskRuntime, desk_order_ref
from tree_options.trex.desk_spec import DeskSpec
from tree_options.trex.ibkr import IbkrTrex
from tree_options.trex.state import Status
from tree_options.trex.supervised import SupervisedPaths
from tree_options.trex.supervised_ibkr import (
    SUPERVISED_CLIENT_ID,
    SupervisedEffect,
    supervised_order_ref,
)

pytest.importorskip("ib_async")

IID = "eq-spy-001"
STOP = 66500
TARGET = 68000
EARLY_CLOSE_DAY = date(2026, 11, 27)


class Rig:
    """A runtime over the Wave 2 gateway, its clock and state dirs."""

    def __init__(self, tmp: Path, start: datetime | None = None) -> None:
        self.clock = SessionClock() if start is None else SessionClock(start)
        self.gw = Wave2Gateway(clock=self.clock)
        self.ib = IbkrTrex(client_id=SUPERVISED_CLIENT_ID)
        self.ib._ib = self.gw
        self.paths = DeskPaths(tmp / "desk")
        self.sup = SupervisedPaths(tmp / "supervised")
        self.sup.prepare()
        self.notified: list[tuple[str, str, str]] = []
        self.rt = self._runtime()

    def _runtime(self) -> DeskRuntime:
        rt = DeskRuntime(self.ib, self.paths, supervised=self.sup, clock=self.clock)
        rt.acquire()
        rt.notify = lambda title, message, priority="default": self.notified.append(
            (title, message, priority)
        )
        return rt

    def restart(self) -> None:
        """A fresh desk process: no in-memory trades; book and specs on disk."""
        self.rt.release()
        self.clock.restart(down_s=60)
        self.rt = self._runtime()

    # -- the entry (lane B's send, emulated on the wire) -------------------

    def send_entry(self, effect: SupervisedEquityEffect) -> Any:
        from ib_async import LimitOrder, Stock

        stock = Stock(effect.symbol, "SMART", "USD")
        self.gw.qualifyContracts(stock)
        order = LimitOrder(
            "BUY",
            effect.quantity,
            effect.limit_cents / 100,
            tif="DAY",
            outsideRth=False,
            transmit=True,
            orderRef=effect.order_ref,
            account=effect.account_id,
        )
        return self.gw.placeOrder(stock, order)

    def open_position(self, effect: SupervisedEquityEffect | None = None) -> Any:
        effect = effect or equity_effect()
        self.rt.register(effect)
        self.gw.script_fills(effect.order_ref, [FillStep()])
        entry = self.send_entry(effect)
        self.rt.tick()  # adopted from the live tag
        self.gw.sleep(1)  # the fill
        self.rt.tick()
        assert self.book()[effect.intent_id]["status"] == Status.OPEN.value
        return entry

    # -- market --------------------------------------------------------------

    def tick_bid(self, bid: int, *, spread: int = 4) -> None:
        """One new NBBO tick a second later, then one desk tick."""
        self.clock.advance(1)
        self.gw.push_tick("SPY", bid, bid + spread)
        self.rt.tick()

    # -- reads ---------------------------------------------------------------

    def book(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.paths.book().read_text())["structures"]
        return loaded

    def records(self) -> list[dict[str, Any]]:
        if not self.paths.events().exists():
            return []
        return [json.loads(line) for line in self.paths.events().read_text().splitlines()]

    def events(self) -> list[str]:
        return [r["event"] for r in self.records()]

    def sells(self) -> list[Any]:
        return [t for t in self.gw.trades if t.order.action == "SELL"]

    def held(self) -> float:
        return sum(float(p.position) for p in self.gw.positions() if p.contract.conId == SPY_CON_ID)


@pytest.fixture()
def rig(tmp_path: Path) -> Iterator[Rig]:
    r = Rig(tmp_path)
    yield r
    r.rt.release()


def _confirm_stop(rig: Rig, bid: int = STOP) -> None:
    for _ in range(equity_effect().exit_policy.stop_confirm_ticks):
        rig.tick_bid(bid)


# ------------------------------------------------------------ registration


def test_register_writes_the_v2_equity_spec_and_is_idempotent(rig: Rig) -> None:
    effect = equity_effect()
    registered_at = rig.clock()
    spec = rig.rt.register(effect)
    raw = rig.paths.spec(IID).read_bytes()
    assert raw == model_bytes(desk_spec_v2_for(effect, registered_at))
    parsed = parse_desk_spec(json.loads(raw))
    assert isinstance(parsed, EquityDeskSpecV2) and parsed == spec
    assert rig.book()[IID]["status"] == Status.PLANNED.value
    rig.clock.advance(30)
    assert rig.rt.register(effect) == spec, "idempotent: the first registration stands"
    with pytest.raises(ValueError, match="already registered"):
        rig.rt.register(equity_effect(quantity=3))
    assert rig.rt.specs() == {}, "the option view never carries an equity spec"
    assert list(rig.rt.equity_specs()) == [IID]


def test_option_registration_still_writes_the_v1_spec_bytes(rig: Rig) -> None:
    """The canary-001 path is untouched: a supervised option effect still
    registers a ``trex-desk-spec/1`` file in the same canonical bytes."""
    effect = SupervisedEffect.model_validate(contract_fixture("option_effect_canary.json"))
    registered_at = rig.clock()
    rig.rt.register(effect)
    expected = DeskSpec(
        intent_id=effect.intent_id,
        account_id=effect.account_id,
        structure=effect.structure,
        entry_order_ref=supervised_order_ref(effect.intent_id),
        registered_at=registered_at,
    )
    raw = rig.paths.spec("canary-001").read_bytes()
    assert raw == canonical_bytes(expected.model_dump(mode="json", by_alias=True))
    assert json.loads(raw)["schema"] == "trex-desk-spec/1"
    assert list(rig.rt.specs()) == ["canary-001"] and rig.rt.equity_specs() == {}


def test_a_v2_option_spec_is_read_as_the_option_it_describes(rig: Rig) -> None:
    effect = SupervisedEffect.model_validate(contract_fixture("option_effect_canary.json"))
    v2 = desk_spec_v2_for(effect, rig.clock())
    rig.paths.specs().mkdir(parents=True, exist_ok=True)
    rig.paths.spec("canary-001").write_bytes(model_bytes(v2))
    specs = rig.rt.specs()
    assert list(specs) == ["canary-001"]
    assert specs["canary-001"].structure == effect.structure
    assert specs["canary-001"].entry_order_ref == "trex:sup:canary-001"


def test_an_unknown_spec_schema_fails_the_tick_closed(rig: Rig) -> None:
    rig.rt.register(equity_effect())
    doc = json.loads(rig.paths.spec(IID).read_bytes())
    doc["schema"] = "trex-desk-spec/9"
    rig.paths.spec(IID).write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        rig.rt.tick()


# ------------------------------------------------------------ entry lane


def test_a_filled_entry_opens_the_position_at_its_fill(rig: Rig) -> None:
    rig.open_position()
    st = rig.book()[IID]
    assert (st["filled_qty"], st["entry_fill"]) == (2, "671.5")
    assert "entry_filled" in rig.events()
    assert rig.held() == 2


def test_pause_cancels_an_unfilled_entry_which_closes_unentered(rig: Rig) -> None:
    effect = equity_effect()
    rig.rt.register(effect)
    entry = rig.send_entry(effect)
    rig.rt.tick()
    assert rig.book()[IID]["status"] == Status.ENTER_WORKING.value
    rig.paths.pause_entries().touch()
    rig.rt.tick()
    assert rig.gw.order_cancels == [entry.order.orderId]
    assert entry.orderStatus.status == "Cancelled"
    rig.rt.tick()
    st = rig.book()[IID]
    assert (st["status"], st["close_reason"]) == ("closed", "entry_unfilled")
    cancel = [r for r in rig.records() if r["event"] == "cancel"]
    assert cancel[0]["why"] == "entries_paused"


def test_partial_entry_fill_then_pause_exits_exactly_the_filled_shares(rig: Rig) -> None:
    effect = equity_effect(quantity=100)
    rig.rt.register(effect)
    rig.gw.script_fills(effect.order_ref, [FillStep(37)])
    entry = rig.send_entry(effect)
    rig.rt.tick()
    rig.gw.sleep(1)  # 37 of 100 fill; the rest keeps working
    rig.rt.tick()
    st = rig.book()[IID]
    assert (st["status"], st["filled_qty"]) == ("enter_working", 37)
    rig.paths.pause_entries().touch()
    rig.rt.tick()  # the unfilled remainder is cancelled (confirmed)
    assert entry.orderStatus.status == "Cancelled"
    rig.rt.tick()
    st = rig.book()[IID]
    assert (st["status"], st["filled_qty"]) == ("open", 37)
    assert rig.held() == 37
    _confirm_stop(rig)
    (sell,) = rig.sells()
    assert sell.order.totalQuantity == 37, "exactly the filled shares, never the order size"
    assert rig.gw.wire_violations == []


def test_an_exit_rule_during_a_partial_entry_cancels_the_remainder(rig: Rig) -> None:
    effect = equity_effect(quantity=100)
    rig.rt.register(effect)
    rig.gw.script_fills(effect.order_ref, [FillStep(37)])
    entry = rig.send_entry(effect)
    rig.rt.tick()
    rig.gw.sleep(1)
    rig.rt.tick()
    _confirm_stop(rig)  # the stop confirms while the entry still works
    assert entry.orderStatus.status == "Cancelled"
    assert rig.sells() == [], "the remainder goes first"
    rig.tick_bid(STOP)
    rig.tick_bid(STOP)
    (sell,) = rig.sells()
    assert sell.order.totalQuantity == 37


# ------------------------------------------------------------ exit rules


def test_pause_never_blocks_a_stop_exit(rig: Rig) -> None:
    rig.open_position()
    rig.paths.pause_entries().touch()
    rig.tick_bid(STOP)
    rig.tick_bid(STOP - 10)
    assert rig.sells() == [], "two ticks of three do not confirm"
    rig.tick_bid(STOP - 20)
    (sell,) = rig.sells()
    order, contract = sell.order, sell.contract
    assert (order.action, order.totalQuantity, order.lmtPrice) == ("SELL", 2, 664.80)
    assert order.orderRef == desk_order_ref(IID) == "trex:desk:eq-spy-001"
    assert (order.orderType, order.tif, order.outsideRth, order.account) == (
        "LMT",
        "DAY",
        False,
        "DU0000001",
    )
    assert (contract.secType, contract.conId, contract.exchange) == ("STK", SPY_CON_ID, "SMART")
    assert rig.gw.wire_violations == []
    st = rig.book()[IID]
    assert (st["status"], st["exit_reason"]) == ("exit_working", "stop")
    assert DeskEvent.EQUITY_EXIT_STOP.value in rig.events()


def test_halt_and_a_confirmed_stop_sends_nothing_until_resumed(rig: Rig) -> None:
    rig.open_position()
    rig.paths.halt().touch()
    _confirm_stop(rig)
    assert rig.sells() == []
    assert rig.book()[IID]["status"] == Status.OPEN.value
    assert "halt_no_new_orders" in rig.events()
    rig.paths.halt().unlink()
    rig.clock.advance(1)
    rig.rt.tick()  # the confirmed stop still stands: the exit goes out
    assert [s.order.totalQuantity for s in rig.sells()] == [2]


def test_a_delayed_quote_never_confirms_a_stop(rig: Rig) -> None:
    rig.open_position()
    rig.gw.set_market_data_type("SPY", "delayed")
    for _ in range(5):
        rig.tick_bid(STOP - 100)
    assert rig.sells() == []
    assert rig.book()[IID].get("stop_ticks", 0) == 0


def test_target_exit_fills_and_closes_the_book(rig: Rig) -> None:
    rig.open_position()
    rig.gw.script_fills(desk_order_ref(IID), [FillStep()])
    rig.tick_bid(TARGET)
    (sell,) = rig.sells()
    assert sell.order.lmtPrice == 680.0
    rig.gw.sleep(1)
    rig.rt.tick()
    st = rig.book()[IID]
    assert (st["status"], st["close_reason"]) == ("closed", "target")
    assert Decimal(st["exit_fill"]) == Decimal("680")
    assert rig.held() == 0
    assert DeskEvent.EQUITY_EXIT_TARGET.value in rig.events()


def test_a_working_exit_reprices_only_down_to_a_lower_bid(rig: Rig) -> None:
    rig.open_position()
    _confirm_stop(rig)
    first = rig.sells()[0]
    rig.tick_bid(STOP + 50)  # a higher bid: the resting sell is marketable
    assert rig.sells() == [first]
    rig.tick_bid(STOP - 50)
    assert first.orderStatus.status == "Cancelled"
    second = rig.sells()[-1]
    assert second is not first and second.order.lmtPrice == 664.50
    assert second.order.totalQuantity == 2
    assert rig.book()[IID]["exit_cycles"] == 1
    assert rig.gw.wire_violations == []


def test_forced_exit_on_an_early_close_goes_out_at_1245(tmp_path: Path) -> None:
    rig = Rig(tmp_path, start=datetime(2026, 11, 27, 14, 30, tzinfo=UTC))  # 09:30 ET
    try:
        effect = equity_effect(
            intent_id="eq-spy-ec", exit_policy={"exit_deadline_session": "2026-11-27"}
        )
        rig.open_position(effect)
        rig.clock.to_et("12:44", EARLY_CLOSE_DAY)
        rig.gw.push_tick("SPY", 67000, 67004)
        rig.rt.tick()
        assert rig.sells() == [], "the close is 13:00: nothing before 12:45"
        rig.clock.to_et("12:45", EARLY_CLOSE_DAY)
        rig.gw.push_tick("SPY", 67000, 67004)
        rig.rt.tick()
        (sell,) = rig.sells()
        assert (sell.order.totalQuantity, sell.order.lmtPrice) == (2, 670.0)
        assert rig.book()["eq-spy-ec"]["exit_reason"] == "forced"
        assert DeskEvent.EQUITY_EXIT_FORCED.value in rig.events()
    finally:
        rig.rt.release()


def test_a_passed_deadline_exits_at_the_next_open(rig: Rig) -> None:
    rig.open_position()
    rig.clock.to_open(date(2026, 10, 26))  # the deadline session 10-23 passed
    rig.gw.push_tick("SPY", 67000, 67004)
    rig.rt.tick()
    (sell,) = rig.sells()
    assert sell.order.totalQuantity == 2
    assert rig.book()[IID]["exit_reason"] == "deadline"
    assert DeskEvent.EQUITY_EXIT_DEADLINE.value in rig.events()


def test_flatten_sells_an_equity_at_the_bid(rig: Rig) -> None:
    rig.open_position()
    rig.paths.flatten().touch()
    rig.tick_bid(67100)
    (sell,) = rig.sells()
    assert (sell.order.totalQuantity, sell.order.lmtPrice) == (2, 671.0)
    assert rig.book()[IID]["exit_reason"] == "flatten"


# ------------------------------------------------------------ holdings check


def test_a_holdings_mismatch_blocks_the_sell_and_requires_reconciliation(rig: Rig) -> None:
    rig.open_position()
    rig.gw.override_position(SPY_CON_ID, 1)
    _confirm_stop(rig)
    assert rig.sells() == [], "never sell what the broker does not show"
    assert rig.book()[IID]["status"] == Status.OPEN.value
    recon = [r for r in rig.records() if r["event"] == DeskEvent.RECONCILIATION_REQUIRED.value]
    assert len(recon) == 1
    assert (recon[0]["broker_qty"], recon[0]["book_qty"]) == ("1", 2)
    assert any("reconciliation_required" in n[0] and n[2] == "urgent" for n in rig.notified)
    rig.gw.clear_position_faults()
    rig.clock.advance(1)
    rig.rt.tick()  # retried, not stuck
    assert [s.order.totalQuantity for s in rig.sells()] == [2]


def test_unexplained_extra_shares_also_block_the_sell(rig: Rig) -> None:
    rig.open_position()
    rig.gw.add_broker_position("SPY", 5)
    _confirm_stop(rig)
    assert rig.sells() == []
    assert DeskEvent.RECONCILIATION_REQUIRED.value in rig.events()


def test_a_fractional_broker_position_blocks_the_sell(rig: Rig) -> None:
    rig.open_position()
    rig.gw.override_position(SPY_CON_ID, 2.5)
    _confirm_stop(rig)
    assert rig.sells() == []
    assert DeskEvent.RECONCILIATION_REQUIRED.value in rig.events()


def test_an_unreadable_position_view_blocks_the_sell(rig: Rig) -> None:
    rig.open_position()
    rig.gw.account_view_error = RuntimeError("positions down")
    _confirm_stop(rig)
    assert rig.sells() == []
    assert "holdings_unverifiable" in rig.events()


# ------------------------------------------------------------ restarts


def test_restart_mid_position_keeps_the_count_and_still_exits(rig: Rig) -> None:
    rig.open_position()
    rig.tick_bid(STOP)
    rig.tick_bid(STOP)
    rig.restart()
    rig.tick_bid(STOP)  # the third consecutive tick, across the restart
    (sell,) = rig.sells()
    assert sell.order.totalQuantity == 2
    assert rig.book()[IID]["exit_reason"] == "stop"


def test_restart_readopts_a_working_sell_and_closes_on_its_fill(rig: Rig) -> None:
    rig.open_position()
    _confirm_stop(rig)
    (sell,) = rig.sells()
    rig.restart()
    rig.tick_bid(STOP + 10)
    assert rig.sells() == [sell], "the live desk sell is adopted, never doubled"
    rig.gw.script_fills(desk_order_ref(IID), [FillStep()])
    rig.gw.sleep(1)
    rig.rt.tick()
    st = rig.book()[IID]
    assert (st["status"], st["close_reason"]) == ("closed", "stop")
    assert Decimal(st["exit_fill"]) == Decimal("665")


def test_an_exit_filled_while_down_closes_from_todays_executions(rig: Rig) -> None:
    rig.open_position()
    rig.gw.script_fills(desk_order_ref(IID), [FillStep()])
    rig.tick_bid(TARGET)
    rig.rt.release()
    rig.gw.sleep(1)  # the sell fills while no desk runs
    rig.clock.restart(down_s=60)
    rig.rt = rig._runtime()
    placed = len(rig.gw.trades)
    rig.rt.tick()
    st = rig.book()[IID]
    assert (st["status"], st["close_reason"]) == ("closed", "target")
    assert len(rig.gw.trades) == placed, "a filled exit is never re-sent"


def test_an_entry_filled_while_down_opens_from_todays_executions(rig: Rig) -> None:
    effect = equity_effect()
    rig.rt.register(effect)
    rig.gw.script_fills(effect.order_ref, [FillStep()])
    rig.send_entry(effect)
    rig.rt.tick()
    rig.rt.release()
    rig.gw.sleep(1)
    rig.clock.restart(down_s=60)
    rig.rt = rig._runtime()
    rig.rt.tick()
    st = rig.book()[IID]
    assert (st["status"], st["filled_qty"], st["entry_fill"]) == ("open", 2, "671.5")


# ------------------------------------------------------------ exit watch


def test_monitor_json_guards_the_symbol_and_feeds_the_quote_blind_alarm(rig: Rig) -> None:
    rig.open_position()
    rig.tick_bid(67100)
    health = json.loads(rig.paths.health().read_text())
    assert health["quote_guarded"] == ["SPY"] and health["quote_blind"] == {}
    rig.gw.set_market_data_type("SPY", "delayed")
    rig.tick_bid(67100)
    blind_at = rig.clock()
    health = json.loads(rig.paths.health().read_text())
    assert health["quote_blind"] == {"SPY": blind_at.isoformat()}
    books = exit_watch.scan_books(rig.paths.root.parent)
    obs = exit_watch.ExitObs(
        now=blind_at.timestamp() + exit_watch.QUOTE_BLIND_S,
        books=books,
        gateway_status=None,
        gateway_since=None,
        market=True,
        unit_state=None,
    )
    assert exit_watch.classify(obs)[0] == "quote_blind"
    rig.gw.set_market_data_type("SPY", "realtime")
    rig.tick_bid(67100)
    assert json.loads(rig.paths.health().read_text())["quote_blind"] == {}
