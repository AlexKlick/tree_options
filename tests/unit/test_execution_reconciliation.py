"""M6 reconciliation: retained reasons adjudicated, classified, explained."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tree_options.execution import (
    BrokerAcknowledgement,
    CompleteFill,
    ExecutionLifecycle,
    ExecutionState,
    OrderIntent,
    PartialFill,
    ReconciliationReason,
    ReconciliationSeverity,
    reconcile,
)

T0 = datetime(2026, 9, 9, 20, 0, tzinfo=UTC)


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
        "fill_quantity": 1,
        "cumulative_quantity": 3,
        "unit_price": Decimal("1.22"),
        "fees": Decimal("1.30"),
        "exchange_event_at": _at(9),
        "locally_received_at": _at(10),
        "source": "synthetic-paper-adapter",
        "source_sequence_id": "source-fill-003",
        "broker_sequence_id": "broker-fill-003",
    }
    fields.update(over)
    return CompleteFill(**fields)


def _submit_acknowledge() -> ExecutionLifecycle:
    from tree_options.execution import SubmitAttempt

    attempt = SubmitAttempt(
        record_id="attempt-001",
        intent_id="intent-001",
        send_attempt_at=_at(1),
        source="executor",
        source_sequence_id="attempt-seq-001",
    )
    return ExecutionLifecycle.start(_intent()).apply(attempt).apply(_ack())


def _overlapping_fills_lifecycle() -> ExecutionLifecycle:
    """ack + fill(0,1) + fill(0,2) — the second fill double-books contracts."""
    return (
        _submit_acknowledge()
        .apply(_partial(1))
        .apply(_partial(2, fill_quantity=2, cumulative_quantity=2))
    )


def test_clean_lifecycle_reports_no_findings() -> None:
    lifecycle = _submit_acknowledge().apply(_partial(1)).apply(_partial(2)).apply(_complete())
    report = reconcile(lifecycle)
    assert report.is_clean
    assert report.findings == ()
    assert report.severities == frozenset()
    assert report.economic_findings == ()
    assert report.summary() == "intent-001: clean (FILLED)"
    assert report.state is ExecutionState.FILLED


def test_overlapping_fills_are_adjudicated_economic_with_the_pinned_explanation() -> None:
    report = reconcile(_overlapping_fills_lifecycle())
    assert not report.is_clean
    overlap = [f for f in report.findings if f.reason is ReconciliationReason.FILL_ECONOMIC_OVERLAP]
    assert overlap, "overlapping fills must retain FILL_ECONOMIC_OVERLAP"
    assert overlap[0].severity is ReconciliationSeverity.ECONOMIC
    assert overlap[0].explanation == (
        "two fills claim economics over the same contracts — the executed quantity is double-booked"
    )
    assert ReconciliationSeverity.ECONOMIC in report.severities
    assert overlap[0] in report.economic_findings


def test_missing_submit_is_protocol_and_ambiguous_readback_is_identity() -> None:
    # broker facts with no submit attempt retained
    unsubmitted = ExecutionLifecycle.start(_intent()).apply(_ack(5, record_id="ack-005"))
    report = reconcile(unsubmitted)
    severities = {f.reason: f.severity for f in report.findings}
    assert severities[ReconciliationReason.MISSING_SUBMIT] is ReconciliationSeverity.PROTOCOL
    # an ambiguous broker readback leaves the order's standing undecidable
    # (an AMBIGUOUS readback must not claim a total_quantity — record contract)
    from tree_options.execution import BrokerReadback, BrokerReadbackStatus

    ambiguous = _submit_acknowledge().apply(
        BrokerReadback(
            record_id="readback-001",
            intent_id="intent-001",
            status=BrokerReadbackStatus.AMBIGUOUS,
            broker_order_id=None,
            total_quantity=None,
            cumulative_quantity=0,
            broker_snapshot_at=_at(12),
            locally_received_at=_at(13),
            source="synthetic-paper-adapter",
            source_sequence_id="readback-seq-001",
            broker_sequence_id="broker-readback-001",
        )
    )
    severities = {f.reason: f.severity for f in reconcile(ambiguous).findings}
    assert severities[ReconciliationReason.AMBIGUOUS_READBACK] is ReconciliationSeverity.IDENTITY


def test_severity_and_explanation_mappings_are_total_and_pinned() -> None:
    from tree_options.execution.reconciliation import (
        _REASON_EXPLANATIONS,
        _REASON_SEVERITY,
        _SEVERITY_RANK,
    )

    assert set(_REASON_SEVERITY) == set(ReconciliationReason)
    assert set(_REASON_EXPLANATIONS) == set(ReconciliationReason)
    assert all(str(reason.value).isupper() for reason in ReconciliationReason)
    for reason in ReconciliationReason:
        assert _REASON_EXPLANATIONS[reason]
        assert _REASON_SEVERITY[reason] in _SEVERITY_RANK


def test_findings_sort_worst_severity_first_then_reason_name() -> None:
    # ECONOMIC (overlap) + IDENTITY (ambiguous readback): severity order is
    # the REVERSE of the reasons' alphabetical order, so a name-only sort
    # cannot pass this fixture.
    from tree_options.execution import BrokerReadback, BrokerReadbackStatus

    ambiguous = BrokerReadback(
        record_id="readback-001",
        intent_id="intent-001",
        status=BrokerReadbackStatus.AMBIGUOUS,
        broker_order_id=None,
        total_quantity=None,
        cumulative_quantity=0,
        broker_snapshot_at=_at(12),
        locally_received_at=_at(13),
        source="synthetic-paper-adapter",
        source_sequence_id="readback-seq-001",
        broker_sequence_id="broker-readback-001",
    )
    mixed = _overlapping_fills_lifecycle().apply(ambiguous)
    report = reconcile(mixed)
    assert [f.reason for f in report.findings] == [
        ReconciliationReason.FILL_ECONOMIC_OVERLAP,
        ReconciliationReason.AMBIGUOUS_READBACK,
    ]
    # within one severity, findings order by reason NAME (the PROTOCOL pair
    # of a no-submit overlap: MISSING_SUBMIT + TOTAL_UNCONFIRMED)
    unsubmitted = (
        ExecutionLifecycle.start(_intent())
        .apply(_ack(2, record_id="ack-002"))
        .apply(_partial(1))
        .apply(_partial(2, fill_quantity=2, cumulative_quantity=2))
    )
    protocol = reconcile(unsubmitted)
    assert [
        f.reason for f in protocol.findings if f.severity is ReconciliationSeverity.PROTOCOL
    ] == [
        ReconciliationReason.MISSING_SUBMIT,
        ReconciliationReason.TOTAL_UNCONFIRMED,
    ]
    # the ECONOMIC filter isolates exactly the economic finding(s)
    assert len(report.economic_findings) == 1
    assert report.economic_findings[0].reason is ReconciliationReason.FILL_ECONOMIC_OVERLAP


def test_reconcile_is_pure_and_repeatable() -> None:
    lifecycle = _overlapping_fills_lifecycle()
    reasons_before = lifecycle.reconciliation_reasons
    first = reconcile(lifecycle)
    second = reconcile(lifecycle)
    assert first == second
    assert lifecycle.reconciliation_reasons == reasons_before


def test_summary_lines_are_deterministic() -> None:
    dirty = reconcile(_overlapping_fills_lifecycle())
    assert dirty.summary().startswith("intent-001: ")
    assert "worst " in dirty.summary()
    assert dirty.summary() == reconcile(_overlapping_fills_lifecycle()).summary()
    # the clean summary names the state, nothing else
    clean = reconcile(ExecutionLifecycle.start(_intent()))
    assert clean.summary() == "intent-001: clean (CREATED)"
