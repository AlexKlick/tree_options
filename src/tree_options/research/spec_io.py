"""ComparisonSpec wire I/O — one parser shared by the HTTP view and the
worker (RL1-03: the stored canonical spec and the POSTed body must parse
IDENTICALLY, and neither the view nor the worker owns a private dialect).

``spec_from_dict`` raises ``ValueError`` with a field-specific message;
the HTTP layer wraps it in a 400, the worker lets it fail a run.
Decimal fields reject non-finite magnitudes (``NaN``/``Infinity`` were
previously accepted and persisted), capital must be positive and
representable, and ``candidate_ids`` must be a deduplicated list of
non-empty strings.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from tree_options.research.contracts import (
    BorrowingPolicy,
    CashflowTiming,
    CollateralPolicy,
    ComparisonSpec,
    CostModelKind,
    Currency,
    IdleCashPolicy,
    PositionSizing,
    PriceBasis,
    Rebalancing,
)

#: Upper bound on starting capital (money max_digits=18 in the ledger
#: schemas; a comparison with more digits than that cannot execute).
_MAX_CAPITAL = Decimal("999999999999999.99")


def _decimal(payload: dict[str, Any], field: str, *,
             default: str = "0", positive: bool = False) -> Decimal:
    raw = payload.get(field, default)
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"field {field!r} not a decimal: {raw!r}") from exc
    if not value.is_finite():
        raise ValueError(f"field {field!r} must be finite, got {raw!r}")
    if positive and value <= 0:
        raise ValueError(f"field {field!r} must be positive, got {raw!r}")
    if positive and value > _MAX_CAPITAL:
        raise ValueError(f"field {field!r} exceeds the representable maximum "
                         f"{_MAX_CAPITAL}")
    return value


def _candidate_ids(payload: dict[str, Any]) -> tuple[str, ...]:
    raw = payload.get("candidate_ids")
    if not isinstance(raw, list) or not raw:
        raise ValueError("'candidate_ids' must be a non-empty list")
    if len(raw) > 50:
        raise ValueError("'candidate_ids' must contain at most 50 candidates")
    ids: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"'candidate_ids' entries must be non-empty "
                             f"strings, got {item!r}")
        if item in ids:
            raise ValueError(f"'candidate_ids' contains duplicates: {item!r}")
        ids.append(item)
    return tuple(ids)


def _iso_date(payload: dict[str, Any], field: str) -> date | None:
    raw = payload.get(field)
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ValueError(f"field {field!r} not an ISO date: {raw!r}") from exc


def spec_from_dict(payload: dict[str, Any]) -> ComparisonSpec:
    """Parse a wire-format spec dict. Raises ``ValueError`` on any
    malformed field (the view maps this to HTTP 400)."""
    if not isinstance(payload, dict):
        raise ValueError("spec body must be a JSON object")
    candidate_ids = _candidate_ids(payload)
    cutoff_raw = payload.get("knowledge_cutoff")
    cutoff: datetime | None = None
    if cutoff_raw not in (None, ""):
        try:
            cutoff = datetime.fromisoformat(str(cutoff_raw))
        except ValueError as exc:
            raise ValueError(f"field 'knowledge_cutoff' not an ISO datetime: "
                             f"{cutoff_raw!r}") from exc
    benchmark = payload.get("benchmark_candidate_id")
    if benchmark is not None and not isinstance(benchmark, str):
        raise ValueError("'benchmark_candidate_id' must be a string or null")
    proposed_by = payload.get("proposed_by", "operator")
    if not isinstance(proposed_by, str):
        raise ValueError("'proposed_by' must be a string")
    notes = payload.get("notes", "")
    if not isinstance(notes, str):
        raise ValueError("'notes' must be a string")
    return ComparisonSpec(
        candidate_ids=candidate_ids,
        starting_capital=_decimal(payload, "starting_capital", positive=True),
        common_start=_iso_date(payload, "common_start"),
        common_end=_iso_date(payload, "common_end"),
        cashflow_timing=CashflowTiming(
            payload.get("cashflow_timing", "beginning_of_period")),
        contribution_per_period=_decimal(payload, "contribution_per_period"),
        cost_model_kind=CostModelKind(
            payload.get("cost_model_kind", "five_bp_fixed")),
        benchmark_candidate_id=benchmark,
        currency=Currency(payload.get("currency", "USD")),
        price_basis=PriceBasis(payload.get("price_basis", "nominal_pretax")),
        idle_cash_policy=IdleCashPolicy(
            payload.get("idle_cash_policy", "cash_yields_zero")),
        rebalancing=Rebalancing(payload.get("rebalancing", "none")),
        position_sizing=PositionSizing(payload.get("position_sizing", "integer")),
        collateral=CollateralPolicy(payload.get("collateral", "none")),
        borrowing=BorrowingPolicy(payload.get("borrowing", "none")),
        knowledge_cutoff=cutoff,
        proposed_by=proposed_by,
        notes=notes,
    )


__all__ = ["spec_from_dict"]
