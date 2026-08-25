"""Adversarial contracts for the first M6 lifecycle remediation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tree_options.execution import (
    BrokerAcknowledgement,
    BrokerReadback,
    BrokerReadbackStatus,
    CompleteFill,
    DisconnectObserved,
    ExecutionLifecycle,
    ExecutionState,
    OrderIntent,
    OrderReject,
    PartialFill,
    ReplaceIntent,
    ReplacementRefusedError,
    SubmitAttempt,
    TemporalOrderError,
    TimeoutObserved,
)

T0 = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _intent(quantity: int = 3) -> OrderIntent:
    return OrderIntent(
        intent_id="intent-001",
        contract_id="O:XYZ260918C00100000",
        side="BUY",
        position_effect="OPEN_LONG",
        quantity=quantity,
        order_type="LIMIT",
        limit_price=Decimal("1.25"),
        execution_style="single",
        package_id=None,
        intent_created_at=T0,
        source="synthetic-test",
        source_sequence_id="intent-seq-001",
    )


def _attempt() -> SubmitAttempt:
    return SubmitAttempt(
        record_id="attempt-001",
        intent_id="intent-001",
        send_attempt_at=_at(1),
        source="executor",
        source_sequence_id="attempt-seq-001",
    )


def _ack(number: int = 1, *, received: int = 3) -> BrokerAcknowledgement:
    return BrokerAcknowledgement(
        record_id=f"ack-{number:03d}",
        intent_id="intent-001",
        broker_order_id="paper-order-001",
        broker_acknowledged_at=_at(received - 1),
        locally_received_at=_at(received),
        source="synthetic-paper-adapter",
        source_sequence_id=f"source-ack-{number:03d}",
        broker_sequence_id=f"broker-ack-{number:03d}",
    )


def _fill(
    number: int,
    *,
    fill_quantity: int,
    cumulative_quantity: int,
    exchange_at: int,
    received: int,
    complete: bool = False,
) -> PartialFill | CompleteFill:
    fill_type = CompleteFill if complete else PartialFill
    return fill_type(
        record_id=f"fill-{number:03d}",
        intent_id="intent-001",
        broker_order_id="paper-order-001",
        fill_quantity=fill_quantity,
        cumulative_quantity=cumulative_quantity,
        unit_price=Decimal("1.20") + Decimal(number) / Decimal("100"),
        fees=Decimal("0.65"),
        exchange_event_at=_at(exchange_at),
        locally_received_at=_at(received),
        source="synthetic-paper-adapter",
        source_sequence_id=f"source-fill-{number:03d}",
        broker_sequence_id=f"broker-fill-{number:03d}",
    )


def _readback(
    number: int,
    *,
    status: BrokerReadbackStatus = BrokerReadbackStatus.OPEN,
    total_quantity: int | None = 3,
    cumulative_quantity: int = 0,
    snapshot_at: int | None = None,
    received: int | None = None,
) -> BrokerReadback:
    snapshot_second = 10 + number if snapshot_at is None else snapshot_at
    received_second = snapshot_second + 1 if received is None else received
    broker_order_id = (
        None
        if status
        in {
            BrokerReadbackStatus.ABSENT,
            BrokerReadbackStatus.AMBIGUOUS,
            BrokerReadbackStatus.REJECTED,
        }
        else "paper-order-001"
    )
    return BrokerReadback(
        record_id=f"readback-{number:03d}",
        intent_id="intent-001",
        status=status,
        broker_order_id=broker_order_id,
        total_quantity=total_quantity,
        cumulative_quantity=cumulative_quantity,
        broker_snapshot_at=_at(snapshot_second),
        locally_received_at=_at(received_second),
        source="synthetic-paper-adapter",
        source_sequence_id=f"source-readback-{number:03d}",
        broker_sequence_id=f"broker-readback-{number:03d}",
    )


def _replace(number: int = 1, *, total: int = 5, basis: int = 1) -> ReplaceIntent:
    return ReplaceIntent(
        record_id=f"replace-{number:03d}",
        intent_id="intent-001",
        based_on_readback_id=f"readback-{basis:03d}",
        broker_order_id="paper-order-001",
        new_total_quantity=total,
        new_limit_price=Decimal("1.10"),
        replace_created_at=_at(13 + number),
        source="executor",
        source_sequence_id=f"replace-seq-{number:03d}",
    )


def _acknowledged() -> ExecutionLifecycle:
    return ExecutionLifecycle.start(_intent()).apply(_attempt()).apply(_ack())


def _semantic(lifecycle: ExecutionLifecycle) -> tuple[object, ...]:
    return (
        lifecycle.state,
        lifecycle.broker_order_id,
        lifecycle.filled_quantity,
        lifecycle.broker_confirmed_total_quantity,
        lifecycle.pending_replace_total_quantity,
        lifecycle.fill_economic_intervals,
        lifecycle.reconciliation_reasons,
        lifecycle.uncertain_since_at,
        lifecycle.replace_basis_id,
    )


def test_replace_request_stays_pending_until_broker_confirms_total() -> None:
    replaced = _acknowledged().apply(_readback(1, total_quantity=3)).apply(_replace(total=5))
    assert replaced.broker_confirmed_total_quantity == 3
    assert replaced.pending_replace_total_quantity == 5

    with pytest.raises(ReplacementRefusedError, match="pending"):
        replaced.apply(_replace(2, total=6, basis=1))

    confirmed = replaced.apply(_readback(2, total_quantity=5, snapshot_at=16, received=17))
    assert confirmed.state is ExecutionState.ACKNOWLEDGED
    assert confirmed.broker_confirmed_total_quantity == 5
    assert confirmed.pending_replace_total_quantity is None

    later_fill = confirmed.apply(
        _fill(4, fill_quantity=4, cumulative_quantity=4, exchange_at=18, received=19)
    )
    assert later_fill.state is ExecutionState.PARTIALLY_FILLED
    assert later_fill.filled_quantity == 4


def test_fill_above_confirmed_total_does_not_infer_pending_replace_acceptance() -> None:
    replaced = _acknowledged().apply(_readback(1, total_quantity=3)).apply(_replace(total=5))
    premature_fill = replaced.apply(
        _fill(4, fill_quantity=4, cumulative_quantity=4, exchange_at=16, received=17)
    )
    assert premature_fill.state is ExecutionState.RECONCILIATION_REQUIRED
    assert premature_fill.broker_confirmed_total_quantity == 3
    assert premature_fill.pending_replace_total_quantity == 5
    assert "FILL_EXCEEDS_CONFIRMED_TOTAL" in premature_fill.reconciliation_reasons

    confirmed = premature_fill.apply(
        _readback(
            2,
            status=BrokerReadbackStatus.PARTIALLY_FILLED,
            total_quantity=5,
            cumulative_quantity=4,
            snapshot_at=15,
            received=18,
        )
    )
    assert confirmed.state is ExecutionState.PARTIALLY_FILLED
    assert confirmed.broker_confirmed_total_quantity == 5
    assert confirmed.pending_replace_total_quantity is None
    assert not confirmed.reconciliation_reasons


def test_replace_confirmation_mismatch_is_sticky_after_pending_clears() -> None:
    replaced = _acknowledged().apply(_readback(1, total_quantity=3)).apply(_replace(total=5))
    mismatched = replaced.apply(_readback(2, total_quantity=4, snapshot_at=16, received=17))
    assert mismatched.state is ExecutionState.RECONCILIATION_REQUIRED
    assert mismatched.broker_confirmed_total_quantity == 4
    assert mismatched.pending_replace_total_quantity is None
    assert "REPLACE_CONFIRMATION_MISMATCH" in mismatched.reconciliation_reasons

    later_matching_snapshot = mismatched.apply(
        _readback(3, total_quantity=5, snapshot_at=18, received=19)
    )
    assert later_matching_snapshot.broker_confirmed_total_quantity == 5
    assert later_matching_snapshot.state is ExecutionState.RECONCILIATION_REQUIRED
    assert "REPLACE_CONFIRMATION_MISMATCH" in later_matching_snapshot.reconciliation_reasons
    assert later_matching_snapshot.replace_basis_id is None


def test_unrequested_broker_total_change_is_sticky_before_and_after_replace() -> None:
    changed_without_replace = _acknowledged().apply(
        _readback(1, total_quantity=4, snapshot_at=11, received=12)
    )
    assert changed_without_replace.broker_confirmed_total_quantity == 4
    assert changed_without_replace.state is ExecutionState.RECONCILIATION_REQUIRED
    assert "UNEXPECTED_CONFIRMED_TOTAL_CHANGE" in changed_without_replace.reconciliation_reasons

    confirmed_replace = (
        _acknowledged()
        .apply(_readback(1, total_quantity=3))
        .apply(_replace(total=5))
        .apply(_readback(2, total_quantity=5, snapshot_at=16, received=17))
    )
    changed_after_replace = confirmed_replace.apply(
        _readback(3, total_quantity=6, snapshot_at=18, received=19)
    )
    assert changed_after_replace.broker_confirmed_total_quantity == 6
    assert changed_after_replace.state is ExecutionState.RECONCILIATION_REQUIRED
    assert "UNEXPECTED_CONFIRMED_TOTAL_CHANGE" in changed_after_replace.reconciliation_reasons


def test_first_readback_total_must_match_submitted_intent_without_ack() -> None:
    submitting = ExecutionLifecycle.start(_intent()).apply(_attempt())
    unknown = submitting.apply(
        TimeoutObserved(
            record_id="timeout-001",
            intent_id="intent-001",
            attempt_id="attempt-001",
            locally_received_at=_at(5),
            source="executor",
            source_sequence_id="timeout-seq-001",
        )
    )
    observed = unknown.apply(_readback(1, total_quantity=4, snapshot_at=6, received=7))
    assert observed.broker_confirmed_total_quantity == 4
    assert observed.pending_replace_total_quantity is None
    assert observed.state is ExecutionState.RECONCILIATION_REQUIRED
    assert "UNEXPECTED_CONFIRMED_TOTAL_CHANGE" in observed.reconciliation_reasons


def test_fact_before_submit_accepts_temporally_earlier_submit_and_converges() -> None:
    created = ExecutionLifecycle.start(_intent())
    acknowledgement = _ack(received=3)

    missing_submit = created.apply(acknowledgement)
    assert missing_submit.reconciliation_reasons == {"MISSING_SUBMIT"}
    repaired = missing_submit.apply(_attempt())
    chronological = created.apply(_attempt()).apply(acknowledgement)

    assert _semantic(repaired) == _semantic(chronological)
    assert repaired.state is ExecutionState.ACKNOWLEDGED
    assert not repaired.reconciliation_reasons


def test_fact_before_submit_refuses_a_submit_that_postdates_the_fact() -> None:
    created = ExecutionLifecycle.start(_intent())
    missing_submit = created.apply(_ack(received=3))
    too_late = SubmitAttempt(
        record_id="attempt-too-late",
        intent_id="intent-001",
        send_attempt_at=_at(3),
        source="executor",
        source_sequence_id="attempt-too-late-seq",
    )

    with pytest.raises(TemporalOrderError, match="retrospective submit"):
        missing_submit.apply(too_late)
    assert missing_submit.reconciliation_reasons == {"MISSING_SUBMIT"}
    assert too_late not in missing_submit.records


def test_fill_before_submit_closes_only_missing_submit_and_converges() -> None:
    created = ExecutionLifecycle.start(_intent())
    fill = _fill(1, fill_quantity=1, cumulative_quantity=1, exchange_at=5, received=6)

    missing_submit = created.apply(fill)
    assert "MISSING_SUBMIT" in missing_submit.reconciliation_reasons
    repaired = missing_submit.apply(_attempt())
    chronological = created.apply(_attempt()).apply(fill)

    assert _semantic(repaired) == _semantic(chronological)
    assert "MISSING_SUBMIT" not in repaired.reconciliation_reasons
    assert repaired.reconciliation_reasons == chronological.reconciliation_reasons
    assert repaired.reconciliation_reasons == {"TOTAL_UNCONFIRMED"}


def test_fill_above_confirmed_total_without_pending_replace_is_retained() -> None:
    contradictory = _acknowledged().apply(
        _fill(4, fill_quantity=4, cumulative_quantity=4, exchange_at=5, received=6)
    )
    assert contradictory.state is ExecutionState.RECONCILIATION_REQUIRED
    assert contradictory.filled_quantity == 4
    assert contradictory.fill_economic_intervals == ((0, 4),)
    assert "FILL_EXCEEDS_CONFIRMED_TOTAL" in contradictory.reconciliation_reasons
    assert "FILL_KIND_TOTAL_MISMATCH" in contradictory.reconciliation_reasons
    assert contradictory.records[-1].record_id == "fill-004"


def test_delayed_fill_kind_mismatch_after_replace_confirmation_is_retained() -> None:
    confirmed_replace = (
        _acknowledged()
        .apply(_readback(1, total_quantity=3))
        .apply(_replace(total=5))
        .apply(_readback(2, total_quantity=5, snapshot_at=15, received=16))
    )
    delayed_fill = _fill(
        3,
        fill_quantity=3,
        cumulative_quantity=3,
        exchange_at=17,
        received=20,
        complete=True,
    )
    projected = confirmed_replace.apply(delayed_fill)
    assert projected.state is ExecutionState.RECONCILIATION_REQUIRED
    assert projected.filled_quantity == 3
    assert "FILL_KIND_TOTAL_MISMATCH" in projected.reconciliation_reasons
    assert projected.records[-1] == delayed_fill


@pytest.mark.parametrize("uncertainty", ["timeout", "disconnect"])
def test_stale_uncertainty_and_later_ack_converge_in_either_application_order(
    uncertainty: str,
) -> None:
    submitting = ExecutionLifecycle.start(_intent()).apply(_attempt())
    observed = (
        TimeoutObserved(
            record_id="timeout-001",
            intent_id="intent-001",
            attempt_id="attempt-001",
            locally_received_at=_at(5),
            source="executor",
            source_sequence_id="timeout-seq-001",
        )
        if uncertainty == "timeout"
        else DisconnectObserved(
            record_id="disconnect-001",
            intent_id="intent-001",
            locally_received_at=_at(5),
            source="executor",
            source_sequence_id="disconnect-seq-001",
        )
    )
    acknowledgement = _ack(received=6)

    uncertainty_first = submitting.apply(observed).apply(acknowledgement)
    acknowledgement_first = submitting.apply(acknowledgement).apply(observed)
    assert _semantic(uncertainty_first) == _semantic(acknowledgement_first)
    assert acknowledgement_first.state is ExecutionState.ACKNOWLEDGED


def test_stale_disconnect_and_later_fill_converge_without_unknown_regression() -> None:
    acknowledged = _acknowledged()
    disconnected = DisconnectObserved(
        record_id="disconnect-001",
        intent_id="intent-001",
        locally_received_at=_at(7),
        source="executor",
        source_sequence_id="disconnect-seq-001",
    )
    fill = _fill(1, fill_quantity=1, cumulative_quantity=1, exchange_at=6, received=8)

    disconnect_first = acknowledged.apply(disconnected).apply(fill)
    fill_first = acknowledged.apply(fill).apply(disconnected)
    assert _semantic(disconnect_first) == _semantic(fill_first)
    assert fill_first.state is ExecutionState.PARTIALLY_FILLED


def test_unknown_with_observed_fill_recovers_to_partial_never_acknowledged() -> None:
    partial = _acknowledged().apply(
        _fill(1, fill_quantity=1, cumulative_quantity=1, exchange_at=5, received=6)
    )
    unknown = partial.apply(
        DisconnectObserved(
            record_id="disconnect-001",
            intent_id="intent-001",
            locally_received_at=_at(7),
            source="executor",
            source_sequence_id="disconnect-seq-001",
        )
    )
    recovered = unknown.apply(_ack(2, received=9))
    assert recovered.state is ExecutionState.PARTIALLY_FILLED
    assert recovered.filled_quantity == 1


def test_unknown_with_observed_fill_refuses_reject_projection() -> None:
    partial = _acknowledged().apply(
        _fill(1, fill_quantity=1, cumulative_quantity=1, exchange_at=5, received=6)
    )
    unknown = partial.apply(
        DisconnectObserved(
            record_id="disconnect-001",
            intent_id="intent-001",
            locally_received_at=_at(7),
            source="executor",
            source_sequence_id="disconnect-seq-001",
        )
    )
    rejected = unknown.apply(
        OrderReject(
            record_id="reject-001",
            intent_id="intent-001",
            broker_order_id="paper-order-001",
            reason_code="synthetic-reject",
            broker_acknowledged_at=_at(8),
            locally_received_at=_at(9),
            source="synthetic-paper-adapter",
            source_sequence_id="source-reject-001",
            broker_sequence_id="broker-reject-001",
        )
    )
    assert rejected.state is ExecutionState.RECONCILIATION_REQUIRED
    assert rejected.filled_quantity == 1
    assert "REJECT_WITH_OBSERVED_FILL" in rejected.reconciliation_reasons


@pytest.mark.parametrize(
    ("status", "total"),
    [
        (BrokerReadbackStatus.OPEN, 3),
        (BrokerReadbackStatus.REJECTED, None),
    ],
)
def test_unknown_with_observed_fill_does_not_recover_to_zero_fill_status(
    status: BrokerReadbackStatus, total: int | None
) -> None:
    partial = _acknowledged().apply(
        _fill(1, fill_quantity=1, cumulative_quantity=1, exchange_at=5, received=6)
    )
    unknown = partial.apply(
        DisconnectObserved(
            record_id="disconnect-001",
            intent_id="intent-001",
            locally_received_at=_at(7),
            source="executor",
            source_sequence_id="disconnect-seq-001",
        )
    )
    contradictory = unknown.apply(
        _readback(
            1,
            status=status,
            total_quantity=total,
            cumulative_quantity=0,
            snapshot_at=8,
            received=9,
        )
    )
    assert contradictory.state is ExecutionState.RECONCILIATION_REQUIRED
    assert "READBACK_FILL_CONTRADICTION" in contradictory.reconciliation_reasons


def test_unknown_with_matching_partial_readback_recovers_consistently() -> None:
    partial = _acknowledged().apply(
        _fill(1, fill_quantity=1, cumulative_quantity=1, exchange_at=5, received=6)
    )
    unknown = partial.apply(
        DisconnectObserved(
            record_id="disconnect-001",
            intent_id="intent-001",
            locally_received_at=_at(7),
            source="executor",
            source_sequence_id="disconnect-seq-001",
        )
    )
    recovered = unknown.apply(
        _readback(
            1,
            status=BrokerReadbackStatus.PARTIALLY_FILLED,
            total_quantity=3,
            cumulative_quantity=1,
            snapshot_at=8,
            received=9,
        )
    )
    assert recovered.state is ExecutionState.PARTIALLY_FILLED
    assert not recovered.reconciliation_reasons


def test_missing_fill_interval_closes_gap_and_application_orders_converge() -> None:
    acknowledged = _acknowledged()
    missing_first = _fill(1, fill_quantity=1, cumulative_quantity=1, exchange_at=5, received=9)
    later_interval = _fill(2, fill_quantity=1, cumulative_quantity=2, exchange_at=6, received=8)

    gap = acknowledged.apply(later_interval)
    assert gap.state is ExecutionState.RECONCILIATION_REQUIRED
    assert gap.fill_economic_intervals == ((1, 2),)
    assert "FILL_ECONOMIC_GAP" in gap.reconciliation_reasons

    gap_then_closed = gap.apply(missing_first)
    chronological = acknowledged.apply(missing_first).apply(later_interval)
    assert _semantic(gap_then_closed) == _semantic(chronological)
    assert gap_then_closed.state is ExecutionState.PARTIALLY_FILLED
    assert gap_then_closed.fill_economic_intervals == ((0, 2),)
    assert not gap_then_closed.reconciliation_reasons


def test_overlapping_fill_economics_reconcile_in_either_application_order() -> None:
    acknowledged = _acknowledged()
    whole = _fill(1, fill_quantity=2, cumulative_quantity=2, exchange_at=5, received=6)
    overlap = _fill(2, fill_quantity=1, cumulative_quantity=2, exchange_at=7, received=8)

    whole_first = acknowledged.apply(whole).apply(overlap)
    overlap_first = acknowledged.apply(overlap).apply(whole)
    assert _semantic(whole_first) == _semantic(overlap_first)
    assert whole_first.state is ExecutionState.RECONCILIATION_REQUIRED
    assert whole_first.fill_economic_intervals == ((0, 2),)
    assert "FILL_ECONOMIC_OVERLAP" in whole_first.reconciliation_reasons
    assert "FILL_ECONOMIC_GAP" not in whole_first.reconciliation_reasons


def test_readback_cumulative_without_economics_cannot_authorize_replace() -> None:
    missing_economics = _acknowledged().apply(
        _readback(
            1,
            status=BrokerReadbackStatus.PARTIALLY_FILLED,
            total_quantity=3,
            cumulative_quantity=2,
        )
    )
    assert missing_economics.state is ExecutionState.RECONCILIATION_REQUIRED
    assert missing_economics.replace_basis_id is None
    assert "FILL_ECONOMIC_GAP" in missing_economics.reconciliation_reasons
    with pytest.raises(ReplacementRefusedError, match="readback"):
        missing_economics.apply(_replace(total=4))

    covered = missing_economics.apply(
        _fill(2, fill_quantity=2, cumulative_quantity=2, exchange_at=10, received=13)
    )
    assert covered.state is ExecutionState.PARTIALLY_FILLED
    assert covered.replace_basis_id is None
    refreshed = covered.apply(
        _readback(
            2,
            status=BrokerReadbackStatus.PARTIALLY_FILLED,
            total_quantity=3,
            cumulative_quantity=2,
            snapshot_at=14,
            received=15,
        )
    )
    authorized = refreshed.apply(_replace(2, total=4, basis=2))
    assert authorized.pending_replace_total_quantity == 4


def test_later_delivered_fill_invalidates_readback_basis_in_either_order() -> None:
    acknowledged = _acknowledged()
    fill = _fill(1, fill_quantity=1, cumulative_quantity=1, exchange_at=9, received=13)
    readback = _readback(
        1,
        status=BrokerReadbackStatus.PARTIALLY_FILLED,
        total_quantity=3,
        cumulative_quantity=1,
        snapshot_at=10,
        received=12,
    )

    readback_first = acknowledged.apply(readback).apply(fill)
    fill_first = acknowledged.apply(fill).apply(readback)
    assert _semantic(readback_first) == _semantic(fill_first)
    assert readback_first.replace_basis_id is None
    assert fill_first.replace_basis_id is None
    with pytest.raises(ReplacementRefusedError, match="readback"):
        readback_first.apply(_replace(total=4))
    with pytest.raises(ReplacementRefusedError, match="readback"):
        fill_first.apply(_replace(total=4))

    fresh = _readback(
        2,
        status=BrokerReadbackStatus.PARTIALLY_FILLED,
        total_quantity=3,
        cumulative_quantity=1,
        snapshot_at=14,
        received=15,
    )
    refreshed_a = readback_first.apply(fresh)
    refreshed_b = fill_first.apply(fresh)
    assert refreshed_a.replace_basis_id == "readback-002"
    assert refreshed_b.replace_basis_id == "readback-002"
    assert refreshed_a.apply(_replace(2, total=4, basis=2)).pending_replace_total_quantity == 4
    assert refreshed_b.apply(_replace(2, total=4, basis=2)).pending_replace_total_quantity == 4


def test_equal_receipt_fill_and_readback_fail_closed_for_replace_basis() -> None:
    acknowledged = _acknowledged()
    fill = _fill(1, fill_quantity=1, cumulative_quantity=1, exchange_at=9, received=12)
    readback = _readback(
        1,
        status=BrokerReadbackStatus.PARTIALLY_FILLED,
        total_quantity=3,
        cumulative_quantity=1,
        snapshot_at=10,
        received=12,
    )

    readback_first = acknowledged.apply(readback).apply(fill)
    fill_first = acknowledged.apply(fill).apply(readback)
    assert _semantic(readback_first) == _semantic(fill_first)
    assert readback_first.replace_basis_id is None
    assert fill_first.replace_basis_id is None


@pytest.mark.parametrize(
    "status", [BrokerReadbackStatus.OPEN, BrokerReadbackStatus.PARTIALLY_FILLED]
)
@pytest.mark.parametrize("uncertainty_kind", ["timeout", "disconnect"])
def test_equal_receipt_nonterminal_readback_and_uncertainty_fail_closed(
    status: BrokerReadbackStatus,
    uncertainty_kind: str,
) -> None:
    if status is BrokerReadbackStatus.OPEN:
        prefix = ExecutionLifecycle.start(_intent()).apply(_attempt())
        readback = _readback(
            1,
            status=status,
            total_quantity=3,
            cumulative_quantity=0,
            snapshot_at=11,
            received=12,
        )
    else:
        prefix = _acknowledged().apply(
            _fill(1, fill_quantity=1, cumulative_quantity=1, exchange_at=5, received=6)
        )
        readback = _readback(
            1,
            status=status,
            total_quantity=3,
            cumulative_quantity=1,
            snapshot_at=11,
            received=12,
        )

    uncertainty = (
        TimeoutObserved(
            record_id="timeout-equal-readback",
            intent_id="intent-001",
            attempt_id="attempt-001",
            locally_received_at=_at(12),
            source="executor",
            source_sequence_id="timeout-equal-readback-seq",
        )
        if uncertainty_kind == "timeout"
        else DisconnectObserved(
            record_id="disconnect-equal-readback",
            intent_id="intent-001",
            locally_received_at=_at(12),
            source="executor",
            source_sequence_id="disconnect-equal-readback-seq",
        )
    )

    readback_first = prefix.apply(readback).apply(uncertainty)
    uncertainty_first = prefix.apply(uncertainty).apply(readback)
    assert _semantic(readback_first) == _semantic(uncertainty_first)
    assert readback_first.state is ExecutionState.UNKNOWN
    assert readback_first.uncertain_since_at == _at(12)
    assert readback_first.replace_basis_id is None
    assert uncertainty_first.replace_basis_id is None
    assert uncertainty in readback_first.records
    assert uncertainty in uncertainty_first.records
    with pytest.raises(ReplacementRefusedError, match="readback"):
        readback_first.apply(_replace(total=4))
    with pytest.raises(ReplacementRefusedError, match="readback"):
        uncertainty_first.apply(_replace(total=4))


def test_canceled_full_cumulative_requires_complete_fill_economics() -> None:
    canceled = _acknowledged().apply(
        _readback(
            1,
            status=BrokerReadbackStatus.CANCELED,
            total_quantity=3,
            cumulative_quantity=3,
        )
    )
    assert canceled.state is ExecutionState.RECONCILIATION_REQUIRED
    assert "FILL_ECONOMIC_GAP" in canceled.reconciliation_reasons

    covered = canceled.apply(
        _fill(
            3,
            fill_quantity=3,
            cumulative_quantity=3,
            exchange_at=10,
            received=13,
            complete=True,
        )
    )
    assert covered.state is ExecutionState.FILLED
    assert not covered.reconciliation_reasons


def test_canceled_full_and_complete_fill_converge_in_either_order() -> None:
    acknowledged = _acknowledged()
    complete = _fill(
        3,
        fill_quantity=3,
        cumulative_quantity=3,
        exchange_at=10,
        received=13,
        complete=True,
    )
    canceled = _readback(
        1,
        status=BrokerReadbackStatus.CANCELED,
        total_quantity=3,
        cumulative_quantity=3,
        snapshot_at=11,
        received=12,
    )

    readback_first = acknowledged.apply(canceled).apply(complete)
    fill_first = acknowledged.apply(complete).apply(canceled)
    assert _semantic(readback_first) == _semantic(fill_first)
    assert fill_first.state is ExecutionState.FILLED
    assert not fill_first.reconciliation_reasons


@pytest.mark.parametrize("terminal_kind", ["filled", "canceled", "rejected"])
@pytest.mark.parametrize("uncertainty_kind", ["timeout", "disconnect"])
def test_terminal_fact_and_later_uncertainty_converge_in_either_order(
    terminal_kind: str,
    uncertainty_kind: str,
) -> None:
    if terminal_kind == "filled":
        prefix = _acknowledged()
        terminal = _fill(
            3,
            fill_quantity=3,
            cumulative_quantity=3,
            exchange_at=8,
            received=9,
            complete=True,
        )
        expected = ExecutionState.FILLED
    elif terminal_kind == "canceled":
        prefix = _acknowledged().apply(
            _fill(1, fill_quantity=1, cumulative_quantity=1, exchange_at=5, received=6)
        )
        terminal = _readback(
            1,
            status=BrokerReadbackStatus.CANCELED,
            total_quantity=3,
            cumulative_quantity=1,
            snapshot_at=8,
            received=9,
        )
        expected = ExecutionState.CANCELED
    else:
        prefix = ExecutionLifecycle.start(_intent()).apply(_attempt())
        terminal = OrderReject(
            record_id="reject-001",
            intent_id="intent-001",
            broker_order_id="paper-order-001",
            reason_code="synthetic-reject",
            broker_acknowledged_at=_at(8),
            locally_received_at=_at(9),
            source="synthetic-paper-adapter",
            source_sequence_id="source-reject-001",
            broker_sequence_id="broker-reject-001",
        )
        expected = ExecutionState.REJECTED

    uncertainty = (
        TimeoutObserved(
            record_id="timeout-terminal",
            intent_id="intent-001",
            attempt_id="attempt-001",
            locally_received_at=_at(20),
            source="executor",
            source_sequence_id="timeout-terminal-seq",
        )
        if uncertainty_kind == "timeout"
        else DisconnectObserved(
            record_id="disconnect-terminal",
            intent_id="intent-001",
            locally_received_at=_at(20),
            source="executor",
            source_sequence_id="disconnect-terminal-seq",
        )
    )

    uncertainty_first = prefix.apply(uncertainty).apply(terminal)
    terminal_first = prefix.apply(terminal).apply(uncertainty)
    assert _semantic(uncertainty_first) == _semantic(terminal_first)
    assert terminal_first.state is expected
    assert terminal_first.uncertain_since_at is None
    assert uncertainty in terminal_first.records


@pytest.mark.parametrize("terminal_kind", ["filled", "canceled", "rejected"])
@pytest.mark.parametrize("uncertainty_kind", ["timeout", "disconnect"])
def test_equal_receipt_terminal_fact_outranks_transport_uncertainty(
    terminal_kind: str,
    uncertainty_kind: str,
) -> None:
    if terminal_kind == "filled":
        prefix = _acknowledged()
        terminal = _fill(
            3,
            fill_quantity=3,
            cumulative_quantity=3,
            exchange_at=8,
            received=9,
            complete=True,
        )
        expected = ExecutionState.FILLED
    elif terminal_kind == "canceled":
        prefix = _acknowledged().apply(
            _fill(1, fill_quantity=1, cumulative_quantity=1, exchange_at=5, received=6)
        )
        terminal = _readback(
            1,
            status=BrokerReadbackStatus.CANCELED,
            total_quantity=3,
            cumulative_quantity=1,
            snapshot_at=8,
            received=9,
        )
        expected = ExecutionState.CANCELED
    else:
        prefix = ExecutionLifecycle.start(_intent()).apply(_attempt())
        terminal = OrderReject(
            record_id="reject-equal-uncertainty",
            intent_id="intent-001",
            broker_order_id="paper-order-001",
            reason_code="synthetic-reject",
            broker_acknowledged_at=_at(8),
            locally_received_at=_at(9),
            source="synthetic-paper-adapter",
            source_sequence_id="source-reject-equal-uncertainty",
            broker_sequence_id="broker-reject-equal-uncertainty",
        )
        expected = ExecutionState.REJECTED

    uncertainty = (
        TimeoutObserved(
            record_id="timeout-equal-terminal",
            intent_id="intent-001",
            attempt_id="attempt-001",
            locally_received_at=_at(9),
            source="executor",
            source_sequence_id="timeout-equal-terminal-seq",
        )
        if uncertainty_kind == "timeout"
        else DisconnectObserved(
            record_id="disconnect-equal-terminal",
            intent_id="intent-001",
            locally_received_at=_at(9),
            source="executor",
            source_sequence_id="disconnect-equal-terminal-seq",
        )
    )

    uncertainty_first = prefix.apply(uncertainty).apply(terminal)
    terminal_first = prefix.apply(terminal).apply(uncertainty)
    assert _semantic(uncertainty_first) == _semantic(terminal_first)
    assert terminal_first.state is expected
    assert terminal_first.uncertain_since_at is None
    assert terminal_first.replace_basis_id is None
    assert uncertainty in terminal_first.records


def test_terminal_fact_reconciliation_overrides_later_uncertainty() -> None:
    acknowledged = _acknowledged()
    contradictory_terminal = _fill(
        2,
        fill_quantity=2,
        cumulative_quantity=2,
        exchange_at=8,
        received=9,
        complete=True,
    )
    disconnected = DisconnectObserved(
        record_id="disconnect-terminal-reconciliation",
        intent_id="intent-001",
        locally_received_at=_at(20),
        source="executor",
        source_sequence_id="disconnect-terminal-reconciliation-seq",
    )

    uncertainty_first = acknowledged.apply(disconnected).apply(contradictory_terminal)
    terminal_first = acknowledged.apply(contradictory_terminal).apply(disconnected)
    assert _semantic(uncertainty_first) == _semantic(terminal_first)
    assert terminal_first.state is ExecutionState.RECONCILIATION_REQUIRED
    assert "FILL_KIND_TOTAL_MISMATCH" in terminal_first.reconciliation_reasons
    assert disconnected in terminal_first.records
