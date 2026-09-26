"""Scenario engine — fork a parent ComparisonResult and replay a diff.

The engine is THIN: the handoff's RL-2 contract is that a scenario is
a documented fork and the comparison engine does the actual work.

``fork_parent_and_replay``:
    1. Reads the parent's stored ``result`` envelope from the store.
    2. Verifies the parent's effective identity still matches the
       attach-time ``ParentRef`` (lineage honesty).
    3. Refuses with a ``ScenarioRefusal`` for:
        * missing/pending/failed parent;
        * parent identity mismatch (parent_changed);
        * request for the type-B stress surface (RL-2 ships no real
          shock engine — it documents the gap, per handoff §5B and
          the "implement the control or refuse it explicitly" rule
          in comparison/plan.py);
        * scenario diff that would re-tune a retired candidate
          (``plot_funded_account == False`` while the diff is a
          funding / cost / rebalancing change).
    4. Constructs a rewritten ``ComparisonSpec`` by applying the diff
       to the parent's spec. The rewritten spec passes through every
       unchanged field as the parent declared it.
    5. Calls ``run_comparison(...)`` once (the bounded worker already
       encloses this in its single-thread loop).
    6. Returns the rewritten spec + the comparison result + the
       accepted/refused parent ref so the worker can persist them.

The engine NEVER re-runs the parent. The parent's stored result is
the source of truth; the child's job is to surface the diff the
parent's stored inputs would produce under different assumptions.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tree_options.research.comparison.engine import run_comparison
from tree_options.research.contracts import (
    ComparisonSpec,
    PositionSizing,
    Rebalancing,
)
from tree_options.research.scenarios.contracts import (
    ScenarioDiff,
    ScenarioKind,
    ScenarioSpec,
)
from tree_options.research.scenarios.lineage import (
    ParentRef,
    parent_changed,
    parent_missing,
)
from tree_options.research.scenarios.refusal_codes import (
    SCENARIO_MISSING_CAPABILITY,
    SCENARIO_NOT_A_FORK,
    SCENARIO_STRESS_UNSUPPORTED,
    ScenarioRefusal,
)
from tree_options.research.spec_io import spec_from_dict


@dataclass(frozen=True)
class ForkOutcome:
    """The result of attempting to fork. EITHER ``refusal`` is set
    (no work was done) OR ``rewritten_spec``/``result`` are set.

    ``parent_ref`` is the parent's CURRENT identity once the envelope
    is well-formed (the worker persists it on first fork; immutable
    thereafter). It is ``None`` for the earliest refusals (missing
    parent / malformed envelope) where no identity could be bound.
    """
    spec: ScenarioSpec
    parent_ref: ParentRef | None
    rewritten_spec: ComparisonSpec | None
    refusal: ScenarioRefusal | None
    result: Any  # ComparisonResult | None when refused


def fork_parent_and_replay(
    store: Any,
    *,
    scenario: ScenarioSpec,
    parent_result_envelope: dict[str, Any],
    catalog_provider: Callable[[], list],
    engine_fn: Callable[..., Any] = run_comparison,
    attach_ref: ParentRef | None = None,
) -> ForkOutcome:
    """Fork ``scenario`` over its parent and return ``ForkOutcome``.

    ``attach_ref`` is the ParentRef stored at FIRST attach time (read
    back via ``lineage.load_parent_ref``). When present it is the
    comparison baseline for lineage honesty: the parent's CURRENT
    envelope identity must still match what it was at attach, or the
    fork refuses with ``SCENARIO_PARENT_CHANGED``. When ``None``
    (first fork of this parent) no drift comparison is possible and
    the caller persists the returned ``parent_ref`` as the attach-time
    record — comparing the envelope against a ref built from itself
    would be a no-op, which is exactly the defect this parameter
    exists to prevent.

    The caller (the bounded worker, see ``ResearchWorker`` in
    ``tree_options.research.runstate.worker``) is responsible for
    persisting the outcome — including the *parent_ref* on first fork.
    """
    parent_status = parent_missing(parent_result_envelope)
    if parent_status is not None:
        return ForkOutcome(
            spec=scenario, parent_ref=None, rewritten_spec=None,
            refusal=parent_status, result=None,
        )

    parent_engine_sha = parent_result_envelope.get("engine_sha256")
    parent_input_sha = parent_result_envelope.get("input_snapshot_sha256")
    parent_calendar_sha = parent_result_envelope.get("calendar_sha256")
    parent_spec_hash = parent_result_envelope.get("spec_hash")
    if not all(isinstance(x, str) and x for x in (
            parent_engine_sha, parent_input_sha,
            parent_calendar_sha, parent_spec_hash)):
        return ForkOutcome(
            spec=scenario, parent_ref=None, rewritten_spec=None,
            refusal=ScenarioRefusal(
                code=SCENARIO_NOT_A_FORK,
                message="parent result envelope is missing one of "
                        "{engine_sha256, input_snapshot_sha256, "
                        "calendar_sha256, spec_hash}; refuse to fork",
            ),
            result=None,
        )

    parent_ref = ParentRef(
        parent_run_id=scenario.parent_run_id,
        parent_spec_hash=str(parent_spec_hash),
        parent_engine_sha256=str(parent_engine_sha),
        parent_input_snapshot_sha256=str(parent_input_sha),
        parent_calendar_sha256=str(parent_calendar_sha),
    )

    # -- lineage honesty: the parent's CURRENT identity still matches --
    # -- what it was at attach time (the stored ParentRef), not itself --
    if attach_ref is not None:
        changed = parent_changed(parent_result_envelope, attach_ref)
        if changed is not None:
            return ForkOutcome(
                spec=scenario, parent_ref=attach_ref,
                rewritten_spec=None, refusal=changed, result=None,
            )

    # -- type B stress: explicit refusal until the shock surface ships --
    if scenario.kind is ScenarioKind.CONDITIONAL_STRESS:
        return ForkOutcome(
            spec=scenario, parent_ref=parent_ref, rewritten_spec=None,
            refusal=ScenarioRefusal(
                code=SCENARIO_STRESS_UNSUPPORTED,
                message=("RL-2 ships no option-valuation shock engine; "
                         "the type-B surface (underlying move, IV move in "
                         "absolute percentage points, term/slip) is left "
                         "as a documented gap"),
            ),
            result=None,
        )

    # -- rewrite the spec: parent.diff applied, rest inherited verbatim --
    parent_spec_payload = store.get("spec", scenario.parent_run_id)
    if parent_spec_payload is None:
        return ForkOutcome(
            spec=scenario, parent_ref=parent_ref, rewritten_spec=None,
            refusal=ScenarioRefusal(
                code=SCENARIO_NOT_A_FORK,
                message=("parent run has no stored spec payload; the "
                         "fork cannot be reconstructed"),
            ),
            result=None,
        )
    parent_spec = spec_from_dict(parent_spec_payload)
    rewritten = _apply_diff(parent_spec, scenario.diff)
    if not _diff_is_legal(rewritten):
        return ForkOutcome(
            spec=scenario, parent_ref=parent_ref, rewritten_spec=None,
            refusal=ScenarioRefusal(
                code=SCENARIO_MISSING_CAPABILITY,
                message=("the requested diff would require unsupported "
                         "controls (fractional sizing, monthly rebalancing); "
                         "RL-2 implements NONE/INTEGER-only"),
            ),
            result=None,
        )

    # -- missing-capability gate: a funding scenario needs plottable
    # -- candidates, never retired-data ones
    catalog = {c.id: c for c in catalog_provider()}
    missing = [cid for cid in rewritten.candidate_ids
               if cid not in catalog]
    if missing:
        return ForkOutcome(
            spec=scenario, parent_ref=parent_ref,
            rewritten_spec=rewritten, refusal=ScenarioRefusal(
                code=SCENARIO_MISSING_CAPABILITY,
                message=(f"candidates left the catalog since the parent "
                         f"was run: {missing}"),
            ), result=None,
        )
    requested_funding = (
        scenario.kind is ScenarioKind.CONTRIBUTION_PLANNING
        or scenario.diff.contribution_per_period is not None
        or scenario.diff.cost_model_kind is not None
    )
    if requested_funding:
        unplottable = [
            cid for cid in rewritten.candidate_ids
            if not catalog[cid].plot_funded_account
        ]
        if unplottable:
            return ForkOutcome(
                spec=scenario, parent_ref=parent_ref,
                rewritten_spec=rewritten, refusal=ScenarioRefusal(
                    code=SCENARIO_MISSING_CAPABILITY,
                    message=(f"requested scenario diff affects funded "
                             f"accounting but these candidates have no "
                             f"plottable history: {unplottable}"),
                ), result=None,
            )

    # A refused plan is a value too (RL1-02): the comparison surface
    # carries the plan's refusal reason on every candidate row, so we
    # pass it through rather than fail-loud at this layer. The plan is
    # resolved inside ``run_comparison`` (which the engine calls
    # below); no pre-resolve here.
    cands = tuple(catalog[cid] for cid in rewritten.candidate_ids)
    baseline = None
    if rewritten.benchmark_candidate_id:
        baseline = catalog[rewritten.benchmark_candidate_id]
    result = engine_fn(rewritten, cands, baseline=baseline)
    return ForkOutcome(
        spec=scenario, parent_ref=parent_ref,
        rewritten_spec=rewritten, refusal=None, result=result,
    )


def _apply_diff(parent: ComparisonSpec, diff: ScenarioDiff) -> ComparisonSpec:
    """Build a rewritten ComparisonSpec: every None field inherits the
    parent verbatim; every set field overrides it. The returned spec
    is a fresh, frozen dataclass — never the parent."""
    kwargs: dict[str, Any] = {
        "candidate_ids": parent.candidate_ids,
        "starting_capital": parent.starting_capital,
        "common_start": parent.common_start,
        "common_end": parent.common_end,
        "cashflow_timing": (
            diff.cashflow_timing
            if diff.cashflow_timing is not None
            else parent.cashflow_timing
        ),
        "contribution_per_period": (
            diff.contribution_per_period
            if diff.contribution_per_period is not None
            else parent.contribution_per_period
        ),
        "cost_model_kind": (
            diff.cost_model_kind
            if diff.cost_model_kind is not None
            else parent.cost_model_kind
        ),
        "benchmark_candidate_id": parent.benchmark_candidate_id,
        "currency": parent.currency,
        "price_basis": parent.price_basis,
        "idle_cash_policy": parent.idle_cash_policy,
        "rebalancing": (
            diff.rebalancing
            if diff.rebalancing is not None
            else parent.rebalancing
        ),
        "position_sizing": (
            diff.position_sizing
            if diff.position_sizing is not None
            else parent.position_sizing
        ),
        "collateral": parent.collateral,
        "borrowing": parent.borrowing,
        "knowledge_cutoff": parent.knowledge_cutoff,
        "proposed_by": parent.proposed_by,
        "notes": parent.notes,
    }
    return ComparisonSpec(**kwargs)


def _diff_is_legal(spec: ComparisonSpec) -> bool:
    """The diff surface is intentionally narrow (SCENARIO_DIFF_FIELDS).
    Any candidate whose rewritten spec calls an unsupported control
    is refused here before the engine runs — this is RL-2's analogue
    of the comparison plan's "implement the control or refuse it
    explicitly" rule."""
    return (spec.position_sizing is PositionSizing.INTEGER
            and spec.rebalancing is Rebalancing.NONE)


__all__ = ["ForkOutcome", "fork_parent_and_replay"]
