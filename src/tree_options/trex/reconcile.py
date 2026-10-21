"""Books lane: ledger holdings vs broker positions (``trex-reconciliation/1``).

Pure comparison over C2.4/C2.5 types (WAVE2-CONTRACTS.md C2.5):

- scope: every contract the Wave 2 ledger has touched, flat ones included
  (a stray broker position in a contract we closed is ``unexplained``);
- per con_id: ``unexplained`` (ledger flat, broker holds), ``missing``
  (ledger holds, broker flat or absent), ``quantity_mismatch`` (both hold,
  different quantities); a 0 broker row is the same as no row;
- a broker position in a contract the ledger never touched is NOT a
  mismatch: it is an outside position (``outside_positions``), counted by
  the risk snapshot;
- the broker view is ``unavailable`` when it could not be read, holds a
  row the contract cannot represent (another security type, a fractional
  quantity: ``parse_broker_rows`` returns None rather than drop it) or
  contradicts itself (two rows, two quantities, one contract).

Only rows of the reconciled account count. Both ``unavailable`` and
``reconciliation_required`` block ENTRIES only (``reconciliation_blockers``);
exits never read this.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from tree_options.trex.contracts.books import BrokerPortfolioItem, LedgerEvent
from tree_options.trex.contracts.canonical import model_bytes, sha256_hex
from tree_options.trex.contracts.layout import Wave2RunDir
from tree_options.trex.contracts.risk import OutsidePosition, PositionMismatch, Reconciliation
from tree_options.trex.ledger import OUTSIDE, LedgerFold, write_atomic

__all__ = [
    "broker_positions",
    "outside_positions",
    "parse_broker_rows",
    "read_reconciliation",
    "reconcile",
    "reconcile_ledger",
    "reconciliation_event",
    "reconciliation_sha256",
    "write_reconciliation",
]


def parse_broker_rows(
    raw: Iterable[Mapping[str, Any]] | None,
) -> tuple[BrokerPortfolioItem, ...] | None:
    """Validate raw broker position rows; ANY unrepresentable row makes the
    whole view unreadable (None), never a silently shorter view."""
    if raw is None:
        return None
    try:
        return tuple(BrokerPortfolioItem.model_validate(dict(row)) for row in raw)
    except (ValidationError, TypeError, ValueError):
        return None


def broker_positions(
    account_id: str, broker_rows: Sequence[BrokerPortfolioItem]
) -> dict[int, BrokerPortfolioItem] | None:
    """One row per con_id for the account (None when rows contradict)."""
    out: dict[int, BrokerPortfolioItem] = {}
    for row in broker_rows:
        if row.account_id != account_id:
            continue
        held = out.setdefault(row.con_id, row)
        if held.position != row.position or held.sec_type != row.sec_type:
            return None
    return out


def _mismatch(con_id: int, local: int, broker: int) -> PositionMismatch | None:
    if local == broker:
        return None
    kind: Literal["unexplained", "missing", "quantity_mismatch"]
    if local == 0:
        kind = "unexplained"
    elif broker == 0:
        kind = "missing"
    else:
        kind = "quantity_mismatch"
    return PositionMismatch(con_id=con_id, local_qty=local, broker_qty=broker, kind=kind)


def reconcile(
    *,
    account_id: str,
    as_of: datetime,
    local_positions: Mapping[int, int],
    broker_rows: Sequence[BrokerPortfolioItem] | None,
) -> Reconciliation:
    """Compare the ledger's quantity in every touched contract
    (``local_positions``, flat ones included) with the broker's."""
    rows = None if broker_rows is None else broker_positions(account_id, broker_rows)
    if rows is None:
        return Reconciliation(account_id=account_id, status="unavailable", as_of=as_of)
    mismatches = []
    for con_id in sorted(local_positions):
        row = rows.get(con_id)
        found = _mismatch(con_id, local_positions[con_id], row.position if row else 0)
        if found is not None:
            mismatches.append(found)
    return Reconciliation(
        account_id=account_id,
        status="reconciliation_required" if mismatches else "clean",
        as_of=as_of,
        mismatches=tuple(mismatches),
    )


def reconcile_ledger(
    fold: LedgerFold,
    *,
    account_id: str,
    as_of: datetime,
    broker_rows: Sequence[BrokerPortfolioItem] | None,
) -> Reconciliation:
    return reconcile(
        account_id=account_id,
        as_of=as_of,
        local_positions=fold.positions(account_id),
        broker_rows=broker_rows,
    )


def outside_positions(
    *,
    account_id: str,
    touched: Iterable[int],
    broker_rows: Sequence[BrokerPortfolioItem],
) -> tuple[OutsidePosition, ...]:
    """Non-zero broker positions in contracts the ledger never touched."""
    seen = set(touched)
    rows = sorted(
        (r for r in broker_rows if r.account_id == account_id and r.con_id not in seen),
        key=lambda r: r.con_id,
    )
    out: dict[int, OutsidePosition] = {}
    for row in rows:
        if row.position:
            out.setdefault(
                row.con_id,
                OutsidePosition(
                    con_id=row.con_id,
                    sec_type=row.sec_type,
                    symbol=row.symbol,
                    quantity=row.position,
                ),
            )
    return tuple(out.values())


def reconciliation_sha256(reconciliation: Reconciliation) -> str:
    """sha256 of the document's canonical bytes (what the file holds)."""
    return sha256_hex(model_bytes(reconciliation))


def reconciliation_event(reconciliation: Reconciliation, *, recorded_at: datetime) -> LedgerEvent:
    """The ledger marker binding a reconciliation document by its hash."""
    sha = reconciliation_sha256(reconciliation)
    return LedgerEvent(
        event_id=f"reconciliation:{sha}",
        kind="reconciliation",
        account_id=reconciliation.account_id,
        at=reconciliation.as_of,
        recorded_at=recorded_at,
        attribution=OUTSIDE,
        quantity_delta=0,
        cash_delta_micros=0,
        ref_sha256=sha,
        detail=reconciliation.status,
    )


def write_reconciliation(run_dir: Wave2RunDir, reconciliation: Reconciliation) -> Path:
    """Atomically replace ``ledger/reconciliation.json`` (canonical bytes)."""
    path = run_dir.reconciliation()
    write_atomic(path, model_bytes(reconciliation))
    return path


def read_reconciliation(run_dir: Wave2RunDir) -> Reconciliation | None:
    """The latest reconciliation; None when absent or unreadable (which
    ``reconciliation_blockers`` treats as ``reconciliation_required``)."""
    try:
        return Reconciliation.model_validate_json(run_dir.reconciliation().read_bytes())
    except (OSError, ValidationError, ValueError):
        return None
