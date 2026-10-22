"""Wave 2b lane c2: equity forced/deadline exits never stall without a
real-time bid (review P2).

Drives the REAL ``DeskRuntime`` and ``IbkrTrex`` over the shared C2.8
``Wave2Gateway`` and ``SessionClock`` (the lane C ``Rig``), switching the
market-data type IBKR delivers with ``set_market_data_type``. The wire
checks of the gateway (LMT, DAY, the desk tag, never selling more than held
net of working sells) hold on every order.

Oracles: the frozen fixture effect (2 SPY, stop 665.00, target 680.00,
deadline session 2026-10-23, forced 15:45 ET) and the documented fallback
policy: a non-real-time bid prices a time-driven exit 2 cents below it, 2
more per full minute the exit works, capped at 10 cents; 300 s unpriced
raises ``exit_quote_blind`` once per structure per session. A standing
FLATTEN (review P1) closes on the same fallback: the runtime's flatten
branch prices through ``equity_exits.marketable_limit``.

``exit_quote_blind`` is wired into ``DeskRuntime._equity_close`` itself
(Wave 3/4 integration; the lane proved it with a subclass carrying the same
hook), and the event is pushed urgent (``desk_runtime._NOTIFY_EVENTS``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, time
from pathlib import Path

import pytest

from tests.unit.test_desk_runtime_equity import IID, STOP, TARGET, Rig
from tests.unit.trex_fakes import FillStep, equity_effect
from tree_options.trex import equity_exits as ee
from tree_options.trex.clock import ET
from tree_options.trex.contracts import DeskEvent
from tree_options.trex.state import Status

pytest.importorskip("ib_async")

ENTRY_DAY = date(2026, 10, 22)  # the SessionClock's first session
DEADLINE = date(2026, 10, 23)
NEXT_SESSION = date(2026, 10, 26)


@pytest.fixture()
def rig(tmp_path: Path) -> Iterator[Rig]:
    r = Rig(tmp_path)
    yield r
    r.rt.release()


@pytest.fixture()
def wired(rig: Rig) -> Rig:
    """The real ``DeskRuntime`` (the alert hook is its own since integration)."""
    return rig


def _limits(rig: Rig) -> list[float]:
    return [s.order.lmtPrice for s in rig.sells()]


def _quote_at(rig: Rig, wall: str, day: date, bid: int | None) -> None:
    """Move to ``wall`` ET on ``day``, deliver one NBBO tick, tick the desk."""
    rig.clock.to_et(wall, day)
    rig.gw.push_tick("SPY", bid, None if bid is None else bid + 4)
    rig.rt.tick()


def _tick_every(rig: Rig, seconds: int, count: int) -> None:
    for _ in range(count):
        rig.rt.tick()
        rig.clock.advance(seconds)


# ------------------------------------------------------------ fallback pricing


def test_a_delayed_only_forced_exit_goes_out_two_ticks_below_the_delayed_bid(rig: Rig) -> None:
    rig.open_position()
    rig.gw.set_market_data_type("SPY", "delayed")
    _quote_at(rig, "15:44", DEADLINE, 67000)
    assert rig.sells() == [], "nothing before the forced time"
    _quote_at(rig, "15:45", DEADLINE, 67000)
    (sell,) = rig.sells()
    order = sell.order
    assert (order.action, order.totalQuantity, order.lmtPrice) == ("SELL", 2, 669.98)
    assert (order.orderType, order.tif, order.outsideRth) == ("LMT", "DAY", False)
    assert rig.gw.wire_violations == []
    st = rig.book()[IID]
    assert (st["status"], st["exit_reason"]) == ("exit_working", "forced")
    assert DeskEvent.EQUITY_EXIT_FORCED.value in rig.events()
    assert "exit_pending_no_quote" not in rig.events()


def test_a_frozen_only_deadline_exit_goes_out_at_the_next_open(rig: Rig) -> None:
    rig.open_position()
    rig.gw.set_market_data_type("SPY", "frozen")
    rig.clock.to_open(NEXT_SESSION)  # the deadline session 10-23 passed
    rig.gw.push_tick("SPY", 67000, 67004)
    rig.rt.tick()
    (sell,) = rig.sells()
    assert (sell.order.totalQuantity, sell.order.lmtPrice) == (2, 669.98)
    assert rig.book()[IID]["exit_reason"] == "deadline"
    assert DeskEvent.EQUITY_EXIT_DEADLINE.value in rig.events()
    assert rig.gw.wire_violations == []


def test_delayed_quotes_trigger_neither_stop_nor_target_but_the_forced_exit_closes(
    rig: Rig,
) -> None:
    rig.open_position()
    rig.gw.set_market_data_type("SPY", "delayed")
    for _ in range(4 * equity_effect().exit_policy.stop_confirm_ticks):
        rig.tick_bid(STOP - 100)
    for _ in range(3):
        rig.tick_bid(TARGET + 100)
    assert rig.sells() == [], "a delayed quote never triggers a price rule"
    assert rig.book()[IID].get("stop_ticks", 0) == 0
    assert rig.book()[IID]["status"] == Status.OPEN.value
    _quote_at(rig, "15:45", DEADLINE, STOP - 100)
    (sell,) = rig.sells()
    assert (sell.order.totalQuantity, sell.order.lmtPrice) == (2, 663.98)
    assert rig.book()[IID]["exit_reason"] == "forced"
    assert DeskEvent.EQUITY_EXIT_STOP.value not in rig.events()


def test_a_stale_exit_steps_down_two_ticks_a_minute_to_its_cap(rig: Rig) -> None:
    rig.open_position()
    rig.gw.set_market_data_type("SPY", "delayed")
    _quote_at(rig, "15:45", DEADLINE, 67000)
    for wall in ("15:46", "15:47", "15:48", "15:49", "15:50", "15:52"):
        _quote_at(rig, wall, DEADLINE, 67000)  # the delayed bid never moves
    assert _limits(rig) == [669.98, 669.96, 669.94, 669.92, 669.90]
    assert all(s.order.totalQuantity == 2 for s in rig.sells())
    assert [s.orderStatus.status for s in rig.sells()[:-1]] == ["Cancelled"] * 4
    assert rig.book()[IID]["exit_cycles"] == 4
    assert rig.gw.wire_violations == [], "never more than held net of working sells"


def test_a_delayed_forced_exit_sells_only_the_filled_shares(rig: Rig) -> None:
    effect = equity_effect(quantity=100)
    rig.rt.register(effect)
    rig.gw.script_fills(effect.order_ref, [FillStep(37)])
    rig.send_entry(effect)
    rig.rt.tick()
    rig.gw.sleep(1)  # 37 of 100 fill
    rig.rt.tick()
    rig.paths.pause_entries().touch()
    rig.rt.tick()  # the unfilled remainder is cancelled
    rig.rt.tick()
    assert (rig.book()[IID]["status"], rig.held()) == ("open", 37)
    rig.gw.set_market_data_type("SPY", "delayed")
    _quote_at(rig, "15:45", DEADLINE, 67000)
    (sell,) = rig.sells()
    assert (sell.order.totalQuantity, sell.order.lmtPrice) == (37, 669.98)
    assert rig.gw.wire_violations == []


# ------------------------------------------------------------ standing FLATTEN
# (review P1: the flatten branch returns before decide_equity and priced its
# close from a real-time bid only, so a FLATTEN on delayed data never sold)


@pytest.mark.parametrize(
    ("kind", "wall", "day"),
    [
        ("delayed", "10:00", ENTRY_DAY),  # long before any time rule
        ("delayed", "15:50", DEADLINE),  # the forced time is due too
        ("frozen", "09:31", NEXT_SESSION),  # the deadline session passed
    ],
)
def test_a_standing_flatten_sells_two_ticks_below_a_non_realtime_bid(
    rig: Rig, kind: str, wall: str, day: date
) -> None:
    rig.open_position()
    rig.gw.set_market_data_type("SPY", kind)
    rig.paths.flatten().touch()
    _quote_at(rig, wall, day, 67000)
    (sell,) = rig.sells()
    order = sell.order
    assert (order.action, order.totalQuantity, order.lmtPrice) == ("SELL", 2, 669.98)
    assert (order.orderType, order.tif, order.outsideRth) == ("LMT", "DAY", False)
    assert rig.gw.wire_violations == []
    st = rig.book()[IID]
    assert (st["status"], st["exit_reason"]) == ("exit_working", "flatten")
    assert "exit_pending_no_quote" not in rig.events()


def test_a_flatten_on_delayed_data_sells_only_the_filled_shares(rig: Rig) -> None:
    effect = equity_effect(quantity=100)
    rig.rt.register(effect)
    rig.gw.script_fills(effect.order_ref, [FillStep(37)])
    rig.send_entry(effect)
    rig.rt.tick()
    rig.gw.sleep(1)  # 37 of 100 fill
    rig.rt.tick()
    rig.gw.set_market_data_type("SPY", "delayed")
    rig.paths.flatten().touch()
    rig.tick_bid(67000)  # the unfilled remainder is cancelled
    rig.tick_bid(67000)
    (sell,) = rig.sells()
    assert (sell.order.totalQuantity, sell.order.lmtPrice) == (37, 669.98)
    assert rig.held() == 37
    assert rig.gw.wire_violations == []


def test_a_flatten_follows_a_falling_delayed_bid_and_never_oversells(rig: Rig) -> None:
    rig.open_position()
    rig.gw.set_market_data_type("SPY", "delayed")
    rig.paths.flatten().touch()
    for bid in (67000, 67000, 66950, 66960, 66950):
        rig.tick_bid(bid)
    assert _limits(rig) == [669.98, 669.48], "repriced only when the bid falls below"
    assert [s.orderStatus.status for s in rig.sells()[:-1]] == ["Cancelled"]
    assert all(s.order.totalQuantity == 2 for s in rig.sells())
    assert rig.book()[IID]["exit_cycles"] == 1
    assert rig.gw.wire_violations == []


def test_a_flatten_without_any_quote_sends_nothing_and_alerts_once(wired: Rig) -> None:
    wired.open_position()  # no NBBO tick ever arrives
    wired.paths.flatten().touch()
    wired.clock.to_et("10:00", ENTRY_DAY)
    _tick_every(wired, 30, 20)
    alerts = [r for r in wired.records() if r["event"] == ee.EXIT_QUOTE_BLIND]
    assert [(a["reason"], a["blind_s"]) for a in alerts] == [("flatten", 300)]
    assert wired.sells() == [], "blind: no order, never a market order"


def test_no_quote_at_all_sends_no_order(rig: Rig) -> None:
    rig.open_position()  # no NBBO tick ever arrives
    rig.clock.to_et("15:45", DEADLINE)
    _tick_every(rig, 30, 14)
    assert rig.sells() == []
    assert rig.book()[IID]["status"] == Status.OPEN.value
    assert rig.events().count("exit_pending_no_quote") == 1


# ------------------------------------------------------------ exit_quote_blind


def test_no_quote_at_all_alerts_exit_quote_blind_once_after_five_minutes(wired: Rig) -> None:
    wired.open_position()
    since = wired.clock.to_et("15:45", DEADLINE)
    _tick_every(wired, 30, 20)  # 15:45:00 .. 15:54:30
    alerts = [r for r in wired.records() if r["event"] == ee.EXIT_QUOTE_BLIND]
    assert len(alerts) == 1
    (alert,) = alerts
    assert alert["ts"] == datetime.combine(DEADLINE, time(15, 50), tzinfo=ET).isoformat()
    assert {k: alert[k] for k in ("structure", "symbol", "reason", "session")} == {
        "structure": IID,
        "symbol": "SPY",
        "reason": "forced",
        "session": "2026-10-23",
    }
    assert (alert["blind_since"], alert["blind_s"]) == (since.isoformat(), 300)
    assert wired.sells() == [], "blind: no order, never a market order"
    pushes = [(t, p) for t, _m, p in wired.notified if t == f"trex-desk {ee.EXIT_QUOTE_BLIND}"]
    assert pushes == [(f"trex-desk {DeskEvent.EXIT_QUOTE_BLIND.value}", "urgent")]


def test_exit_quote_blind_is_wired_into_the_runtime_itself(tmp_path: Path) -> None:
    """Wave 3/4 integration: the plain runtime carries the watch (no subclass)."""
    from tree_options.trex.desk_runtime import DeskRuntime

    rig = Rig(tmp_path)
    try:
        assert type(rig.rt) is DeskRuntime
        assert isinstance(rig.rt.exit_quotes, ee.ExitQuoteWatch)
    finally:
        rig.rt.release()


def test_a_quote_inside_five_minutes_sends_the_exit_and_never_alerts(wired: Rig) -> None:
    wired.open_position()
    wired.clock.to_et("15:45", DEADLINE)
    _tick_every(wired, 30, 8)  # 15:45:00 .. 15:48:30, unpriced
    wired.gw.set_market_data_type("SPY", "delayed")
    _quote_at(wired, "15:49", DEADLINE, 67000)  # 240 s blind: under the 300
    (sell,) = wired.sells()
    assert sell.order.lmtPrice == 669.98
    _tick_every(wired, 30, 14)
    assert ee.EXIT_QUOTE_BLIND not in wired.events()
