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
        # fakes confirm instantly (ib_async cancel is async; runner waits)
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
