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

from tree_options.research.comparison.drawdown import compute_drawdown
from tree_options.research.comparison.funded import run_funded_account
from tree_options.research.comparison.missingness import reason_broker_paper
from tree_options.research.comparison.pair import align_pair
from tree_options.research.comparison.plan import ComparisonPlan, resolve_plan
from tree_options.research.contracts import (
    ComparisonSpec,
    ResearchCandidate,
    ResearchEvidenceKind,
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
    excluded_out_of_window: int = 0  # adapter observations outside the declared window
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

    def to_wire(self) -> dict[str, Any]:
        """The ONE serialization boundary: ISO date keys and money
        strings everywhere — the exact shape that crosses HTTP and feeds
        the TS types (RL1-02: the first nonempty result died with a 500
        because ``datetime.date`` keys went straight into
        ``JSONResponse``; nonempty results must serialize)."""
        return {
            "spec": self.spec.to_dict(),
            "rejection": self.rejection,
            "candidates": [
                {
                    "candidate_id": s.candidate_id,
                    "candidate": s.candidate.to_dict(),
                    "rows_by_date": {
                        d.isoformat(): row
                        for d, row in s.rows_by_date.items()
                    },
                    "drawdown": {
                        d.isoformat(): cell
                        for d, cell in s.drawdown.items()
                    },
                    "fees_paid_total": str(s.fees_paid_total),
                    "excluded_out_of_window": s.excluded_out_of_window,
                    "sample_size": s.sample_size,
                    "sample_floor": s.sample_floor,
                    "sample_floor_met": s.sample_floor_met,
                    "rejection_reason": s.rejection_reason,
                    "final_ending_value": (str(s.final_ending_value)
                                            if s.final_ending_value is not None
                                            else None),
                }
                for s in self.candidates
            ],
            "paired_diff": self.paired_diff,
        }


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
                       plan: ComparisonPlan | None = None,
                       *args: Any, **kwargs: Any) -> tuple[list, list]:
    """Shadow-proxy adapter (RL-2).

    Reads the desk's EOD-deadline proxy marks for ``candidate`` and
    converts them into the comparison engine's ``executions`` /
    ``marks`` surface. The defense lives on the candidate's
    ``funded_history`` field; this adapter delivers ONLY if the
    candidate cleared the data-support gate at catalog time. Empty
    lists (no fabricated executions) are the right answer when
    the proxy produced no marks for the window.

    The catalog adapter is responsible for loading marks (read-only
    access to the desk evidence store). This hook returns empty
    lists until the desk-side read lands; the conversion shape is
    wired and tested in ``tests/research/test_shadow_proxy.py``
    against synthetic fixtures, so wiring the loader is a one-line
    change once desk shadow tables are available to read.
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
                          plan: ComparisonPlan | None = None,
                          *args: Any, **kwargs: Any) -> tuple[list, list]:
    """Synthetic/v1 fixture adapter — the RL-1 vertical slice.

    Loads the sha-pinned fixture (``data/research/fixtures/
    synthetic-v1.json``) and converts the candidate's declared series
    into executions + shared SPY marks. PERMANENTLY SYNTHETIC: the
    numbers are invented machinery-validation values, labeled as such
    on the candidate, the envelope, and every payload they reach. The
    knowledge cutoff is honored at fixture granularity: observations
    recorded after the cutoff instant are not knowable at it.
    """
    from datetime import datetime

    from tree_options.research.catalog.fixture_slice import load_fixture
    from tree_options.research.comparison.funded import (
        MarkObservation,
        TradeExecution,
    )

    doc = load_fixture()
    if doc is None:
        return [], []
    series = doc.get("candidates", {}).get(candidate.id)
    if series is None:
        return [], []
    cutoff = plan.cutoff if plan is not None else None
    if cutoff is not None:
        recorded = datetime.fromisoformat(doc["recorded_at"])
        if recorded > cutoff:
            return [], []  # the fixture postdates the knowledge cutoff
    executions = [
        TradeExecution(
            date=date.fromisoformat(e["date"]),
            symbol=e["symbol"],
            signed_quantity=int(e["signed_quantity"]),
            price=Decimal(str(e["price"])),
        )
        for e in series.get("executions", [])
    ]
    marks = [
        MarkObservation(date.fromisoformat(d), symbol, Decimal(str(p)))
        for symbol, series_marks in doc.get("marks", {}).items()
        for d, p in series_marks.items()
    ]
    return executions, marks


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

    The spec is BINDING (RL1-02): ``resolve_plan`` validates it and pins
    the calendar, contribution schedule, fee policy and cutoff. A refused
    plan rejects every candidate with the plan's reason — never a
    silently defaulted basis.
    """
    plan = resolve_plan(spec)
    summaries: list[CandidateSummary] = []
    baseline_run = None

    for cand in candidates:
        if not cand.plot_funded_account:
            # Data capability speaks, not the verdict: a PASS without a
            # reconstructable funded history cannot plot, and the reason
            # is the missing data (RL1-06).
            summaries.append(CandidateSummary(
                candidate_id=cand.id,
                candidate=cand,
                rejection_reason=(
                    f"no funded history ({cand.disposition.value}): "
                    + (cand.ineligibility_reason
                       or cand.funded_history_reason
                       or "funded series not reconstructable")
                ),
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

        # Candidate-level gates spoke first (their reasons are about the
        # candidate); a refused plan rejects everything that would
        # otherwise have run.
        if plan.refused:
            summaries.append(CandidateSummary(
                candidate_id=cand.id,
                candidate=cand,
                rejection_reason=plan.refusal_reason,
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

        raw_executions, raw_marks = adapter(cand, plan)
        executions, marks, excluded = _clip_to_window(
            raw_executions, raw_marks, plan)
        run = run_funded_account(
            candidate_id=cand.id,
            starting_capital=spec.starting_capital,
            calendar=plan.sessions,
            executions=executions,
            marks=marks,
            cashflows=plan.cashflows,
            fee_model=plan.fee_model,
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
            excluded_out_of_window=excluded,
            sample_size=len(run.rows),
            sample_floor_met=len(run.rows) >= SAMPLE_FLOOR,
            final_ending_value=run.final_nav,
        ))

    # Baseline run (same admission path as candidates — never a
    # privileged series).
    if baseline is not None and not plan.refused:
        if baseline.plot_funded_account:
            base_adapter = _ADAPTERS.get(baseline.evidence_kind)
            if base_adapter is not None:
                base_ex, base_marks = base_adapter(baseline, plan)
                base_ex, base_marks, _base_excluded = _clip_to_window(
                    base_ex, base_marks, plan)
                baseline_run = run_funded_account(
                    candidate_id=baseline.id,
                    starting_capital=spec.starting_capital,
                    calendar=plan.sessions,
                    executions=base_ex,
                    marks=base_marks,
                    cashflows=plan.cashflows,
                    fee_model=plan.fee_model,
                )

    paired_diff: dict[str, dict[str, dict[str, Any]]] = {}
    if baseline_run is not None:
        base_by_session = baseline_run.ending_value_by_session()
        for s in summaries:
            if s.candidate.plot_funded_account:
                cand_by_session: dict[date, Decimal] = {
                    d: Decimal(r["nav"])
                    for d, r in s.rows_by_date.items()
                    if r["nav"] is not None
                }
                series = align_pair(
                    s.candidate,
                    baseline,
                    cand_by_session,
                    base_by_session,
                    sessions=plan.sessions,
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
        rejection=plan.refusal_reason,
    )


def _clip_to_window(
    executions: list[Any],
    marks: list[Any],
    plan: ComparisonPlan,
) -> tuple[list[Any], list[Any], int]:
    """Drop adapter observations outside the declared window and count
    what was excluded — data beyond the requested basis is surfaced on
    the summary, never silently truncated into the comparison."""
    lo = plan.window_start
    hi = plan.window_end
    if lo is None or hi is None:  # refused plans never reach the engine body
        return executions, marks, 0
    clipped_ex = [e for e in executions if lo <= e.date <= hi]
    clipped_marks = [m for m in marks if lo <= m.date <= hi]
    excluded = ((len(executions) - len(clipped_ex))
                + (len(marks) - len(clipped_marks)))
    return clipped_ex, clipped_marks, excluded


__all__ = [
    "SAMPLE_FLOOR",
    "CandidateSummary",
    "ComparisonResult",
    "run_comparison",
]
