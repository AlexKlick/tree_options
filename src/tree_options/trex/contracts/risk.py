"""C2.5: ``trex-risk-snapshot/1`` and ``trex-reconciliation/1``.

Both are produced by the books lane from ONE broker session's views and
read by admission. Each is usable only while fresh (``as_of`` within its
max age, never in the future); an unusable snapshot or reconciliation
blocks new entries and never blocks exits.

Reconciliation scope: the contracts the Wave 2 ledger has ever touched.
For each, the ledger quantity is compared with the broker position:

- ``unexplained``: the broker holds it, the ledger says flat;
- ``missing``: the ledger holds it, the broker says flat;
- ``quantity_mismatch``: both hold it, in different quantities.

A broker position in a contract the ledger never touched is not a
mismatch: it is an ``outside_positions`` row of the risk snapshot
(operator ruling 3b: other books do not block, their risk still counts).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from tree_options.schemas.common import IdStr, StrictModel
from tree_options.trex.contracts.canonical import UTCInstant
from tree_options.trex.contracts.defaults import (
    RECONCILIATION_MAX_AGE_S,
    RISK_SNAPSHOT_STALE_AFTER_S,
)
from tree_options.trex.contracts.vocabulary import Blocker

__all__ = [
    "RECONCILIATION_SCHEMA",
    "RISK_SNAPSHOT_SCHEMA",
    "CashSource",
    "OutsidePosition",
    "PositionMismatch",
    "Reconciliation",
    "RiskSnapshot",
    "reconciliation_blockers",
    "risk_snapshot_blockers",
]

RISK_SNAPSHOT_SCHEMA = "trex-risk-snapshot/1"
RECONCILIATION_SCHEMA = "trex-reconciliation/1"

Cents = Annotated[int, Field(strict=True)]
NonNegativeCents = Annotated[int, Field(strict=True, ge=0)]
#: the IBKR account-value tag ``cash_cents`` was read from (amendment
#: 2026-10-21, cash tag): ``SettledCash``, else ``TotalCashValue``
CashSource = Literal["SettledCash", "TotalCashValue"]


def _fresh(as_of: datetime, now: datetime, max_age_s: int) -> bool:
    age = (now - as_of).total_seconds()
    return 0 <= age <= max_age_s


class OutsidePosition(StrictModel):
    """A broker position in a contract the Wave 2 ledger never touched."""

    con_id: int = Field(strict=True, ge=1)
    sec_type: Literal["OPT", "STK"]
    symbol: IdStr
    quantity: int = Field(strict=True)

    @model_validator(mode="after")
    def _held(self) -> OutsidePosition:
        if self.quantity == 0:
            raise ValueError("an outside position is non-zero")
        return self


class RiskSnapshot(StrictModel):
    """Account risk as the broker prices it.

    - ``open_loss_cents``: the loss reservation of every open position
      (ours and outside) at broker marks; equities reserve
      max(quantity x (mark - stop), gap reserve of mark notional), options
      their remaining defined max loss;
    - ``day_realized_loss_cents``: today's realized losses (>= 0);
    - ``cash_cents``: settled cash (negative = borrowing, which a
      cash-only account must treat as ``insufficient_cash``);
    - ``equity_gross_cents``: gross long stock exposure at marks;
    - ``cash_source`` (additive, optional; amendment 2026-10-21): the
      IBKR account-value tag ``cash_cents`` was read from. Absent from
      the document when None (every earlier snapshot); an unavailable
      snapshot never carries it.

    ``complete``: every figure was read; ``unavailable``: none is trusted
    (all figures None).
    """

    schema_version: Literal["trex-risk-snapshot/1"] = Field(
        default="trex-risk-snapshot/1", alias="schema"
    )
    account_id: IdStr
    as_of: UTCInstant
    status: Literal["complete", "unavailable"]
    open_loss_cents: NonNegativeCents | None
    day_realized_loss_cents: NonNegativeCents | None
    cash_cents: Cents | None
    equity_gross_cents: NonNegativeCents | None
    outside_positions: tuple[OutsidePosition, ...] = ()
    stale_after_s: Literal[120] = RISK_SNAPSHOT_STALE_AFTER_S
    cash_source: CashSource | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_cash_source(self, handler: SerializerFunctionWrapHandler) -> Any:
        """An absent ``cash_source`` stays absent: the document (and its
        canonical bytes) of a snapshot without one is unchanged."""
        document = handler(self)
        if self.cash_source is None and isinstance(document, dict):
            document.pop("cash_source", None)
        return document

    @model_validator(mode="after")
    def _status_shape(self) -> RiskSnapshot:
        figures = (
            self.open_loss_cents,
            self.day_realized_loss_cents,
            self.cash_cents,
            self.equity_gross_cents,
        )
        if self.status == "complete" and any(f is None for f in figures):
            raise ValueError("a complete snapshot reads every figure")
        if self.status == "unavailable" and (
            any(f is not None for f in figures)
            or self.outside_positions
            or self.cash_source is not None
        ):
            raise ValueError("an unavailable snapshot carries no figures")
        return self

    def usable_at(self, now: datetime) -> bool:
        return self.status == "complete" and _fresh(self.as_of, now, self.stale_after_s)


class PositionMismatch(StrictModel):
    con_id: int = Field(strict=True, ge=1)
    local_qty: int = Field(strict=True)
    broker_qty: int = Field(strict=True)
    kind: Literal["unexplained", "missing", "quantity_mismatch"]

    @model_validator(mode="after")
    def _kind_matches(self) -> PositionMismatch:
        local, broker = self.local_qty, self.broker_qty
        expected = (
            "unexplained"
            if local == 0 and broker != 0
            else "missing"
            if local != 0 and broker == 0
            else "quantity_mismatch"
            if local != broker
            else None
        )
        if expected is None:
            raise ValueError("equal quantities are not a mismatch")
        if self.kind != expected:
            raise ValueError(f"local {local} vs broker {broker} is {expected}, not {self.kind}")
        return self


class Reconciliation(StrictModel):
    """Ledger vs broker positions, one account, one instant."""

    schema_version: Literal["trex-reconciliation/1"] = Field(
        default="trex-reconciliation/1", alias="schema"
    )
    account_id: IdStr
    status: Literal["clean", "reconciliation_required", "unavailable"]
    as_of: UTCInstant
    mismatches: tuple[PositionMismatch, ...] = ()
    max_age_s: Literal[120] = RECONCILIATION_MAX_AGE_S

    @model_validator(mode="after")
    def _status_shape(self) -> Reconciliation:
        if self.status == "reconciliation_required" and not self.mismatches:
            raise ValueError("reconciliation_required lists its mismatches")
        if self.status != "reconciliation_required" and self.mismatches:
            raise ValueError(f"a {self.status} reconciliation lists no mismatches")
        con_ids = [m.con_id for m in self.mismatches]
        if len(con_ids) != len(set(con_ids)):
            raise ValueError("one mismatch per con_id")
        return self

    def clean_at(self, now: datetime) -> bool:
        return self.status == "clean" and _fresh(self.as_of, now, self.max_age_s)


def risk_snapshot_blockers(snapshot: RiskSnapshot | None, now: datetime) -> tuple[Blocker, ...]:
    """Absent, unavailable, stale or future-dated: ``risk_snapshot_stale``."""
    if snapshot is None or not snapshot.usable_at(now):
        return (Blocker.RISK_SNAPSHOT_STALE,)
    return ()


def reconciliation_blockers(
    reconciliation: Reconciliation | None, now: datetime
) -> tuple[Blocker, ...]:
    """Anything but a fresh ``clean``: ``reconciliation_required``."""
    if reconciliation is None or not reconciliation.clean_at(now):
        return (Blocker.RECONCILIATION_REQUIRED,)
    return ()
