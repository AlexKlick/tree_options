"""The desk's entry gate (lane E3): what a structure's FIRST entry order
must pass before the desk runtime (E5) places it.

1. ``plan.validate_package_order`` on the opening order (side = the
   package's open side: BUY for debit kinds, SELL for credit kinds);
2. IBKR's what-if on exactly that order (``IbkrTrex.whatif``: bounded in
   time, places nothing);
3. ``plan.margin_within_max_loss`` on its initial-margin change: above 1.1x
   the computed max loss, IBKR is treating the package as undefined risk.

Fail closed: an order outside the bounds, no answer (None, not a finite
Decimal) or ANY error from the broker call refuses the entry. The verdict's
``detail`` is for the log only, never a push (it can carry prices).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from tree_options.trex.plan import LegStructure, margin_within_max_loss, validate_package_order


class WhatIfBroker(Protocol):
    """The one broker call the gate makes (IbkrTrex, FakeDeskBroker)."""

    def whatif(
        self, struct: LegStructure, side: str, qty: int, limit: Decimal
    ) -> Decimal | None: ...


@dataclass(frozen=True)
class EntryVerdict:
    """``ok`` or why not: ``order_refused`` (outside the package bounds),
    ``whatif_error`` (the broker call raised), ``whatif_no_answer`` (no
    number), ``margin_above_max_loss``. ``margin`` is IBKR's initial-margin
    change and ``max_loss`` the order's computed max loss, in dollars."""

    ok: bool
    reason: str
    margin: Decimal | None = None
    max_loss: Decimal | None = None
    detail: str = ""


def entry_gate(broker: WhatIfBroker, spec: LegStructure, qty: int, limit: Decimal) -> EntryVerdict:
    """Check the opening order of ``qty`` packages of ``spec`` at ``limit``
    (a debit-orientation price) before it is placed."""
    side = spec.open_side
    try:
        validate_package_order(spec, side, qty, limit)
    except ValueError as exc:
        return EntryVerdict(False, "order_refused", detail=str(exc))
    max_loss = spec.max_loss_per_package() * 100 * qty
    try:
        margin = broker.whatif(spec, side, qty, limit)
    except Exception as exc:  # timeout, disconnect, not prepared, anything: refuse
        return EntryVerdict(False, "whatif_error", max_loss=max_loss, detail=type(exc).__name__)
    if not isinstance(margin, Decimal) or not margin.is_finite():
        return EntryVerdict(False, "whatif_no_answer", max_loss=max_loss)
    if not margin_within_max_loss(spec, qty, margin):
        return EntryVerdict(False, "margin_above_max_loss", margin, max_loss)
    return EntryVerdict(True, "ok", margin, max_loss)
