"""Scenario refusal reason codes (wire-visible; the SPA renders these as
the honest blocker). Mirrors the ``research.plan.*`` namespace style:
every refusal is machine-readable and rendered in the comparison
workspace exactly like a plan refusal."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class ScenarioRefusal:
    code: str
    message: str


SCENARIO_NOT_A_FORK: Final = "research.scenario.not_a_fork"
SCENARIO_PARENT_MISSING: Final = "research.scenario.parent_missing"
SCENARIO_PARENT_CHANGED: Final = "research.scenario.parent_changed"
SCENARIO_MISSING_CAPABILITY: Final = "research.scenario.missing_capability"
SCENARIO_STRESS_UNSUPPORTED: Final = "research.scenario.stress_unsupported"

SCENARIO_REFUSAL_KINDS: Final[tuple[str, ...]] = (
    SCENARIO_NOT_A_FORK,
    SCENARIO_PARENT_MISSING,
    SCENARIO_PARENT_CHANGED,
    SCENARIO_MISSING_CAPABILITY,
    SCENARIO_STRESS_UNSUPPORTED,
)


__all__ = [
    "SCENARIO_MISSING_CAPABILITY",
    "SCENARIO_NOT_A_FORK",
    "SCENARIO_PARENT_CHANGED",
    "SCENARIO_PARENT_MISSING",
    "SCENARIO_REFUSAL_KINDS",
    "SCENARIO_STRESS_UNSUPPORTED",
    "ScenarioRefusal",
]
