"""E5 desk runtime v1: the exit owner of supervised positions (ruling 2b).

Drives the REAL ``IbkrTrex`` (BAG orders, package quotes, fill evidence) and
the REAL pure desk engine over ``SupervisedGateway``; the only doubles are
the gateway and the clock. Oracles are wire fields, book states and event
names; prices are chosen so the package mid is exact (bid 0.70 / ask 1.10).
"""

from __future__ import annotations

import fcntl
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.unit.trex_fakes import SupervisedGateway, fill_row, position_row
from tree_options.desk.dividends import DividendRecord, DividendSnapshot
from tree_options.execution import SubmitAttempt
from tree_options.trex.clock import ET
from tree_options.trex.desk_runtime import (
    DeskPaths,
    DeskRuntime,
    RuntimeLocked,
    assignment_plan,
    declared_dividend,
    desk_order_ref,
    exit_owner_ready,
)
from tree_options.trex.ibkr import IbkrTrex
from tree_options.trex.plan import LegStructure
from tree_options.trex.state import Status
from tree_options.trex.supervised import SupervisedPaths
from tree_options.trex.supervised_ibkr import (
    SUPERVISED_CLIENT_ID,
    IbkrSupervisedBroker,
    SupervisedEffect,
    effect_bytes,
    supervised_order_ref,
)

pytest.importorskip("ib_async")

ACCOUNT = "DU1234567"
F = "20261016"
CON = {
    ("OPT", "SPY", F, 100.0, "P"): 100,
    ("OPT", "SPY", F, 95.0, "P"): 95,
    ("OPT", "SPY", F, 100.0, "C"): 200,
    ("OPT", "SPY", F, 105.0, "C"): 205,
    ("STK", "SPY", "", 0.0, ""): 1,
}
ENTRY_DAY = datetime(2026, 10, 1, 10, 0, tzinfo=ET)
HOLD_DAY = datetime(2026, 10, 2, 10, 0, tzinfo=ET)
EXIT_DAY = datetime(2026, 10, 9, 10, 0, tzinfo=ET)


def _put_vertical(sid: str = "dv1") -> LegStructure:
    return LegStructure(
        id=sid, underlying="SPY", kind="debit_vertical",
        legs=[{"right": "P", "action": "BUY", "strike": "100", "expiry": date(2026, 10, 16)},
              {"right": "P", "action": "SELL", "strike": "95", "expiry": date(2026, 10, 16)}],
        quantity=1, entry_date=date(2026, 10, 1), exit_deadline=date(2026, 10, 9),
        limit="1.00", exits={"touch": False, "breach": False})


def _call_vertical() -> LegStructure:
    return LegStructure(
        id="cv1", underlying="SPY", kind="debit_vertical",
        legs=[{"right": "C", "action": "BUY", "strike": "100", "expiry": date(2026, 10, 16)},
              {"right": "C", "action": "SELL", "strike": "105", "expiry": date(2026, 10, 16)}],
        quantity=1, entry_date=date(2026, 10, 1), exit_deadline=date(2026, 10, 9),
        limit="3.00", exits={"touch": False, "breach": False})


def _effect(structure: LegStructure | None = None, intent_id: str = "sup-001"
            ) -> SupervisedEffect:
    structure = structure or _put_vertical()
    return SupervisedEffect(intent_id=intent_id, account_id=ACCOUNT, structure=structure,
                            side=structure.open_side, quantity=1, limit=Decimal("0.90"),
                            order_ref=supervised_order_ref(intent_id))


class DeskGateway(SupervisedGateway):
    """Cancels confirm at once unless ``cancel_confirms`` is False."""

    def __init__(self) -> None:
        super().__init__(CON, ACCOUNT)
        self.cancel_confirms = True

    def cancelOrder(self, order: Any) -> None:
        super().cancelOrder(order)
        if self.cancel_confirms:
            for trade in self.trades:
                if trade.order is order:
                    trade.orderStatus.status = "Cancelled"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Desk:
    """A runtime, its gateway, broker adapter, clock and state dirs."""

    def __init__(self, tmp: Path, now: datetime = ENTRY_DAY) -> None:
        self.gw = DeskGateway()
        self.gw.quote(100, 2.00, 2.20)
        self.gw.quote(95, 1.10, 1.30)
        self.ib = IbkrTrex(client_id=SUPERVISED_CLIENT_ID)
        self.ib._ib = self.gw
        self.clock = Clock(now)
        self.paths = DeskPaths(tmp / "desk")
        self.sup = SupervisedPaths(tmp / "supervised")
        self.sup.prepare()
        self.rt = DeskRuntime(self.ib, self.paths, supervised=self.sup, clock=self.clock)
        self.rt.acquire()
        self.broker = IbkrSupervisedBroker(self.ib, clock=self.clock)

    def send_entry(self, effect: SupervisedEffect) -> Any:
        attempt = SubmitAttempt(record_id=f"sup-send-{effect.intent_id}",
                                intent_id=effect.intent_id, send_attempt_at=self.clock.now,
                                source="supervised", source_sequence_id=effect.intent_id)
        self.broker.submit(attempt, effect_bytes(effect))
        return self.gw.trades[-1]

    def book(self) -> dict[str, Any]:
        return json.loads(self.paths.book().read_text())["structures"]

    def events(self) -> list[str]:
        if not self.paths.events().exists():
            return []
        return [json.loads(line)["event"] for line in self.paths.events().read_text().splitlines()]

    def hold_legs(self, qty: int = 1) -> None:
        self.gw.position_rows[:] = [position_row(100, qty, account=ACCOUNT, symbol="SPY"),
                                    position_row(95, -qty, account=ACCOUNT, symbol="SPY")]

    def fill(self, trade: Any, qty: int, price: float) -> None:
        trade.orderStatus.status = "Filled"
        trade.orderStatus.filled = qty
        trade.orderStatus.avgFillPrice = price

    def restart(self) -> None:
        """A fresh process: no in-memory trades; the book and outbox on disk."""
        self.rt.release()
        self.rt = DeskRuntime(self.ib, self.paths, supervised=self.sup, clock=self.clock)
        self.rt.acquire()

    def exit_while_down(self, *, status: str, executed: int = 0, qty: int = 1) -> Any:
        """Open ``qty``, place the time-stop exit, then the exit order leaves
        the live view (``status``) with ``executed`` packages filled today
        (long leg sold at 2.10, short leg bought at 1.20: 0.90 a package)."""
        if qty == 1:
            self.open_position()
        else:
            two = _put_vertical().model_copy(update={"quantity": qty})
            effect = _effect(two).model_copy(update={"quantity": qty})
            self.rt.register(effect)
            entry = self.send_entry(effect)
            self.rt.tick()
            self.fill(entry, qty, 0.90)
            self.hold_legs(qty)
            self.rt.tick()
        self.clock.now = EXIT_DAY
        self.rt.tick()
        exit_trade = self.trades_placed()[-1]
        oid = exit_trade.order.orderId
        exit_trade.orderStatus.status = status
        if executed:
            self.gw.fill_rows[:] = [fill_row(100, executed, 2.10, oid, SUPERVISED_CLIENT_ID),
                                    fill_row(95, executed, 1.20, oid, SUPERVISED_CLIENT_ID)]
        self.restart()
        return exit_trade

    def trades_placed(self) -> list[Any]:
        return list(self.gw.trades)

    def open_position(self) -> Any:
        """Register, send, fill the entry, tick to OPEN; returns the entry trade."""
        effect = _effect()
        self.rt.register(effect)
        entry = self.send_entry(effect)
        self.rt.tick()
        self.fill(entry, 1, 0.90)
        self.hold_legs()
        self.rt.tick()
        assert self.book()["dv1"]["status"] == Status.OPEN.value
        return entry


@pytest.fixture()
def desk(tmp_path: Path) -> Desk:
    d = Desk(tmp_path)
    yield d
    d.rt.release()


# ------------------------------------------------------------ ownership


def test_one_runtime_per_run_dir(desk, tmp_path):
    second = DeskRuntime(desk.ib, desk.paths, supervised=desk.sup, clock=desk.clock)
    with pytest.raises(RuntimeLocked):
        second.acquire()


def test_register_requires_the_lock_and_is_idempotent(desk, tmp_path):
    unlocked = DeskRuntime(desk.ib, DeskPaths(tmp_path / "other"), supervised=desk.sup,
                           clock=desk.clock)
    with pytest.raises(RuntimeLocked):
        unlocked.register(_effect())
    first = desk.rt.register(_effect())
    assert desk.rt.register(_effect()) == first
    with pytest.raises(ValueError, match="already registered"):
        desk.rt.register(_effect(intent_id="sup-999"))
    assert desk.book()["dv1"]["status"] == Status.PLANNED.value


# ---------------------------------------------------------- entry lane


def test_live_tagged_entry_is_adopted_then_filled_to_open(desk):
    effect = _effect()
    desk.rt.register(effect)
    entry = desk.send_entry(effect)
    desk.rt.tick()
    assert desk.book()["dv1"]["status"] == Status.ENTER_WORKING.value
    assert desk.book()["dv1"]["entry_order"] == str(entry.order.orderId)
    desk.fill(entry, 1, 0.90)
    desk.rt.tick()
    book = desk.book()["dv1"]
    assert (book["status"], book["filled_qty"], book["entry_fill"]) == ("open", 1, "0.9")


def test_supervised_entry_is_never_repriced(desk):
    effect = _effect()
    desk.rt.register(effect)
    desk.send_entry(effect)
    for _ in range(3):
        desk.rt.tick()
    assert len(desk.gw.trades) == 1, "only the permitted order ever went out"
    assert desk.events().count("entry_reprice_not_sent") == 1


def test_entry_window_close_cancels_then_closes_unfilled(desk):
    effect = _effect()
    desk.rt.register(effect)
    entry = desk.send_entry(effect)
    desk.rt.tick()
    desk.clock.now = ENTRY_DAY.replace(hour=11, minute=31)
    desk.rt.tick()
    assert entry.orderStatus.status == "Cancelled"
    assert desk.gw.order_cancels == [entry.order.orderId]
    desk.rt.tick()
    assert desk.book()["dv1"]["status"] == Status.CLOSED.value
    assert desk.book()["dv1"]["close_reason"] == "entry_unfilled"


@pytest.mark.parametrize("outbox, status, reason", [
    ({"terminal": {"outcome": "rejected"}}, "closed", "entry_rejected"),
    ({"reconciled": {"verdict": "confirmed_not_submitted"}}, "closed", "not_submitted"),
    ({"terminal": {"outcome": "uncertain", "reason": "ack_timeout"}}, "planned", None),
    ({"pending": {"send_deadline": "2026-10-01T09:59:00-04:00"}}, "closed",
     "not_sent_by_deadline"),
    ({"pending": {"send_deadline": "2026-10-01T10:05:00-04:00"}}, "planned", None),
])
def test_planned_structures_resolve_from_the_supervised_outbox(desk, outbox, status, reason):
    desk.rt.register(_effect())
    for kind, doc in outbox.items():
        getattr(desk.sup, kind)("sup-001").write_text(json.dumps(doc))
    desk.rt.tick()
    book = desk.book()["dv1"]
    assert book["status"] == status
    assert book["close_reason"] == reason


def test_acknowledged_receipt_with_fill_evidence_after_restart(desk, tmp_path):
    """The entry filled while the runtime was down: only broker evidence opens it."""
    desk.rt.register(_effect())
    desk.sup.terminal("sup-001").write_text(json.dumps(
        {"outcome": "acknowledged", "broker_order_id": "777"}))
    desk.gw.fill_rows[:] = [fill_row(100, 1, 2.00, 777, SUPERVISED_CLIENT_ID),
                            fill_row(95, 1, 1.10, 777, SUPERVISED_CLIENT_ID)]
    desk.rt.tick()
    book = desk.book()["dv1"]
    assert (book["status"], book["filled_qty"]) == ("open", 1)
    assert Decimal(book["entry_fill"]) == Decimal("0.90")  # 2.00 paid - 1.10 received


def test_inconclusive_evidence_holds_and_alerts(desk):
    desk.rt.register(_effect())
    desk.sup.terminal("sup-001").write_text(json.dumps(
        {"outcome": "acknowledged", "broker_order_id": "777"}))
    desk.hold_legs()  # legs held, but no execution of 777 today: never guessed
    desk.rt.tick()
    assert desk.book()["dv1"]["status"] == Status.ENTER_WORKING.value
    assert "entry_evidence_inconclusive" in desk.events()


# ----------------------------------------------------------- exit lane


def test_time_stop_exit_is_placed_tagged_with_the_account_then_closes(desk):
    desk.open_position()
    desk.clock.now = EXIT_DAY
    desk.rt.tick()
    book = desk.book()["dv1"]
    assert (book["status"], book["exit_reason"]) == ("exit_working", "time_stop")
    exit_trade = desk.gw.trades[-1]
    assert exit_trade.order.orderRef == desk_order_ref("dv1") == "trex:desk:dv1"
    assert exit_trade.order.account == ACCOUNT
    assert (exit_trade.order.action, exit_trade.order.totalQuantity,
            exit_trade.order.lmtPrice) == ("SELL", 1, 0.90)
    desk.fill(exit_trade, 1, 0.85)
    desk.gw.position_rows.clear()
    desk.rt.tick()
    book = desk.book()["dv1"]
    assert (book["status"], book["close_reason"], book["exit_fill"]) == (
        "closed", "time_stop", "0.85")


def test_no_close_without_the_legs_held(desk):
    desk.open_position()
    desk.gw.position_rows.clear()  # the broker shows no position: never sell into nothing
    desk.clock.now = EXIT_DAY
    desk.rt.tick()
    assert desk.book()["dv1"]["status"] == Status.OPEN.value
    assert all(t.order.action == "BUY" for t in desk.gw.trades)
    assert "legs_mismatch" in desk.events()


def test_exit_reprice_cancels_confirms_then_replaces(desk):
    desk.open_position()
    desk.clock.now = EXIT_DAY
    desk.rt.tick()
    first = desk.gw.trades[-1]
    desk.gw.quote(95, 1.10, 1.20)  # package bid 0.80 / ask 1.10 -> mid 0.95
    desk.rt.tick()
    assert first.orderStatus.status == "Cancelled"
    second = desk.gw.trades[-1]
    assert second is not first
    assert (second.order.lmtPrice, second.order.orderRef) == (0.95, "trex:desk:dv1")
    assert desk.book()["dv1"]["exit_cycles"] == 1


def test_unconfirmed_cancel_keeps_the_old_exit_and_sends_nothing(desk):
    desk.open_position()
    desk.clock.now = EXIT_DAY
    desk.rt.tick()
    first = desk.gw.trades[-1]
    desk.gw.cancel_confirms = False
    desk.gw.quote(95, 1.10, 1.20)
    desk.rt.tick()
    assert desk.gw.trades[-1] is first, "no second sell while the first may still work"
    assert "cancel_unconfirmed" in desk.events()


def test_partial_exit_fill_on_reprice_sizes_the_replacement(desk):
    two = _put_vertical().model_copy(update={"quantity": 2})
    effect = _effect(two).model_copy(update={"quantity": 2})
    desk.rt.register(effect)
    entry = desk.send_entry(effect)
    desk.rt.tick()
    desk.fill(entry, 2, 0.90)
    desk.hold_legs(2)
    desk.rt.tick()
    assert (desk.book()["dv1"]["status"], desk.book()["dv1"]["filled_qty"]) == ("open", 2)
    desk.clock.now = EXIT_DAY
    desk.rt.tick()
    first = desk.gw.trades[-1]
    assert first.order.totalQuantity == 2
    first.orderStatus.filled, first.orderStatus.avgFillPrice = 1, 0.90
    desk.hold_legs(1)
    desk.gw.quote(95, 1.10, 1.20)
    desk.rt.tick()
    second = desk.gw.trades[-1]
    assert second is not first and second.order.totalQuantity == 1


def test_unknown_exposure_is_never_acted_on(desk):
    desk.open_position()
    contract, order = desk.ib._order(_put_vertical(), "BUY", 1, Decimal("0.90"))
    order.orderRef = desk_order_ref("dv1")  # our exit tag on an OPEN-side order
    desk.gw.placeOrder(contract, order)
    placed = len(desk.gw.trades)
    desk.clock.now = EXIT_DAY
    desk.rt.tick()
    assert len(desk.gw.trades) == placed
    assert "unknown_exposure" in desk.events()


# ------------------------------------- the exit left the view while down


def test_exit_filled_while_down_closes_from_todays_executions(desk):
    desk.exit_while_down(status="Filled", executed=1)
    desk.gw.position_rows.clear()
    placed = len(desk.gw.trades)
    desk.rt.tick()
    book = desk.book()["dv1"]
    assert (book["status"], book["close_reason"]) == ("closed", "time_stop")
    assert Decimal(book["exit_fill"]) == Decimal("0.90")
    assert len(desk.gw.trades) == placed, "a filled exit is never re-sent"


def test_partial_exit_then_expiry_while_down_replaces_only_the_remainder(desk):
    desk.exit_while_down(status="Cancelled", executed=1, qty=2)
    desk.hold_legs(1)
    placed = len(desk.gw.trades)
    desk.rt.tick()
    book = desk.book()["dv1"]
    assert (book["status"], book["exit_filled_qty"]) == ("exit_working", 1)
    assert len(desk.gw.trades) == placed + 1
    replacement = desk.gw.trades[-1]
    assert (replacement.order.action, replacement.order.totalQuantity) == ("SELL", 1)


def test_unfilled_exit_expired_while_down_is_replaced(desk):
    desk.exit_while_down(status="Cancelled")
    desk.hold_legs(1)
    placed = len(desk.gw.trades)
    desk.rt.tick()
    assert len(desk.gw.trades) == placed + 1
    assert desk.gw.trades[-1].order.orderRef == "trex:desk:dv1"


def test_flat_without_todays_executions_waits_for_the_operator(desk):
    """An earlier-day fill and unloaded positions look the same: never guess."""
    desk.exit_while_down(status="Filled")
    desk.gw.position_rows.clear()
    placed = len(desk.gw.trades)
    desk.rt.tick()
    desk.rt.tick()
    assert desk.book()["dv1"]["status"] == Status.EXIT_WORKING.value
    assert desk.events().count("exit_flat_unexplained") == 1
    assert len(desk.gw.trades) == placed, "nothing is sent into a flat account"
    desk.paths.resolve_flat("dv1").touch()
    desk.rt.tick()
    book = desk.book()["dv1"]
    assert (book["status"], book["close_reason"]) == ("closed", "operator_confirmed_flat")
    assert book["exit_unpriced_qty"] == 1
    assert not desk.paths.resolve_flat("dv1").exists()
    assert desk.paths.resolve_flat("dv1").with_name("RESOLVE-FLAT-dv1.applied").exists()


def test_resolve_file_never_closes_a_held_position(desk):
    desk.exit_while_down(status="Cancelled")
    desk.hold_legs(1)
    desk.paths.resolve_flat("dv1").touch()
    desk.rt.tick()
    assert desk.book()["dv1"]["status"] == Status.EXIT_WORKING.value
    assert desk.paths.resolve_flat("dv1").exists(), "not consumed: the legs are held"


def test_leg_mismatch_alerts_once_while_it_persists(desk):
    desk.exit_while_down(status="Cancelled")
    desk.hold_legs(3)
    desk.rt.tick()
    desk.rt.tick()
    assert desk.events().count("legs_mismatch") == 1
    desk.hold_legs(2)  # a different mismatch alerts again
    desk.rt.tick()
    assert desk.events().count("legs_mismatch") == 2


# ------------------------------------------------------------ kill files


def test_halt_places_no_exit(desk):
    desk.open_position()
    desk.paths.halt().touch()
    desk.clock.now = EXIT_DAY
    desk.rt.tick()
    assert desk.book()["dv1"]["status"] == Status.OPEN.value
    assert "halt_no_new_orders" in desk.events()


def test_flatten_closes_open_at_the_bid(desk):
    desk.open_position()
    desk.paths.flatten().touch()
    desk.clock.now = HOLD_DAY  # nothing due: only FLATTEN acts
    desk.rt.tick()
    exit_trade = desk.gw.trades[-1]
    assert (exit_trade.order.action, exit_trade.order.lmtPrice) == ("SELL", 0.70)
    assert desk.book()["dv1"]["exit_reason"] == "flatten"


def test_flatten_cancels_a_working_entry(desk):
    effect = _effect()
    desk.rt.register(effect)
    entry = desk.send_entry(effect)
    desk.rt.tick()
    desk.paths.flatten().touch()
    desk.rt.tick()
    assert entry.orderStatus.status == "Cancelled"


def test_hold_day_places_nothing(desk):
    desk.open_position()
    placed = len(desk.gw.trades)
    desk.clock.now = HOLD_DAY
    desk.rt.tick()
    assert len(desk.gw.trades) == placed


# -------------------------------------------------------------- arm gate


def test_exit_owner_ready_needs_a_fresh_beat_and_the_held_lock(desk):
    desk.rt.register(_effect())
    desk.rt.tick()
    assert exit_owner_ready(desk.paths, ENTRY_DAY.replace(second=20))
    assert not exit_owner_ready(desk.paths, ENTRY_DAY.replace(second=31))
    desk.rt.release()
    assert not exit_owner_ready(desk.paths, ENTRY_DAY.replace(second=5))
    desk.rt.acquire()


def test_exit_owner_not_ready_without_a_book(tmp_path):
    assert not exit_owner_ready(DeskPaths(tmp_path / "none"), ENTRY_DAY)


# ------------------------------------------------------ assignment plan


def _snapshot(*records: DividendRecord) -> DividendSnapshot:
    return DividendSnapshot(symbol="SPY", session=date(2026, 9, 30), records=records)


def test_put_vertical_needs_no_dividend_feed():
    ok, why = assignment_plan(_put_vertical(), None, date(2026, 10, 1))
    assert ok and "no short call" in why


def test_short_call_without_a_feed_fails_closed():
    ok, why = assignment_plan(_call_vertical(), None, date(2026, 10, 1))
    assert not ok and "dividend snapshot" in why


def test_short_call_with_a_projected_ex_date_in_the_hold_fails_closed():
    last = DividendRecord(ex_date=date(2026, 7, 6), declared=date(2026, 6, 20),
                          cash_amount=Decimal("1.80"), frequency=4, dividend_type="CD")
    ok, why = assignment_plan(_call_vertical(), _snapshot(last), date(2026, 10, 1))
    assert not ok and "projected" in why


def test_short_call_with_a_declared_ex_date_is_fed_to_the_engine():
    declared = DividendRecord(ex_date=date(2026, 10, 5), declared=date(2026, 9, 20),
                              cash_amount=Decimal("1.85"), frequency=4, dividend_type="CD")
    snap = _snapshot(declared)
    ok, _ = assignment_plan(_call_vertical(), snap, date(2026, 10, 1))
    assert ok
    div = declared_dividend(snap, date(2026, 10, 1), date(2026, 10, 15))
    assert div is not None
    assert (div.ex_date, div.prev_session, div.amount) == (
        date(2026, 10, 5), date(2026, 10, 2), Decimal("1.85"))


def test_short_call_mids_reach_the_engine_snapshot(desk):
    desk.gw.quote(200, 3.00, 3.20)
    desk.gw.quote(205, 1.00, 1.20)
    effect = _effect(_call_vertical(), intent_id="sup-002")
    effect = effect.model_copy(update={"limit": Decimal("2.00")})
    desk.rt.register(effect)
    snap = desk.rt._snapshot(desk.rt.specs(), ENTRY_DAY)
    assert snap.short_call_mids == {"cv1|C105|2026-10-16": Decimal("1.10")}


def test_lock_file_is_what_the_arm_gate_probes(desk):
    with open(desk.paths.lock(), "a+b") as probe, pytest.raises(BlockingIOError):
        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
