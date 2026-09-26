"""RL-2: Reproducible scenario branching (handoff §5 / §10).

Module map (alphabetical; import-side lazy where the engine might
form a cycle):

    contracts      ScenarioSpec, ScenarioKind, ScenarioAccessMode
    refusal_codes  SCENARIO_* refusal reason constants
    spec_io        scenario_from_dict(parent_run_id, payload)

The hot-path (``fork_parent_and_replay``) and the lineage helpers
are exposed via lazy import so this package's import-time graph
stays acyclic: importing ``tree_options.research.scenarios.contracts``
or ``tree_options.research.scenarios.spec_io`` must NOT pull in the
engine. The engine itself imports ``spec_from_dict`` from
``tree_options.research.spec_io``, which imports
``scenario_from_dict`` here. Keeping the engine out of the
``__init__`` is what makes that work.

The scenario engine reuses ``tree_options.research.comparison.run_comparison``
verbatim: a scenario IS a ComparisonSpec with a documented diff over a
parent result. The lineage records the parent's effective
``engine_sha256`` + ``input_snapshot_sha256`` + ``calendar_sha256``
so a child can refuse to publish if the parent's stored identity no
longer matches the live inputs (lineage != whatever the parent looks
like today).

RL-2 ships only the type-of-scenario C path (contribution / allocation
planning) as the primary use case. Type A (historical rule replay, e.g.
different cost model / sizing) routes through the same diff surface;
type B (conditional stress valuation for option shocks) is left
explicit as a typed refusal until the supported shock surface is
defined.
"""
from __future__ import annotations

from tree_options.research.scenarios.contracts import (
    SCENARIO_DIFF_FIELDS,
    ScenarioAccessMode,
    ScenarioDiff,
    ScenarioKind,
    ScenarioSpec,
    scenario_diff_sha256,
    scenario_spec_hash,
)
from tree_options.research.scenarios.refusal_codes import (
    SCENARIO_MISSING_CAPABILITY,
    SCENARIO_NOT_A_FORK,
    SCENARIO_PARENT_CHANGED,
    SCENARIO_PARENT_MISSING,
    SCENARIO_REFUSAL_KINDS,
    SCENARIO_STRESS_UNSUPPORTED,
    ScenarioRefusal,
)
from tree_options.research.scenarios.spec_io import scenario_from_dict

__all__ = [
    "SCENARIO_DIFF_FIELDS",
    "SCENARIO_MISSING_CAPABILITY",
    "SCENARIO_NOT_A_FORK",
    "SCENARIO_PARENT_CHANGED",
    "SCENARIO_PARENT_MISSING",
    "SCENARIO_REFUSAL_KINDS",
    "SCENARIO_STRESS_UNSUPPORTED",
    "ScenarioAccessMode",
    "ScenarioDiff",
    "ScenarioKind",
    "ScenarioRefusal",
    "ScenarioSpec",
    "scenario_diff_sha256",
    "scenario_from_dict",
    "scenario_spec_hash",
]


def __getattr__(name: str):
    """Lazy access for the engine + lineage surface — keeps the import
    graph acyclic. Access via ``from tree_options.research.scenarios
    import fork_parent_and_replay`` still works because the
    ``__getattr__`` proxy resolves on first attribute access.
    """
    if name in {
        "ForkOutcome", "fork_parent_and_replay",
        "PARENT_KIND", "ParentRef", "ChildRef",
        "store_parent_ref", "load_parent_ref",
        "attach_child", "list_children",
        "parent_changed", "parent_missing",
    }:
        from tree_options.research.scenarios import engine as _engine_mod
        from tree_options.research.scenarios import lineage as _lineage_mod
        g = {**_engine_mod.__dict__, **_lineage_mod.__dict__}
        value = g.get(name)
        if value is not None:
            return value
    raise AttributeError(f"module 'scenarios' has no attribute {name!r}")
