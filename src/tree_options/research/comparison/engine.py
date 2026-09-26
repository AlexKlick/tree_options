"""Comparison engine orchestrator.

Handoff §4 / §5 / §11 acceptance rows:
    * Cost / size changes — sizing is recomputed via
      ``FiveBasisPointFeeModel.affordable_quantity``; never linearly
      rescale a historical curve.
    * Missingness (priced-subset never prices whole) — cells where the
      priced-subset average is the only available price render as null,
      not as the priced subset scaled to the whole fill.
    * Dataset scope — synthetic, paper, sealed, shadow, broker-paper
      are distinct evidence kinds; the engine never produces a broker-
      paper number from a shadow proxy.
    * Inference / sample floor — the engine reports ``sample_floor`` and
      ``sample_floor_met`` on every per-candidate summary so the SPA can
      refuse to plot family-wide aggregates that haven't cleared the
      campaign's floor.

RL-1 scope: the engine runs the funded-account replay per candidate,
drawdown per candidate, and a pairwise common-support diff vs the
baseline. Forecast/scenario routes return 410 at the HTTP layer (see
``research_view``); the engine itself does not need to know about those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from tree_options.research.comparison.calendar import sessions_between
from tree_options.research.comparison.drawdown import compute_drawdown
from tree_options.research.comparison.funded import run_funded_account
from tree_options.research.comparison.missingness import (
    reason_broker_paper,
    reason_retrospective_only,
)
from tree_options.research.comparison.pair import align_pair
from tree_options.research.contracts import (
    ComparisonSpec,
    ResearchCandidate,
    ResearchEvidenceKind,
    ResearchRegistration,
)

#: Engine's per-candidate sample floor for "promotion_ready" semantics.
#: Mirrors the desk's ``MIN_RESOLVED = 20`` in scorecards.summarize.
SAMPLE_FLOOR = 20


@dataclass(frozen=True)
class CandidateSummary:
    """One candidate's contribution to the comparison result."""
    candidate_id: str
    candidate: ResearchCandidate
    rows_by_date: dict[date, dict[str, Any]] = field(default_factory=dict)
    drawdown: dict[date, dict[str, Any]] = field(default_factory=dict)
    fees_paid_total: Decimal = Decimal("0")
    sample_size: int = 0
    sample_floor: int = SAMPLE_FLOOR
    sample_floor_met: bool = False
    rejection_reason: str | None = None
    final_ending_value: Decimal | None = None  # populated when plot_funded_account=False


@dataclass(frozen=True)
class ComparisonResult:
    """The full output of ``run_comparison``."""
    spec: ComparisonSpec
    candidates: tuple[CandidateSummary, ...]
    paired_diff: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    rejection: str | None = None


# -- Adapters: candidate → execution series ---------------------------------
#
# Adapter contract: one adapter per evidence kind, returning the raw
# observation streams for the funded replay —
#     (executions, marks) -> tuple[list[TradeExecution], list[MarkObservation]]
# The RESOLVED plan (window, calendar, cutoff, costs) governs how the
# engine admits them; adapters only surface what the evidence contains.
# An adapter that finds nothing returns ([], []) and the summary reports
# zero observations — never a fabricated curve.


def _shadow_executions(candidate: ResearchCandidate,
                       *args: Any, **kwargs: Any) -> tuple[list, list]:
    """Adapter stub for ``evidence_kind=SHADOW_PROXY``.

    The desk's EOD-deadline proxy marks are NOT execution evidence (RL
    §4 dataset scope); wiring them as funded history is refused until a
    real adapter exists that can defend the conversion.
    """
    return [], []


def _sealed_executions(candidate: ResearchCandidate,
                      *args: Any, **kwargs: Any) -> tuple[list, list]:
    """Adapter stub for ``evidence_kind=SEALED_CAMPAIGN``.

    Sealed trials are per-trial dispatch records, not a reconstructable
    daily portfolio history; no funded series is derivable from them
    (the honest data blocker lives on the candidate).
    """
    return [], []


def _synthetic_executions(candidate: ResearchCandidate,
                          *args: Any, **kwargs: Any) -> tuple[list, list]:
    """Adapter stub for ``evidence_kind=SYNTHETIC_BACKTEST``.

    The synthetic/v1 vertical slice (permanently labeled synthetic)
    lands with the fixture loader.
    """
    return [], []


_ADAPTERS = {
    ResearchEvidenceKind.SHADOW_PROXY: _shadow_executions,
    ResearchEvidenceKind.SEALED_CAMPAIGN: _sealed_executions,
    ResearchEvidenceKind.SYNTHETIC_BACKTEST: _synthetic_executions,
}


# -- Public engine -----------------------------------------------------------


def run_comparison(
    spec: ComparisonSpec,
    candidates: tuple[ResearchCandidate, ...],
    *,
    baseline: ResearchCandidate | None = None,
    ledgerbook=None,  # reserved — Step 5 pluggable harness-state store
) -> ComparisonResult:
    """Run a funded-account comparison of ``candidates`` against
    ``baseline`` (when supplied) under ``spec``.

    The orchestrator:
        1. Filters candidates to ``plot_funded_account=True``; the rest
           produce a summary with ``rejection_reason`` populated and no
           rows (per RL §4 ineligible candidates still render reasons,
           not invented series).
        2. Resolves each candidate's executions through its
           ``evidence_kind``-specific adapter.
        3. Runs ``run_funded_account`` + ``compute_drawdown`` per
           candidate.
        4. Pairs each candidate against ``baseline`` over the common
           supported window using ``align_pair``.

    The four-line decomposition is enforced inside
    ``run_funded_account`` via ``LedgerBook.assert_conservation``.
    """
    summaries: list[CandidateSummary] = []
    baseline_run = None

    for cand in candidates:
        if not cand.plot_funded_account:
            summaries.append(CandidateSummary(
                candidate_id=cand.id,
                candidate=cand,
                rejection_reason=(
                    f"ineligible disposition: {cand.disposition.value}"
                    + (f" — {cand.ineligibility_reason}"
                       if cand.ineligibility_reason else "")
                ),
            ))
            continue

        if cand.registration is ResearchRegistration.RETROSPECTIVE_BACKFILL:
            summaries.append(CandidateSummary(
                candidate_id=cand.id,
                candidate=cand,
                rejection_reason=reason_retrospective_only(cand.family).code,
            ))
            continue

        if cand.evidence_kind in (
            ResearchEvidenceKind.PAPER_EXECUTION,
            ResearchEvidenceKind.BROKER_PAPER,
        ):
            summaries.append(CandidateSummary(
                candidate_id=cand.id,
                candidate=cand,
                rejection_reason=reason_broker_paper().code,
            ))
            continue

        adapter = _ADAPTERS.get(cand.evidence_kind)
        if adapter is None:
            summaries.append(CandidateSummary(
                candidate_id=cand.id,
                candidate=cand,
                rejection_reason="research.adapter_missing",
            ))
            continue

        executions, marks = adapter(cand)
        run = run_funded_account(
            candidate_id=cand.id,
            starting_capital=spec.starting_capital,
            calendar=sessions_between(spec.common_start, spec.common_end),
            executions=executions,
            marks=marks,
            cashflows=[],  # contribution schedule lands with the resolved plan (RL1-02)
        )
        if run.refusal_reason is not None:
            summaries.append(CandidateSummary(
                candidate_id=cand.id,
                candidate=cand,
                rejection_reason=run.refusal_reason,
            ))
            continue
        dd = compute_drawdown(
            candidate_id=cand.id,
            registration=cand.registration,
            ending_value_by_session=run.ending_value_by_session(),
        )

        rows_by_date = {
            r.date: {
                "cash": str(r.cash),
                "inventory": [[sym, qty] for sym, qty in r.inventory],
                "marked_value": str(r.marked_value) if r.marked_value is not None else None,
                "nav": str(r.nav) if r.nav is not None else None,
                "contributions_cum": str(r.contributions_cum),
                "withdrawals_cum": str(r.withdrawals_cum),
                "fees_cum": str(r.fees_cum),
                "realized_pnl_cum": str(r.realized_pnl_cum),
                "investment_gain": (str(r.investment_gain)
                                    if r.investment_gain is not None else None),
                "missing_mark_symbols": list(r.missing_mark_symbols),
            }
            for r in run.rows
        }
        drawdown_by_date = {
            dd_cell.date: {
                "drawdown_dollar": str(dd_cell.drawdown_dollar),
                "drawdown_pct": str(dd_cell.drawdown_pct),
                "recovery_end_date": dd_cell.recovery_end_date.isoformat() if dd_cell.recovery_end_date else None,
            }
            for dd_cell in dd.cells
        }
        summaries.append(CandidateSummary(
            candidate_id=cand.id,
            candidate=cand,
            rows_by_date=rows_by_date,
            drawdown=drawdown_by_date,
            fees_paid_total=run.total_fees,
            sample_size=len(run.rows),
            sample_floor_met=len(run.rows) >= SAMPLE_FLOOR,
            final_ending_value=run.final_nav,
        ))

    # Baseline run (same admission path as candidates — never a
    # privileged series).
    if baseline is not None:
        if baseline.plot_funded_account:
            base_adapter = _ADAPTERS.get(baseline.evidence_kind)
            if base_adapter is not None:
                base_ex, base_marks = base_adapter(baseline)
                baseline_run = run_funded_account(
                    candidate_id=baseline.id,
                    starting_capital=spec.starting_capital,
                    calendar=sessions_between(spec.common_start, spec.common_end),
                    executions=base_ex,
                    marks=base_marks,
                    cashflows=[],
                )

    paired_diff: dict[str, dict[str, dict[str, Any]]] = {}
    if baseline_run is not None:
        base_by_session = baseline_run.ending_value_by_session()
        for s in summaries:
            if s.candidate.plot_funded_account:
                cand_by_session: dict[date, Decimal] = {}
                for d, r in s.rows_by_date.items():
                    cand_by_session[d] = Decimal(r["ending_value"])
                series = align_pair(
                    s.candidate,
                    baseline,
                    cand_by_session,
                    base_by_session,
                )
                paired_diff[s.candidate_id] = {
                    pc.date.isoformat(): {
                        "value": str(pc.value) if pc.value is not None else None,
                        "reason": pc.reason.code if pc.reason is not None else None,
                    }
                    for pc in series.cells
                }

    return ComparisonResult(
        spec=spec,
        candidates=tuple(summaries),
        paired_diff=paired_diff,
        rejection=None,
    )


__all__ = [
    "SAMPLE_FLOOR",
    "CandidateSummary",
    "ComparisonResult",
    "run_comparison",
]
