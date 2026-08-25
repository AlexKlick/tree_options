"""Broker-neutral execution records for the synthetic M6 foundation.

The records deliberately describe facts and local intents, not a broker API.
They are immutable, reject unknown fields, name each timestamp role explicitly,
and refuse binary floating-point inputs for monetary values.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BeforeValidator, Field, model_validator

from tree_options.schemas.common import IdStr, StrictModel


def _require_decimal(value: object) -> Decimal:
    """Accept Decimal or its lossless string encoding; refuse binary floats."""
    if type(value) is Decimal:
        exact = value
    elif isinstance(value, str):
        try:
            exact = Decimal(value)
        except InvalidOperation as error:
            raise ValueError("monetary string must encode a Decimal") from error
    else:
        raise ValueError(f"monetary value must be Decimal, got {type(value).__name__}")
    if not exact.is_finite():
        raise ValueError("monetary Decimal must be finite")
    return exact


def _require_execution_utc(value: object) -> datetime:
    """Accept datetime or its lossless JSON encoding, then normalize to UTC."""
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("timestamp must be an ISO-8601 datetime") from error
    else:
        raise ValueError(f"timestamp must be datetime, got {type(value).__name__}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("naive datetime rejected")
    return parsed.astimezone(UTC)


ExactPrice = Annotated[
    Decimal,
    BeforeValidator(_require_decimal),
    Field(gt=Decimal("0"), max_digits=18, decimal_places=8),
]
ExactMoney = Annotated[
    Decimal,
    BeforeValidator(_require_decimal),
    Field(ge=Decimal("0"), max_digits=18, decimal_places=8),
]
ExecutionUTCDatetime = Annotated[datetime, BeforeValidator(_require_execution_utc)]


class BrokerReadbackStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    ABSENT = "ABSENT"
    AMBIGUOUS = "AMBIGUOUS"


class OrderIntent(StrictModel):
    """One stable economic order intent; send retries never mint another id."""

    record_type: Literal["ORDER_INTENT"] = "ORDER_INTENT"
    intent_id: IdStr
    contract_id: IdStr
    side: Literal["BUY", "SELL"]
    position_effect: Literal["OPEN_LONG", "CLOSE_LONG"]
    quantity: int = Field(strict=True, ge=1)
    order_type: Literal["MARKET", "LIMIT"]
    limit_price: ExactPrice | None = None
    execution_style: Literal["single", "package", "legged"] = "single"
    package_id: IdStr | None = None
    intent_created_at: ExecutionUTCDatetime
    source: IdStr
    source_sequence_id: IdStr

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if (self.side, self.position_effect) not in {
            ("BUY", "OPEN_LONG"),
            ("SELL", "CLOSE_LONG"),
        }:
            raise ValueError("only buy/open-long and sell/close-long intents are representable")
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("LIMIT order requires limit_price")
        if self.order_type == "MARKET" and self.limit_price is not None:
            raise ValueError("MARKET order must not carry limit_price")
        if self.execution_style == "single" and self.package_id is not None:
            raise ValueError("single execution must not carry package_id")
        if self.execution_style != "single" and self.package_id is None:
            raise ValueError(f"{self.execution_style} execution requires package_id")
        return self


class SubmitAttempt(StrictModel):
    """A transport attempt for an existing intent, never a new order identity."""

    record_type: Literal["SUBMIT_ATTEMPT"] = "SUBMIT_ATTEMPT"
    record_id: IdStr
    intent_id: IdStr
    send_attempt_at: ExecutionUTCDatetime
    source: IdStr
    source_sequence_id: IdStr


class BrokerAcknowledgement(StrictModel):
    record_type: Literal["BROKER_ACKNOWLEDGEMENT"] = "BROKER_ACKNOWLEDGEMENT"
    record_id: IdStr
    intent_id: IdStr
    broker_order_id: IdStr
    broker_acknowledged_at: ExecutionUTCDatetime
    locally_received_at: ExecutionUTCDatetime
    source: IdStr
    source_sequence_id: IdStr
    broker_sequence_id: IdStr

    @model_validator(mode="after")
    def _validate_timestamps(self) -> Self:
        if self.broker_acknowledged_at > self.locally_received_at:
            raise ValueError("broker_acknowledged_at must be <= locally_received_at")
        return self


class OrderReject(StrictModel):
    record_type: Literal["ORDER_REJECT"] = "ORDER_REJECT"
    record_id: IdStr
    intent_id: IdStr
    broker_order_id: IdStr | None = None
    reason_code: IdStr
    broker_acknowledged_at: ExecutionUTCDatetime
    locally_received_at: ExecutionUTCDatetime
    source: IdStr
    source_sequence_id: IdStr
    broker_sequence_id: IdStr

    @model_validator(mode="after")
    def _validate_timestamps(self) -> Self:
        if self.broker_acknowledged_at > self.locally_received_at:
            raise ValueError("broker_acknowledged_at must be <= locally_received_at")
        return self


class _FillRecord(StrictModel):
    record_id: IdStr
    intent_id: IdStr
    broker_order_id: IdStr
    fill_quantity: int = Field(strict=True, ge=1)
    cumulative_quantity: int = Field(strict=True, ge=1)
    unit_price: ExactPrice
    fees: ExactMoney
    exchange_event_at: ExecutionUTCDatetime
    locally_received_at: ExecutionUTCDatetime
    source: IdStr
    source_sequence_id: IdStr
    broker_sequence_id: IdStr

    @model_validator(mode="after")
    def _validate_fill(self) -> Self:
        if self.fill_quantity > self.cumulative_quantity:
            raise ValueError("fill_quantity must be <= cumulative_quantity")
        if self.exchange_event_at > self.locally_received_at:
            raise ValueError("exchange_event_at must be <= locally_received_at")
        return self


class PartialFill(_FillRecord):
    record_type: Literal["PARTIAL_FILL"] = "PARTIAL_FILL"


class CompleteFill(_FillRecord):
    record_type: Literal["COMPLETE_FILL"] = "COMPLETE_FILL"


class TimeoutObserved(StrictModel):
    """Local uncertainty after a submit transport timeout."""

    record_type: Literal["TIMEOUT_OBSERVED"] = "TIMEOUT_OBSERVED"
    record_id: IdStr
    intent_id: IdStr
    attempt_id: IdStr
    locally_received_at: ExecutionUTCDatetime
    source: IdStr
    source_sequence_id: IdStr


class DisconnectObserved(StrictModel):
    """Local uncertainty after a transport/session disconnect."""

    record_type: Literal["DISCONNECT_OBSERVED"] = "DISCONNECT_OBSERVED"
    record_id: IdStr
    intent_id: IdStr
    locally_received_at: ExecutionUTCDatetime
    source: IdStr
    source_sequence_id: IdStr


class BrokerReadback(StrictModel):
    """An explicit broker snapshot used to recover an uncertain projection."""

    record_type: Literal["BROKER_READBACK"] = "BROKER_READBACK"
    record_id: IdStr
    intent_id: IdStr
    status: BrokerReadbackStatus
    broker_order_id: IdStr | None
    total_quantity: int | None = Field(default=None, strict=True, ge=1)
    cumulative_quantity: int = Field(strict=True, ge=0)
    broker_snapshot_at: ExecutionUTCDatetime
    locally_received_at: ExecutionUTCDatetime
    source: IdStr
    source_sequence_id: IdStr
    broker_sequence_id: IdStr

    @model_validator(mode="after")
    def _validate_readback(self) -> Self:
        if self.broker_snapshot_at > self.locally_received_at:
            raise ValueError("broker_snapshot_at must be <= locally_received_at")
        order_required = {
            BrokerReadbackStatus.OPEN,
            BrokerReadbackStatus.PARTIALLY_FILLED,
            BrokerReadbackStatus.FILLED,
            BrokerReadbackStatus.CANCELED,
        }
        if self.status in order_required and self.broker_order_id is None:
            raise ValueError(f"{self.status} readback requires broker_order_id")
        if self.status in order_required and self.total_quantity is None:
            raise ValueError(f"{self.status} readback requires total_quantity")
        no_accepted_total = {
            BrokerReadbackStatus.REJECTED,
            BrokerReadbackStatus.ABSENT,
            BrokerReadbackStatus.AMBIGUOUS,
        }
        if self.status in no_accepted_total and self.total_quantity is not None:
            raise ValueError(f"{self.status} readback must not claim total_quantity")
        if self.status in {BrokerReadbackStatus.ABSENT, BrokerReadbackStatus.AMBIGUOUS}:
            if self.broker_order_id is not None:
                raise ValueError(f"{self.status} readback must not claim one broker_order_id")
            if self.cumulative_quantity != 0:
                raise ValueError(f"{self.status} readback must have zero cumulative_quantity")
        if self.status is BrokerReadbackStatus.OPEN and self.cumulative_quantity != 0:
            raise ValueError("OPEN readback must have zero cumulative_quantity")
        if self.status is BrokerReadbackStatus.PARTIALLY_FILLED:
            if self.cumulative_quantity == 0:
                raise ValueError("PARTIALLY_FILLED readback requires positive cumulative_quantity")
            if self.total_quantity is not None and self.cumulative_quantity >= self.total_quantity:
                raise ValueError(
                    "PARTIALLY_FILLED cumulative_quantity must be less than total_quantity"
                )
        if (
            self.status is BrokerReadbackStatus.FILLED
            and self.cumulative_quantity != self.total_quantity
        ):
            raise ValueError("FILLED cumulative_quantity must equal total_quantity")
        if (
            self.status is BrokerReadbackStatus.CANCELED
            and self.total_quantity is not None
            and self.cumulative_quantity > self.total_quantity
        ):
            raise ValueError("CANCELED cumulative_quantity must not exceed total_quantity")
        if self.status is BrokerReadbackStatus.REJECTED and self.cumulative_quantity != 0:
            raise ValueError("REJECTED readback must have zero cumulative_quantity")
        return self


class ReplaceIntent(StrictModel):
    """A replace request tied to one current broker readback observation."""

    record_type: Literal["REPLACE_INTENT"] = "REPLACE_INTENT"
    record_id: IdStr
    intent_id: IdStr
    based_on_readback_id: IdStr
    broker_order_id: IdStr
    new_total_quantity: int = Field(strict=True, ge=1)
    new_limit_price: ExactPrice
    replace_created_at: ExecutionUTCDatetime
    source: IdStr
    source_sequence_id: IdStr


ExecutionRecord = (
    SubmitAttempt
    | BrokerAcknowledgement
    | OrderReject
    | PartialFill
    | CompleteFill
    | TimeoutObserved
    | DisconnectObserved
    | BrokerReadback
    | ReplaceIntent
)
StoredExecutionRecord = OrderIntent | ExecutionRecord
