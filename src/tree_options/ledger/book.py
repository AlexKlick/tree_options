"""Cash/position ledgers with exact-Decimal conservation (INV-12).

FIFO lot accounting; every cent is 2-decimal exact. Each lot carries full
provenance (fill/order id, execution timestamp and session, unit premium,
multiplier, total cost basis) so historical cash never depends on later
contract-master revisions.

realized_pnl is GROSS of fees (fees live only in cash and total_fees); the
flat-book identity is cash == initial + sum(realized_gross) - total_fees.

M3 (plan §3.C): `apply_settlement` mints the reserved `exercise_settlement`
entry kind — expiry and early-exercise cash settlements walk the SAME FIFO
lots as sells, so realized PnL and the flat-book identity extend unchanged
(settlement cash folds into realized exactly as sell proceeds do). Fills
and settlements share one non-decreasing application timeline.

The conservation check is an INDEPENDENT oracle: it recomputes cash, fees,
quantities, and realized PnL from primitive fill fields (price, quantity,
multiplier, side) and settlement fields (cash, quantity) rather than
calling Fill.notional()/Fill.signed_cash(), so a bug in those methods
cannot validate itself. Ledger entries are conserved too: their signed
amounts must sum to the cash delta.

Integrity (audit §4.3): duplicate fill_id/settlement_id application fails
closed; events must arrive in non-decreasing timestamp order;
opened_session is the earliest session among the CURRENTLY OPEN lots
(reopen reports the reopen session).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from tree_options.options.settlement import ExerciseSettlement, intrinsic_value
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


def _replay(events: list[Fill | ExerciseSettlement]) -> _ReplayResult:
    """Deterministic FIFO replay in APPLICATION order, using
    primitive-field arithmetic only (review r1 P1-4: replaying the
    accepted sequence — never a re-derived ordering of tied timestamps,
    which could disagree with the book's own FIFO walk).

    Settlement cash is INDEPENDENTLY recomputed from (call_put, strike,
    settlement_price, quantity, multiplier) and compared with the recorded
    cash (review r1 P1-7): the oracle must not trust the very field it is
    supposed to check."""
    cash = Decimal("0")
    fees = Decimal("0")
    qty: defaultdict[str, int] = defaultdict(int)
    realized: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    lots: defaultdict[str, list[tuple[int, Decimal, int]]] = defaultdict(list)
    seen_fill_ids: set[str] = set()
    seen_settlement_ids: set[str] = set()
    for event in events:
        if isinstance(event, Fill):
            fill = event
            if fill.fill_id in seen_fill_ids:
                raise LedgerViolation("DUPLICATE_FILL", f"fill {fill.fill_id} applied twice")
            seen_fill_ids.add(fill.fill_id)
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
        else:
            settlement = event
            if settlement.settlement_id in seen_settlement_ids:
                raise LedgerViolation(
                    "DUPLICATE_SETTLEMENT", f"settlement {settlement.settlement_id} applied twice"
                )
            seen_settlement_ids.add(settlement.settlement_id)
            recomputed_cash = (
                intrinsic_value(settlement.call_put, settlement.strike, settlement.settlement_price)
                * settlement.quantity
                * settlement.multiplier
            ).quantize(FEE_TICK)
            if recomputed_cash != settlement.cash:
                raise LedgerViolation(
                    "SETTLEMENT_CASH_MISMATCH",
                    f"settlement {settlement.settlement_id} records cash "
                    f"{settlement.cash} but intrinsic arithmetic gives {recomputed_cash}",
                )
            if qty[settlement.contract_id] < settlement.quantity:
                raise LedgerViolation(
                    "POSITION_UNDERFLOW",
                    f"settlement stream goes short {settlement.contract_id}",
                )
            remaining = settlement.quantity
            cost_removed = Decimal("0")
            while remaining > 0:
                head_qty, head_price, head_mult = lots[settlement.contract_id][0]
                take = min(head_qty, remaining)
                cost_removed += head_price * take * head_mult
                head_qty -= take
                remaining -= take
                if head_qty == 0:
                    lots[settlement.contract_id].pop(0)
                else:
                    lots[settlement.contract_id][0] = (head_qty, head_price, head_mult)
            realized[settlement.contract_id] += (recomputed_cash - cost_removed).quantize(FEE_TICK)
            qty[settlement.contract_id] -= settlement.quantity
            cash += recomputed_cash
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
        # the accepted-event sequence in APPLICATION order (review r1
        # P1-4): the oracle replays exactly what happened, never a
        # re-derived ordering of tied timestamps. This is the single
        # authoritative record — no parallel fill/settlement lists.
        self._events: list[Fill | ExerciseSettlement] = []
        self._lots: defaultdict[str, list[Lot]] = defaultdict(list)
        self._realized: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        self._entries: list[LedgerEntry] = []
        self._entry_seq = 0
        self._last_execution_at: datetime | None = None
        self._applied_fill_ids: set[str] = set()
        self._applied_settlement_ids: set[str] = set()

    # -- application -------------------------------------------------------

    def apply(self, fill: Fill) -> None:
        """Apply a fill: cash now, provenance lots now, realized on closes.

        Fully atomic (review r2 P1): the two LedgerEntry models are
        CONSTRUCTED (and therefore schema-validated — an unrepresentable
        notional fails here) before any state changes. A rejected fill
        leaves the book exactly as it was."""
        if fill.fill_id in self._applied_fill_ids:
            raise LedgerViolation("DUPLICATE_FILL", f"fill {fill.fill_id} applied twice")
        if self._last_execution_at is not None and fill.execution_at < self._last_execution_at:
            raise LedgerViolation(
                "OUT_OF_ORDER_FILL",
                f"fill {fill.fill_id} at {fill.execution_at} precedes "
                f"last applied {self._last_execution_at}",
            )
        if fill.side == "sell":
            held = sum(lot.quantity for lot in self._lots[fill.contract_id])
            if held < fill.quantity:
                raise LedgerViolation(
                    "POSITION_UNDERFLOW",
                    f"sell {fill.quantity} of {fill.contract_id} but held {held}",
                )
        # stage + validate the ledger entries BEFORE any mutation: entry
        # construction is the only step after this that can still fail
        staged = self._stage_fill_entries(fill)
        # preflight complete — state mutations begin here
        self._apply_fill_arithmetic(fill)
        self._entries.extend(staged)
        self._entry_seq += 2
        self._last_execution_at = fill.execution_at
        self._events.append(fill)

    def _stage_fill_entries(self, fill: Fill) -> list[LedgerEntry]:
        """Construct (and thereby validate) the fill's two ledger entries
        without touching book state. Raises pydantic ValidationError for a
        schema-valid fill whose notional/fees are unrepresentable as
        Money — which is exactly the point: it must fail BEFORE the lot
        walk, not after it."""
        first = self._entry_seq + 1
        second = first + 1
        return [
            LedgerEntry(
                entry_id=f"ENT-{first:06d}",
                ts=fill.execution_at,
                session=fill.execution_session,
                kind="fill_notional",
                amount=_primitive_cash(fill),
                contract_id=fill.contract_id,
                ref_id=fill.fill_id,
            ),
            LedgerEntry(
                entry_id=f"ENT-{second:06d}",
                ts=fill.execution_at,
                session=fill.execution_session,
                kind="fee",
                amount=-fill.fees,
                contract_id=fill.contract_id,
                ref_id=fill.fill_id,
            ),
        ]

    def apply_settlement(self, settlement: ExerciseSettlement) -> None:
        """Apply an exercise settlement: close FIFO lots, add the cash,
        mint exactly one `exercise_settlement` entry. OTM expiry is the
        zero-cash case — the lots still close."""
        if settlement.settlement_id in self._applied_settlement_ids:
            raise LedgerViolation(
                "DUPLICATE_SETTLEMENT", f"settlement {settlement.settlement_id} applied twice"
            )
        if self._last_execution_at is not None and settlement.ts < self._last_execution_at:
            raise LedgerViolation(
                "OUT_OF_ORDER_SETTLEMENT",
                f"settlement {settlement.settlement_id} at {settlement.ts} precedes "
                f"last applied {self._last_execution_at}",
            )
        held = sum(lot.quantity for lot in self._lots[settlement.contract_id])
        if held < settlement.quantity:
            raise LedgerViolation(
                "POSITION_UNDERFLOW",
                f"settle {settlement.quantity} of {settlement.contract_id} but held {held}",
            )
        # preflight complete — state mutations begin here (review r1 P1-5:
        # a rejected settlement must not poison the merged timeline)
        self._last_execution_at = settlement.ts
        self._events.append(settlement)

        remaining = settlement.quantity
        cost_removed = Decimal("0")
        while remaining > 0:
            head = self._lots[settlement.contract_id][0]
            take = min(head.quantity, remaining)
            removed = (head.unit_price * take * head.multiplier).quantize(FEE_TICK)
            cost_removed += removed
            head.quantity -= take
            head.cost_basis = (head.cost_basis - removed).quantize(FEE_TICK)
            remaining -= take
            if head.quantity == 0:
                self._lots[settlement.contract_id].pop(0)
        self._realized[settlement.contract_id] += (settlement.cash - cost_removed).quantize(
            FEE_TICK
        )
        self.cash = (self.cash + settlement.cash).quantize(FEE_TICK)
        self._applied_settlement_ids.add(settlement.settlement_id)
        self._entry_seq += 1
        self._entries.append(
            LedgerEntry(
                entry_id=f"ENT-{self._entry_seq:06d}",
                ts=settlement.ts,
                session=settlement.session,
                kind="exercise_settlement",
                amount=settlement.cash,
                contract_id=settlement.contract_id,
                ref_id=settlement.settlement_id,
            )
        )

    def _apply_fill_arithmetic(self, fill: Fill) -> None:
        """Fill cash/lots/realized (shared by apply; ordering guarded above)."""
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
                removed = (head.unit_price * take * head.multiplier).quantize(FEE_TICK)
                cost_removed += removed
                head.quantity -= take
                head.cost_basis = (head.cost_basis - removed).quantize(FEE_TICK)
                remaining -= take
                if head.quantity == 0:
                    self._lots[fill.contract_id].pop(0)
            proceeds = (fill.price * fill.quantity * fill.multiplier).quantize(FEE_TICK)
            self._realized[fill.contract_id] += proceeds - cost_removed

        self.cash = (self.cash + _primitive_cash(fill) - fill.fees).quantize(FEE_TICK)
        self.total_fees = (self.total_fees + fill.fees).quantize(FEE_TICK)
        self._applied_fill_ids.add(fill.fill_id)

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
        result = _replay(self._events)
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
