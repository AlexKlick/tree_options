"""Cash/position ledgers with exact-Decimal conservation (INV-12).

FIFO lot accounting; every cent is 2-decimal exact. Each lot carries full
provenance (fill/order id, execution timestamp and session, unit premium,
multiplier, total cost basis) so historical cash never depends on later
contract-master revisions.

realized_pnl is GROSS of fees (fees live only in cash and total_fees); the
flat-book identity is cash == initial + sum(realized_gross) - total_fees.

The conservation check is an INDEPENDENT oracle: it recomputes cash, fees,
quantities, and realized PnL from primitive fill fields (price, quantity,
multiplier, side) rather than calling Fill.notional()/Fill.signed_cash(),
so a bug in those methods cannot validate itself. Ledger entries are
conserved too: their signed amounts must sum to the cash delta.

Integrity (audit §4.3): duplicate fill_id application fails closed; fills
must arrive in non-decreasing execution order; opened_session is the
earliest session among the CURRENTLY OPEN lots (reopen reports the reopen
session).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from tree_options.schemas.common import FEE_TICK
from tree_options.schemas.ledger import LedgerEntry
from tree_options.schemas.trading import Fill, Position


class LedgerViolation(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class Lot:
    """Provenance-carrying FIFO lot (audit §4.2)."""

    fill_id: str
    order_id: str
    execution_at: datetime
    execution_session: date
    quantity: int
    unit_price: Decimal
    multiplier: int
    cost_basis: Decimal  # unit_price * quantity * multiplier, exact 2dp


@dataclass(frozen=True)
class _ReplayResult:
    cash_delta: Decimal
    fees: Decimal
    quantities: dict[str, int]
    realized: dict[str, Decimal]


def _primitive_cash(fill: Fill) -> Decimal:
    """Independent cash arithmetic from primitive fields (NOT Fill methods)."""
    magnitude = (fill.price * fill.quantity * fill.multiplier).quantize(FEE_TICK)
    return -magnitude if fill.side == "buy" else magnitude


def _replay(fills: list[Fill]) -> _ReplayResult:
    """Deterministic FIFO replay using primitive-field arithmetic only."""
    cash = Decimal("0")
    fees = Decimal("0")
    qty: defaultdict[str, int] = defaultdict(int)
    realized: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    lots: defaultdict[str, list[tuple[int, Decimal, int]]] = defaultdict(list)
    seen_ids: set[str] = set()
    for fill in fills:
        if fill.fill_id in seen_ids:
            raise LedgerViolation("DUPLICATE_FILL", f"fill {fill.fill_id} applied twice")
        seen_ids.add(fill.fill_id)
        cash += _primitive_cash(fill) - fill.fees
        fees += fill.fees
        if fill.side == "buy":
            qty[fill.contract_id] += fill.quantity
            lots[fill.contract_id].append((fill.quantity, fill.price, fill.multiplier))
        else:
            if qty[fill.contract_id] < fill.quantity:
                raise LedgerViolation(
                    "POSITION_UNDERFLOW", f"fill stream goes short {fill.contract_id}"
                )
            remaining = fill.quantity
            cost_removed = Decimal("0")
            while remaining > 0:
                head_qty, head_price, head_mult = lots[fill.contract_id][0]
                take = min(head_qty, remaining)
                cost_removed += head_price * take * head_mult
                head_qty -= take
                remaining -= take
                if head_qty == 0:
                    lots[fill.contract_id].pop(0)
                else:
                    lots[fill.contract_id][0] = (head_qty, head_price, head_mult)
            proceeds = fill.price * fill.quantity * fill.multiplier
            realized[fill.contract_id] += (proceeds - cost_removed).quantize(FEE_TICK)
            qty[fill.contract_id] -= fill.quantity
    return _ReplayResult(
        cash_delta=cash.quantize(FEE_TICK),
        fees=fees.quantize(FEE_TICK),
        quantities=dict(qty),
        realized=dict(realized),
    )


class LedgerBook:
    def __init__(self, initial_cash: Decimal) -> None:
        self.initial_cash = initial_cash.quantize(FEE_TICK)
        self.cash = self.initial_cash
        self.total_fees = Decimal("0")
        self._fills: list[Fill] = []
        self._lots: defaultdict[str, list[Lot]] = defaultdict(list)
        self._realized: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        self._entries: list[LedgerEntry] = []
        self._entry_seq = 0
        self._last_execution_at: datetime | None = None
        self._applied_fill_ids: set[str] = set()

    # -- application -------------------------------------------------------

    def apply(self, fill: Fill) -> None:
        """Apply a fill: cash now, provenance lots now, realized on closes."""
        if fill.fill_id in self._applied_fill_ids:
            raise LedgerViolation("DUPLICATE_FILL", f"fill {fill.fill_id} applied twice")
        if self._last_execution_at is not None and fill.execution_at < self._last_execution_at:
            raise LedgerViolation(
                "OUT_OF_ORDER_FILL",
                f"fill {fill.fill_id} at {fill.execution_at} precedes "
                f"last applied {self._last_execution_at}",
            )
        self._last_execution_at = fill.execution_at

        if fill.side == "buy":
            basis = (fill.price * fill.quantity * fill.multiplier).quantize(FEE_TICK)
            self._lots[fill.contract_id].append(
                Lot(
                    fill_id=fill.fill_id,
                    order_id=fill.order_id,
                    execution_at=fill.execution_at,
                    execution_session=fill.execution_session,
                    quantity=fill.quantity,
                    unit_price=fill.price,
                    multiplier=fill.multiplier,
                    cost_basis=basis,
                )
            )
        else:
            held = sum(lot.quantity for lot in self._lots[fill.contract_id])
            if held < fill.quantity:
                raise LedgerViolation(
                    "POSITION_UNDERFLOW",
                    f"sell {fill.quantity} of {fill.contract_id} but held {held}",
                )
            remaining = fill.quantity
            cost_removed = Decimal("0")
            while remaining > 0:
                head = self._lots[fill.contract_id][0]
                take = min(head.quantity, remaining)
                cost_removed += head.unit_price * take * head.multiplier
                head.quantity -= take
                remaining -= take
                if head.quantity == 0:
                    self._lots[fill.contract_id].pop(0)
            proceeds = (fill.price * fill.quantity * fill.multiplier).quantize(FEE_TICK)
            self._realized[fill.contract_id] += proceeds - cost_removed

        self.cash = (self.cash + _primitive_cash(fill) - fill.fees).quantize(FEE_TICK)
        self.total_fees = (self.total_fees + fill.fees).quantize(FEE_TICK)
        self._applied_fill_ids.add(fill.fill_id)
        self._fills.append(fill)
        self._record_entries(fill)

    def _record_entries(self, fill: Fill) -> None:
        self._entry_seq += 1
        self._entries.append(
            LedgerEntry(
                entry_id=f"ENT-{self._entry_seq:06d}",
                ts=fill.execution_at,
                session=fill.execution_session,
                kind="fill_notional",
                amount=_primitive_cash(fill),
                contract_id=fill.contract_id,
                ref_id=fill.fill_id,
            )
        )
        self._entry_seq += 1
        self._entries.append(
            LedgerEntry(
                entry_id=f"ENT-{self._entry_seq:06d}",
                ts=fill.execution_at,
                session=fill.execution_session,
                kind="fee",
                amount=-fill.fees,
                contract_id=fill.contract_id,
                ref_id=fill.fill_id,
            )
        )

    # -- accessors ----------------------------------------------------------

    def quantity(self, contract_id: str) -> int:
        return sum(lot.quantity for lot in self._lots[contract_id])

    def lots(self, contract_id: str) -> tuple[Lot, ...]:
        return tuple(self._lots[contract_id])

    def opened_session(self, contract_id: str) -> date:
        """Earliest session among the CURRENTLY OPEN lots (audit §4.3)."""
        open_lots = self._lots[contract_id]
        if not open_lots:
            raise KeyError(f"no open position for {contract_id}")
        return min(lot.execution_session for lot in open_lots)

    def position(self, contract_id: str) -> Position:
        q = self.quantity(contract_id)
        if q == 0:
            raise KeyError(f"no open position for {contract_id}")
        basis = sum((lot.cost_basis for lot in self._lots[contract_id]), Decimal("0"))
        return Position(
            contract_id=contract_id,
            quantity=q,
            average_cost=(basis / (q * self._lots[contract_id][0].multiplier)).quantize(FEE_TICK),
            opened_session=self.opened_session(contract_id),
        )

    def realized_pnl(self, contract_id: str) -> Decimal:
        """Gross of fees (fees are tracked separately in total_fees)."""
        return self._realized[contract_id]

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    # -- conservation -------------------------------------------------------

    def assert_conservation(self) -> None:
        result = _replay(self._fills)
        expected_cash = (self.initial_cash + result.cash_delta).quantize(FEE_TICK)
        if self.cash != expected_cash:
            raise LedgerViolation(
                "CASH_MISMATCH", f"running cash {self.cash} != recomputed {expected_cash}"
            )
        if self.total_fees != result.fees:
            raise LedgerViolation(
                "FEE_MISMATCH", f"running fees {self.total_fees} != recomputed {result.fees}"
            )
        for contract_id, stream_qty in result.quantities.items():
            if self.quantity(contract_id) != stream_qty:
                raise LedgerViolation(
                    "POSITION_MISMATCH",
                    f"{contract_id}: book {self.quantity(contract_id)} != stream {stream_qty}",
                )
        for contract_id, stream_realized in result.realized.items():
            if self.realized_pnl(contract_id) != stream_realized:
                raise LedgerViolation(
                    "REALIZED_MISMATCH",
                    f"{contract_id}: book {self.realized_pnl(contract_id)} "
                    f"!= stream {stream_realized}",
                )
        entry_sum = sum((e.amount for e in self._entries), Decimal("0")).quantize(FEE_TICK)
        if entry_sum != result.cash_delta:
            raise LedgerViolation(
                "ENTRY_MISMATCH",
                f"entries sum {entry_sum} != replayed cash delta {result.cash_delta}",
            )
