"""Pure, immutable execution lifecycle projection.

The reducer has no I/O and performs no broker action.  It accepts immutable
records, checks identity/sequence collisions before projecting them, and
returns a new value.  UNKNOWN is uncertainty: only broker facts/readback can
recover it; it never grants permission to resubmit or replace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from tree_options.execution.records import (
    BrokerAcknowledgement,
    BrokerReadback,
    BrokerReadbackStatus,
    CompleteFill,
    DisconnectObserved,
    ExecutionRecord,
    OrderIntent,
    OrderReject,
    PartialFill,
    ReplaceIntent,
    StoredExecutionRecord,
    SubmitAttempt,
    TimeoutObserved,
)


class ExecutionState(StrEnum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class ExecutionLifecycleError(RuntimeError):
    """Base class for fail-closed lifecycle refusals."""


class IntentMismatchError(ExecutionLifecycleError):
    pass


class RecordIdentityCollisionError(ExecutionLifecycleError):
    pass


class SequenceIdentityCollisionError(ExecutionLifecycleError):
    pass


class TemporalOrderError(ExecutionLifecycleError):
    pass


class TransitionRefusedError(ExecutionLifecycleError):
    pass


class ReplacementRefusedError(TransitionRefusedError):
    pass


def _record_id(record: StoredExecutionRecord) -> str:
    if isinstance(record, OrderIntent):
        return record.intent_id
    return record.record_id


def _canonical_bytes(record: StoredExecutionRecord) -> bytes:
    body = {
        "model": type(record).__name__,
        "payload": record.model_dump(mode="json"),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _broker_sequence(record: StoredExecutionRecord) -> str | None:
    sequence = getattr(record, "broker_sequence_id", None)
    return sequence if isinstance(sequence, str) else None


def _primary_time(record: ExecutionRecord) -> datetime:
    if isinstance(record, SubmitAttempt):
        return record.send_attempt_at
    if isinstance(record, (BrokerAcknowledgement, OrderReject)):
        return record.broker_acknowledged_at
    if isinstance(record, (PartialFill, CompleteFill)):
        return record.exchange_event_at
    if isinstance(record, (TimeoutObserved, DisconnectObserved)):
        return record.locally_received_at
    if isinstance(record, BrokerReadback):
        return record.broker_snapshot_at
    return record.replace_created_at


@dataclass(frozen=True, slots=True)
class ExecutionLifecycle:
    """Immutable projection for one and only one OrderIntent."""

    intent: OrderIntent
    state: ExecutionState
    records: tuple[StoredExecutionRecord, ...]
    submit_attempt_ids: tuple[str, ...] = ()
    broker_order_id: str | None = None
    filled_quantity: int = 0
    latest_fill_exchange_at: datetime | None = None
    latest_broker_fact_at: datetime | None = None
    uncertain_since_at: datetime | None = None
    replace_basis: BrokerReadback | None = None

    @classmethod
    def start(cls, intent: OrderIntent) -> ExecutionLifecycle:
        return cls(
            intent=intent,
            state=ExecutionState.CREATED,
            records=(intent,),
        )

    @property
    def replace_basis_id(self) -> str | None:
        return None if self.replace_basis is None else self.replace_basis.record_id

    def apply(self, record: ExecutionRecord) -> ExecutionLifecycle:
        """Validate and project one record without mutating this instance."""
        if record.intent_id != self.intent.intent_id:
            raise IntentMismatchError(
                f"record intent {record.intent_id!r} does not match {self.intent.intent_id!r}"
            )

        duplicate = self._check_identity(record)
        if duplicate:
            return self

        if _primary_time(record) < self.intent.intent_created_at:
            raise TemporalOrderError("execution record predates intent_created_at")

        if isinstance(record, SubmitAttempt):
            return self._apply_submit(record)
        if isinstance(record, (TimeoutObserved, DisconnectObserved)):
            return self._apply_uncertainty(record)
        if isinstance(record, BrokerAcknowledgement):
            return self._apply_ack(record)
        if isinstance(record, OrderReject):
            return self._apply_reject(record)
        if isinstance(record, (PartialFill, CompleteFill)):
            return self._apply_fill(record)
        if isinstance(record, BrokerReadback):
            return self._apply_readback(record)
        return self._apply_replace(record)

    def _check_identity(self, record: ExecutionRecord) -> bool:
        candidate_bytes = _canonical_bytes(record)
        candidate_id = _record_id(record)
        candidate_source_key = (record.source, record.source_sequence_id)
        candidate_broker_sequence = _broker_sequence(record)

        for existing in self.records:
            existing_bytes = _canonical_bytes(existing)
            if _record_id(existing) == candidate_id:
                if existing_bytes == candidate_bytes:
                    return True
                raise RecordIdentityCollisionError(
                    f"record id {candidate_id!r} is already bound to different bytes"
                )
            if (existing.source, existing.source_sequence_id) == candidate_source_key:
                if existing_bytes == candidate_bytes:
                    return True
                raise SequenceIdentityCollisionError(
                    f"source sequence {candidate_source_key!r} is bound to different bytes"
                )
            existing_broker_sequence = _broker_sequence(existing)
            if (
                candidate_broker_sequence is not None
                and existing_broker_sequence == candidate_broker_sequence
                and existing.source == record.source
            ):
                if existing_bytes == candidate_bytes:
                    return True
                raise SequenceIdentityCollisionError(
                    "broker sequence "
                    f"{(record.source, candidate_broker_sequence)!r} is bound to different bytes"
                )
        return False

    def _with_record(
        self,
        record: ExecutionRecord,
        *,
        state: ExecutionState | None = None,
        broker_order_id: str | None = None,
        preserve_broker_order_id: bool = True,
        filled_quantity: int | None = None,
        latest_fill_exchange_at: datetime | None = None,
        preserve_fill_time: bool = True,
        broker_fact_at: datetime | None = None,
        replace_basis: BrokerReadback | None = None,
        preserve_replace_basis: bool = False,
    ) -> ExecutionLifecycle:
        next_state = self.state if state is None else state
        latest_broker_fact_at = self.latest_broker_fact_at
        if broker_fact_at is not None and (
            latest_broker_fact_at is None or broker_fact_at > latest_broker_fact_at
        ):
            latest_broker_fact_at = broker_fact_at
        return replace(
            self,
            state=next_state,
            records=(*self.records, record),
            broker_order_id=(self.broker_order_id if preserve_broker_order_id else broker_order_id),
            filled_quantity=(self.filled_quantity if filled_quantity is None else filled_quantity),
            latest_fill_exchange_at=(
                self.latest_fill_exchange_at if preserve_fill_time else latest_fill_exchange_at
            ),
            latest_broker_fact_at=latest_broker_fact_at,
            uncertain_since_at=(
                self.uncertain_since_at if next_state is ExecutionState.UNKNOWN else None
            ),
            replace_basis=(self.replace_basis if preserve_replace_basis else replace_basis),
        )

    def _reconciliation(self, record: ExecutionRecord) -> ExecutionLifecycle:
        return self._with_record(
            record,
            state=ExecutionState.RECONCILIATION_REQUIRED,
            broker_fact_at=_primary_time(record),
        )

    def _first_submit_at(self) -> datetime | None:
        attempts = [
            record.send_attempt_at for record in self.records if isinstance(record, SubmitAttempt)
        ]
        return min(attempts) if attempts else None

    def _require_after_first_submit(self, event_at: datetime) -> None:
        first_submit_at = self._first_submit_at()
        if first_submit_at is not None and event_at < first_submit_at:
            raise TemporalOrderError("broker fact predates first submit attempt")

    def _received_before_uncertainty(self, locally_received_at: datetime) -> bool:
        return (
            self.state is ExecutionState.UNKNOWN
            and self.uncertain_since_at is not None
            and locally_received_at < self.uncertain_since_at
        )

    def _apply_submit(self, record: SubmitAttempt) -> ExecutionLifecycle:
        if self.submit_attempt_ids:
            latest = max(
                existing.send_attempt_at
                for existing in self.records
                if isinstance(existing, SubmitAttempt)
            )
            if record.send_attempt_at < latest:
                raise TemporalOrderError("submit retry predates an existing attempt")
        if self.state is ExecutionState.UNKNOWN:
            raise TransitionRefusedError("UNKNOWN requires broker readback before any action")
        if self.state not in {ExecutionState.CREATED, ExecutionState.SUBMITTING}:
            raise TransitionRefusedError(f"submit attempt refused from {self.state}")
        projected = self._with_record(record, state=ExecutionState.SUBMITTING)
        return replace(
            projected,
            submit_attempt_ids=(*self.submit_attempt_ids, record.record_id),
        )

    def _apply_uncertainty(
        self, record: TimeoutObserved | DisconnectObserved
    ) -> ExecutionLifecycle:
        if isinstance(record, TimeoutObserved) and record.attempt_id not in self.submit_attempt_ids:
            raise TransitionRefusedError(
                f"timeout references unknown submit attempt {record.attempt_id!r}"
            )
        if isinstance(record, TimeoutObserved):
            attempt = next(
                item
                for item in self.records
                if isinstance(item, SubmitAttempt) and item.record_id == record.attempt_id
            )
            if record.locally_received_at < attempt.send_attempt_at:
                raise TemporalOrderError("timeout predates its submit attempt")
        else:
            self._require_after_first_submit(record.locally_received_at)
        uncertain_sources = {
            ExecutionState.SUBMITTING,
            ExecutionState.ACKNOWLEDGED,
            ExecutionState.PARTIALLY_FILLED,
            ExecutionState.CANCEL_PENDING,
            ExecutionState.UNKNOWN,
        }
        if self.state not in uncertain_sources:
            raise TransitionRefusedError(f"uncertainty observation refused from {self.state}")
        projected = self._with_record(record, state=ExecutionState.UNKNOWN)
        uncertain_since_at = record.locally_received_at
        if self.uncertain_since_at is not None and self.uncertain_since_at > uncertain_since_at:
            uncertain_since_at = self.uncertain_since_at
        return replace(projected, uncertain_since_at=uncertain_since_at)

    def _broker_id_conflicts(self, broker_order_id: str | None) -> bool:
        return (
            broker_order_id is not None
            and self.broker_order_id is not None
            and broker_order_id != self.broker_order_id
        )

    def _apply_ack(self, record: BrokerAcknowledgement) -> ExecutionLifecycle:
        if not self.submit_attempt_ids:
            return self._reconciliation(record)
        self._require_after_first_submit(record.broker_acknowledged_at)
        if self._broker_id_conflicts(record.broker_order_id):
            return self._reconciliation(record)
        if self._received_before_uncertainty(record.locally_received_at):
            return self._with_record(
                record,
                state=ExecutionState.UNKNOWN,
                broker_order_id=record.broker_order_id,
                preserve_broker_order_id=False,
                broker_fact_at=record.broker_acknowledged_at,
            )
        if self.state in {ExecutionState.CANCELED, ExecutionState.REJECTED}:
            return self._reconciliation(record)
        next_state = self.state
        if self.state in {ExecutionState.SUBMITTING, ExecutionState.UNKNOWN}:
            next_state = ExecutionState.ACKNOWLEDGED
        return self._with_record(
            record,
            state=next_state,
            broker_order_id=record.broker_order_id,
            preserve_broker_order_id=False,
            broker_fact_at=record.broker_acknowledged_at,
        )

    def _apply_reject(self, record: OrderReject) -> ExecutionLifecycle:
        if not self.submit_attempt_ids:
            return self._reconciliation(record)
        self._require_after_first_submit(record.broker_acknowledged_at)
        if self._broker_id_conflicts(record.broker_order_id):
            return self._reconciliation(record)
        if self._received_before_uncertainty(record.locally_received_at):
            return self._with_record(
                record,
                state=ExecutionState.UNKNOWN,
                broker_order_id=record.broker_order_id or self.broker_order_id,
                preserve_broker_order_id=False,
                broker_fact_at=record.broker_acknowledged_at,
            )
        if self.state in {ExecutionState.SUBMITTING, ExecutionState.UNKNOWN}:
            resolved_broker_order_id = record.broker_order_id or self.broker_order_id
            return self._with_record(
                record,
                state=ExecutionState.REJECTED,
                broker_order_id=resolved_broker_order_id,
                preserve_broker_order_id=False,
                broker_fact_at=record.broker_acknowledged_at,
            )
        if self.state is ExecutionState.REJECTED:
            return self._with_record(record)
        return self._reconciliation(record)

    def _apply_fill(self, record: PartialFill | CompleteFill) -> ExecutionLifecycle:
        total = self.intent.quantity
        if isinstance(record, CompleteFill) and record.cumulative_quantity != total:
            raise TransitionRefusedError(
                f"complete fill cumulative quantity must equal intent quantity {total}"
            )
        if isinstance(record, PartialFill) and record.cumulative_quantity >= total:
            raise TransitionRefusedError(
                f"partial fill cumulative quantity must be less than intent quantity {total}"
            )
        if self._broker_id_conflicts(record.broker_order_id):
            return self._reconciliation(record)

        self._require_after_first_submit(record.exchange_event_at)

        if self._received_before_uncertainty(record.locally_received_at):
            return self._with_record(
                record,
                state=ExecutionState.UNKNOWN,
                broker_order_id=record.broker_order_id,
                preserve_broker_order_id=False,
                filled_quantity=max(self.filled_quantity, record.cumulative_quantity),
                latest_fill_exchange_at=record.exchange_event_at,
                preserve_fill_time=False,
                broker_fact_at=record.exchange_event_at,
            )

        if not self.submit_attempt_ids:
            return self._with_record(
                record,
                state=ExecutionState.RECONCILIATION_REQUIRED,
                broker_order_id=record.broker_order_id,
                preserve_broker_order_id=False,
                filled_quantity=record.cumulative_quantity,
                latest_fill_exchange_at=record.exchange_event_at,
                preserve_fill_time=False,
                broker_fact_at=record.exchange_event_at,
            )

        if record.cumulative_quantity <= self.filled_quantity:
            if (
                self.latest_fill_exchange_at is not None
                and record.exchange_event_at <= self.latest_fill_exchange_at
            ):
                return self._with_record(record, preserve_replace_basis=True)
            return self._reconciliation(record)

        if (
            self.filled_quantity > 0
            and record.fill_quantity != record.cumulative_quantity - self.filled_quantity
        ):
            return self._reconciliation(record)

        next_state = (
            ExecutionState.FILLED
            if isinstance(record, CompleteFill)
            else ExecutionState.PARTIALLY_FILLED
        )
        if self.state in {
            ExecutionState.CANCELED,
            ExecutionState.REJECTED,
            ExecutionState.RECONCILIATION_REQUIRED,
        }:
            next_state = ExecutionState.RECONCILIATION_REQUIRED
        return self._with_record(
            record,
            state=next_state,
            broker_order_id=record.broker_order_id,
            preserve_broker_order_id=False,
            filled_quantity=record.cumulative_quantity,
            latest_fill_exchange_at=record.exchange_event_at,
            preserve_fill_time=False,
            broker_fact_at=record.exchange_event_at,
        )

    def _apply_readback(self, record: BrokerReadback) -> ExecutionLifecycle:
        if not self.submit_attempt_ids:
            return self._reconciliation(record)
        self._require_after_first_submit(record.broker_snapshot_at)
        if self._broker_id_conflicts(record.broker_order_id):
            return self._reconciliation(record)
        if self._received_before_uncertainty(record.locally_received_at):
            return self._with_record(
                record,
                state=ExecutionState.UNKNOWN,
                broker_order_id=record.broker_order_id or self.broker_order_id,
                preserve_broker_order_id=False,
                broker_fact_at=record.broker_snapshot_at,
            )
        if (
            self.latest_broker_fact_at is not None
            and record.broker_snapshot_at < self.latest_broker_fact_at
        ):
            return self._with_record(record, preserve_replace_basis=True)

        total = self.intent.quantity
        status = record.status
        if status in {BrokerReadbackStatus.ABSENT, BrokerReadbackStatus.AMBIGUOUS}:
            return self._reconciliation(record)
        if record.cumulative_quantity > total:
            return self._reconciliation(record)
        if record.cumulative_quantity < self.filled_quantity:
            return self._reconciliation(record)
        if status is BrokerReadbackStatus.OPEN and self.filled_quantity != 0:
            return self._reconciliation(record)
        if status is BrokerReadbackStatus.PARTIALLY_FILLED and not (
            self.filled_quantity <= record.cumulative_quantity < total
        ):
            return self._reconciliation(record)
        if status is BrokerReadbackStatus.FILLED and record.cumulative_quantity != total:
            return self._reconciliation(record)
        if status is BrokerReadbackStatus.REJECTED and self.filled_quantity != 0:
            return self._reconciliation(record)

        state_for_status = {
            BrokerReadbackStatus.OPEN: ExecutionState.ACKNOWLEDGED,
            BrokerReadbackStatus.PARTIALLY_FILLED: ExecutionState.PARTIALLY_FILLED,
            BrokerReadbackStatus.FILLED: ExecutionState.FILLED,
            BrokerReadbackStatus.CANCELED: ExecutionState.CANCELED,
            BrokerReadbackStatus.REJECTED: ExecutionState.REJECTED,
        }
        next_state = state_for_status[status]
        if self.state is ExecutionState.RECONCILIATION_REQUIRED:
            next_state = ExecutionState.RECONCILIATION_REQUIRED
        elif self.state is ExecutionState.FILLED and next_state is not ExecutionState.FILLED:
            next_state = ExecutionState.RECONCILIATION_REQUIRED
        elif self.state in {ExecutionState.CANCELED, ExecutionState.REJECTED}:
            if next_state is not self.state:
                next_state = ExecutionState.RECONCILIATION_REQUIRED

        basis = (
            record
            if next_state
            in {
                ExecutionState.ACKNOWLEDGED,
                ExecutionState.PARTIALLY_FILLED,
            }
            else None
        )
        return self._with_record(
            record,
            state=next_state,
            broker_order_id=record.broker_order_id or self.broker_order_id,
            preserve_broker_order_id=False,
            filled_quantity=max(self.filled_quantity, record.cumulative_quantity),
            broker_fact_at=record.broker_snapshot_at,
            replace_basis=basis,
        )

    def _apply_replace(self, record: ReplaceIntent) -> ExecutionLifecycle:
        basis = self.replace_basis
        if basis is None or basis.record_id != record.based_on_readback_id:
            raise ReplacementRefusedError("replace requires a current matching readback")
        if self.state not in {ExecutionState.ACKNOWLEDGED, ExecutionState.PARTIALLY_FILLED}:
            raise ReplacementRefusedError(f"replace refused from {self.state}")
        if (
            basis.broker_order_id != record.broker_order_id
            or self.broker_order_id != record.broker_order_id
        ):
            raise ReplacementRefusedError("replace broker order does not match readback")
        if basis.cumulative_quantity != self.filled_quantity:
            raise ReplacementRefusedError("replace readback filled quantity is stale")
        if record.new_total_quantity <= self.filled_quantity:
            raise ReplacementRefusedError("replace total must exceed filled quantity")
        if record.replace_created_at < basis.locally_received_at:
            raise TemporalOrderError("replace predates its broker readback")
        return self._with_record(record)
