"""Resolved comparison plans — the spec becomes binding (RL1-02).

The 2026-09-25 audit reproduced a ``ComparisonSpec`` that was hashed,
stored, and then ignored: adapters ran without it, ``cashflows=[]`` was
hardwired, requested windows, cutoffs, contribution schedules and cost
models had no effect. A field in a dataclass is not implemented
behavior.

``resolve_plan`` turns a spec into everything the engine actually uses:

    * the DECLARED window — both bounds required, ordered, and covered
      by at least one pinned session (``sessions`` comes from the
      sha-pinned XNYS calendar, never from observed trade dates);
    * the resolved contribution schedule (monthly periods; first or
      last session of each month per ``cashflow_timing``);
    * the fee policy (``five_bp_fixed`` -> the five-basis-point model;
      ``pass_through`` -> explicit per-execution fees only);
    * the knowledge cutoff, which must be timezone-aware when present.

RULE: implement the control or refuse it explicitly. Anything the plan
cannot honor (monthly rebalancing, fractional sizing, an unsorted
window, a naive cutoff) is a machine-readable refusal — never a field
that gets hashed and silently dropped. Refused plans carry no sessions
and no schedule; the engine renders the refusal on every candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from tree_options.backtest.equity import FiveBasisPointFeeModel
from tree_options.research.comparison.calendar import calendar_sha256, sessions_between
from tree_options.research.comparison.funded import CashflowEvent
from tree_options.research.contracts import (
    CashflowTiming,
    ComparisonSpec,
    CostModelKind,
    PositionSizing,
    Rebalancing,
)

#: Refusal codes (rendered by the SPA as the honest blocker).
REFUSAL_WINDOW_REQUIRED = "research.plan.window_required"
REFUSAL_INVALID_WINDOW = "research.plan.invalid_window"
REFUSAL_NO_SESSIONS = "research.plan.window_has_no_sessions"
REFUSAL_NEGATIVE_CONTRIBUTION = "research.plan.negative_contribution"
REFUSAL_CUTOFF_NEEDS_TIMEZONE = "research.plan.cutoff_needs_timezone"
REFUSAL_UNSUPPORTED_REBALANCING = "research.plan.unsupported_rebalancing"
REFUSAL_UNSUPPORTED_SIZING = "research.plan.unsupported_sizing"


@dataclass(frozen=True)
class ComparisonPlan:
    """Everything ``run_comparison`` needs that the spec only names."""
    spec: ComparisonSpec
    sessions: tuple[date, ...] = ()
    window_start: date | None = None
    window_end: date | None = None
    cashflows: tuple[CashflowEvent, ...] = ()
    fee_model: FiveBasisPointFeeModel | None = None
    cost_model_kind: CostModelKind | None = None
    cutoff: datetime | None = None
    calendar_sha256: str = ""
    refusal_reason: str | None = None
    refusal_detail: str | None = None

    @property
    def refused(self) -> bool:
        return self.refusal_reason is not None


def resolve_plan(spec: ComparisonSpec) -> ComparisonPlan:
    """Validate the spec against what the engine actually implements
    and pin the resolved schedule. Never raises: a refused plan is a
    value, so the engine (and the HTTP layer) can surface the reason.
    """
    calendar_id = calendar_sha256()

    # -- controls that must be implemented or refused --------------------
    if spec.rebalancing is not Rebalancing.NONE:
        return _refusal(spec, REFUSAL_UNSUPPORTED_REBALANCING,
                        f"rebalancing={spec.rebalancing.value} is not implemented",
                        calendar_id)
    if spec.position_sizing is not PositionSizing.INTEGER:
        return _refusal(spec, REFUSAL_UNSUPPORTED_SIZING,
                        f"position_sizing={spec.position_sizing.value} is not implemented",
                        calendar_id)

    # -- window ------------------------------------------------------------
    if spec.common_start is None or spec.common_end is None:
        return _refusal(spec, REFUSAL_WINDOW_REQUIRED,
                        "a comparison must declare both common_start and common_end "
                        "(the common basis is the point of the exercise)",
                        calendar_id)
    if spec.common_start > spec.common_end:
        return _refusal(spec, REFUSAL_INVALID_WINDOW,
                        f"common_start {spec.common_start} is after common_end "
                        f"{spec.common_end}",
                        calendar_id)
    sessions = sessions_between(spec.common_start, spec.common_end)
    if not sessions:
        return _refusal(spec, REFUSAL_NO_SESSIONS,
                        f"no declared sessions between {spec.common_start} and "
                        f"{spec.common_end}",
                        calendar_id)

    # -- contribution schedule ----------------------------------------------
    if spec.contribution_per_period < 0:
        return _refusal(spec, REFUSAL_NEGATIVE_CONTRIBUTION,
                        f"contribution_per_period {spec.contribution_per_period} < 0",
                        calendar_id)
    cashflows = _contribution_schedule(
        spec.contribution_per_period, sessions, spec.cashflow_timing)

    # -- knowledge cutoff ----------------------------------------------------
    if spec.knowledge_cutoff is not None and spec.knowledge_cutoff.tzinfo is None:
        return _refusal(spec, REFUSAL_CUTOFF_NEEDS_TIMEZONE,
                        "knowledge_cutoff must be timezone-aware (an instant, "
                        "not a wall-clock ambiguity)",
                        calendar_id)

    # -- cost model -----------------------------------------------------------
    fee_model = (FiveBasisPointFeeModel()
                 if spec.cost_model_kind is CostModelKind.FIVE_BP_FIXED
                 else None)  # pass_through: explicit per-execution fees only

    return ComparisonPlan(
        spec=spec,
        sessions=sessions,
        window_start=spec.common_start,
        window_end=spec.common_end,
        cashflows=cashflows,
        fee_model=fee_model,
        cost_model_kind=spec.cost_model_kind,
        cutoff=spec.knowledge_cutoff,
        calendar_sha256=calendar_id,
    )


def _refusal(spec: ComparisonSpec, code: str, detail: str,
             calendar_id: str) -> ComparisonPlan:
    return ComparisonPlan(
        spec=spec, calendar_sha256=calendar_id,
        refusal_reason=code, refusal_detail=detail,
    )


def _contribution_schedule(
    per_period: Decimal,
    sessions: tuple[date, ...],
    timing: CashflowTiming,
) -> tuple[CashflowEvent, ...]:
    """Monthly periods over the declared sessions: beginning-of-period
    contributes on each month's FIRST declared session, end-of-period on
    its LAST. A zero contribution yields no events. (Period = calendar
    month, the handoff's representative "$500 monthly contributions".)
    """
    if per_period == 0:
        return ()
    by_month: dict[tuple[int, int], list[date]] = {}
    for s in sessions:
        by_month.setdefault((s.year, s.month), []).append(s)
    flow_dates = (
        dates[0] if timing is CashflowTiming.BEGINNING_OF_PERIOD else dates[-1]
        for dates in by_month.values()
    )
    return tuple(
        CashflowEvent(date=d, amount=per_period) for d in sorted(flow_dates)
    )


__all__ = [
    "REFUSAL_CUTOFF_NEEDS_TIMEZONE",
    "REFUSAL_INVALID_WINDOW",
    "REFUSAL_NEGATIVE_CONTRIBUTION",
    "REFUSAL_NO_SESSIONS",
    "REFUSAL_UNSUPPORTED_REBALANCING",
    "REFUSAL_UNSUPPORTED_SIZING",
    "REFUSAL_WINDOW_REQUIRED",
    "ComparisonPlan",
    "resolve_plan",
]
