"""SnapTrade → TREX execution-record normalization.

This is intentionally *not* a second execution engine. It emits the existing
TREX records consumed by ``ExecutionLifecycle``, ``reconcile`` and
``assess_evidence``.

The initial adapter is conservative:

* one TREX intent gets one stable client-order UUID for correlation;
* provider/broker uniqueness is not assumed to make ambiguous resubmission safe;
* cumulative order status produces ``BrokerReadback`` only;
* it never fabricates ``PartialFill``/``CompleteFill`` or zero fees;
* current TREX execution records use integer quantities, so fractional provider
  quantities fail closed until the domain contract is intentionally extended.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from tree_options.execution.records import (
    BrokerAcknowledgement,
    BrokerReadback,
    BrokerReadbackStatus,
    OrderIntent,
    OrderReject,
    SubmitAttempt,
    TimeoutObserved,
)

SOURCE = "snaptrade"
EXACT_FILL_EVIDENCE_SUPPORTED = False
_CLIENT_ORDER_NAMESPACE = uuid.UUID("ecb6fceb-81e0-4f99-ace9-d6c80b7756a2")
_RECORD_NAMESPACE = uuid.UUID("89506446-acde-4e3a-a7c5-478046f3dfe5")


class SnapTradeNormalizationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SnapTradeOrderSnapshot:
    status: str
    brokerage_order_id: str | None
    total_quantity: int | None
    filled_quantity: int
    broker_snapshot_at: datetime
    broker_placed_at: datetime | None = None
    client_order_id: str | None = None
    request_id: str | None = None
    raw_digest: str = ""

    def __post_init__(self) -> None:
        if self.broker_snapshot_at.tzinfo is None or self.broker_snapshot_at.utcoffset() is None:
            raise SnapTradeNormalizationError("broker_snapshot_at must be timezone-aware")
        object.__setattr__(self, "broker_snapshot_at", self.broker_snapshot_at.astimezone(UTC))
        if self.broker_placed_at is not None:
            if self.broker_placed_at.tzinfo is None or self.broker_placed_at.utcoffset() is None:
                raise SnapTradeNormalizationError("broker_placed_at must be timezone-aware")
            object.__setattr__(self, "broker_placed_at", self.broker_placed_at.astimezone(UTC))
        if type(self.filled_quantity) is not int or (
            self.total_quantity is not None and type(self.total_quantity) is not int
        ):
            raise SnapTradeNormalizationError("integer quantities required")
        if self.total_quantity is not None and self.total_quantity < 1:
            raise SnapTradeNormalizationError("total_quantity must be >= 1 when present")
        if self.filled_quantity < 0:
            raise SnapTradeNormalizationError("filled_quantity must be >= 0")
        if self.total_quantity is not None and self.filled_quantity > self.total_quantity:
            raise SnapTradeNormalizationError("filled_quantity exceeds total_quantity")


def stable_client_order_id(intent_id: str) -> str:
    """Stable canonical UUID for provider correlation, not a retry guarantee."""
    if not intent_id:
        raise SnapTradeNormalizationError("intent_id is required")
    return str(uuid.uuid5(_CLIENT_ORDER_NAMESPACE, intent_id))


def _record_id(*parts: str) -> str:
    return str(uuid.uuid5(_RECORD_NAMESPACE, "|".join(parts)))


def _int_quantity(value: Any, *, field: str, allow_zero: bool = False) -> int:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise SnapTradeNormalizationError(f"{field} is not numeric") from error
    if not number.is_finite() or number != number.to_integral_value():
        raise SnapTradeNormalizationError(
            f"{field}={value!r} is fractional/non-finite; current TREX execution records require integer quantity"
        )
    result = int(number)
    minimum = 0 if allow_zero else 1
    if result < minimum:
        raise SnapTradeNormalizationError(f"{field} must be >= {minimum}")
    return result


def _parse_time(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise SnapTradeNormalizationError(f"{field} is not ISO-8601") from error
    else:
        raise SnapTradeNormalizationError(f"{field} is required")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SnapTradeNormalizationError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _digest_mapping(raw: Mapping[str, Any]) -> str:
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def snapshot_from_mapping(
    raw: Mapping[str, Any], *, request_id: str | None = None
) -> SnapTradeOrderSnapshot:
    """Normalize a current SnapTrade order-details object.

    SnapTrade/broker payloads evolve and integration capabilities differ. This
    parser intentionally accepts a small documented semantic subset and refuses
    a snapshot with no trustworthy time/quantity rather than inventing one.
    """

    status = raw.get("status")
    if not isinstance(status, str) or not status:
        raise SnapTradeNormalizationError("order snapshot missing status")

    brokerage_order_id = raw.get("brokerage_order_id") or raw.get("broker_order_id")
    if brokerage_order_id is not None:
        brokerage_order_id = str(brokerage_order_id)

    total_raw = raw.get("total_quantity")
    if total_raw is None:
        total_raw = raw.get("quantity")
    total = None if total_raw is None else _int_quantity(total_raw, field="total_quantity")

    filled_raw = raw.get("filled_quantity")
    filled = _int_quantity(filled_raw, field="filled_quantity", allow_zero=True)

    # SnapTrade documents total_quantity as filled + canceled + open. Preserve
    # that invariant when all three components are present instead of accepting
    # a self-contradictory provider snapshot. Missing components remain unknown.
    open_raw = raw.get("open_quantity")
    canceled_raw = raw.get("canceled_quantity")
    open_quantity = (
        None
        if open_raw is None
        else _int_quantity(open_raw, field="open_quantity", allow_zero=True)
    )
    canceled_quantity = (
        None
        if canceled_raw is None
        else _int_quantity(canceled_raw, field="canceled_quantity", allow_zero=True)
    )
    if (
        total is not None
        and open_quantity is not None
        and canceled_quantity is not None
        and total != filled + open_quantity + canceled_quantity
    ):
        raise SnapTradeNormalizationError(
            "total_quantity must equal filled_quantity + open_quantity + canceled_quantity"
        )

    timestamp = None
    for key in ("time_updated",):
        if raw.get(key):
            timestamp = raw[key]
            break
    if timestamp is None:
        raise SnapTradeNormalizationError(
            "order snapshot requires time_updated; placement/execution/local poll times are not snapshot times"
        )

    placed_at = (
        _parse_time(raw["time_placed"], field="time_placed") if raw.get("time_placed") else None
    )
    client_order_id = raw.get("client_order_id")
    return SnapTradeOrderSnapshot(
        status=status.upper(),
        brokerage_order_id=brokerage_order_id,
        total_quantity=total,
        filled_quantity=filled,
        broker_snapshot_at=_parse_time(timestamp, field="order timestamp"),
        broker_placed_at=placed_at,
        client_order_id=str(client_order_id) if client_order_id else None,
        request_id=request_id,
        raw_digest=_digest_mapping(raw),
    )


def _require_correlation(intent: OrderIntent, snapshot: SnapTradeOrderSnapshot) -> None:
    if snapshot.client_order_id is None:
        return
    expected = stable_client_order_id(intent.intent_id)
    if snapshot.client_order_id != expected:
        raise SnapTradeNormalizationError(
            f"client_order_id mismatch for {intent.intent_id}: expected {expected}, "
            f"observed {snapshot.client_order_id}"
        )


def normalize_status(status: str) -> BrokerReadbackStatus:
    """Fail-closed mapping from SnapTrade order status to TREX readback state."""
    value = status.upper()
    if value in {"PENDING", "ACCEPTED", "QUEUED", "TRIGGERED", "ACTIVATED", "OPEN"}:
        return BrokerReadbackStatus.OPEN
    if value == "PARTIAL":
        return BrokerReadbackStatus.PARTIALLY_FILLED
    if value == "EXECUTED" or value == "FILLED":
        return BrokerReadbackStatus.FILLED
    if value in {"CANCELED", "PARTIAL_CANCELED", "EXPIRED"}:
        return BrokerReadbackStatus.CANCELED
    if value in {"REJECTED", "FAILED"}:
        return BrokerReadbackStatus.REJECTED
    # CANCEL_PENDING / REPLACE_PENDING / REPLACED and any future provider state
    # are intentionally ambiguous until TREX grows corresponding external facts.
    return BrokerReadbackStatus.AMBIGUOUS


def submit_attempt(
    intent: OrderIntent,
    *,
    attempt_id: str,
    send_attempt_at: datetime,
) -> SubmitAttempt:
    return SubmitAttempt(
        record_id=attempt_id,
        intent_id=intent.intent_id,
        send_attempt_at=send_attempt_at,
        source=SOURCE,
        source_sequence_id=f"submit:{attempt_id}",
    )


def timeout_observed(
    intent: OrderIntent,
    *,
    attempt_id: str,
    locally_received_at: datetime,
) -> TimeoutObserved:
    """Record ambiguity. Caller must read back; this function never retries."""
    return TimeoutObserved(
        record_id=_record_id(intent.intent_id, attempt_id, "timeout"),
        intent_id=intent.intent_id,
        attempt_id=attempt_id,
        locally_received_at=locally_received_at,
        source=SOURCE,
        source_sequence_id=f"timeout:{attempt_id}",
    )


def acknowledgement_from_snapshot(
    intent: OrderIntent,
    snapshot: SnapTradeOrderSnapshot,
    *,
    locally_received_at: datetime,
) -> BrokerAcknowledgement:
    _require_correlation(intent, snapshot)
    status = normalize_status(snapshot.status)
    if status in {BrokerReadbackStatus.REJECTED, BrokerReadbackStatus.AMBIGUOUS}:
        raise SnapTradeNormalizationError(f"status {snapshot.status} is not an acknowledgement")
    if not snapshot.brokerage_order_id:
        raise SnapTradeNormalizationError("acknowledgement requires brokerage_order_id")
    if snapshot.broker_placed_at is None:
        raise SnapTradeNormalizationError(
            "acknowledgement requires provider/broker placement time; refusing to reuse a later update time"
        )
    token = snapshot.request_id or snapshot.raw_digest or snapshot.brokerage_order_id
    return BrokerAcknowledgement(
        record_id=_record_id(intent.intent_id, token, "ack"),
        intent_id=intent.intent_id,
        broker_order_id=snapshot.brokerage_order_id,
        broker_acknowledged_at=snapshot.broker_placed_at,
        locally_received_at=locally_received_at,
        source=SOURCE,
        source_sequence_id=f"ack:{token}",
        broker_sequence_id=f"ack:{token}",
    )


def rejection_from_snapshot(
    intent: OrderIntent,
    snapshot: SnapTradeOrderSnapshot,
    *,
    locally_received_at: datetime,
    broker_rejected_at: datetime | None = None,
) -> OrderReject:
    _require_correlation(intent, snapshot)
    if normalize_status(snapshot.status) is not BrokerReadbackStatus.REJECTED:
        raise SnapTradeNormalizationError(f"status {snapshot.status} is not a rejection")
    if broker_rejected_at is None:
        raise SnapTradeNormalizationError(
            "authoritative rejection event time is required; placement/update times cannot substitute"
        )
    if snapshot.filled_quantity != 0:
        raise SnapTradeNormalizationError(
            "rejection with positive filled quantity cannot be represented losslessly"
        )
    token = snapshot.request_id or snapshot.raw_digest or intent.intent_id
    return OrderReject(
        record_id=_record_id(intent.intent_id, token, "reject"),
        intent_id=intent.intent_id,
        broker_order_id=snapshot.brokerage_order_id,
        reason_code=f"SNAPTRADE_{snapshot.status}",
        broker_acknowledged_at=_parse_time(
            broker_rejected_at, field="authoritative rejection time"
        ),
        locally_received_at=locally_received_at,
        source=SOURCE,
        source_sequence_id=f"reject:{token}",
        broker_sequence_id=f"reject:{token}",
    )


def readback_from_snapshot(
    intent: OrderIntent,
    snapshot: SnapTradeOrderSnapshot,
    *,
    locally_received_at: datetime,
) -> BrokerReadback:
    _require_correlation(intent, snapshot)
    status = normalize_status(snapshot.status)
    if snapshot.total_quantity is not None and snapshot.total_quantity != intent.quantity:
        raise SnapTradeNormalizationError(
            "provider total does not match intent quantity; replacement requires explicit domain records"
        )
    token = (
        snapshot.request_id
        or snapshot.raw_digest
        or (f"{snapshot.brokerage_order_id}:{snapshot.status}:{snapshot.filled_quantity}")
    )

    if status in {
        BrokerReadbackStatus.REJECTED,
        BrokerReadbackStatus.ABSENT,
        BrokerReadbackStatus.AMBIGUOUS,
    }:
        # TREX cannot encode a REJECTED/AMBIGUOUS readback with positive
        # cumulative execution. Never erase such a provider contradiction by
        # coercing the quantity to zero: fail closed and require operator/data
        # reconciliation instead.
        if snapshot.filled_quantity != 0:
            raise SnapTradeNormalizationError(
                f"{status} status with positive filled_quantity cannot be represented losslessly"
            )
        total = None
        broker_order_id = (
            None if status is BrokerReadbackStatus.AMBIGUOUS else snapshot.brokerage_order_id
        )
        cumulative = 0
    else:
        if not snapshot.brokerage_order_id:
            raise SnapTradeNormalizationError(f"{status} readback requires brokerage_order_id")
        if snapshot.total_quantity is None:
            raise SnapTradeNormalizationError(f"{status} readback requires total_quantity")
        total = snapshot.total_quantity
        broker_order_id = snapshot.brokerage_order_id
        cumulative = snapshot.filled_quantity

    # Tighten provider-status semantics before constructing the existing record.
    if status is BrokerReadbackStatus.OPEN and cumulative != 0:
        raise SnapTradeNormalizationError(
            "OPEN status with positive filled_quantity is inconsistent"
        )
    if status is BrokerReadbackStatus.PARTIALLY_FILLED:
        if cumulative <= 0 or total is None or cumulative >= total:
            raise SnapTradeNormalizationError(
                "PARTIAL requires 0 < filled_quantity < total_quantity"
            )
    if status is BrokerReadbackStatus.FILLED and cumulative != total:
        raise SnapTradeNormalizationError("FILLED requires filled_quantity == total_quantity")

    return BrokerReadback(
        record_id=_record_id(intent.intent_id, token, "readback"),
        intent_id=intent.intent_id,
        status=status,
        observed_order_id=snapshot.brokerage_order_id,
        observed_total_quantity=snapshot.total_quantity,
        observed_status=snapshot.status,
        broker_order_id=broker_order_id,
        total_quantity=total,
        cumulative_quantity=cumulative,
        broker_snapshot_at=snapshot.broker_snapshot_at,
        locally_received_at=locally_received_at,
        source=SOURCE,
        source_sequence_id=f"readback:{token}",
        broker_sequence_id=f"readback:{token}",
    )


def require_exact_fill_evidence() -> None:
    """Explicit tripwire preventing accidental promotion of order snapshots."""
    raise SnapTradeNormalizationError(
        "SnapTrade cumulative order status is not exact fill/fee evidence; "
        "install and validate a fill-level source before emitting TREX fill records"
    )


__all__ = [
    "EXACT_FILL_EVIDENCE_SUPPORTED",
    "SnapTradeNormalizationError",
    "SnapTradeOrderSnapshot",
    "acknowledgement_from_snapshot",
    "normalize_status",
    "readback_from_snapshot",
    "rejection_from_snapshot",
    "require_exact_fill_evidence",
    "snapshot_from_mapping",
    "stable_client_order_id",
    "submit_attempt",
    "timeout_observed",
]
