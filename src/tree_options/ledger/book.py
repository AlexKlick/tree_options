"""Cash/position ledgers with exact-Decimal conservation (INV-12).

Accounting is FIFO lot-based so every cent is 2-decimal exact: buys append
lots at the fill price, sells consume lots head-first, and realized PnL on a
close is `sell notional - cost of the lots removed`. No division ever touches
money (a reported `average_cost` is display-only and explicitly quantized).

`assert_conservation` replays the raw fill stream and recomputes cash,
quantities, fees, and realized PnL; any drift — including a tampered fill
list — raises LedgerViolation. Never a silent rounding difference.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
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
class _Lot:
    quantity: int
    price: Decimal


def _replay(fills: list[Fill]) -> tuple[Decimal, Decimal, dict[str, int], dict[str, Decimal]]:
    """Deterministic FIFO replay: (cash, fees, quantities, realized)."""
    cash = Decimal("0")
    fees = Decimal("0")
    qty: defaultdict[str, int] = defaultdict(int)
    realized: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    lots: defaultdict[str, list[_Lot]] = defaultdict(list)
    for fill in fills:
        cash += fill.signed_cash() - fill.fees
        fees += fill.fees
        if fill.side == "buy":
            qty[fill.contract_id] += fill.quantity
            lots[fill.contract_id].append(_Lot(fill.quantity, fill.price))
        else:
            if qty[fill.contract_id] < fill.quantity:
                raise LedgerViolation(
                    "POSITION_UNDERFLOW", f"fill stream goes short {fill.contract_id}"
                )
            remaining = fill.quantity
            cost_removed = Decimal("0")
            while remaining > 0:
                head = lots[fill.contract_id][0]
                take = min(head.quantity, remaining)
                cost_removed += head.price * take
                head.quantity -= take
                remaining -= take
                if head.quantity == 0:
                    lots[fill.contract_id].pop(0)
            realized[fill.contract_id] += fill.notional() - cost_removed
            qty[fill.contract_id] -= fill.quantity
    return cash.quantize(FEE_TICK), fees.quantize(FEE_TICK), dict(qty), dict(realized)


class LedgerBook:
    def __init__(self, initial_cash: Decimal) -> None:
        self.initial_cash = initial_cash.quantize(FEE_TICK)
        self.cash = self.initial_cash
        self.total_fees = Decimal("0")
        self._fills: list[Fill] = []
        self._lots: defaultdict[str, list[_Lot]] = defaultdict(list)
        self._realized: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        self._entries: list[LedgerEntry] = []
        self._entry_seq = 0

    # -- application -------------------------------------------------------

    def apply(self, fill: Fill) -> None:
        """Apply a fill: cash now, lots now, realized on closes (FIFO)."""
        if fill.side == "buy":
            self._lots[fill.contract_id].append(_Lot(fill.quantity, fill.price))
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
                cost_removed += head.price * take
                head.quantity -= take
                remaining -= take
                if head.quantity == 0:
                    self._lots[fill.contract_id].pop(0)
            self._realized[fill.contract_id] += fill.notional() - cost_removed

        self.cash = (self.cash + fill.signed_cash() - fill.fees).quantize(FEE_TICK)
        self.total_fees = (self.total_fees + fill.fees).quantize(FEE_TICK)
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
                amount=fill.signed_cash(),
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

    def position(self, contract_id: str) -> Position:
        q = self.quantity(contract_id)
        if q == 0:
            raise KeyError(f"no open position for {contract_id}")
        basis = sum((lot.price * lot.quantity for lot in self._lots[contract_id]), Decimal("0"))
        opened = next(
            fill.execution_session
            for fill in self._fills
            if fill.contract_id == contract_id and fill.side == "buy"
        )
        return Position(
            contract_id=contract_id,
            quantity=q,
            average_cost=(basis / q).quantize(FEE_TICK),
            opened_session=opened,
        )

    def realized_pnl(self, contract_id: str) -> Decimal:
        return self._realized[contract_id]

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    # -- conservation -------------------------------------------------------

    def assert_conservation(self) -> None:
        cash, fees, quantities, realized = _replay(self._fills)
        expected_cash = (self.initial_cash + cash).quantize(FEE_TICK)
        if self.cash != expected_cash:
            raise LedgerViolation(
                "CASH_MISMATCH", f"running cash {self.cash} != recomputed {expected_cash}"
            )
        if self.total_fees != fees:
            raise LedgerViolation(
                "FEE_MISMATCH", f"running fees {self.total_fees} != recomputed {fees}"
            )
        for contract_id, stream_qty in quantities.items():
            if self.quantity(contract_id) != stream_qty:
                raise LedgerViolation(
                    "POSITION_MISMATCH",
                    f"{contract_id}: book {self.quantity(contract_id)} != stream {stream_qty}",
                )
        for contract_id, stream_realized in realized.items():
            if self.realized_pnl(contract_id) != stream_realized:
                raise LedgerViolation(
                    "REALIZED_MISMATCH",
                    f"{contract_id}: book {self.realized_pnl(contract_id)} "
                    f"!= stream {stream_realized}",
                )
