"""ScenarioSpec wire I/O (RL-2).

Sits beside ``tree_options.research.spec_io``: the ComparisonSpec and
ScenarioSpec parsers are siblings, not nested. Both speak the wire
dialect the HTTP view and the bounded worker share verbatim (RL1-03:
the stored canonical spec and the POSTed body must parse IDENTICALLY).

This module is intentionally FREE of imports from
``tree_options.research.scenarios.engine`` (and likewise
``spec_io.spec_from_dict`` is not imported here at module load) so
either side can be loaded before the other — the import graph stays
acyclic.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from tree_options.research.contracts import (
    CashflowTiming,
    CostModelKind,
    PositionSizing,
    Rebalancing,
)
from tree_options.research.scenarios.contracts import (
    SCENARIO_DIFF_FIELDS,
    ScenarioAccessMode,
    ScenarioDiff,
    ScenarioKind,
    ScenarioSpec,
)


def scenario_from_dict(parent_run_id: str,
                       payload: dict[str, Any]) -> ScenarioSpec:
    """Parse a wire-format scenario fork body (RL-2).

    The body's shape is::

        {
            "kind": "historical_rule_replay" | "contribution_planning"
                  | "conditional_stress",
            "access_mode": "registered" | "held_out" | "exploratory",
            "diff": {
                "contribution_per_period": "500",   # optional Decimal string
                "cashflow_timing": "beginning_of_period",  # optional
                "cost_model_kind": "five_bp_fixed",        # optional
                "rebalancing": "none",                    # optional
                "position_sizing": "integer"              # optional
            },
            "proposed_by": "operator",
            "notes": ""
        }

    Anything outside ``SCENARIO_DIFF_FIELDS`` in the diff is rejected
    pre-write (this is the "scenario != new base comparison" boundary):
    a base comparison goes through ``spec_from_dict`` and
    ``POST /api/research/compare`` instead. The parent_run_id is
    supplied separately (it is the URL path component, not the body)
    so a forged body cannot change the fork's subject.
    """
    if not isinstance(payload, dict):
        raise ValueError("scenario body must be a JSON object")
    if not isinstance(parent_run_id, str) or not parent_run_id.strip():
        raise ValueError("parent_run_id must be a non-empty string")
    kind = ScenarioKind(payload.get("kind", "contribution_planning"))
    access_mode = ScenarioAccessMode(
        payload.get("access_mode", "exploratory"))
    diff_payload = payload.get("diff") or {}
    if not isinstance(diff_payload, dict):
        raise ValueError("'diff' must be a JSON object")
    unknown = set(diff_payload) - set(SCENARIO_DIFF_FIELDS)
    if unknown:
        raise ValueError(
            f"'diff' contains fields outside the scenario surface "
            f"{list(SCENARIO_DIFF_FIELDS)}: {sorted(unknown)}"
        )
    contrib = diff_payload.get("contribution_per_period")
    if contrib is not None:
        if not isinstance(contrib, (str, int, float)):
            raise ValueError(
                f"'contribution_per_period' must be a decimal string, got "
                f"{contrib!r}")
        try:
            contrib_d = Decimal(str(contrib))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(
                f"'contribution_per_period' not a decimal: {contrib!r}"
            ) from exc
        if not contrib_d.is_finite():
            raise ValueError(
                f"'contribution_per_period' must be finite, got {contrib!r}"
            )
    else:
        contrib_d = None
    cashflow_timing_raw = diff_payload.get("cashflow_timing")
    cost_model_raw = diff_payload.get("cost_model_kind")
    rebalancing_raw = diff_payload.get("rebalancing")
    sizing_raw = diff_payload.get("position_sizing")
    proposed_by = payload.get("proposed_by", "operator")
    if not isinstance(proposed_by, str):
        raise ValueError("'proposed_by' must be a string")
    notes = payload.get("notes", "")
    if not isinstance(notes, str):
        raise ValueError("'notes' must be a string")
    return ScenarioSpec(
        parent_run_id=parent_run_id,
        kind=kind,
        access_mode=access_mode,
        diff=ScenarioDiff(
            contribution_per_period=contrib_d,
            cashflow_timing=(
                CashflowTiming(cashflow_timing_raw)
                if cashflow_timing_raw is not None else None
            ),
            cost_model_kind=(
                CostModelKind(cost_model_raw)
                if cost_model_raw is not None else None
            ),
            rebalancing=(
                Rebalancing(rebalancing_raw)
                if rebalancing_raw is not None else None
            ),
            position_sizing=(
                PositionSizing(sizing_raw)
                if sizing_raw is not None else None
            ),
        ),
        proposed_by=proposed_by,
        notes=notes,
    )


__all__ = ["scenario_from_dict"]
