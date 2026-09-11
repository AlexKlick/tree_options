"""M6 reconciliation: adjudicate the reasons a lifecycle retained.

The lifecycle NEVER resolves a contradiction — it retains every anomaly as a
machine-readable ``ReconciliationReason`` and keeps the effective state fail
closed.  This module is the adjudication layer over those retained reasons:
it classifies each reason by WHAT THE DISCREPANCY POISONS (fill economics,
the terminal-outcome claim, the order's identity, or local-action protocol),
attaches a deterministic human-readable explanation, and orders findings
worst-first.  It is classification and explanation ONLY — an adjudicated
report still never edits, drops, or "fixes" a single retained reason.

Conventions pinned here: the severity mapping is TOTAL (every
``ReconciliationReason`` maps to exactly one severity — an unmapped reason is
a construction error, not a silent default); findings sort by severity rank
(worst first) then reason name, so reports are byte-deterministic; and a
report with zero findings is ``is_clean`` — cleanliness is never graded, it
is binary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tree_options.execution.lifecycle import (
    ExecutionLifecycle,
    ExecutionState,
    ReconciliationReason,
)


class ReconciliationSeverity(StrEnum):
    """What class of trust a retained reason poisons — worst first."""

    ECONOMIC = "ECONOMIC"
    TERMINAL = "TERMINAL"
    IDENTITY = "IDENTITY"
    PROTOCOL = "PROTOCOL"


#: Worst-first rank for deterministic finding order.
_SEVERITY_RANK: dict[ReconciliationSeverity, int] = {
    ReconciliationSeverity.ECONOMIC: 0,
    ReconciliationSeverity.TERMINAL: 1,
    ReconciliationSeverity.IDENTITY: 2,
    ReconciliationSeverity.PROTOCOL: 3,
}

#: The total severity mapping.  An entry per enum member, no defaults.
_REASON_SEVERITY: dict[ReconciliationReason, ReconciliationSeverity] = {
    ReconciliationReason.FILL_ECONOMIC_GAP: ReconciliationSeverity.ECONOMIC,
    ReconciliationReason.FILL_ECONOMIC_OVERLAP: ReconciliationSeverity.ECONOMIC,
    ReconciliationReason.FILL_CUMULATIVE_REGRESSION: ReconciliationSeverity.ECONOMIC,
    ReconciliationReason.FILL_KIND_TOTAL_MISMATCH: ReconciliationSeverity.ECONOMIC,
    ReconciliationReason.FILL_EXCEEDS_CONFIRMED_TOTAL: ReconciliationSeverity.ECONOMIC,
    ReconciliationReason.READBACK_FILL_CONTRADICTION: ReconciliationSeverity.ECONOMIC,
    ReconciliationReason.TERMINAL_FACT_CONTRADICTION: ReconciliationSeverity.TERMINAL,
    ReconciliationReason.READBACK_CUMULATIVE_REGRESSION: ReconciliationSeverity.TERMINAL,
    ReconciliationReason.REJECT_WITH_OBSERVED_FILL: ReconciliationSeverity.TERMINAL,
    ReconciliationReason.BROKER_ORDER_ID_CONFLICT: ReconciliationSeverity.IDENTITY,
    ReconciliationReason.AMBIGUOUS_READBACK: ReconciliationSeverity.IDENTITY,
    ReconciliationReason.AMBIGUOUS_REPLACE_CONFIRMATION: ReconciliationSeverity.IDENTITY,
    ReconciliationReason.MISSING_SUBMIT: ReconciliationSeverity.PROTOCOL,
    ReconciliationReason.TOTAL_UNCONFIRMED: ReconciliationSeverity.PROTOCOL,
    ReconciliationReason.RETRY_AFTER_LOCAL_KNOWLEDGE: ReconciliationSeverity.PROTOCOL,
    ReconciliationReason.OVERLAPPING_REPLACE_INTENTS: ReconciliationSeverity.PROTOCOL,
    ReconciliationReason.INVALID_REPLACE_BASIS: ReconciliationSeverity.PROTOCOL,
    ReconciliationReason.REPLACE_CONFIRMATION_MISMATCH: ReconciliationSeverity.PROTOCOL,
    ReconciliationReason.UNEXPECTED_CONFIRMED_TOTAL_CHANGE: ReconciliationSeverity.PROTOCOL,
}

#: Deterministic one-line explanations, one per reason.
_REASON_EXPLANATIONS: dict[ReconciliationReason, str] = {
    ReconciliationReason.FILL_ECONOMIC_GAP: (
        "the fill history leaves a quantity gap — the executed intervals do not "
        "cover a contiguous range from zero"
    ),
    ReconciliationReason.FILL_ECONOMIC_OVERLAP: (
        "two fills claim economics over the same contracts — the executed quantity is double-booked"
    ),
    ReconciliationReason.FILL_CUMULATIVE_REGRESSION: (
        "a later fill reports a lower cumulative quantity than an earlier one — "
        "the running total moves backwards"
    ),
    ReconciliationReason.FILL_KIND_TOTAL_MISMATCH: (
        "a complete fill closes at a cumulative that disagrees with the order's "
        "authoritative quantity"
    ),
    ReconciliationReason.FILL_EXCEEDS_CONFIRMED_TOTAL: (
        "fills claim more contracts than the broker ever confirmed — execution "
        "beyond the acknowledged order"
    ),
    ReconciliationReason.READBACK_FILL_CONTRADICTION: (
        "a broker readback denies execution while fills were observed — the "
        "money story and the status story disagree"
    ),
    ReconciliationReason.TERMINAL_FACT_CONTRADICTION: (
        "two broker facts claim different terminal outcomes for one order — "
        "the outcome itself is contested"
    ),
    ReconciliationReason.READBACK_CUMULATIVE_REGRESSION: (
        "a later readback reports less cumulative quantity than already "
        "observed — confirmed execution disappears"
    ),
    ReconciliationReason.REJECT_WITH_OBSERVED_FILL: (
        "the order was rejected after fills were observed — rejection and "
        "execution cannot both be true"
    ),
    ReconciliationReason.BROKER_ORDER_ID_CONFLICT: (
        "broker facts disagree about which broker order this intent maps to — "
        "the order's identity is contested"
    ),
    ReconciliationReason.AMBIGUOUS_READBACK: (
        "a readback reports the order absent or ambiguous — the broker cannot "
        "or will not state the order's standing"
    ),
    ReconciliationReason.AMBIGUOUS_REPLACE_CONFIRMATION: (
        "a replace confirmation cannot be attributed to one readback — which "
        "confirmation applies is undecidable"
    ),
    ReconciliationReason.MISSING_SUBMIT: (
        "broker facts exist for an intent with no submit attempt retained — "
        "execution without a recorded request"
    ),
    ReconciliationReason.TOTAL_UNCONFIRMED: (
        "the order's total quantity was never confirmed by any broker fact"
    ),
    ReconciliationReason.RETRY_AFTER_LOCAL_KNOWLEDGE: (
        "a send retry occurred after the runner already knew the original submit reached the broker"
    ),
    ReconciliationReason.OVERLAPPING_REPLACE_INTENTS: (
        "two replace intents are outstanding at once — which replacement "
        "governs is ambiguous at replace time"
    ),
    ReconciliationReason.INVALID_REPLACE_BASIS: (
        "a replace cites a basis readback that was not the current basis when "
        "the replacement was created"
    ),
    ReconciliationReason.REPLACE_CONFIRMATION_MISMATCH: (
        "the readback confirming a replace reports a different total than the replacement requested"
    ),
    ReconciliationReason.UNEXPECTED_CONFIRMED_TOTAL_CHANGE: (
        "the broker-confirmed total changed without a replace to explain it"
    ),
}

_UNMAPPED = frozenset(ReconciliationReason) - frozenset(_REASON_SEVERITY)
if _UNMAPPED or len(_REASON_SEVERITY) != len(ReconciliationReason):
    raise RuntimeError(  # pragma: no cover - construction-time totality guard
        f"severity mapping is not total: {sorted(reason.value for reason in _UNMAPPED)}"
    )
_UNEXPLAINED = frozenset(ReconciliationReason) - frozenset(_REASON_EXPLANATIONS)
if _UNEXPLAINED or len(_REASON_EXPLANATIONS) != len(ReconciliationReason):
    raise RuntimeError(  # pragma: no cover - construction-time totality guard
        f"explanation mapping is not total: {sorted(reason.value for reason in _UNEXPLAINED)}"
    )


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    """One retained reason, classified and explained."""

    reason: ReconciliationReason
    severity: ReconciliationSeverity
    explanation: str


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """The adjudication of one lifecycle's retained reasons."""

    intent_id: str
    state: ExecutionState
    broker_state: ExecutionState
    findings: tuple[ReconciliationFinding, ...]

    @property
    def is_clean(self) -> bool:
        """Binary cleanliness — zero retained reasons, no grading."""
        return not self.findings

    @property
    def severities(self) -> frozenset[ReconciliationSeverity]:
        return frozenset(finding.severity for finding in self.findings)

    @property
    def economic_findings(self) -> tuple[ReconciliationFinding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if finding.severity is ReconciliationSeverity.ECONOMIC
        )

    def summary(self) -> str:
        """One deterministic line: what the lifecycle retained, worst first."""
        if self.is_clean:
            return f"{self.intent_id}: clean ({self.state.value})"
        worst = min(self.severities, key=lambda severity: _SEVERITY_RANK[severity])
        names = ",".join(sorted(finding.reason.value for finding in self.findings))
        return f"{self.intent_id}: {len(self.findings)} reasons, worst {worst.value} ({names})"


def reconcile(lifecycle: ExecutionLifecycle) -> ReconciliationReport:
    """Adjudicate one lifecycle: classify, explain, and order its retained
    reasons — without resolving, editing, or dropping any of them."""
    findings = tuple(
        sorted(
            (
                ReconciliationFinding(
                    reason=reason,
                    severity=_REASON_SEVERITY[reason],
                    explanation=_REASON_EXPLANATIONS[reason],
                )
                for reason in lifecycle.reconciliation_reasons
            ),
            key=lambda finding: (_SEVERITY_RANK[finding.severity], finding.reason.value),
        )
    )
    return ReconciliationReport(
        intent_id=lifecycle.intent.intent_id,
        state=lifecycle.state,
        broker_state=lifecycle.broker_state,
        findings=findings,
    )
