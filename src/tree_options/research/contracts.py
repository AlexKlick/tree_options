"""Typed contracts for the Research Lab (RL-1).

Mirrors the Decimal/string discipline of ``tree_options.desk.contracts``
(money is a string of decimal digits across the JSON boundary; the
client formats; this layer never rounds). The TS counterparts live in
``web/src/lib/types.ts``.

Handoff references:
- RL §4: capability matrix, comparability contract, common supported
  dates, comparability reason text.
- RL §6: three projection types — assumed-growth illustration,
  scenario ensemble, predictive forecast. Only the first is in RL-1
  scope; scenario ensemble lands in RL-2; predictive forecast lands in
  RL-3.
- RL §7: scientific study mode — hypothesis, estimand, exact strategy
  and data versions, cohort membership, registered vs exploratory,
  diagnostics, robustness, source artifacts, reproduction command.
- RL §8: typed contracts + missing-metric reason convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

# -- Enumerations ------------------------------------------------------------

class ResearchEvidenceKind(StrEnum):
    """How the candidate's evidence was produced.

    The 410-Gone routes (forecast/scenarios) are NOT evidence kinds —
    they are HTTP-level refusals enforced in ``research_view``.
    """

    SYNTHETIC_BACKTEST = "synthetic_backtest"   # backtest/equity.py synthetic/v1
    SHADOW_PROXY = "shadow_proxy"               # desk/shadows.py EOD-deadline proxy
    SEALED_CAMPAIGN = "sealed_campaign"         # artifacts/campaign-2026-09/<scope>/sealed-round.json
    PAPER_EXECUTION = "paper_execution"         # DESK_PAPER_DIR; reserved for RL-2
    BROKER_PAPER = "broker_paper"               # E5; explicit out-of-scope for RL-1


class ResearchRegistration(StrEnum):
    """The only forward/retrospective flag in the desk layer."""

    BEFORE_ENTRY_WINDOW_END = "before_entry_window_end"
    RETROSPECTIVE_BACKFILL = "retrospective_backfill"


# String values from docs/campaign-2026-09/REPORT.md + scripts/campaign/*.py.
# Closed enum: the desk layer does not emit these; only the campaign scripts do.
class ResearchDisposition(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    HOLD_STANDS = "HOLD-STANDS"
    DATA_GATED_NOT_RUN = "DATA-GATED-NOT-RUN"
    NOT_EVALUABLE = "NOT_EVALUABLE"
    WITHDRAWN = "WITHDRAWN"
    INSUFFICIENT_N = "INSUFFICIENT_N"
    INSUFFICIENT_COVERAGE = "INSUFFICIENT_COVERAGE"
    NOT_CANDIDATE = "NOT_CANDIDATE"
    DESCRIPTIVE_ONLY_NO_REGIME_SIGNAL = "DESCRIPTIVE-ONLY:NO-REGIME-SIGNAL"
    NOT_EVALUABLE_SEALED = "NOT_EVALUABLE-SEALED"


#: Dispositions whose candidate may plot a funded account curve.
PLOT_FUNDED_ALLOWED: frozenset[ResearchDisposition] = frozenset({
    ResearchDisposition.PASS,
    ResearchDisposition.HOLD_STANDS,
})


class CashflowTiming(StrEnum):
    BEGINNING_OF_PERIOD = "beginning_of_period"
    END_OF_PERIOD = "end_of_period"


class CostModelKind(StrEnum):
    FIVE_BP_FIXED = "five_bp_fixed"
    PASS_THROUGH = "pass_through"


class IdleCashPolicy(StrEnum):
    CASH_YIELDS_ZERO = "cash_yields_zero"


class Rebalancing(StrEnum):
    NONE = "none"
    MONTHLY = "monthly"


class PositionSizing(StrEnum):
    INTEGER = "integer"
    FRACTIONAL = "fractional"


class CollateralPolicy(StrEnum):
    NONE = "none"


class BorrowingPolicy(StrEnum):
    NONE = "none"


class PriceBasis(StrEnum):
    NOMINAL_PRETAX = "nominal_pretax"


class Currency(StrEnum):
    USD = "USD"


class ResearchRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# -- Records -----------------------------------------------------------------

@dataclass(frozen=True)
class ResearchCandidate:
    """One catalog entry: a registered study, a sealed family, or a
    shadow-proxy candidate."""

    id: str
    family: str
    version: str
    evidence_kind: ResearchEvidenceKind
    registration: ResearchRegistration
    disposition: ResearchDisposition
    plot_funded_account: bool
    supported_start: date | None
    supported_end: date | None
    artifact_hashes: dict[str, str] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    ineligibility_reason: str | None = None
    data_completeness: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    source_url: str = ""  # relative path inside the catalog

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "family": self.family,
            "version": self.version,
            "evidence_kind": self.evidence_kind.value,
            "registration": self.registration.value,
            "disposition": self.disposition.value,
            "plot_funded_account": self.plot_funded_account,
            "supported_start": self.supported_start.isoformat() if self.supported_start else None,
            "supported_end": self.supported_end.isoformat() if self.supported_end else None,
            "artifact_hashes": dict(self.artifact_hashes),
            "capabilities": list(self.capabilities),
            "ineligibility_reason": self.ineligibility_reason,
            "data_completeness": dict(self.data_completeness),
            "warnings": list(self.warnings),
            "source_url": self.source_url,
        }
        return d


@dataclass(frozen=True)
class ComparisonSpec:
    """RL §4 comparability contract.

    All fields are required by the operator UI; defaults are applied by
    the catalog adapter when the operator accepts the recommended
    starting-capital/cost-model pair.
    """

    candidate_ids: tuple[str, ...]
    starting_capital: Decimal
    common_start: date | None
    common_end: date | None
    cashflow_timing: CashflowTiming = CashflowTiming.BEGINNING_OF_PERIOD
    contribution_per_period: Decimal = Decimal("0")
    cost_model_kind: CostModelKind = CostModelKind.FIVE_BP_FIXED
    benchmark_candidate_id: str | None = None
    currency: Currency = Currency.USD
    price_basis: PriceBasis = PriceBasis.NOMINAL_PRETAX
    idle_cash_policy: IdleCashPolicy = IdleCashPolicy.CASH_YIELDS_ZERO
    rebalancing: Rebalancing = Rebalancing.NONE
    position_sizing: PositionSizing = PositionSizing.INTEGER
    collateral: CollateralPolicy = CollateralPolicy.NONE
    borrowing: BorrowingPolicy = BorrowingPolicy.NONE
    knowledge_cutoff: datetime | None = None  # ISO instant; reuses R2-03 separation
    proposed_by: str = "operator"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_ids": list(self.candidate_ids),
            "starting_capital": str(self.starting_capital),
            "common_start": self.common_start.isoformat() if self.common_start else None,
            "common_end": self.common_end.isoformat() if self.common_end else None,
            "cashflow_timing": self.cashflow_timing.value,
            "contribution_per_period": str(self.contribution_per_period),
            "cost_model_kind": self.cost_model_kind.value,
            "benchmark_candidate_id": self.benchmark_candidate_id,
            "currency": self.currency.value,
            "price_basis": self.price_basis.value,
            "idle_cash_policy": self.idle_cash_policy.value,
            "rebalancing": self.rebalancing.value,
            "position_sizing": self.position_sizing.value,
            "collateral": self.collateral.value,
            "borrowing": self.borrowing.value,
            "knowledge_cutoff": self.knowledge_cutoff.isoformat() if self.knowledge_cutoff else None,
            "proposed_by": self.proposed_by,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class ResearchRun:
    """Spooled comparison job. RL-1 leaves the body empty; the implementer
    fills it in once ``runstate.store`` lands (Step 5)."""

    id: str
    spec: ComparisonSpec
    spec_hash: str
    started_at: datetime
    status: ResearchRunStatus = ResearchRunStatus.QUEUED
    result_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "spec": self.spec.to_dict(),
            "spec_hash": self.spec_hash,
            "started_at": self.started_at.isoformat(),
            "status": self.status.value,
            "result_id": self.result_id,
            "error": self.error,
        }


@dataclass(frozen=True)
class EvidenceEnvelope:
    """RL §7 evidence drawer payload — every chart point's provenance.

    ``reproduction_command`` is the exact command a researcher runs to
    regenerate the point; ``source_artifacts`` are sha-pinned so a chart
    can never claim provenance it doesn't have.
    """

    candidate_id: str
    point_session: date | None
    hypothesis: str
    estimand: str
    exact_versions: dict[str, str]  # {strategy, data, miner, playbook}
    cohort_membership: tuple[str, ...]
    registered_or_exploratory: ResearchRegistration
    diagnostics: dict[str, Any] = field(default_factory=dict)
    robustness: tuple[str, ...] = ()
    source_artifacts: tuple[tuple[str, str], ...] = ()  # (path, sha256)
    reproduction_command: str = ""
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "point_session": self.point_session.isoformat() if self.point_session else None,
            "hypothesis": self.hypothesis,
            "estimand": self.estimand,
            "exact_versions": dict(self.exact_versions),
            "cohort_membership": list(self.cohort_membership),
            "registered_or_exploratory": self.registered_or_exploratory.value,
            "diagnostics": dict(self.diagnostics),
            "robustness": list(self.robustness),
            "source_artifacts": [
                {"path": p, "sha256": s} for (p, s) in self.source_artifacts
            ],
            "reproduction_command": self.reproduction_command,
            "warnings": list(self.warnings),
        }


__all__ = [
    "PLOT_FUNDED_ALLOWED",
    "BorrowingPolicy",
    "CashflowTiming",
    "CollateralPolicy",
    "ComparisonSpec",
    "CostModelKind",
    "Currency",
    "EvidenceEnvelope",
    "IdleCashPolicy",
    "PositionSizing",
    "PriceBasis",
    "Rebalancing",
    "ResearchCandidate",
    "ResearchDisposition",
    "ResearchEvidenceKind",
    "ResearchRegistration",
    "ResearchRun",
    "ResearchRunStatus",
]
