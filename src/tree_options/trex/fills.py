"""Books lane: IBKR execution reports folded into per-order fill state.

Pure and broker-free: the input is C2.4 ``BrokerExecution`` facts (lane B
maps ib_async ``Fill`` objects onto them) plus late ``CommissionReport``
rows; the output is one ``OrderFills`` per IBKR ``permId``.

Rules (WAVE2-CONTRACTS.md C2.4):

- One FILL per base exec id. IBKR re-reports a corrected execution with
  the last dot segment incremented (``...01.01`` then ``...01.02``); the
  highest version is the fill's figures, earlier ones are superseded.
  Versions order by ``version_key`` (suffix length, then text), never by
  arrival, so the fold is independent of the order reports arrive in.
- The same exec id reported twice is one report: every field must agree,
  except that a missing commission is filled in by the later copy. Any
  other disagreement is a ``FillConflict`` (never a silent overwrite).
- A commission report joins its execution by exec id. Every version of a
  fill carries its own report (a correction's is usually 0); the fill's
  fee is their sum. A lifecycle with ANY report still missing is
  ``incomplete`` and its fee is None, never zero.
- ``BAG`` (combo-level) executions are evidence only: kept apart in
  ``combo_fills``, counted for the package quantity, never booked as fills
  and not required to carry a commission (the legs carry them).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from tree_options.trex.contracts.books import BrokerExecution

__all__ = [
    "CommissionReport",
    "Fill",
    "FillBook",
    "FillConflict",
    "FillProgress",
    "FillStatus",
    "OrderFills",
    "fold_executions",
    "version_key",
]

FillStatus = Literal["complete", "incomplete"]
FillProgress = Literal["unfilled", "partial", "filled"]

#: fields every version of one fill shares (a correction re-reports the
#: same order's execution in the same contract and direction)
_FILL_IDENTITY = ("perm_id", "order_ref", "account_id", "con_id", "sec_type", "side", "multiplier")


class FillConflict(ValueError):
    """Two broker reports contradict each other: the books cannot fold them."""


@dataclass(frozen=True)
class CommissionReport:
    """IBKR's ``commissionReport`` for one exec id, in micros."""

    exec_id: str
    commission_micros: int

    def __post_init__(self) -> None:
        if type(self.commission_micros) is not int:
            raise TypeError("commission_micros is an integer number of micros")


def version_key(exec_id: str) -> tuple[int, str]:
    """Order of the versions of one execution: the last dot segment, as
    IBKR spells it (zero-padded), by length then text (``09 < 0a < 10``)."""
    suffix = exec_id.rpartition(".")[2]
    return (len(suffix), suffix)


@dataclass(frozen=True)
class Fill:
    """One execution (one base exec id) with every version reported."""

    base_exec_id: str
    #: every report, commission joined, oldest version first
    versions: tuple[BrokerExecution, ...]

    @property
    def execution(self) -> BrokerExecution:
        """The latest version: the figures the fill stands at."""
        return self.versions[-1]

    @property
    def exec_id(self) -> str:
        return self.execution.exec_id

    @property
    def superseded_exec_ids(self) -> tuple[str, ...]:
        return tuple(v.exec_id for v in self.versions[:-1])

    @property
    def signed_quantity(self) -> int:
        """Contracts or shares moved: + bought, - sold."""
        return signed_quantity(self.execution)

    @property
    def cash_micros(self) -> int:
        """Cash the fill moved, before commission: - paid, + received."""
        return cash_micros(self.execution)

    @property
    def missing_commissions(self) -> tuple[str, ...]:
        return tuple(v.exec_id for v in self.versions if v.commission_micros is None)

    @property
    def complete(self) -> bool:
        return not self.missing_commissions

    @property
    def commission_micros(self) -> int | None:
        """Sum of every version's commission; None while any is missing."""
        fees = [v.commission_micros for v in self.versions]
        if any(fee is None for fee in fees):
            return None
        return sum(fee for fee in fees if fee is not None)


def signed_quantity(execution: BrokerExecution) -> int:
    return execution.quantity if execution.side == "BOT" else -execution.quantity


def cash_micros(execution: BrokerExecution) -> int:
    return -signed_quantity(execution) * execution.price_micros * execution.multiplier


@dataclass(frozen=True)
class OrderFills:
    """Every fill of one order (one IBKR ``permId``)."""

    perm_id: int
    account_id: str
    order_ref: str
    #: booked fills (OPT/STK), by first-report time then base exec id
    fills: tuple[Fill, ...]
    #: ``BAG`` combo-level reports: evidence only
    combo_fills: tuple[Fill, ...] = ()

    @property
    def missing_commissions(self) -> tuple[str, ...]:
        return tuple(exec_id for fill in self.fills for exec_id in fill.missing_commissions)

    @property
    def complete(self) -> bool:
        return not self.missing_commissions

    @property
    def status(self) -> FillStatus:
        return "complete" if self.complete else "incomplete"

    @property
    def commission_micros(self) -> int | None:
        """Total commission; None (never 0) while any report is missing."""
        if not self.complete:
            return None
        return sum(fill.commission_micros or 0 for fill in self.fills)

    @property
    def net_cash_micros(self) -> int:
        """Cash the order's fills moved, before commission."""
        return sum(fill.cash_micros for fill in self.fills)

    @property
    def quantity_by_con_id(self) -> dict[int, int]:
        """Signed quantity per contract (legs of a combo separately)."""
        out: dict[int, int] = defaultdict(int)
        for fill in self.fills:
            out[fill.execution.con_id] += fill.signed_quantity
        return dict(sorted(out.items()))

    @property
    def filled_quantity(self) -> int:
        """Units of the ORDER filled: the combo reports' packages when
        present; else one contract's quantity; else (legs only) the packages
        every leg has filled (ratio-1 legs)."""
        if self.combo_fills:
            return sum(fill.execution.quantity for fill in self.combo_fills)
        moved = [abs(q) for q in self.quantity_by_con_id.values()]
        if not moved:
            return 0
        return moved[0] if len(moved) == 1 else min(moved)

    def progress(self, ordered_quantity: int) -> FillProgress:
        """Where the order stands against the quantity it was sent for."""
        if ordered_quantity < 1:
            raise ValueError("an order is for at least one unit")
        filled = self.filled_quantity
        if filled > ordered_quantity:
            raise FillConflict(
                f"perm {self.perm_id}: overfilled ({filled} of {ordered_quantity} ordered)"
            )
        if filled == 0:
            return "unfilled"
        return "filled" if filled == ordered_quantity else "partial"


@dataclass(frozen=True)
class FillBook:
    """Every order's fills, plus commission reports no execution claimed."""

    orders: tuple[OrderFills, ...]
    unmatched_commissions: tuple[CommissionReport, ...] = ()

    @property
    def complete(self) -> bool:
        return all(order.complete for order in self.orders)

    def order(self, perm_id: int) -> OrderFills | None:
        for order in self.orders:
            if order.perm_id == perm_id:
                return order
        return None

    def for_order_ref(self, order_ref: str) -> tuple[OrderFills, ...]:
        return tuple(order for order in self.orders if order.order_ref == order_ref)

    def fills(self) -> tuple[Fill, ...]:
        """Every booked fill across orders, by time then base exec id."""
        every = [fill for order in self.orders for fill in order.fills]
        return tuple(sorted(every, key=_fill_order))


def _fill_order(fill: Fill) -> tuple[datetime, str]:
    return (fill.versions[0].time, fill.base_exec_id)


def _merge_report(held: BrokerExecution, new: BrokerExecution) -> BrokerExecution:
    """One exec id reported twice: identical, or the later carries the
    commission the earlier lacked."""
    if held == new:
        return held
    plain_held = held.model_copy(update={"commission_micros": None})
    plain_new = new.model_copy(update={"commission_micros": None})
    if plain_held != plain_new:
        raise FillConflict(f"exec {held.exec_id}: two reports disagree on the execution")
    return _with_commission(held, new.commission_micros)


def _with_commission(report: BrokerExecution, commission: int | None) -> BrokerExecution:
    if commission is None or report.commission_micros == commission:
        return report
    if report.commission_micros is not None:
        raise FillConflict(
            f"exec {report.exec_id}: commission reported as {report.commission_micros} "
            f"and as {commission}"
        )
    return report.model_copy(update={"commission_micros": commission})


def _check_versions(base: str, versions: list[BrokerExecution]) -> None:
    first = versions[0]
    for later in versions[1:]:
        for name in _FILL_IDENTITY:
            if getattr(later, name) != getattr(first, name):
                raise FillConflict(
                    f"fill {base}: version {later.exec_id} changes {name} "
                    f"({getattr(first, name)!r} -> {getattr(later, name)!r})"
                )


def fold_executions(
    executions: Iterable[BrokerExecution], commissions: Iterable[CommissionReport] = ()
) -> FillBook:
    """Fold reports (any order, replays allowed) into per-order fill state."""
    reports: dict[str, BrokerExecution] = {}
    for execution in executions:
        held = reports.get(execution.exec_id)
        reports[execution.exec_id] = execution if held is None else _merge_report(held, execution)
    unmatched: dict[str, CommissionReport] = {}
    for commission in commissions:
        target = reports.get(commission.exec_id)
        if target is not None:
            reports[commission.exec_id] = _with_commission(target, commission.commission_micros)
            continue
        seen = unmatched.get(commission.exec_id)
        if seen is not None and seen != commission:
            raise FillConflict(f"exec {commission.exec_id}: two commission reports disagree")
        unmatched[commission.exec_id] = commission

    by_base: dict[str, list[BrokerExecution]] = defaultdict(list)
    for report in reports.values():
        by_base[report.base_exec_id].append(report)
    by_order: dict[int, list[Fill]] = defaultdict(list)
    for base, versions in by_base.items():
        versions.sort(key=lambda v: version_key(v.exec_id))
        _check_versions(base, versions)
        by_order[versions[0].perm_id].append(Fill(base_exec_id=base, versions=tuple(versions)))

    orders = []
    for perm_id in sorted(by_order):
        fills = sorted(by_order[perm_id], key=_fill_order)
        head = fills[0].versions[0]
        for fill in fills[1:]:
            other = fill.versions[0]
            if (other.account_id, other.order_ref) != (head.account_id, head.order_ref):
                raise FillConflict(
                    f"perm {perm_id}: fills {head.exec_id} and {other.exec_id} name "
                    "different accounts or order refs"
                )
        orders.append(
            OrderFills(
                perm_id=perm_id,
                account_id=head.account_id,
                order_ref=head.order_ref,
                fills=tuple(f for f in fills if f.execution.sec_type != "BAG"),
                combo_fills=tuple(f for f in fills if f.execution.sec_type == "BAG"),
            )
        )
    return FillBook(
        orders=tuple(orders),
        unmatched_commissions=tuple(unmatched[k] for k in sorted(unmatched)),
    )
