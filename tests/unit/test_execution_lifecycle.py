"""Pure idempotent broker-neutral execution lifecycle tests."""

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
    IntentMismatchError,
    OrderIntent,
    OrderReject,
    PartialFill,
    RecordIdentityCollisionError,
    ReplaceIntent,
    ReplacementRefusedError,
    SequenceIdentityCollisionError,
    SubmitAttempt,
    TemporalOrderError,
    TimeoutObserved,
)

T0 = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _intent(**over: object) -> OrderIntent:
    fields: dict[str, object] = {
        "intent_id": "intent-001",
        "contract_id": "O:XYZ260918C00100000",
        "side": "BUY",
        "position_effect": "OPEN_LONG",
        "quantity": 3,
        "order_type": "LIMIT",
        "limit_price": Decimal("1.25"),
        "execution_style": "single",
        "package_id": None,
        "intent_created_at": T0,
        "source": "synthetic-test",
        "source_sequence_id": "intent-seq-001",
    }
    fields.update(over)
    return OrderIntent(**fields)


def _attempt(number: int = 1, **over: object) -> SubmitAttempt:
    fields: dict[str, object] = {
        "record_id": f"attempt-{number:03d}",
        "intent_id": "intent-001",
        "send_attempt_at": _at(number),
        "source": "executor",
        "source_sequence_id": f"attempt-seq-{number:03d}",
    }
    fields.update(over)
    return SubmitAttempt(**fields)


def _ack(number: int = 1, **over: object) -> BrokerAcknowledgement:
    fields: dict[str, object] = {
        "record_id": f"ack-{number:03d}",
        "intent_id": "intent-001",
        "broker_order_id": "paper-order-001",
        "broker_acknowledged_at": _at(number + 1),
        "locally_received_at": _at(number + 2),
        "source": "synthetic-paper-adapter",
        "source_sequence_id": f"source-ack-{number:03d}",
        "broker_sequence_id": f"broker-ack-{number:03d}",
    }
    fields.update(over)
    return BrokerAcknowledgement(**fields)


def _partial(number: int = 1, **over: object) -> PartialFill:
    fields: dict[str, object] = {
        "record_id": f"fill-{number:03d}",
        "intent_id": "intent-001",
        "broker_order_id": "paper-order-001",
        "fill_quantity": 1,
        "cumulative_quantity": number,
        "unit_price": Decimal("1.20"),
        "fees": Decimal("0.65"),
        "exchange_event_at": _at(number + 3),
        "locally_received_at": _at(number + 4),
        "source": "synthetic-paper-adapter",
        "source_sequence_id": f"source-fill-{number:03d}",
        "broker_sequence_id": f"broker-fill-{number:03d}",
    }
    fields.update(over)
    return PartialFill(**fields)


def _complete(**over: object) -> CompleteFill:
    fields: dict[str, object] = {
        "record_id": "fill-003",
        "intent_id": "intent-001",
        "broker_order_id": "paper-order-001",
        "fill_quantity": 2,
        "cumulative_quantity": 3,
        "unit_price": Decimal("1.22"),
        "fees": Decimal("1.30"),
        "exchange_event_at": _at(8),
        "locally_received_at": _at(9),
        "source": "synthetic-paper-adapter",
        "source_sequence_id": "source-fill-003",
        "broker_sequence_id": "broker-fill-003",
    }
    fields.update(over)
    return CompleteFill(**fields)


def _timeout(**over: object) -> TimeoutObserved:
    fields: dict[str, object] = {
        "record_id": "timeout-001",
        "intent_id": "intent-001",
        "attempt_id": "attempt-001",
        "locally_received_at": _at(5),
        "source": "executor",
        "source_sequence_id": "timeout-seq-001",
    }
    fields.update(over)
    return TimeoutObserved(**fields)


def _disconnect(**over: object) -> DisconnectObserved:
    fields: dict[str, object] = {
        "record_id": "disconnect-001",
        "intent_id": "intent-001",
        "locally_received_at": _at(5),
        "source": "executor",
        "source_sequence_id": "disconnect-seq-001",
    }
    fields.update(over)
    return DisconnectObserved(**fields)


def _readback(number: int = 1, **over: object) -> BrokerReadback:
    fields: dict[str, object] = {
        "record_id": f"readback-{number:03d}",
        "intent_id": "intent-001",
        "status": BrokerReadbackStatus.OPEN,
        "broker_order_id": "paper-order-001",
        "total_quantity": 3,
        "cumulative_quantity": 0,
        "broker_snapshot_at": _at(10 + number),
        "locally_received_at": _at(11 + number),
        "source": "synthetic-paper-adapter",
        "source_sequence_id": f"readback-seq-{number:03d}",
        "broker_sequence_id": f"broker-readback-{number:03d}",
    }
    fields.update(over)
    return BrokerReadback(**fields)


def _replace(**over: object) -> ReplaceIntent:
    fields: dict[str, object] = {
        "record_id": "replace-001",
        "intent_id": "intent-001",
        "based_on_readback_id": "readback-001",
        "broker_order_id": "paper-order-001",
        "new_total_quantity": 3,
        "new_limit_price": Decimal("1.10"),
        "replace_created_at": _at(13),
        "source": "executor",
        "source_sequence_id": "replace-seq-001",
    }
    fields.update(over)
    return ReplaceIntent(**fields)


def _submitting() -> ExecutionLifecycle:
    return ExecutionLifecycle.start(_intent()).apply(_attempt())


def test_start_and_retry_reuse_one_intent_identity() -> None:
    created = ExecutionLifecycle.start(_intent())
    first = created.apply(_attempt())
    retry = first.apply(_attempt(2))
    assert created.state is ExecutionState.CREATED
    assert first.state is ExecutionState.SUBMITTING
    assert retry.state is ExecutionState.SUBMITTING
    assert retry.intent.intent_id == created.intent.intent_id
    assert retry.submit_attempt_ids == ("attempt-001", "attempt-002")

    foreign = _attempt(3, intent_id="intent-NEW")
    with pytest.raises(IntentMismatchError):
        retry.apply(foreign)
    assert retry.submit_attempt_ids == ("attempt-001", "attempt-002")


@pytest.mark.parametrize("uncertain", [_timeout(), _disconnect()])
def test_timeout_or_disconnect_is_unknown_and_cannot_authorize_resubmit_or_replace(
    uncertain: TimeoutObserved | DisconnectObserved,
) -> None:
    unknown = _submitting().apply(uncertain)
    assert unknown.state is ExecutionState.UNKNOWN
    invalid_retry = _attempt(2, send_attempt_at=_at(6))
    retained = unknown.apply(invalid_retry)
    assert retained.state is ExecutionState.RECONCILIATION_REQUIRED
    assert "RETRY_AFTER_LOCAL_KNOWLEDGE" in retained.reconciliation_reasons
    assert invalid_retry in retained.records
    with pytest.raises(IntentMismatchError):
        unknown.apply(_attempt(2, intent_id="new-intent", send_attempt_at=_at(6)))
    with pytest.raises(ReplacementRefusedError, match="readback"):
        unknown.apply(_replace())
    assert unknown.state is ExecutionState.UNKNOWN


def test_only_fresh_readback_can_recover_unknown() -> None:
    unknown = _submitting().apply(_timeout())
    acknowledged = unknown.apply(_ack(locally_received_at=_at(6)))
    assert acknowledged.state is ExecutionState.UNKNOWN
    assert acknowledged.broker_order_id == "paper-order-001"

    recovered_by_readback = acknowledged.apply(_readback())
    assert recovered_by_readback.state is ExecutionState.ACKNOWLEDGED
    assert recovered_by_readback.replace_basis_id == "readback-001"


def test_broker_fact_received_before_uncertainty_cannot_recover_unknown() -> None:
    unknown = _submitting().apply(_timeout(locally_received_at=_at(5)))
    stale_delivery = unknown.apply(
        _ack(
            broker_acknowledged_at=_at(2),
            locally_received_at=_at(4),
        )
    )
    assert stale_delivery.state is ExecutionState.UNKNOWN
    assert stale_delivery.broker_order_id == "paper-order-001"


def test_exact_duplicate_payload_is_an_identity_preserving_noop() -> None:
    submitting = _submitting()
    ack = _ack()
    once = submitting.apply(ack)
    twice = once.apply(ack)
    assert twice is once
    assert len(twice.records) == len(once.records)


def test_same_record_id_with_different_bytes_refuses_without_state_change() -> None:
    once = _submitting().apply(_ack())
    collision = _ack(broker_order_id="different-order")
    with pytest.raises(RecordIdentityCollisionError):
        once.apply(collision)
    assert once.state is ExecutionState.ACKNOWLEDGED
    assert once.broker_order_id == "paper-order-001"


def test_same_source_or_broker_sequence_with_different_bytes_refuses() -> None:
    once = _submitting().apply(_ack())
    source_collision = _ack(
        number=2,
        source_sequence_id="source-ack-001",
        broker_sequence_id="broker-ack-002",
    )
    with pytest.raises(SequenceIdentityCollisionError, match="source"):
        once.apply(source_collision)

    broker_collision = _ack(
        number=2,
        source_sequence_id="source-ack-002",
        broker_sequence_id="broker-ack-001",
    )
    with pytest.raises(SequenceIdentityCollisionError, match="broker"):
        once.apply(broker_collision)
    assert once.state is ExecutionState.ACKNOWLEDGED


def test_ack_reordered_after_partial_fill_does_not_regress_state() -> None:
    partial = _submitting().apply(_partial())
    assert partial.state is ExecutionState.RECONCILIATION_REQUIRED
    late_ack = partial.apply(_ack())
    assert late_ack.state is ExecutionState.PARTIALLY_FILLED
    assert late_ack.filled_quantity == 1


def test_older_missing_partial_fill_after_complete_closes_economic_gap() -> None:
    incomplete = _submitting().apply(_ack()).apply(_complete())
    assert incomplete.state is ExecutionState.RECONCILIATION_REQUIRED
    reordered = incomplete.apply(
        _partial(
            exchange_event_at=_at(7),
            locally_received_at=_at(10),
        )
    )
    assert reordered.state is ExecutionState.FILLED
    assert reordered.filled_quantity == 3


def test_newer_regressive_fill_requires_reconciliation() -> None:
    partial_two = (
        _submitting()
        .apply(_ack())
        .apply(_partial(number=2, fill_quantity=2, cumulative_quantity=2))
    )
    regressive = partial_two.apply(
        _partial(
            number=3,
            cumulative_quantity=1,
            exchange_event_at=_at(20),
            locally_received_at=_at(21),
        )
    )
    assert regressive.state is ExecutionState.RECONCILIATION_REQUIRED
    assert regressive.filled_quantity == 2


def test_blind_or_stale_readback_replacement_is_refused() -> None:
    acknowledged = _submitting().apply(_ack())
    with pytest.raises(ReplacementRefusedError, match="current matching readback"):
        acknowledged.apply(_replace())

    refreshed = acknowledged.apply(_readback())
    accepted = refreshed.apply(_replace())
    assert accepted.state is ExecutionState.ACKNOWLEDGED
    assert accepted.replace_basis_id is None

    refreshed_again = acknowledged.apply(_readback())
    changed_after_readback = refreshed_again.apply(
        _ack(
            number=2,
            broker_acknowledged_at=_at(14),
            locally_received_at=_at(15),
        )
    )
    replayed = changed_after_readback.apply(_replace())
    assert replayed.pending_replace_total_quantity == 3

    with pytest.raises(ReplacementRefusedError, match="current matching readback"):
        changed_after_readback.apply(_replace(replace_created_at=_at(16)))


def test_temporally_stale_readback_does_not_regress_or_authorize_replace() -> None:
    partial = _submitting().apply(_ack()).apply(_partial())
    stale = _readback(
        status=BrokerReadbackStatus.OPEN,
        cumulative_quantity=0,
        broker_snapshot_at=_at(3),
        locally_received_at=_at(12),
    )
    projected = partial.apply(stale)
    assert projected.state is ExecutionState.PARTIALLY_FILLED
    assert projected.filled_quantity == 1
    assert projected.replace_basis_id is None
    with pytest.raises(ReplacementRefusedError, match="current matching readback"):
        projected.apply(_replace())


def test_broker_fact_cannot_predate_first_submit_attempt() -> None:
    submitting = ExecutionLifecycle.start(_intent()).apply(_attempt(send_attempt_at=_at(5)))
    with pytest.raises(TemporalOrderError):
        submitting.apply(
            _ack(
                broker_acknowledged_at=_at(4),
                locally_received_at=_at(6),
            )
        )


def test_replace_basis_must_match_order_quantity_and_time() -> None:
    current = (
        _submitting()
        .apply(_ack())
        .apply(_partial(number=2, fill_quantity=2, cumulative_quantity=2))
        .apply(
            _readback(
                status=BrokerReadbackStatus.PARTIALLY_FILLED,
                cumulative_quantity=2,
            )
        )
    )
    with pytest.raises(ReplacementRefusedError, match="broker order"):
        current.apply(_replace(broker_order_id="other-order"))
    with pytest.raises(ReplacementRefusedError, match="filled quantity"):
        current.apply(_replace(new_total_quantity=1))
    with pytest.raises(TemporalOrderError):
        current.apply(_replace(replace_created_at=_at(1)))


def test_no_state_is_inferred_from_missing_submit_events() -> None:
    created = ExecutionLifecycle.start(_intent())
    stray_ack = created.apply(_ack())
    stray_fill = created.apply(_partial())
    assert stray_ack.state is ExecutionState.RECONCILIATION_REQUIRED
    assert stray_fill.state is ExecutionState.RECONCILIATION_REQUIRED


def test_conflicting_broker_order_identity_requires_reconciliation() -> None:
    acknowledged = _submitting().apply(_ack())
    conflicting = acknowledged.apply(_partial(broker_order_id="other-order"))
    assert conflicting.state is ExecutionState.RECONCILIATION_REQUIRED
    assert conflicting.broker_order_id == "paper-order-001"


@pytest.mark.parametrize(
    "contradictory_fill",
    [
        _complete(fill_quantity=2, cumulative_quantity=2),
        _partial(fill_quantity=3, cumulative_quantity=3),
    ],
)
def test_fill_total_kind_contradictions_are_retained_for_reconciliation(
    contradictory_fill: PartialFill | CompleteFill,
) -> None:
    projected = _submitting().apply(_ack()).apply(contradictory_fill)
    assert projected.state is ExecutionState.RECONCILIATION_REQUIRED
    assert "FILL_KIND_TOTAL_MISMATCH" in projected.reconciliation_reasons
    assert projected.records[-1] == contradictory_fill


def test_reject_is_terminal_but_fill_after_reject_requires_reconciliation() -> None:
    rejected = _submitting().apply(
        OrderReject(
            record_id="reject-001",
            intent_id="intent-001",
            broker_order_id="paper-order-001",
            reason_code="synthetic-reject",
            broker_acknowledged_at=_at(2),
            locally_received_at=_at(3),
            source="synthetic-paper-adapter",
            source_sequence_id="source-reject-001",
            broker_sequence_id="broker-reject-001",
        )
    )
    assert rejected.state is ExecutionState.REJECTED
    raced = rejected.apply(_partial())
    assert raced.state is ExecutionState.RECONCILIATION_REQUIRED


def test_reject_without_echoed_order_id_preserves_known_broker_identity() -> None:
    unknown = _submitting().apply(_ack()).apply(_disconnect(locally_received_at=_at(5)))
    rejected = unknown.apply(
        OrderReject(
            record_id="reject-no-order-id",
            intent_id="intent-001",
            broker_order_id=None,
            reason_code="synthetic-reject",
            broker_acknowledged_at=_at(6),
            locally_received_at=_at(7),
            source="synthetic-paper-adapter",
            source_sequence_id="source-reject-no-order-id",
            broker_sequence_id="broker-reject-no-order-id",
        )
    )
    assert rejected.state is ExecutionState.REJECTED
    assert rejected.broker_order_id == "paper-order-001"


def test_readback_cannot_erase_an_observed_fill_quantity() -> None:
    partial = _submitting().apply(_ack()).apply(_partial())
    contradictory = partial.apply(
        _readback(
            status=BrokerReadbackStatus.CANCELED,
            cumulative_quantity=0,
        )
    )
    assert contradictory.state is ExecutionState.RECONCILIATION_REQUIRED
    assert contradictory.filled_quantity == 1


def test_timeout_after_filled_is_retained_without_regression() -> None:
    filled = _submitting().apply(_ack()).apply(_complete(fill_quantity=3))
    timeout = _timeout(
        record_id="timeout-late",
        locally_received_at=_at(20),
        source_sequence_id="timeout-late",
    )
    projected = filled.apply(timeout)
    assert projected.state is ExecutionState.FILLED
    assert projected.records[-1] == timeout
