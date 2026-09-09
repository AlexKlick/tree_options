"""M6 evidence gate: is a lifecycle's record set ADMISSIBLE as execution proof?

The gate is the layer that stops TRUSTING the lifecycle and starts VERIFYING
it.  A lifecycle derived through ``apply`` maintains its own invariants, but
evidence for the program's honesty discipline must survive a consumer that
re-derives everything checkable from the retained records themselves: the
fill-economics intervals and observed quantity are recomputed here from the
fill records and compared against the lifecycle's claims (a drift is a
``DERIVATION_MISMATCH`` refusal, not a pass), the state must be terminal,
the economics must be complete for the state claimed (``FILLED`` requires
exactly ``(0, quantity)``; ``CANCELED``/``REJECTED`` require whatever was
executed to hang contiguously from zero), and the adjudicated
``ReconciliationReport`` must be CLEAN — every retained reason blocks
admission individually.

Conventions pinned here: admissibility is BINARY (ADMISSIBLE or REFUSED —
never "admissible with warnings"); a REFUSED receipt carries its blockers
and NO money story (economics is ``None`` — a refused record set must not
be consumable as if its economics were real); an ADMISSIBLE receipt carries
the exact money story recomputed from the fill records — per-fill
``quantity x unit_price`` summed exactly in Decimal, fees summed per fill,
and one signed ``net_cash_flow`` (the ACCOUNT's cash movement: BUY is
``-(gross + fees)`` out, SELL is ``gross - fees`` in); and the receipt is
byte-deterministic for one lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum

from tree_options.execution.lifecycle import (
    ExecutionLifecycle,
    ExecutionState,
)
from tree_options.execution.records import (
    BrokerReadback,
    CompleteFill,
    PartialFill,
)
from tree_options.execution.reconciliation import (
    ReconciliationReport,
    reconcile,
)

#: States that end an order's story.  Only these can be evidence.
_TERMINAL_STATES = frozenset(
    {
        ExecutionState.FILLED,
        ExecutionState.CANCELED,
        ExecutionState.REJECTED,
    }
)


class EvidenceVerdict(StrEnum):
    ADMISSIBLE = "ADMISSIBLE"
    REFUSED = "REFUSED"


class EvidenceBlockerKind(StrEnum):
    NON_TERMINAL_STATE = "NON_TERMINAL_STATE"
    DERIVATION_MISMATCH = "DERIVATION_MISMATCH"
    INCOMPLETE_FILL_ECONOMICS = "INCOMPLETE_FILL_ECONOMICS"
    RECONCILIATION_RETAINED = "RECONCILIATION_RETAINED"


@dataclass(frozen=True, slots=True)
class EvidenceBlocker:
    kind: EvidenceBlockerKind
    detail: str


@dataclass(frozen=True, slots=True)
class ExecutionEconomics:
    """The exact money story of the executed quantity, recomputed from fills.

    ``gross_amount`` is the unsigned sum of per-fill ``quantity x unit_price``;
    ``total_fees`` sums each fill's fees; ``net_cash_flow`` is the account's
    signed movement — negative (out) for a BUY, positive (in) for a SELL net
    of fees.  All Decimal-exact; empty executions are honest zeros.
    """

    filled_quantity: int
    gross_amount: Decimal
    total_fees: Decimal
    net_cash_flow: Decimal


@dataclass(frozen=True, slots=True)
class EvidenceReceipt:
    intent_id: str
    contract_id: str
    side: str
    verdict: EvidenceVerdict
    state: ExecutionState
    economically_covered_quantity: int
    economics: ExecutionEconomics | None
    blockers: tuple[EvidenceBlocker, ...]
    basis: str

    @property
    def is_admissible(self) -> bool:
        return self.verdict is EvidenceVerdict.ADMISSIBLE


def _fill_records(lifecycle: ExecutionLifecycle) -> tuple[PartialFill | CompleteFill, ...]:
    return tuple(
        record for record in lifecycle.records if isinstance(record, (PartialFill, CompleteFill))
    )


def _merge_intervals(
    intervals: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    """Union overlapping/adjacent intervals — the economics actually covered."""
    ordered = sorted(intervals)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _recompute_economics(
    lifecycle: ExecutionLifecycle,
) -> tuple[tuple[tuple[int, int], ...], int, int, ExecutionEconomics]:
    """Independent re-derivation: intervals, covered/observed quantity, money.

    A fill record states what it added (``fill_quantity``) and the running
    total (``cumulative_quantity``) — its interval is the delta
    ``(cumulative - fill_quantity, cumulative)``, and touching intervals
    merge.  The OBSERVED quantity is the max cumulative across fills and
    readbacks (the lifecycle's quantity authority); the money sums each
    fill's own economics under a precision that keeps the products EXACT
    (an int quantity times an 18-digit price can exceed the default 28
    significant digits, which would round gross and make the receipt
    context-dependent); exact for the non-overlapping fills an ADMISSIBLE
    set requires (a refused set never publishes its economics).
    """
    fills = _fill_records(lifecycle)
    intervals = _merge_intervals(
        tuple(
            (fill.cumulative_quantity - fill.fill_quantity, fill.cumulative_quantity)
            for fill in fills
        )
    )
    covered = sum(end - start for start, end in intervals)
    observed = max(
        (
            record.cumulative_quantity
            for record in lifecycle.records
            if isinstance(record, (PartialFill, CompleteFill, BrokerReadback))
        ),
        default=0,
    )
    total_contracts = sum(fill.fill_quantity for fill in fills)
    with localcontext() as context:
        # Digits needed: quantity digits + 18-digit price + generous sum headroom.
        context.prec = max(60, len(str(total_contracts)) + 24)
        gross = sum(
            (Decimal(fill.fill_quantity) * fill.unit_price for fill in fills),
            Decimal("0"),
        )
        fees = sum((fill.fees for fill in fills), Decimal("0"))
        if lifecycle.intent.side == "BUY":
            net = -(gross + fees)
        else:
            net = gross - fees
    return (
        intervals,
        covered,
        observed,
        ExecutionEconomics(
            filled_quantity=covered,
            gross_amount=gross,
            total_fees=fees,
            net_cash_flow=net,
        ),
    )


def _expected_terminal_total(lifecycle: ExecutionLifecycle) -> int:
    """The quantity a FILLED lifecycle must have covered exactly.

    A confirmed replacement total OUTRANKS the immutable intent quantity —
    the broker-confirmed total is the order the fills answered; falling
    back to the intent quantity only when nothing confirmed a total.
    """
    confirmed = lifecycle.broker_confirmed_total_quantity
    if confirmed is not None:
        return confirmed
    return lifecycle.intent.quantity


def _economics_blockers(
    lifecycle: ExecutionLifecycle,
    intervals: tuple[tuple[int, int], ...],
) -> tuple[EvidenceBlocker, ...]:
    """Completeness of the claimed economics for the claimed terminal state."""
    if lifecycle.state is ExecutionState.FILLED:
        expected: tuple[tuple[int, int], ...] = ((0, _expected_terminal_total(lifecycle)),)
        if intervals != expected:
            return (
                EvidenceBlocker(
                    kind=EvidenceBlockerKind.INCOMPLETE_FILL_ECONOMICS,
                    detail=(f"FILLED requires exactly {expected}; intervals are {intervals}"),
                ),
            )
        return ()
    # CANCELED / REJECTED: whatever executed must hang contiguously from zero —
    # the intervals are touching-merged, so any SECOND interval is an interior
    # gap and a first origin away from zero means contracts from nowhere.
    if intervals and (len(intervals) != 1 or intervals[0][0] != 0):
        return (
            EvidenceBlocker(
                kind=EvidenceBlockerKind.INCOMPLETE_FILL_ECONOMICS,
                detail=(
                    f"{lifecycle.state.value} economics must be one interval from 0; "
                    f"intervals are {intervals}"
                ),
            ),
        )
    return ()


def assess_evidence(lifecycle: ExecutionLifecycle) -> EvidenceReceipt:
    """Adjudicate one lifecycle as execution proof — verify, then decide."""
    report: ReconciliationReport = reconcile(lifecycle)
    blockers: list[EvidenceBlocker] = []

    intervals, covered, observed, economics = _recompute_economics(lifecycle)

    if lifecycle.state not in _TERMINAL_STATES:
        blockers.append(
            EvidenceBlocker(
                kind=EvidenceBlockerKind.NON_TERMINAL_STATE,
                detail=f"state {lifecycle.state.value} is not terminal",
            )
        )

    # The lifecycle's own claims must match the recomputed truth.
    if lifecycle.fill_economic_intervals != intervals:
        blockers.append(
            EvidenceBlocker(
                kind=EvidenceBlockerKind.DERIVATION_MISMATCH,
                detail=(
                    f"lifecycle claims intervals {lifecycle.fill_economic_intervals}; "
                    f"records re-derive {intervals}"
                ),
            )
        )
    if lifecycle.filled_quantity != observed:
        blockers.append(
            EvidenceBlocker(
                kind=EvidenceBlockerKind.DERIVATION_MISMATCH,
                detail=(
                    f"lifecycle claims observed quantity {lifecycle.filled_quantity}; "
                    f"records re-derive {observed}"
                ),
            )
        )
    if lifecycle.economically_covered_quantity != covered:
        blockers.append(
            EvidenceBlocker(
                kind=EvidenceBlockerKind.DERIVATION_MISMATCH,
                detail=(
                    f"lifecycle claims covered quantity "
                    f"{lifecycle.economically_covered_quantity}; records re-derive {covered}"
                ),
            )
        )

    if lifecycle.state in _TERMINAL_STATES:
        blockers.extend(_economics_blockers(lifecycle, intervals))

    if not report.is_clean:
        blockers.extend(
            EvidenceBlocker(
                kind=EvidenceBlockerKind.RECONCILIATION_RETAINED,
                detail=f"{finding.severity.value}: {finding.reason.value} — {finding.explanation}",
            )
            for finding in report.findings
        )

    if blockers:
        return EvidenceReceipt(
            intent_id=lifecycle.intent.intent_id,
            contract_id=lifecycle.intent.contract_id,
            side=lifecycle.intent.side,
            verdict=EvidenceVerdict.REFUSED,
            state=lifecycle.state,
            economically_covered_quantity=covered,
            economics=None,
            blockers=tuple(blockers),
            basis="; ".join(blocker.detail for blocker in blockers),
        )
    return EvidenceReceipt(
        intent_id=lifecycle.intent.intent_id,
        contract_id=lifecycle.intent.contract_id,
        side=lifecycle.intent.side,
        verdict=EvidenceVerdict.ADMISSIBLE,
        state=lifecycle.state,
        economically_covered_quantity=covered,
        economics=economics,
        blockers=(),
        basis=(
            f"terminal {lifecycle.state.value} with exact fill economics "
            f"{intervals} and zero retained reconciliation reasons"
        ),
    )
