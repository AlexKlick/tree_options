"""Scenario contract — RL-2 Reproducible scenario branching.

A scenario is a *fork*: it points at a parent ComparisonResult and
applies a documented diff over the parent's ComparisonSpec. The
diff is the only mutable input; the parent's ``wallet_conservation_ok``
(``LedgerBook.assert_conservation`` on the parent's funded rows, in
RL1-01's hand-rolled sense) is the structural witness that the cached
parent is honest. The scenario engine does not re-execute the parent —
it derives the rewritten spec and re-runs ``run_comparison`` once.

Three scenario kinds (handoff §5 A/B/C):
    A — historical_rule_replay: same input snapshot, different decision rule
        (cost model, sizing, rebalancing).
    B — conditional_stress: supported option-valuation shock surface
        (IV move in absolute percentage points; underlying move in dollars;
        term; slip). RL-2 ships no real shock engine — type B scenarios
        REFUSE with ``SCENARIO_STRESS_UNSUPPORTED`` and document the gap
        rather than fabricate a value.
    C — contribution_planning: different contribution cadence / amount /
        allocation. The primary RL-2 use case.

Access modes (study-provenance label per handoff §7):
    registered          — published study; cannot be tuned post-hoc.
    held_out            — produced against a frozen holdout; matches
                          ``research.registration == held_out``.
    exploratory         — interactive tuning; logged but never claims
                          preregistration.

Diff fields (the only controls a scenario may change over its parent):
    contribution_per_period (Decimal)
    cashflow_timing         (CashflowTiming)
    cost_model_kind         (CostModelKind)
    rebalancing             (Rebalancing)
    position_sizing         (PositionSizing)

Anything outside this surface (candidate set, calendar window, benchmark,
knowledge cutoff, currency, price basis) is NOT a scenario diff — those
are base-spec changes and a new comparison, NOT a fork. (See
``SCENARIO_NOT_A_FORK``.)
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from tree_options.desk.contracts import canonical
from tree_options.research.contracts import (
    CashflowTiming,
    CostModelKind,
    PositionSizing,
    Rebalancing,
)


class ScenarioKind(StrEnum):
    HISTORICAL_RULE_REPLAY = "historical_rule_replay"
    CONTRIBUTION_PLANNING = "contribution_planning"
    CONDITIONAL_STRESS = "conditional_stress"


class ScenarioAccessMode(StrEnum):
    REGISTERED = "registered"
    HELD_OUT = "held_out"
    EXPLORATORY = "exploratory"


#: The diff surface a scenario may change over its parent. Any other
#: field on a parent spec is NOT a scenario — it is a different spec
#: (and the wrong API: ``POST /api/research/compare``).
SCENARIO_DIFF_FIELDS: tuple[str, ...] = (
    "contribution_per_period",
    "cashflow_timing",
    "cost_model_kind",
    "rebalancing",
    "position_sizing",
)


@dataclass(frozen=True)
class ScenarioDiff:
    """A documented diff over a parent spec. Every field is OPTIONAL:

    - ``None`` means "inherit from parent" (the SCENARIO_NOT_A_FORK
      oracle: an all-None diff must reproduce the parent byte-for-byte);
    - a typed value is "set to this value for the fork".

    A diff is rejected at parse time if it claims a field outside
    ``SCENARIO_DIFF_FIELDS``; this is the only way to keep the surface
    small enough that "scenario" cannot accidentally mean "new base
    comparison".
    """
    contribution_per_period: Decimal | None = None
    cashflow_timing: CashflowTiming | None = None
    cost_model_kind: CostModelKind | None = None
    rebalancing: Rebalancing | None = None
    position_sizing: PositionSizing | None = None

    def changed_fields(self) -> tuple[str, ...]:
        """Field names actually changed (None means "inherits parent")."""
        out: list[str] = []
        for name in SCENARIO_DIFF_FIELDS:
            v = getattr(self, name)
            if v is not None:
                out.append(name)
        return tuple(out)

    def is_empty(self) -> bool:
        return not self.changed_fields()

    def to_dict(self) -> dict[str, object]:
        return {
            "contribution_per_period": (
                str(self.contribution_per_period)
                if self.contribution_per_period is not None else None
            ),
            "cashflow_timing": (
                self.cashflow_timing.value
                if self.cashflow_timing is not None else None
            ),
            "cost_model_kind": (
                self.cost_model_kind.value
                if self.cost_model_kind is not None else None
            ),
            "rebalancing": (
                self.rebalancing.value
                if self.rebalancing is not None else None
            ),
            "position_sizing": (
                self.position_sizing.value
                if self.position_sizing is not None else None
            ),
        }


@dataclass(frozen=True)
class ScenarioSpec:
    """A scenario fork: who is the parent, what is the diff, what kind,
    what access mode.

    The spec's canonical hash binds (parent_run_id + kind + access_mode +
    diff) and is the run_id of the child scenario. Same as RL1-03:
    immutable canonical payload, no server metadata inside.
    """
    parent_run_id: str
    kind: ScenarioKind
    access_mode: ScenarioAccessMode
    diff: ScenarioDiff = field(default_factory=ScenarioDiff)
    proposed_by: str = "operator"
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "parent_run_id": self.parent_run_id,
            "kind": self.kind.value,
            "access_mode": self.access_mode.value,
            "diff": self.diff.to_dict(),
            "proposed_by": self.proposed_by,
            "notes": self.notes,
        }


def scenario_spec_hash(spec: ScenarioSpec) -> str:
    """Stable sha256 over the canonicalised ScenarioSpec (the child
    run_id). Independent of the parent's identity — the parent's
    identity is captured at *attach* time as ParentRef, NOT at hash
    time, so the hash binds the SCENARIO INPUT not the parent's
    evolution.
    """
    payload = canonical(spec.to_dict())
    return hashlib.sha256(payload).hexdigest()


def scenario_diff_sha256(spec: ScenarioSpec) -> str:
    """The diff alone — used as the scenario's identity surface on the
    child's stored result (``scenario_diff_sha256`` field). Equal to
    the empty-string sha256 ONLY when the diff is fully empty
    (ScenarioDiff.is_empty), which is the SCENARIO_NOT_A_FORK
    acceptance oracle ('unmodified fork reproduces parent').
    """
    payload = canonical(spec.diff.to_dict())
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "SCENARIO_DIFF_FIELDS",
    "ScenarioAccessMode",
    "ScenarioDiff",
    "ScenarioKind",
    "ScenarioSpec",
    "scenario_diff_sha256",
    "scenario_spec_hash",
]
