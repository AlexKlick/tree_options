"""Entry-runner choreography tests against a fake broker.

C1 regression lane: a reprice after a PARTIAL fill must buy only the
remainder (the old _place bought the full plan quantity again) and fill
accounting must accumulate cumulatively with a blended average price —
never compare an order-local count against the book's cumulative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex.clock import EntryWindow
from tree_options.trex.engine import ComboQuote, EngineConfig, configure
from tree_options.trex.enter import Enterer
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
    action: str = "BUY"
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


class FakeEntryIbkr:
    def __init__(self, quote: ComboQuote | None = None) -> None:
        self.quote = quote or ComboQuote(Decimal("0.40"), Decimal("0.48"))
        self.placed: list[tuple[str, str, int, Decimal]] = []
        self.cancelled: list[int] = []
        self.confirm_cancels = True
        self.connected = True
        self._next_oid = 200
        self.open_trades: list[Any] = []

    def snapshot(self, spreads: list[PutSpread], ts: datetime) -> Any:
        from tree_options.trex.engine import Snapshot

        return Snapshot(
            ts=ts,
            spots={s.underlying: Decimal("200.00") for s in spreads},
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
        from tree_options.trex.ibkr import OrderStatusInfo

        os = ref.trade.orderStatus
        avg = Decimal(str(os.avgFillPrice)) if os.filled else Decimal(0)
        return OrderStatusInfo(status=os.status, filled=os.filled, avg_fill_price=avg)

    def cancel(self, ref: Any) -> None:
        self.cancelled.append(ref.trade.order.orderId)
        # fakes confirm instantly (ib_async cancel is async; runner waits)
        if self.confirm_cancels:
            ref.trade.orderStatus.status = "Cancelled"

    def open_combo_trades(self) -> list[Any]:
        return [t for t in self.open_trades if t.orderStatus.status == "Submitted"]

    def structure_for_bag(self, contract: Any) -> str | None:
        return "nvda-oct"

    def sleep(self, seconds: float) -> None:
        return None

    def fill(self, ref: Any, qty: int, price: str) -> None:
        """Fully fill (terminal Filled status)."""
        ref.trade.orderStatus.status = "Filled"
        ref.trade.orderStatus.filled = qty
        ref.trade.orderStatus.avgFillPrice = float(price)

    def fill_partial(self, ref: Any, qty: int, price: str) -> None:
        """Fill qty but keep the order working (reprice path)."""
        ref.trade.orderStatus.filled = qty
        ref.trade.orderStatus.avgFillPrice = float(price)


def _enterer(tmp_path: Path, fake: FakeEntryIbkr, clock_dt: datetime) -> Enterer:
    plan = _plan()
    book = BookState(["nvda-oct"])
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return Enterer(plan, fake, book, run_dir, clock=lambda: clock_dt)


class TestCumulativeEntryAccounting:
    def test_reprice_after_partial_fill_buys_only_the_remainder(
        self, tmp_path: Path
    ) -> None:
        fake = FakeEntryIbkr()
        ent = _enterer(tmp_path, fake, _at(10, 0))
        ent._tick()  # first placement: BUY 5
        assert fake.placed == [("nvda-oct", "BUY", 5, Decimal("0.44"))]
        a = ent.orders["nvda-oct"]

        fake.fill_partial(a, 2, "0.44")  # 2 filled, still working
        ent._tick()  # drain merges the 2
        st = ent.book.structures["nvda-oct"]
        assert st.filled_qty == 2

        # a reprice of a partially-filled working order must buy only the
        # remaining 3 (the old code re-placed the FULL plan quantity)
        ent._reprice(ent.plan.structures[0], Decimal("0.32"))

        assert fake.placed[-1] == ("nvda-oct", "BUY", 3, Decimal("0.32"))

    def test_replacement_fill_completes_cumulative_with_blended_avg(
        self, tmp_path: Path
    ) -> None:
        fake = FakeEntryIbkr()
        ent = _enterer(tmp_path, fake, _at(10, 0))
        ent._tick()
        a = ent.orders["nvda-oct"]
        fake.fill_partial(a, 2, "0.44")
        ent._tick()
        ent._reprice(ent.plan.structures[0], Decimal("0.32"))  # replace for 3
        b = ent.orders["nvda-oct"]
        assert b is not a

        fake.fill(b, 3, "0.32")  # the remainder fills -> fully in the book
        ent._tick()

        st = ent.book.structures["nvda-oct"]
        assert st.filled_qty == 5
        assert st.entry_fill == (Decimal("0.44") * 2 + Decimal("0.32") * 3) / 5
        assert st.status is Status.OPEN
        assert "nvda-oct" not in ent.orders  # entry lane done; monitor's now

    def test_full_fill_on_first_order_still_opens(self, tmp_path: Path) -> None:
        fake = FakeEntryIbkr()
        ent = _enterer(tmp_path, fake, _at(10, 0))
        ent._tick()
        ref = ent.orders["nvda-oct"]

        fake.fill(ref, 5, "0.44")
        ent._tick()

        st = ent.book.structures["nvda-oct"]
        assert st.filled_qty == 5
        assert st.entry_fill == Decimal("0.44")
        assert st.status is Status.OPEN


class TestKillFiles:
    """FLATTEN cancels unfilled entries, and only enter.py can: IBKR lets
    only the placing clientId cancel an order (error 10147), and the
    monitor (clientId 71) never even sees enter.py's (72) BUYs."""

    def _working(self, tmp_path: Path) -> tuple[Enterer, FakeEntryIbkr]:
        fake = FakeEntryIbkr()
        ent = _enterer(tmp_path, fake, _at(10, 0))
        ent._tick()  # BUY 5 working
        assert ent.book.structures["nvda-oct"].status is Status.ENTER_WORKING
        return ent, fake

    def test_flatten_cancels_the_working_entry_and_closes(self, tmp_path: Path) -> None:
        ent, fake = self._working(tmp_path)
        (ent.run_dir / "FLATTEN").touch()
        ent._tick()
        st = ent.book.structures["nvda-oct"]
        assert fake.cancelled == [201] and len(fake.placed) == 1
        assert st.status is Status.CLOSED and st.close_reason == "kill: FLATTEN"
        assert not ent._entry_pending()
        saved = BookState.load(ent.run_dir / "book.json", ["nvda-oct"])
        assert saved.structures["nvda-oct"].status is Status.CLOSED

    def test_flatten_after_a_partial_fill_hands_the_rest_to_the_monitor(
        self, tmp_path: Path
    ) -> None:
        ent, fake = self._working(tmp_path)
        a = ent.orders["nvda-oct"]
        fake.fill_partial(a, 2, "0.44")
        ent._tick()  # drain records 2
        ent._reprice(ent.plan.structures[0], Decimal("0.32"))  # order B for 3
        b = ent.orders["nvda-oct"]
        fake.fill_partial(b, 1, "0.32")  # B fills 1 more before the kill
        (ent.run_dir / "FLATTEN").touch()
        ent._tick()
        st = ent.book.structures["nvda-oct"]
        assert fake.cancelled[-1] == b.trade.order.orderId
        assert st.status is Status.OPEN and st.filled_qty == 3  # cumulative, not order-local
        assert st.entry_fill == (Decimal("0.44") * 2 + Decimal("0.32")) / 3

    def test_an_unconfirmed_cancel_keeps_the_entry_working(self, tmp_path: Path) -> None:
        ent, fake = self._working(tmp_path)
        fake.confirm_cancels = False
        (ent.run_dir / "FLATTEN").touch()
        ent._tick()
        st = ent.book.structures["nvda-oct"]
        assert st.status is Status.ENTER_WORKING  # a live BUY is never closed on paper
        assert "nvda-oct" in ent.orders
        ent.orders["nvda-oct"].trade.orderStatus.status = "Cancelled"  # confirms late
        ent._tick()
        assert st.status is Status.CLOSED and len(fake.placed) == 1

    def test_flatten_enters_nothing_new(self, tmp_path: Path) -> None:
        fake = FakeEntryIbkr()
        ent = _enterer(tmp_path, fake, _at(10, 0))
        (ent.run_dir / "FLATTEN").touch()
        ent._tick()
        st = ent.book.structures["nvda-oct"]
        assert fake.placed == []
        assert st.status is Status.CLOSED and st.close_reason == "kill: FLATTEN"

    def test_a_monitor_exit_write_survives_the_entry_runner_save(self, tmp_path: Path) -> None:
        """The reverse race: enter.py's whole-book save must not revert a
        structure the monitor already moved to EXIT_WORKING back to OPEN
        (the monitor would then place a SECOND sell: naked short)."""
        fake = FakeEntryIbkr()
        ent = _enterer(tmp_path, fake, _at(10, 0))
        ent._tick()
        fake.fill(ent.orders["nvda-oct"], 5, "0.44")
        ent._tick()  # OPEN, saved
        disk = BookState.load(ent.run_dir / "book.json", ["nvda-oct"])
        mon_st = disk.structures["nvda-oct"]
        mon_st.to(Status.EXIT_WORKING, _at(10, 1))  # the monitor's touch exit
        mon_st.exit_reason, mon_st.exit_order = "touch", "999"
        disk.save(ent.run_dir / "book.json")
        (ent.run_dir / "FLATTEN").touch()
        ent._tick()  # saves its (stale) whole book
        after = BookState.load(ent.run_dir / "book.json", ["nvda-oct"]).structures["nvda-oct"]
        assert after.status is Status.EXIT_WORKING and after.exit_order == "999"

    def test_halt_places_no_new_entry(self, tmp_path: Path) -> None:
        fake = FakeEntryIbkr()
        ent = _enterer(tmp_path, fake, _at(10, 0))
        (ent.run_dir / "HALT").touch()
        ent._tick()
        assert fake.placed == []
        assert ent.book.structures["nvda-oct"].status is Status.PLANNED


class TestFlattenAcrossClients:
    """Monitor and entry runner share book.json but NOT their orders: each
    fake broker session sees only its own trades, like IBKR clientIds."""

    def _pair(self, tmp_path: Path) -> tuple[Any, Enterer, FakeEntryIbkr, FakeEntryIbkr]:
        from tree_options.trex.monitor import Monitor

        enter_ib, monitor_ib = FakeEntryIbkr(), FakeEntryIbkr()
        ent = _enterer(tmp_path, enter_ib, _at(10, 0))
        mon = Monitor(_plan(), monitor_ib, BookState(["nvda-oct"]), ent.run_dir,
                      clock=lambda: _at(10, 1))
        ent._tick()  # enter.py's BUY is working at the broker
        return mon, ent, enter_ib, monitor_ib

    def _cycle(self, mon: Any, ent: Enterer) -> None:
        mon._sync_book_from_disk()
        mon._tick()
        ent._sync_book_from_disk()
        ent._tick()
        mon._sync_book_from_disk()
        mon._tick()

    def test_the_working_buy_is_cancelled_by_its_owner(self, tmp_path: Path) -> None:
        mon, ent, enter_ib, monitor_ib = self._pair(tmp_path)
        (ent.run_dir / "FLATTEN").touch()
        self._cycle(mon, ent)
        buy = enter_ib.open_trades[0]
        assert buy.orderStatus.status == "Cancelled"  # no live BUY left behind
        assert monitor_ib.cancelled == []
        assert mon.book.structures["nvda-oct"].status is Status.CLOSED

    def test_a_partial_fill_is_flattened_by_the_monitor(self, tmp_path: Path) -> None:
        mon, ent, enter_ib, monitor_ib = self._pair(tmp_path)
        enter_ib.fill_partial(ent.orders["nvda-oct"], 2, "0.44")
        (ent.run_dir / "FLATTEN").touch()
        self._cycle(mon, ent)
        assert enter_ib.open_trades[0].orderStatus.status == "Cancelled"
        assert monitor_ib.placed == [("nvda-oct", "SELL", 2, Decimal("0.40"))]
        assert mon.book.structures["nvda-oct"].status is Status.EXIT_WORKING
