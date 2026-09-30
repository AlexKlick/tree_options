"""Run inside the TREX repository so this test uses the real execution.records contract."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tree_options.execution.records import BrokerReadbackStatus, OrderIntent
from tree_options.execution.snaptrade_adapter import (
    EXACT_FILL_EVIDENCE_SUPPORTED,
    SnapTradeNormalizationError,
    normalize_status,
    readback_from_snapshot,
    require_exact_fill_evidence,
    snapshot_from_mapping,
    stable_client_order_id,
    timeout_observed,
)

NOW = datetime(2026, 9, 29, 20, 0, tzinfo=UTC)


def intent():
    return OrderIntent(
        intent_id="intent-1",
        contract_id="AAPL",
        side="BUY",
        position_effect="OPEN_LONG",
        quantity=2,
        order_type="LIMIT",
        limit_price=Decimal("100"),
        intent_created_at=NOW - timedelta(minutes=1),
        source="test",
        source_sequence_id="intent-seq-1",
    )


def test_client_id_stable_and_distinct():
    assert stable_client_order_id("a") == stable_client_order_id("a")
    assert stable_client_order_id("a") != stable_client_order_id("b")


def test_unknown_and_pending_mutation_states_fail_closed():
    assert normalize_status("NEW_PROVIDER_STATE") is BrokerReadbackStatus.AMBIGUOUS
    assert normalize_status("CANCEL_PENDING") is BrokerReadbackStatus.AMBIGUOUS
    assert normalize_status("REPLACE_PENDING") is BrokerReadbackStatus.AMBIGUOUS


def test_fractional_quantity_refused_by_current_trex_schema():
    with pytest.raises(SnapTradeNormalizationError, match="fractional"):
        snapshot_from_mapping(
            {
                "status": "PARTIAL",
                "brokerage_order_id": "b1",
                "total_quantity": "2.5",
                "filled_quantity": "1",
                "time_updated": NOW.isoformat(),
            }
        )


def test_partial_readback_maps_without_fabricating_fill():
    snap = snapshot_from_mapping(
        {
            "status": "PARTIAL",
            "brokerage_order_id": "b1",
            "total_quantity": 2,
            "filled_quantity": 1,
            "time_updated": NOW.isoformat(),
        },
        request_id="req-1",
    )
    record = readback_from_snapshot(intent(), snap, locally_received_at=NOW + timedelta(seconds=1))
    assert record.status is BrokerReadbackStatus.PARTIALLY_FILLED
    assert record.cumulative_quantity == 1 and record.total_quantity == 2


def test_quantity_conservation_violation_is_refused():
    with pytest.raises(SnapTradeNormalizationError, match="must equal"):
        snapshot_from_mapping(
            {
                "status": "PARTIAL_CANCELED",
                "brokerage_order_id": "b1",
                "total_quantity": 10,
                "filled_quantity": 4,
                "open_quantity": 1,
                "canceled_quantity": 4,
                "time_updated": NOW.isoformat(),
            }
        )


def test_ambiguous_or_rejected_positive_fill_is_not_erased():
    for status in ("NONE", "CANCEL_PENDING", "REJECTED"):
        snap = snapshot_from_mapping(
            {
                "status": status,
                "brokerage_order_id": "b1",
                "total_quantity": 2,
                "filled_quantity": 1,
                "time_updated": NOW.isoformat(),
            }
        )
        with pytest.raises(SnapTradeNormalizationError, match="cannot be represented losslessly"):
            readback_from_snapshot(intent(), snap, locally_received_at=NOW + timedelta(seconds=1))


def test_timeout_is_uncertainty_not_retry_and_fill_gate_stays_closed():
    row = timeout_observed(intent(), attempt_id="attempt-1", locally_received_at=NOW)
    assert row.attempt_id == "attempt-1"
    assert EXACT_FILL_EVIDENCE_SUPPORTED is False
    with pytest.raises(SnapTradeNormalizationError, match="not exact fill"):
        require_exact_fill_evidence()


def test_real_lifecycle_readback_never_admits_fabricated_economics():
    from tree_options.execution.evidence import assess_evidence
    from tree_options.execution.lifecycle import ExecutionLifecycle, ExecutionState
    from tree_options.execution.reconciliation import reconcile
    from tree_options.execution.snaptrade_adapter import submit_attempt

    order = intent()
    lifecycle = ExecutionLifecycle.start(order).apply(
        submit_attempt(order, attempt_id="s", send_attempt_at=NOW)
    )
    snap = snapshot_from_mapping(
        {
            "status": "EXECUTED",
            "brokerage_order_id": "b1",
            "total_quantity": 2,
            "filled_quantity": 2,
            "time_updated": NOW.isoformat(),
            "execution_price": "100",
            "fees": None,
        }
    )
    row = readback_from_snapshot(order, snap, locally_received_at=NOW + timedelta(seconds=1))
    lifecycle = lifecycle.apply(row)
    assert reconcile(lifecycle).intent_id == order.intent_id
    assert lifecycle.broker_state is ExecutionState.FILLED
    assert lifecycle.apply(row) == lifecycle
    assert assess_evidence(lifecycle).economics is None
    assert not assess_evidence(lifecycle).is_admissible
    assert all(
        record.record_type not in {"PARTIAL_FILL", "COMPLETE_FILL"} for record in lifecycle.records
    )


def test_real_lifecycle_identity_collision_and_order_change():
    from tree_options.execution.lifecycle import (
        ExecutionLifecycle,
        ReconciliationReason,
        RecordIdentityCollisionError,
    )
    from tree_options.execution.snaptrade_adapter import submit_attempt

    order = intent()
    base = ExecutionLifecycle.start(order).apply(
        submit_attempt(order, attempt_id="s", send_attempt_at=NOW)
    )
    snap = snapshot_from_mapping(
        {
            "status": "ACCEPTED",
            "brokerage_order_id": "b1",
            "total_quantity": 2,
            "filled_quantity": 0,
            "time_updated": NOW.isoformat(),
        },
        request_id="req",
    )
    row = readback_from_snapshot(order, snap, locally_received_at=NOW + timedelta(seconds=1))
    base = base.apply(row)
    with pytest.raises(RecordIdentityCollisionError):
        base.apply(row.model_copy(update={"locally_received_at": NOW + timedelta(seconds=2)}))
    other = row.model_copy(
        update={
            "record_id": "different",
            "broker_sequence_id": "different",
            "source_sequence_id": "different",
            "broker_order_id": "b2",
        }
    )
    assert ReconciliationReason.BROKER_ORDER_ID_CONFLICT in base.apply(other).reconciliation_reasons


def test_order_placement_time_is_not_a_snapshot_time():
    with pytest.raises(SnapTradeNormalizationError, match="snapshot"):
        snapshot_from_mapping(
            {
                "status": "ACCEPTED",
                "brokerage_order_id": "b1",
                "total_quantity": 2,
                "filled_quantity": 0,
                "time_placed": NOW.isoformat(),
            }
        )


def test_missing_fill_quantity_is_unknown_not_zero():
    with pytest.raises(SnapTradeNormalizationError, match="filled_quantity"):
        snapshot_from_mapping(
            {
                "status": "ACCEPTED",
                "brokerage_order_id": "b1",
                "total_quantity": 2,
                "time_updated": NOW.isoformat(),
            }
        )


def test_provider_total_must_match_intent():
    snap = snapshot_from_mapping(
        {
            "status": "ACCEPTED",
            "brokerage_order_id": "b1",
            "total_quantity": 3,
            "filled_quantity": 0,
            "time_updated": NOW.isoformat(),
        }
    )
    with pytest.raises(SnapTradeNormalizationError, match="intent quantity"):
        readback_from_snapshot(intent(), snap, locally_received_at=NOW + timedelta(seconds=1))


def test_unknown_state_preserves_known_source_identity_without_accepting_order():
    row = snapshot_from_mapping(
        {
            "status": "PROVIDER_NEW_STATE",
            "brokerage_order_id": "known-id",
            "total_quantity": 2,
            "filled_quantity": 0,
            "time_updated": NOW.isoformat(),
        }
    )
    fact = readback_from_snapshot(intent(), row, locally_received_at=NOW)
    assert fact.status is BrokerReadbackStatus.AMBIGUOUS
    assert fact.broker_order_id is None and fact.total_quantity is None
    assert fact.observed_order_id == "known-id"
    assert fact.observed_total_quantity == 2
    assert fact.observed_status == "PROVIDER_NEW_STATE"


def test_observed_and_accepted_facts_cannot_contradict():
    from pydantic import ValidationError

    row = snapshot_from_mapping(
        {
            "status": "ACCEPTED",
            "brokerage_order_id": "known-id",
            "total_quantity": 2,
            "filled_quantity": 0,
            "time_updated": NOW.isoformat(),
        }
    )
    fact = readback_from_snapshot(intent(), row, locally_received_at=NOW)
    for changes in ({"observed_order_id": "other-id"}, {"observed_total_quantity": 3}):
        with pytest.raises(ValidationError, match="disagree"):
            type(fact).model_validate({**fact.model_dump(), **changes})


def rejected_snapshot():
    return snapshot_from_mapping(
        {
            "status": "REJECTED",
            "brokerage_order_id": "rejected-id",
            "total_quantity": 2,
            "filled_quantity": 0,
            "time_placed": (NOW - timedelta(seconds=10)).isoformat(),
            "time_updated": NOW.isoformat(),
        }
    )


def test_rejection_event_time_cannot_be_inferred_from_snapshot():
    from tree_options.execution.snaptrade_adapter import rejection_from_snapshot

    with pytest.raises(SnapTradeNormalizationError, match="authoritative rejection"):
        rejection_from_snapshot(intent(), rejected_snapshot(), locally_received_at=NOW)


def test_rejection_event_time_uses_only_explicit_authoritative_event():
    from tree_options.execution.snaptrade_adapter import rejection_from_snapshot

    actual_rejection = NOW - timedelta(seconds=3)
    record = rejection_from_snapshot(
        intent(),
        rejected_snapshot(),
        locally_received_at=NOW,
        broker_rejected_at=actual_rejection,
    )
    assert record.broker_acknowledged_at == actual_rejection
    assert record.broker_acknowledged_at != rejected_snapshot().broker_snapshot_at
    assert record.broker_acknowledged_at != rejected_snapshot().broker_placed_at


def test_authoritative_rejection_time_cannot_erase_positive_execution():
    from dataclasses import replace

    from tree_options.execution.snaptrade_adapter import rejection_from_snapshot

    snapshot = replace(rejected_snapshot(), filled_quantity=1)
    with pytest.raises(SnapTradeNormalizationError, match="positive filled"):
        rejection_from_snapshot(
            intent(),
            snapshot,
            locally_received_at=NOW,
            broker_rejected_at=NOW,
        )
