"""Strict broker-neutral execution record contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pydantic
import pytest

from tree_options.execution import (
    BrokerAcknowledgement,
    BrokerReadback,
    BrokerReadbackStatus,
    CompleteFill,
    DisconnectObserved,
    OrderIntent,
    OrderReject,
    PartialFill,
    ReplaceIntent,
    SubmitAttempt,
    TimeoutObserved,
)

T0 = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
T1 = T0 + timedelta(seconds=1)
T2 = T0 + timedelta(seconds=2)


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


def _ack(**over: object) -> BrokerAcknowledgement:
    fields: dict[str, object] = {
        "record_id": "ack-001",
        "intent_id": "intent-001",
        "broker_order_id": "paper-order-001",
        "broker_acknowledged_at": T1,
        "locally_received_at": T2,
        "source": "synthetic-paper-adapter",
        "source_sequence_id": "source-ack-001",
        "broker_sequence_id": "broker-001",
    }
    fields.update(over)
    return BrokerAcknowledgement(**fields)


def _partial_fill(**over: object) -> PartialFill:
    fields: dict[str, object] = {
        "record_id": "fill-001",
        "intent_id": "intent-001",
        "broker_order_id": "paper-order-001",
        "fill_quantity": 1,
        "cumulative_quantity": 1,
        "unit_price": Decimal("1.20"),
        "fees": Decimal("0.65"),
        "exchange_event_at": T1,
        "locally_received_at": T2,
        "source": "synthetic-paper-adapter",
        "source_sequence_id": "source-fill-001",
        "broker_sequence_id": "broker-002",
    }
    fields.update(over)
    return PartialFill(**fields)


def test_records_are_frozen_and_forbid_unknown_fields() -> None:
    intent = _intent()
    with pytest.raises(pydantic.ValidationError):
        intent.quantity = 4
    with pytest.raises(pydantic.ValidationError):
        _intent(broker_name="specific-broker")


@pytest.mark.parametrize(
    "factory",
    [
        lambda naive: _intent(intent_created_at=naive),
        lambda naive: SubmitAttempt(
            record_id="attempt-001",
            intent_id="intent-001",
            send_attempt_at=naive,
            source="executor",
            source_sequence_id="attempt-seq-001",
        ),
        lambda naive: _ack(broker_acknowledged_at=naive),
        lambda naive: OrderReject(
            record_id="reject-001",
            intent_id="intent-001",
            broker_order_id="paper-order-001",
            reason_code="synthetic-reject",
            broker_acknowledged_at=T1,
            locally_received_at=naive,
            source="synthetic-paper-adapter",
            source_sequence_id="source-reject-001",
            broker_sequence_id="broker-003",
        ),
        lambda naive: _partial_fill(exchange_event_at=naive),
        lambda naive: TimeoutObserved(
            record_id="timeout-001",
            intent_id="intent-001",
            attempt_id="attempt-001",
            locally_received_at=naive,
            source="executor",
            source_sequence_id="timeout-seq-001",
        ),
        lambda naive: DisconnectObserved(
            record_id="disconnect-001",
            intent_id="intent-001",
            locally_received_at=naive,
            source="executor",
            source_sequence_id="disconnect-seq-001",
        ),
        lambda naive: BrokerReadback(
            record_id="readback-001",
            intent_id="intent-001",
            status=BrokerReadbackStatus.OPEN,
            broker_order_id="paper-order-001",
            cumulative_quantity=0,
            broker_snapshot_at=T1,
            locally_received_at=naive,
            source="synthetic-paper-adapter",
            source_sequence_id="readback-seq-001",
            broker_sequence_id="broker-readback-001",
        ),
        lambda naive: ReplaceIntent(
            record_id="replace-001",
            intent_id="intent-001",
            based_on_readback_id="readback-001",
            broker_order_id="paper-order-001",
            new_total_quantity=3,
            new_limit_price=Decimal("1.10"),
            replace_created_at=naive,
            source="executor",
            source_sequence_id="replace-seq-001",
        ),
    ],
)
def test_every_timestamp_role_rejects_naive_datetime(factory: object) -> None:
    naive = datetime(2026, 8, 24, 14, 0)
    with pytest.raises(pydantic.ValidationError):
        factory(naive)  # type: ignore[operator]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _intent(limit_price=1.25),
        lambda: _partial_fill(unit_price=1.20),
        lambda: _partial_fill(fees=0.65),
        lambda: ReplaceIntent(
            record_id="replace-001",
            intent_id="intent-001",
            based_on_readback_id="readback-001",
            broker_order_id="paper-order-001",
            new_total_quantity=3,
            new_limit_price=1.10,
            replace_created_at=T2,
            source="executor",
            source_sequence_id="replace-seq-001",
        ),
    ],
)
def test_monetary_fields_refuse_float_inputs(factory: object) -> None:
    with pytest.raises(pydantic.ValidationError, match="Decimal"):
        factory()  # type: ignore[operator]


def test_monetary_fields_remain_exact_decimal() -> None:
    intent = _intent()
    fill = _partial_fill()
    assert type(intent.limit_price) is Decimal
    assert intent.limit_price == Decimal("1.25")
    assert type(fill.unit_price) is Decimal
    assert type(fill.fees) is Decimal


def test_decimal_records_round_trip_json_without_float_conversion() -> None:
    intent = _intent()
    fill = _partial_fill()
    intent_round_trip = OrderIntent.model_validate_json(intent.model_dump_json())
    fill_round_trip = PartialFill.model_validate_json(fill.model_dump_json())
    assert intent_round_trip == intent
    assert fill_round_trip == fill
    assert type(intent_round_trip.limit_price) is Decimal
    assert type(fill_round_trip.unit_price) is Decimal
    assert type(fill_round_trip.fees) is Decimal


def test_market_and_limit_shapes_are_fail_closed() -> None:
    with pytest.raises(pydantic.ValidationError, match="LIMIT order requires"):
        _intent(limit_price=None)
    with pytest.raises(pydantic.ValidationError, match="MARKET order must not"):
        _intent(order_type="MARKET", limit_price=Decimal("1.25"))


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _intent(quantity="3"),
        lambda: _partial_fill(fill_quantity=1.0),
        lambda: _partial_fill(cumulative_quantity="1"),
        lambda: BrokerReadback(
            record_id="readback-001",
            intent_id="intent-001",
            status=BrokerReadbackStatus.OPEN,
            broker_order_id="paper-order-001",
            cumulative_quantity=0.0,
            broker_snapshot_at=T1,
            locally_received_at=T2,
            source="synthetic-paper-adapter",
            source_sequence_id="readback-seq-001",
            broker_sequence_id="broker-readback-001",
        ),
        lambda: ReplaceIntent(
            record_id="replace-001",
            intent_id="intent-001",
            based_on_readback_id="readback-001",
            broker_order_id="paper-order-001",
            new_total_quantity="3",
            new_limit_price=Decimal("1.10"),
            replace_created_at=T2,
            source="executor",
            source_sequence_id="replace-seq-001",
        ),
    ],
)
def test_quantity_fields_refuse_numeric_coercion(factory: object) -> None:
    with pytest.raises(pydantic.ValidationError):
        factory()  # type: ignore[operator]


def test_package_and_legged_execution_remain_distinct() -> None:
    package = _intent(execution_style="package", package_id="spread-001")
    legged = _intent(execution_style="legged", package_id="spread-001")
    assert package.execution_style != legged.execution_style
    with pytest.raises(pydantic.ValidationError, match="requires package_id"):
        _intent(execution_style="package", package_id=None)
    with pytest.raises(pydantic.ValidationError, match="must not carry package_id"):
        _intent(execution_style="single", package_id="spread-001")


def test_internal_external_timestamp_order_is_validated() -> None:
    with pytest.raises(pydantic.ValidationError, match="acknowledged"):
        _ack(broker_acknowledged_at=T2, locally_received_at=T1)
    with pytest.raises(pydantic.ValidationError, match="exchange_event_at"):
        _partial_fill(exchange_event_at=T2, locally_received_at=T1)
    with pytest.raises(pydantic.ValidationError, match="broker_snapshot_at"):
        BrokerReadback(
            record_id="readback-001",
            intent_id="intent-001",
            status=BrokerReadbackStatus.OPEN,
            broker_order_id="paper-order-001",
            cumulative_quantity=0,
            broker_snapshot_at=T2,
            locally_received_at=T1,
            source="synthetic-paper-adapter",
            source_sequence_id="readback-seq-001",
            broker_sequence_id="broker-readback-001",
        )


def test_fill_record_kinds_encode_partial_vs_complete() -> None:
    partial = _partial_fill()
    complete = CompleteFill(
        **partial.model_dump(
            exclude={
                "record_type",
                "record_id",
                "fill_quantity",
                "cumulative_quantity",
                "source_sequence_id",
                "broker_sequence_id",
            }
        ),
        record_id="fill-002",
        fill_quantity=2,
        cumulative_quantity=3,
        source_sequence_id="source-fill-002",
        broker_sequence_id="broker-003",
    )
    assert partial.record_type == "PARTIAL_FILL"
    assert complete.record_type == "COMPLETE_FILL"


def test_readback_shape_is_explicit_for_absent_and_open_orders() -> None:
    absent = BrokerReadback(
        record_id="readback-absent",
        intent_id="intent-001",
        status=BrokerReadbackStatus.ABSENT,
        broker_order_id=None,
        cumulative_quantity=0,
        broker_snapshot_at=T1,
        locally_received_at=T2,
        source="synthetic-paper-adapter",
        source_sequence_id="readback-seq-absent",
        broker_sequence_id="broker-readback-absent",
    )
    assert absent.broker_order_id is None
    with pytest.raises(pydantic.ValidationError, match="requires broker_order_id"):
        absent.model_copy(update={"status": BrokerReadbackStatus.OPEN}).model_validate(
            absent.model_copy(update={"status": BrokerReadbackStatus.OPEN}).model_dump()
        )
    with pytest.raises(pydantic.ValidationError, match=r"REJECTED.*zero"):
        BrokerReadback(
            record_id="readback-rejected",
            intent_id="intent-001",
            status=BrokerReadbackStatus.REJECTED,
            broker_order_id=None,
            cumulative_quantity=1,
            broker_snapshot_at=T1,
            locally_received_at=T2,
            source="synthetic-paper-adapter",
            source_sequence_id="readback-seq-rejected",
            broker_sequence_id="broker-readback-rejected",
        )
