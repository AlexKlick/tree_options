"""Monitor choreography tests against a fake broker (no gateway needed).

These pin the behaviors that would cost real money if wrong: touch fires a
sell, fills drain to CLOSED, the FLATTEN kill exits everything, entry
states are left to the entry runner, and a repriced exit waits for its
cancel before replacing (never two sells on one position).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex.clock import EntryWindow
from tree_options.trex.engine import ComboQuote, EngineConfig, configure
from tree_options.trex.monitor import Monitor, compute_marks
from tree_options.trex.plan import PutSpread, TradePlan
from tree_options.trex.state import BookState, Status

ET = ZoneInfo("America/New_York")


def _at(hh: int, mm: int, day: date = date(2026, 9, 18)) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET)


@pytest.fixture(autouse=True)
def _engine() -> None:
    configure(
        EngineConfig(
            entry_window=EntryWindow(time(9, 45), time(12, 0)),
            entry_max_cycles=4,
            exit_max_mid_cycles=3,
        )
    )


def _plan() -> TradePlan:
    spread = PutSpread(
        id="nvda-oct",
        underlying="NVDA",
        entry_date=date(2026, 9, 18),
        expiry=date(2026, 10, 16),
        long_strike="185",
        short_strike="150",
        quantity=5,
        limit_cap="0.50",
        exit_deadline=date(2026, 10, 9),
    )
    return TradePlan(
        id="t",
        account_mode="paper",
        structures=[spread],
        total_debit_cap="1000",
        entry_window_start="09:45",
        entry_window_end="12:00",
    )


@dataclass
class FakeStatus:
    status: str = "Submitted"
    filled: int = 0
    avgFillPrice: float = 0.0


@dataclass
class FakeOrder:
    orderId: int
    action: str = "SELL"
    totalQuantity: int = 0
    lmtPrice: float = 0.0


@dataclass
class FakeTrade:
    contract: Any
    order: FakeOrder
    orderStatus: FakeStatus = field(default_factory=FakeStatus)


@dataclass
class FakeLeg:
    conId: int


@dataclass
class FakeBag:
    secType: str = "BAG"
    comboLegs: list[FakeLeg] = field(default_factory=list)


class FakeIbkr:
    """Duck-typed IbkrTrex: records orders, flips statuses on demand."""

    def __init__(self, spot: str = "200.00", quote: ComboQuote | None = None) -> None:
        self.spot_price = Decimal(spot)
        self.quote = quote or ComboQuote(Decimal("0.40"), Decimal("0.48"))
        self.placed: list[tuple[str, str, int, Decimal]] = []
        self.cancelled: list[int] = []
        self.open_trades: list[Any] = []
        self._next_oid = 100
        self.account: Any = None  # AccountSnapshot | None (C9)
        self.account_raises = False

    def account_snapshot(self) -> Any:
        if self.account_raises:
            raise RuntimeError("gateway hiccup")
        return self.account

    def snapshot(self, spreads: list[PutSpread], ts: datetime) -> Any:
        from tree_options.trex.engine import Snapshot

        return Snapshot(
            ts=ts,
            spots={s.underlying: self.spot_price for s in spreads},
            quotes={s.id: self.quote for s in spreads},
        )

    def place_combo(self, spread: PutSpread, side: str, qty: int, limit: Decimal) -> Any:
        from tree_options.trex.ibkr import OrderRef

        self._next_oid += 1
        trade = FakeTrade(
            contract=FakeBag(comboLegs=[FakeLeg(1), FakeLeg(2)]),
            order=FakeOrder(
                orderId=self._next_oid,
                action=side,
                totalQuantity=qty,
                lmtPrice=float(limit),
            ),
        )
        self.open_trades.append(trade)
        self.placed.append((spread.id, side, qty, limit))
        return OrderRef(spread.id, side, qty, limit, trade)

    def order_status(self, ref: Any) -> Any:
        os = ref.trade.orderStatus
        avg = Decimal(str(os.avgFillPrice)) if os.filled else Decimal(0)
        from tree_options.trex.ibkr import OrderStatusInfo

        return OrderStatusInfo(
            status=os.status, filled=os.filled, avg_fill_price=avg
        )

    def cancel(self, ref: Any) -> None:
        self.cancelled.append(ref.trade.order.orderId)
        # fakes confirm instantly; tests can override after calling
        ref.trade.orderStatus.status = "Cancelled"

    def open_combo_trades(self) -> list[Any]:
        return [t for t in self.open_trades if t.orderStatus.status == "Submitted"]

    def structure_for_bag(self, contract: Any) -> str | None:
        return "nvda-oct"

    def sleep(self, seconds: float) -> None:  # tests: no waiting
        return None

    def fill(self, ref: Any, qty: int, price: str) -> None:
        ref.trade.orderStatus.status = "Filled"
        ref.trade.orderStatus.filled = qty
        ref.trade.orderStatus.avgFillPrice = float(price)


def _monitor(tmp_path: Path, fake: FakeIbkr, clock_dt: datetime) -> Monitor:
    plan = _plan()
    book = BookState(["nvda-oct"])
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    return Monitor(plan, fake, book, run_dir, clock=lambda: clock_dt)


class TestTouchExit:
    def test_touch_places_single_sell_at_mid(self, tmp_path: Path) -> None:
        fake = FakeIbkr(spot="184.50")  # below the 185 long strike
        mon = _monitor(tmp_path, fake, _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5

        mon._tick()

        assert st.status is Status.EXIT_WORKING
        assert fake.placed == [("nvda-oct", "SELL", 5, Decimal("0.44"))]

    def test_hold_above_strike_places_nothing(self, tmp_path: Path) -> None:
        fake = FakeIbkr(spot="200.00")
        mon = _monitor(tmp_path, fake, _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5

        mon._tick()

        assert st.status is Status.OPEN
        assert fake.placed == []

    def test_monitor_leaves_planned_structures_alone(self, tmp_path: Path) -> None:
        fake = FakeIbkr(spot="100.00")  # would touch if it were open
        mon = _monitor(tmp_path, fake, _at(13, 0))
        mon._tick()
        assert fake.placed == []
        assert mon.book.structures["nvda-oct"].status is Status.PLANNED


class TestFillDrain:
    def test_exit_fill_closes_the_structure(self, tmp_path: Path) -> None:
        fake = FakeIbkr(spot="184.50")
        mon = _monitor(tmp_path, fake, _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5
        mon._tick()
        ref = mon.orders["nvda-oct"]

        fake.fill(ref, 5, "2.10")
        mon._tick()

        assert st.status is Status.CLOSED
        assert st.exit_filled_qty == 5
        assert st.exit_fill == Decimal("2.10")
        assert st.close_reason == "touch"

    def test_partial_exit_fill_keeps_working_the_remainder(
        self, tmp_path: Path
    ) -> None:
        fake = FakeIbkr(spot="184.50")
        mon = _monitor(tmp_path, fake, _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5
        mon._tick()
        ref = mon.orders["nvda-oct"]

        fake.fill(ref, 2, "2.10")  # partial: 3 spreads still held
        mon._tick()

        assert st.status is Status.EXIT_WORKING
        assert st.open_qty == 3
        # remainder order placed for exactly what is left
        assert fake.placed[-1] == ("nvda-oct", "SELL", 3, Decimal("0.44"))


class TestKillFiles:
    def test_flatten_exits_open_positions(self, tmp_path: Path) -> None:
        fake = FakeIbkr(spot="300.00")  # no touch; kill file must force it
        mon = _monitor(tmp_path, fake, _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5
        (mon.run_dir / "FLATTEN").touch()

        mon._tick()

        assert st.status is Status.EXIT_WORKING
        assert fake.placed and fake.placed[0][1] == "SELL"

    def test_halt_blocks_new_exit_orders(self, tmp_path: Path) -> None:
        fake = FakeIbkr(spot="184.50")
        mon = _monitor(tmp_path, fake, _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5
        (mon.run_dir / "HALT").touch()

        mon._tick()

        assert st.status is Status.EXIT_WORKING
        assert fake.placed == []  # begun but not placed: HALT holds orders


class TestExitReprice:
    def test_reprice_waits_for_cancel_before_replacing(self, tmp_path: Path) -> None:
        fake = FakeIbkr(spot="184.50")
        mon = _monitor(tmp_path, fake, _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5
        mon._tick()
        assert fake.placed[-1][3] == Decimal("0.44")

        # fresh quote moves the mid; the fake's cancel does NOT confirm
        fake.quote = ComboQuote(Decimal("0.30"), Decimal("0.34"))
        ref = mon.orders["nvda-oct"]
        def slow_cancel(r: Any) -> None:
            fake.cancelled.append(r.trade.order.orderId)
            # leave status Submitted: cancel not confirmed

        fake.cancel = slow_cancel  # type: ignore[method-assign]
        mon._tick()

        # old order still working, no replacement placed
        assert fake.cancelled
        assert len(fake.placed) == 1
        assert ref.trade.orderStatus.status == "Submitted"

        # now confirm the cancel; next tick replaces at the new mid
        ref.trade.orderStatus.status = "Cancelled"
        fake.quote = ComboQuote(Decimal("0.30"), Decimal("0.34"))
        mon._tick()
        assert fake.placed[-1] == ("nvda-oct", "SELL", 5, Decimal("0.32"))


class TestCumulativeExitAccounting:
    """D2 regression: order-local fill counts restart on every replacement.

    The old drain compared an order-local count against the book's
    cumulative exit_filled_qty — a replacement order that filled the rest
    recorded a phantom open qty and the refresh path then re-sold
    contracts no longer held (naked short). These tests pin cumulative
    quantity + blended average price across replacements.
    """

    def test_replacement_fill_blends_into_cumulative_and_closes(
        self, tmp_path: Path
    ) -> None:
        fake = FakeIbkr(spot="184.50")
        mon = _monitor(tmp_path, fake, _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5
        mon._tick()
        a = mon.orders["nvda-oct"]

        fake.fill(a, 2, "2.10")  # partial: 3 still held
        mon._tick()  # drain 2 + replace for the remainder
        b = mon.orders["nvda-oct"]
        assert b is not a
        assert fake.placed[-1] == ("nvda-oct", "SELL", 3, Decimal("0.44"))

        fake.fill(b, 3, "2.20")  # the remainder fills
        mon._tick()

        assert st.exit_filled_qty == 5
        assert st.exit_fill == (Decimal("2.10") * 2 + Decimal("2.20") * 3) / 5
        assert st.open_qty == 0
        assert st.status is Status.CLOSED
        assert len(fake.placed) == 2  # NO third sell on phantom qty

        fills = [
            json.loads(line)
            for line in (mon.run_dir / "events.jsonl").read_text().splitlines()
            if "exit_fill" in line
        ]
        assert [e["filled"] for e in fills] == [2, 5]  # cumulative
        assert [e["order_filled"] for e in fills] == [2, 3]  # order-local
        assert fills[-1]["avg"] == str(st.exit_fill)

    def test_replacement_smaller_local_count_still_completes(
        self, tmp_path: Path
    ) -> None:
        fake = FakeIbkr(spot="184.50")
        mon = _monitor(tmp_path, fake, _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5
        mon._tick()
        ref = mon.orders["nvda-oct"]

        fake.fill(ref, 4, "2.00")
        mon._tick()
        # the replacement's order-local count (1) is smaller than the
        # previous order's local count (4) — cumulative must still reach 5
        b = mon.orders["nvda-oct"]
        fake.fill(b, 1, "2.50")
        mon._tick()

        assert st.exit_filled_qty == 5
        assert st.exit_fill == (Decimal("2.00") * 4 + Decimal("2.50") * 1) / 5
        assert st.open_qty == 0


class TestAccountWrite:
    """C9: account.json rides the monitor loop, failure-isolated."""

    def _snapshot(self) -> Any:
        from tree_options.trex.account import AccountSnapshot

        return AccountSnapshot(
            account_id="DUT143714",
            net_liquidation=Decimal("1000252.09"),
            cash=Decimal("999516.91"),
            buying_power=Decimal("3998067.63"),
            currency="USD",
            ts=datetime.now(ET),
        )

    def test_writes_account_json_into_the_run_dir(self, tmp_path: Path) -> None:
        fake = FakeIbkr(spot="200.00")
        fake.account = self._snapshot()
        mon = _monitor(tmp_path, fake, _at(13, 0))

        mon._account_cycle()

        payload = json.loads((mon.run_dir / "account.json").read_text())
        assert payload["account_id"] == "DUT143714"
        assert payload["net_liquidation"] == "1000252.09"

    def test_every_third_cycle(self, tmp_path: Path) -> None:
        fake = FakeIbkr(spot="200.00")
        fake.account = self._snapshot()
        mon = _monitor(tmp_path, fake, _at(13, 0))
        calls = {"n": 0}
        original = fake.account_snapshot

        def counting() -> Any:
            calls["n"] += 1
            return original()

        fake.account_snapshot = counting  # type: ignore[method-assign]
        for _ in range(4):
            mon._account_cycle()
        assert calls["n"] == 2  # cycles 1 and 4

    def test_broker_failure_never_breaks_the_loop(self, tmp_path: Path) -> None:
        fake = FakeIbkr(spot="200.00")
        fake.account = self._snapshot()
        fake.account_raises = True
        mon = _monitor(tmp_path, fake, _at(13, 0))

        mon._account_cycle()  # must not raise

        assert not (mon.run_dir / "account.json").exists()

    def test_no_account_data_writes_nothing(self, tmp_path: Path) -> None:
        fake = FakeIbkr(spot="200.00")  # account stays None (tags absent)
        mon = _monitor(tmp_path, fake, _at(13, 0))
        mon._account_cycle()
        assert not (mon.run_dir / "account.json").exists()


class TestAdoption:
    def test_restart_adopts_working_sell(self, tmp_path: Path) -> None:
        fake = FakeIbkr(spot="184.50")
        mon = _monitor(tmp_path, fake, _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5
        st.to(Status.EXIT_WORKING, _at(12, 0))
        st.exit_reason = "touch"

        # a SELL already working at the broker from a previous process
        orphan = fake.place_combo(
            mon.plan.structures[0], "SELL", 5, Decimal("0.44")
        )
        mon.adopt_open_exits()

        assert mon.orders["nvda-oct"].trade is orphan.trade
        # drain path works on the adopted ref
        fake.fill(orphan, 5, "2.00")
        mon._tick()
        assert st.status is Status.CLOSED


class TestComputeMarks:
    """Observation-only mark-to-mid payload for the status panel."""

    def _spread(self, sid: str = "nvda-oct") -> PutSpread:
        return PutSpread(
            id=sid,
            underlying="NVDA",
            entry_date=date(2026, 9, 18),
            expiry=date(2026, 10, 16),
            long_strike="185",
            short_strike="150",
            quantity=5,
            limit_cap="0.50",
            exit_deadline=date(2026, 10, 9),
        )

    def test_mark_to_mid_unrealized_and_total(self) -> None:
        book = BookState(["nvda-oct", "qqq-nov"])
        st = book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(9, 50))
        st.to(Status.OPEN, _at(10, 0))
        st.filled_qty = 2
        st.entry_fill = Decimal("1.00")
        quotes = {
            "nvda-oct": ComboQuote(bid=Decimal("1.10"), ask=Decimal("1.30")),
            "qqq-nov": None,
        }
        marks = compute_marks([self._spread()], book, quotes)
        row = marks["structures"]["nvda-oct"]
        assert row["mark"] == "1.20"
        assert row["unrealized"] == "40.00"
        assert marks["total_unrealized"] == "40.00"
        # unfilled structures are omitted entirely
        assert "qqq-nov" not in marks["structures"]

    def test_no_quote_leaves_mark_none(self) -> None:
        book = BookState(["nvda-oct"])
        st = book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(9, 50))
        st.to(Status.OPEN, _at(10, 0))
        st.filled_qty = 5
        st.entry_fill = Decimal("0.21")
        marks = compute_marks([self._spread()], book, {"nvda-oct": None})
        assert marks["structures"]["nvda-oct"]["mark"] is None
        assert marks["total_unrealized"] == "0.00"


class TestMarksHistoryLog:
    """M1: per-structure marks history as append-only JSONL.

    The append has its OWN failure boundary and sits at the END of the
    tick, after exit decisions - a history failure costs one sample, never
    an exit decision (the Codex-3 pin).
    """

    def _open_monitor(self, tmp_path: Path, fake: FakeIbkr | None = None) -> Monitor:
        fake = fake or FakeIbkr()
        mon = _monitor(tmp_path, fake, _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5
        st.entry_fill = Decimal("0.21")
        return mon

    def _history_path(self, tmp_path: Path) -> Path:
        return tmp_path / "run" / "marks_history.jsonl"

    def test_one_line_per_tick_with_contemporaneous_fields(self, tmp_path: Path) -> None:
        mon = self._open_monitor(tmp_path)
        mon._tick()
        path = self._history_path(tmp_path)
        assert path.exists()
        (row,) = [json.loads(line) for line in path.read_text().splitlines()]
        assert row["total_unrealized"] == "115.00"  # (0.44-0.21)*5*100
        st = row["structures"]["nvda-oct"]
        assert st["mark"] == "0.44" and st["open_qty"] == 5
        assert st["entry"] == "0.21" and st["realized_to_date"] == "0.00"
        assert row["quote_coverage"] == {"open": 1, "quoted": 1}

    def test_no_history_when_tick_early_returns(self, tmp_path: Path) -> None:
        mon = self._open_monitor(tmp_path)
        mon._clock = lambda: _at(3, 0)  # far outside the session
        mon._tick()
        assert not self._history_path(tmp_path).exists()

    def test_history_failure_never_blocks_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeIbkr(spot="184.50")  # below long strike -> touch exit fires
        mon = self._open_monitor(tmp_path, fake)
        import tree_options.trex.history as history_mod

        def boom(path: object, obj: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(history_mod, "append_line", boom)
        mon._tick()
        st = mon.book.structures["nvda-oct"]
        assert st.status is Status.EXIT_WORKING  # the exit STILL ran
        assert fake.placed  # the SELL order was placed

    def test_restart_resumes_without_truncation(self, tmp_path: Path) -> None:
        mon = self._open_monitor(tmp_path)
        mon._tick()
        mon2 = self._open_monitor(tmp_path)  # fresh instance, same run dir
        mon2._tick()
        lines = self._history_path(tmp_path).read_text().splitlines()
        assert len(lines) == 2

    def test_torn_tail_repaired_on_first_append(self, tmp_path: Path) -> None:
        path = self._history_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"ts": "partial", "total_unrealized": ')  # killed
        mon = self._open_monitor(tmp_path)
        mon._tick()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(rows) == 1 and "total_unrealized" in rows[0]

    def test_rotation_at_cap_keeps_newest_half(self, tmp_path: Path) -> None:
        import tree_options.trex.monitor as monitor_mod

        mon = self._open_monitor(tmp_path)
        cap = 6
        original = monitor_mod.MAX_MARKS_HISTORY_LINES
        monitor_mod.MAX_MARKS_HISTORY_LINES = cap
        try:
            for _ in range(cap + 2):
                mon._tick()
        finally:
            monitor_mod.MAX_MARKS_HISTORY_LINES = original
        # 7th append trips the cap -> halved to 3; the 8th appends to 4.
        lines = self._history_path(tmp_path).read_text().splitlines()
        assert len(lines) == cap // 2 + 1


class TestNotionalBlending:
    """Codex-M2 #2: an order's cumulative average times only its NEW
    fills re-prices old fills; blends must merge NOTIONAL increments."""

    def _open_with_exit(self, tmp_path: Path, fake: FakeIbkr) -> tuple[Monitor, Any, Any]:
        mon = _monitor(tmp_path, fake, _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5
        st.entry_fill = Decimal("0.21")
        mon._tick()  # touch exit fires (spot 184.50)
        return mon, st, mon.orders["nvda-oct"]

    def test_exit_blending_merges_notionals_across_prices(self, tmp_path: Path) -> None:
        fake = FakeIbkr(spot="184.50")
        mon, st, ref = self._open_with_exit(tmp_path, fake)
        fake.fill(ref, 2, "0.35")
        ref.trade.orderStatus.status = "Submitted"  # still working
        mon._tick()  # drain 1: 2 @ 0.35
        # same order fills 1 more at 0.50 -> order cumulative avg 0.40
        ref.trade.orderStatus.filled = 3
        ref.trade.orderStatus.avgFillPrice = 0.40
        mon._tick()  # drain 2
        # correct blend: (2*0.35 + 1*0.50) / 3 = 0.40 (NOT 0.3667)
        assert st.exit_fill == Decimal("0.40")
        assert st.exit_filled_qty == 3


class TestAdoptionCheckpoint:
    """Codex-M2 #8: adoption must not re-add fills the book already
    recorded, but MUST absorb fills that happened during downtime."""

    def _working_exit(self, tmp_path: Path) -> tuple[Path, FakeIbkr]:
        fake = FakeIbkr(spot="184.50")
        mon = _monitor(tmp_path, fake, _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5
        st.entry_fill = Decimal("0.21")
        mon._tick()
        ref = mon.orders["nvda-oct"]
        fake.fill(ref, 2, "0.35")
        ref.trade.orderStatus.status = "Submitted"  # still working
        mon._tick()  # records 2 fills; order keeps working
        return tmp_path, fake

    def test_restart_does_not_recount_recorded_fills(self, tmp_path: Path) -> None:
        run, fake = self._working_exit(tmp_path)
        book2 = BookState.load(run / "run" / "book.json", ["nvda-oct"])
        mon2 = Monitor(_plan(), fake, book2, run / "run", clock=lambda: _at(13, 30))
        mon2.adopt_open_exits()
        mon2._tick()  # drain: nothing new at the broker
        st = mon2.book.structures["nvda-oct"]
        assert st.exit_filled_qty == 2  # NOT 4

    def test_downtime_fills_are_absorbed(self, tmp_path: Path) -> None:
        run, fake = self._working_exit(tmp_path)
        trade = fake.open_trades[0]
        # one more contract filled while the monitor was down: 3 total,
        # blended order avg (2*0.35 + 1*0.50)/3 = 0.40
        trade.orderStatus.filled = 3
        trade.orderStatus.avgFillPrice = 0.40
        book2 = BookState.load(run / "run" / "book.json", ["nvda-oct"])
        mon2 = Monitor(_plan(), fake, book2, run / "run", clock=lambda: _at(13, 30))
        mon2.adopt_open_exits()
        mon2._tick()
        st = mon2.book.structures["nvda-oct"]
        assert st.exit_filled_qty == 3
        assert st.exit_fill == Decimal("0.40")


class TestMarksHistoryBasis:
    """Codex-M2 #3: history unrealized must be OPEN-basis (remaining
    contracts), not the filled-basis marks.json total."""

    def test_open_basis_unrealized_and_total(self, tmp_path: Path) -> None:
        mon = _monitor(tmp_path, FakeIbkr(), _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5
        st.entry_fill = Decimal("0.21")
        st.exit_filled_qty = 2  # 3 remain open
        mon._tick()
        path = tmp_path / "run" / "marks_history.jsonl"
        (row,) = [json.loads(line) for line in path.read_text().splitlines()]
        # (0.44 - 0.21) * 3 * 100 = 69.00 (NOT the filled-basis 115.00)
        assert row["total_unrealized_open"] == "69.00"
        assert row["structures"]["nvda-oct"]["unrealized"] == "69.00"
        assert row["structures"]["nvda-oct"]["open_qty"] == 3

    def test_early_return_drain_writes_terminal_line(self, tmp_path: Path) -> None:
        fake = FakeIbkr(spot="184.50")
        mon = _monitor(tmp_path, fake, _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5
        st.entry_fill = Decimal("0.21")
        mon._tick()  # exit working
        fake.fill(mon.orders["nvda-oct"], 5, "2.10")
        # the fill lands outside session hours: early-return drain path
        mon._clock = lambda: _at(3, 0)
        mon._tick()
        st2 = mon.book.structures["nvda-oct"]
        assert st2.status is Status.CLOSED
        path = tmp_path / "run" / "marks_history.jsonl"
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        terminal = lines[-1]
        assert terminal["quote_coverage"]["quoted"] == 0
        assert terminal["structures"]["nvda-oct"]["open_qty"] == 0
        assert terminal["structures"]["nvda-oct"]["realized_to_date"] == "945.00"


class TestHistoryFailureResetsRepair:
    """Codex-M2 #11: a failed append must reset the repair/count caches or
    the next append concatenates onto the torn fragment."""

    def test_flags_reset_after_failure(self, tmp_path: Path) -> None:
        import tree_options.trex.history as history_mod
        import tree_options.trex.monitor as monitor_mod

        mon = _monitor(tmp_path, FakeIbkr(), _at(13, 0))
        st = mon.book.structures["nvda-oct"]
        st.to(Status.ENTER_WORKING, _at(10, 5))
        st.to(Status.OPEN, _at(10, 5))
        st.filled_qty = 5
        st.entry_fill = Decimal("0.21")
        mon._tick()  # one good line
        path = tmp_path / "run" / "marks_history.jsonl"
        first_line = path.read_text().splitlines()[0]
        original = history_mod.append_line
        state = {"failed": False}

        def torn_then_raise(p: object, obj: object) -> None:
            if not state["failed"]:
                state["failed"] = True
                with open(p, "a", encoding="utf-8") as fh:  # type: ignore[arg-type]
                    fh.write('{"ts": "tor')
                raise OSError("disk hiccup")
            original(p, obj)  # type: ignore[arg-type]

        monitor_mod.history.append_line = torn_then_raise
        try:
            mon._tick()  # torn write + raise (isolated by the cycle's guard)
        finally:
            monitor_mod.history.append_line = original
        mon._tick()  # must repair the torn tail and append cleanly
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        parsed = [json.loads(ln) for ln in lines]
        assert len(parsed) == 2  # nothing lost to concatenation
        assert lines[0] == first_line
