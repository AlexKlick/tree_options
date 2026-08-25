"""Pure, immutable execution lifecycle projection.

The reducer has no I/O and performs no broker action. It accepts immutable
records, checks identity/sequence collisions before projection, and derives
authority from the full retained fact set. UNKNOWN never grants permission to
resubmit or replace, and local replace intent never impersonates broker state.
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


class ReconciliationReason(StrEnum):
    """Machine-readable reasons that keep the effective state fail closed."""

    MISSING_SUBMIT = "MISSING_SUBMIT"
    BROKER_ORDER_ID_CONFLICT = "BROKER_ORDER_ID_CONFLICT"
    TOTAL_UNCONFIRMED = "TOTAL_UNCONFIRMED"
    FILL_EXCEEDS_CONFIRMED_TOTAL = "FILL_EXCEEDS_CONFIRMED_TOTAL"
    FILL_KIND_TOTAL_MISMATCH = "FILL_KIND_TOTAL_MISMATCH"
    FILL_ECONOMIC_GAP = "FILL_ECONOMIC_GAP"
    FILL_ECONOMIC_OVERLAP = "FILL_ECONOMIC_OVERLAP"
    REJECT_WITH_OBSERVED_FILL = "REJECT_WITH_OBSERVED_FILL"
    READBACK_FILL_CONTRADICTION = "READBACK_FILL_CONTRADICTION"
    READBACK_CUMULATIVE_REGRESSION = "READBACK_CUMULATIVE_REGRESSION"
    TERMINAL_FACT_CONTRADICTION = "TERMINAL_FACT_CONTRADICTION"
    AMBIGUOUS_READBACK = "AMBIGUOUS_READBACK"
    REPLACE_CONFIRMATION_MISMATCH = "REPLACE_CONFIRMATION_MISMATCH"
    UNEXPECTED_CONFIRMED_TOTAL_CHANGE = "UNEXPECTED_CONFIRMED_TOTAL_CHANGE"


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


BrokerFact = BrokerAcknowledgement | OrderReject | PartialFill | CompleteFill | BrokerReadback
FillRecord = PartialFill | CompleteFill


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


def _event_sort_key(record: ExecutionRecord) -> tuple[datetime, int, bytes]:
    priority = 0 if isinstance(record, (SubmitAttempt, ReplaceIntent)) else 1
    return (_primary_time(record), priority, _canonical_bytes(record))


def _execution_records(
    records: tuple[StoredExecutionRecord, ...],
) -> tuple[ExecutionRecord, ...]:
    return tuple(record for record in records if not isinstance(record, OrderIntent))


def _broker_facts(records: tuple[StoredExecutionRecord, ...]) -> tuple[BrokerFact, ...]:
    return tuple(
        record
        for record in records
        if isinstance(
            record,
            (BrokerAcknowledgement, OrderReject, PartialFill, CompleteFill, BrokerReadback),
        )
    )


def _fill_records(records: tuple[StoredExecutionRecord, ...]) -> tuple[FillRecord, ...]:
    return tuple(record for record in records if isinstance(record, (PartialFill, CompleteFill)))


def _fill_interval(record: FillRecord) -> tuple[int, int]:
    return (record.cumulative_quantity - record.fill_quantity, record.cumulative_quantity)


def _derive_fill_intervals(
    records: tuple[StoredExecutionRecord, ...],
) -> tuple[tuple[tuple[int, int], ...], bool]:
    raw = sorted(_fill_interval(record) for record in _fill_records(records))
    overlap = any(
        left_start < right_end and right_start < left_end
        for index, (left_start, left_end) in enumerate(raw)
        for right_start, right_end in raw[index + 1 :]
    )
    merged: list[tuple[int, int]] = []
    for start, end in raw:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        prior_start, prior_end = merged[-1]
        merged[-1] = (prior_start, max(prior_end, end))
    return tuple(merged), overlap


def _covers_through(intervals: tuple[tuple[int, int], ...], quantity: int) -> bool:
    if quantity == 0:
        return True
    return intervals == ((0, quantity),)


def _derive_observed_cumulative(records: tuple[StoredExecutionRecord, ...]) -> int:
    quantities = [
        record.cumulative_quantity
        for record in records
        if isinstance(record, (PartialFill, CompleteFill, BrokerReadback))
    ]
    return max(quantities, default=0)


def _derive_broker_order_id(
    records: tuple[StoredExecutionRecord, ...],
) -> tuple[str | None, bool]:
    identified = [record for record in _broker_facts(records) if record.broker_order_id is not None]
    if not identified:
        return None, False
    ordered = sorted(identified, key=_event_sort_key)
    identities = {record.broker_order_id for record in ordered}
    return ordered[0].broker_order_id, len(identities) > 1


def _derive_quantity_authority(
    intent: OrderIntent,
    records: tuple[StoredExecutionRecord, ...],
) -> tuple[int | None, int | None, datetime | None, frozenset[ReconciliationReason]]:
    confirmed: int | None = None
    pending: int | None = None
    pending_basis_id: str | None = None
    confirmed_at: datetime | None = None
    submitted = False
    reasons: set[ReconciliationReason] = set()

    relevant = [
        record
        for record in _execution_records(records)
        if isinstance(record, (SubmitAttempt, BrokerAcknowledgement, BrokerReadback, ReplaceIntent))
    ]
    for record in sorted(relevant, key=_event_sort_key):
        if isinstance(record, SubmitAttempt):
            submitted = True
            continue
        if not submitted:
            continue
        if isinstance(record, BrokerAcknowledgement):
            if confirmed is None:
                confirmed = intent.quantity
                confirmed_at = record.broker_acknowledged_at
            continue
        if isinstance(record, ReplaceIntent):
            pending = record.new_total_quantity
            pending_basis_id = record.based_on_readback_id
            continue
        if record.total_quantity is None:
            continue
        observed = record.total_quantity
        if pending is not None and record.record_id != pending_basis_id:
            if observed != pending:
                reasons.add(ReconciliationReason.REPLACE_CONFIRMATION_MISMATCH)
            confirmed = observed
            confirmed_at = record.broker_snapshot_at
            pending = None
            pending_basis_id = None
        elif confirmed is None:
            if observed != intent.quantity:
                reasons.add(ReconciliationReason.UNEXPECTED_CONFIRMED_TOTAL_CHANGE)
            confirmed = observed
            confirmed_at = record.broker_snapshot_at
        elif observed != confirmed:
            reasons.add(ReconciliationReason.UNEXPECTED_CONFIRMED_TOTAL_CHANGE)
            confirmed = observed
            confirmed_at = record.broker_snapshot_at

    return confirmed, pending, confirmed_at, frozenset(reasons)


def _readback_terminal_state(record: BrokerReadback) -> ExecutionState | None:
    if record.status is BrokerReadbackStatus.FILLED:
        return ExecutionState.FILLED
    if record.status is BrokerReadbackStatus.CANCELED:
        if (
            record.total_quantity is not None
            and record.cumulative_quantity == record.total_quantity
        ):
            return ExecutionState.FILLED
        return ExecutionState.CANCELED
    if record.status is BrokerReadbackStatus.REJECTED:
        return ExecutionState.REJECTED
    return None


def _derive_fact_reasons(
    records: tuple[StoredExecutionRecord, ...],
) -> frozenset[ReconciliationReason]:
    reasons: set[ReconciliationReason] = set()
    observed_cumulative = 0
    terminal_state: ExecutionState | None = None
    terminal_cumulative = 0

    for record in sorted(_broker_facts(records), key=_event_sort_key):
        if isinstance(record, BrokerAcknowledgement):
            if terminal_state in {ExecutionState.CANCELED, ExecutionState.REJECTED}:
                reasons.add(ReconciliationReason.TERMINAL_FACT_CONTRADICTION)
            continue
        if isinstance(record, OrderReject):
            if observed_cumulative > 0:
                reasons.add(ReconciliationReason.REJECT_WITH_OBSERVED_FILL)
            if terminal_state is not None and terminal_state is not ExecutionState.REJECTED:
                reasons.add(ReconciliationReason.TERMINAL_FACT_CONTRADICTION)
            terminal_state = ExecutionState.REJECTED
            terminal_cumulative = observed_cumulative
            continue
        if isinstance(record, (PartialFill, CompleteFill)):
            if terminal_state is not None and record.cumulative_quantity > terminal_cumulative:
                reasons.add(ReconciliationReason.TERMINAL_FACT_CONTRADICTION)
            observed_cumulative = max(observed_cumulative, record.cumulative_quantity)
            if isinstance(record, CompleteFill):
                terminal_state = ExecutionState.FILLED
                terminal_cumulative = observed_cumulative
            continue

        if record.status in {BrokerReadbackStatus.ABSENT, BrokerReadbackStatus.AMBIGUOUS}:
            # This bounded foundation has no explicit reconciliation-resolution
            # record, so historical absence/ambiguity remains intentionally sticky.
            reasons.add(ReconciliationReason.AMBIGUOUS_READBACK)
            continue
        if record.cumulative_quantity < observed_cumulative:
            reasons.add(ReconciliationReason.READBACK_CUMULATIVE_REGRESSION)
        if (
            record.status in {BrokerReadbackStatus.OPEN, BrokerReadbackStatus.REJECTED}
            and observed_cumulative > 0
        ):
            reasons.add(ReconciliationReason.READBACK_FILL_CONTRADICTION)

        mapped_terminal = _readback_terminal_state(record)
        if terminal_state is not None:
            if mapped_terminal is None or mapped_terminal is not terminal_state:
                reasons.add(ReconciliationReason.TERMINAL_FACT_CONTRADICTION)
        if mapped_terminal is not None:
            terminal_state = mapped_terminal
            terminal_cumulative = max(observed_cumulative, record.cumulative_quantity)
        observed_cumulative = max(observed_cumulative, record.cumulative_quantity)

    return frozenset(reasons)


def _derive_broker_state(records: tuple[StoredExecutionRecord, ...]) -> ExecutionState:
    if not any(isinstance(record, SubmitAttempt) for record in records):
        return ExecutionState.CREATED

    state = ExecutionState.SUBMITTING
    observed_cumulative = 0
    terminal_states = {
        ExecutionState.FILLED,
        ExecutionState.CANCELED,
        ExecutionState.REJECTED,
    }
    for record in sorted(_broker_facts(records), key=_event_sort_key):
        if isinstance(record, BrokerAcknowledgement):
            if state not in terminal_states:
                state = (
                    ExecutionState.PARTIALLY_FILLED
                    if observed_cumulative > 0
                    else ExecutionState.ACKNOWLEDGED
                )
            continue
        if isinstance(record, OrderReject):
            if observed_cumulative == 0:
                state = ExecutionState.REJECTED
            continue
        if isinstance(record, (PartialFill, CompleteFill)):
            prior_cumulative = observed_cumulative
            observed_cumulative = max(observed_cumulative, record.cumulative_quantity)
            if state in terminal_states and record.cumulative_quantity <= prior_cumulative:
                continue
            state = (
                ExecutionState.FILLED
                if isinstance(record, CompleteFill)
                else ExecutionState.PARTIALLY_FILLED
            )
            continue

        prior_cumulative = observed_cumulative
        observed_cumulative = max(observed_cumulative, record.cumulative_quantity)
        if record.status in {BrokerReadbackStatus.ABSENT, BrokerReadbackStatus.AMBIGUOUS}:
            continue
        if record.cumulative_quantity < prior_cumulative:
            continue
        if record.status in {BrokerReadbackStatus.OPEN, BrokerReadbackStatus.REJECTED}:
            if prior_cumulative > 0:
                continue
        state_for_status = {
            BrokerReadbackStatus.OPEN: ExecutionState.ACKNOWLEDGED,
            BrokerReadbackStatus.PARTIALLY_FILLED: ExecutionState.PARTIALLY_FILLED,
            BrokerReadbackStatus.FILLED: ExecutionState.FILLED,
            BrokerReadbackStatus.CANCELED: (
                ExecutionState.FILLED
                if record.total_quantity is not None
                and record.cumulative_quantity == record.total_quantity
                else ExecutionState.CANCELED
            ),
            BrokerReadbackStatus.REJECTED: ExecutionState.REJECTED,
        }
        state = state_for_status[record.status]
    return state


def _derive_latest_authoritative_receipt(
    records: tuple[StoredExecutionRecord, ...],
) -> datetime | None:
    non_readbacks = [
        record for record in _broker_facts(records) if not isinstance(record, BrokerReadback)
    ]
    latest_non_readback_at = max(
        (_primary_time(record) for record in non_readbacks),
        default=None,
    )
    received = [record.locally_received_at for record in non_readbacks]
    received.extend(
        record.locally_received_at
        for record in records
        if isinstance(record, BrokerReadback)
        and (latest_non_readback_at is None or record.broker_snapshot_at >= latest_non_readback_at)
    )
    return max(received, default=None)


def _derive_uncertain_since(
    records: tuple[StoredExecutionRecord, ...],
    latest_authoritative_receipt: datetime | None,
) -> datetime | None:
    observed = [
        record.locally_received_at
        for record in records
        if isinstance(record, (TimeoutObserved, DisconnectObserved))
    ]
    latest_uncertainty = max(observed, default=None)
    if latest_uncertainty is None:
        return None
    # Equal local receipt times cannot establish which observation is newer.
    # Only a strictly later authoritative broker fact resolves transport uncertainty.
    if (
        latest_authoritative_receipt is not None
        and latest_authoritative_receipt > latest_uncertainty
    ):
        return None
    return latest_uncertainty


def _derive_reconciliation_reasons(
    records: tuple[StoredExecutionRecord, ...],
    *,
    confirmed_total: int | None,
    quantity_reasons: frozenset[ReconciliationReason],
    filled_quantity: int,
    fill_intervals: tuple[tuple[int, int], ...],
    fill_overlap: bool,
    broker_id_conflict: bool,
) -> frozenset[ReconciliationReason]:
    reasons = set(quantity_reasons)
    facts = _broker_facts(records)
    if facts and not any(isinstance(record, SubmitAttempt) for record in records):
        reasons.add(ReconciliationReason.MISSING_SUBMIT)
    if broker_id_conflict:
        reasons.add(ReconciliationReason.BROKER_ORDER_ID_CONFLICT)
    if fill_overlap:
        reasons.add(ReconciliationReason.FILL_ECONOMIC_OVERLAP)
    if filled_quantity > 0 and not _covers_through(fill_intervals, filled_quantity):
        reasons.add(ReconciliationReason.FILL_ECONOMIC_GAP)
    if filled_quantity > 0 and confirmed_total is None:
        reasons.add(ReconciliationReason.TOTAL_UNCONFIRMED)
    if confirmed_total is not None:
        if filled_quantity > confirmed_total:
            reasons.add(ReconciliationReason.FILL_EXCEEDS_CONFIRMED_TOTAL)
        for record in _fill_records(records):
            if isinstance(record, CompleteFill) and record.cumulative_quantity != confirmed_total:
                reasons.add(ReconciliationReason.FILL_KIND_TOTAL_MISMATCH)
            if isinstance(record, PartialFill) and record.cumulative_quantity >= confirmed_total:
                reasons.add(ReconciliationReason.FILL_KIND_TOTAL_MISMATCH)
    reasons.update(_derive_fact_reasons(records))
    return frozenset(reasons)


def _derive_replace_basis(
    records: tuple[StoredExecutionRecord, ...],
    *,
    state: ExecutionState,
    broker_order_id: str | None,
    confirmed_total: int | None,
    pending_total: int | None,
    filled_quantity: int,
    fill_intervals: tuple[tuple[int, int], ...],
) -> BrokerReadback | None:
    """Derive current replace authority from retained broker knowledge.

    A snapshot is current only when no other broker fact was learned later. A
    distinct fact learned at the exact same instant fails closed because the
    records do not establish which fact the snapshot incorporated.
    """
    if (
        state not in {ExecutionState.ACKNOWLEDGED, ExecutionState.PARTIALLY_FILLED}
        or pending_total is not None
        or not _covers_through(fill_intervals, filled_quantity)
    ):
        return None

    facts = _broker_facts(records)
    candidates = [
        record
        for record in records
        if isinstance(record, BrokerReadback)
        and record.status in {BrokerReadbackStatus.OPEN, BrokerReadbackStatus.PARTIALLY_FILLED}
        and record.broker_order_id == broker_order_id
        and record.total_quantity == confirmed_total
        and record.cumulative_quantity == filled_quantity
    ]
    eligible: list[BrokerReadback] = []
    for candidate in candidates:
        invalidated = False
        for fact in facts:
            if fact is candidate:
                continue
            if fact.locally_received_at >= candidate.locally_received_at:
                invalidated = True
                break
            if _primary_time(fact) > candidate.broker_snapshot_at:
                invalidated = True
                break
        if not invalidated:
            eligible.append(candidate)
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda record: (
            record.locally_received_at,
            record.broker_snapshot_at,
            _canonical_bytes(record),
        ),
    )


@dataclass(frozen=True, slots=True)
class ExecutionLifecycle:
    """Immutable projection for one and only one OrderIntent."""

    intent: OrderIntent
    state: ExecutionState
    broker_state: ExecutionState
    records: tuple[StoredExecutionRecord, ...]
    submit_attempt_ids: tuple[str, ...] = ()
    broker_order_id: str | None = None
    broker_confirmed_total_quantity: int | None = None
    pending_replace_total_quantity: int | None = None
    filled_quantity: int = 0
    fill_economic_intervals: tuple[tuple[int, int], ...] = ()
    reconciliation_reasons: frozenset[ReconciliationReason] = frozenset()
    latest_fill_exchange_at: datetime | None = None
    latest_broker_fact_at: datetime | None = None
    latest_broker_fact_received_at: datetime | None = None
    latest_total_confirmation_at: datetime | None = None
    uncertain_since_at: datetime | None = None
    replace_basis: BrokerReadback | None = None

    @classmethod
    def start(cls, intent: OrderIntent) -> ExecutionLifecycle:
        return cls(
            intent=intent,
            state=ExecutionState.CREATED,
            broker_state=ExecutionState.CREATED,
            records=(intent,),
        )

    @property
    def replace_basis_id(self) -> str | None:
        return None if self.replace_basis is None else self.replace_basis.record_id

    @property
    def economically_covered_quantity(self) -> int:
        return sum(end - start for start, end in self.fill_economic_intervals)

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

    def _with_record(self, record: ExecutionRecord) -> ExecutionLifecycle:
        records = (*self.records, record)
        confirmed_total, pending_total, total_at, quantity_reasons = _derive_quantity_authority(
            self.intent, records
        )
        fill_intervals, fill_overlap = _derive_fill_intervals(records)
        filled_quantity = _derive_observed_cumulative(records)
        broker_order_id, broker_id_conflict = _derive_broker_order_id(records)
        reasons = _derive_reconciliation_reasons(
            records,
            confirmed_total=confirmed_total,
            quantity_reasons=quantity_reasons,
            filled_quantity=filled_quantity,
            fill_intervals=fill_intervals,
            fill_overlap=fill_overlap,
            broker_id_conflict=broker_id_conflict,
        )
        broker_state = _derive_broker_state(records)
        authoritative_receipt = _derive_latest_authoritative_receipt(records)
        uncertain_since = (
            None
            if broker_state
            in {ExecutionState.FILLED, ExecutionState.CANCELED, ExecutionState.REJECTED}
            else _derive_uncertain_since(records, authoritative_receipt)
        )
        if reasons:
            state = ExecutionState.RECONCILIATION_REQUIRED
        elif uncertain_since is not None:
            state = ExecutionState.UNKNOWN
        else:
            state = broker_state

        basis = _derive_replace_basis(
            records,
            state=state,
            broker_order_id=broker_order_id,
            confirmed_total=confirmed_total,
            pending_total=pending_total,
            filled_quantity=filled_quantity,
            fill_intervals=fill_intervals,
        )

        fill_times = [item.exchange_event_at for item in _fill_records(records)]
        facts = _broker_facts(records)
        return replace(
            self,
            state=state,
            broker_state=broker_state,
            records=records,
            broker_order_id=broker_order_id,
            broker_confirmed_total_quantity=confirmed_total,
            pending_replace_total_quantity=pending_total,
            filled_quantity=filled_quantity,
            fill_economic_intervals=fill_intervals,
            reconciliation_reasons=reasons,
            latest_fill_exchange_at=max(fill_times, default=None),
            latest_broker_fact_at=max(
                (_primary_time(item) for item in facts),
                default=None,
            ),
            latest_broker_fact_received_at=authoritative_receipt,
            latest_total_confirmation_at=total_at,
            uncertain_since_at=uncertain_since,
            replace_basis=basis,
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

    def _apply_submit(self, record: SubmitAttempt) -> ExecutionLifecycle:
        if self.submit_attempt_ids:
            latest = max(
                existing.send_attempt_at
                for existing in self.records
                if isinstance(existing, SubmitAttempt)
            )
            if record.send_attempt_at < latest:
                raise TemporalOrderError("submit retry predates an existing attempt")
        elif ReconciliationReason.MISSING_SUBMIT in self.reconciliation_reasons:
            if any(
                record.send_attempt_at > _primary_time(fact) for fact in _broker_facts(self.records)
            ):
                raise TemporalOrderError("retrospective submit postdates a retained broker fact")
            projected = self._with_record(record)
            return replace(
                projected,
                submit_attempt_ids=(*self.submit_attempt_ids, record.record_id),
            )
        if self.state is ExecutionState.UNKNOWN:
            raise TransitionRefusedError("UNKNOWN requires broker readback before any action")
        if self.state not in {ExecutionState.CREATED, ExecutionState.SUBMITTING}:
            raise TransitionRefusedError(f"submit attempt refused from {self.state}")
        projected = self._with_record(record)
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

        if not self.submit_attempt_ids:
            raise TransitionRefusedError("uncertainty observation requires a submit attempt")
        return self._with_record(record)

    def _apply_ack(self, record: BrokerAcknowledgement) -> ExecutionLifecycle:
        self._require_after_first_submit(record.broker_acknowledged_at)
        return self._with_record(record)

    def _apply_reject(self, record: OrderReject) -> ExecutionLifecycle:
        self._require_after_first_submit(record.broker_acknowledged_at)
        return self._with_record(record)

    def _apply_fill(self, record: PartialFill | CompleteFill) -> ExecutionLifecycle:
        self._require_after_first_submit(record.exchange_event_at)
        return self._with_record(record)

    def _apply_readback(self, record: BrokerReadback) -> ExecutionLifecycle:
        self._require_after_first_submit(record.broker_snapshot_at)
        return self._with_record(record)

    def _apply_replace(self, record: ReplaceIntent) -> ExecutionLifecycle:
        if self.pending_replace_total_quantity is not None:
            raise ReplacementRefusedError("replace refused while prior replace is pending")
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
        if basis.total_quantity != self.broker_confirmed_total_quantity:
            raise ReplacementRefusedError("replace readback total quantity is stale")
        if basis.cumulative_quantity != self.filled_quantity:
            raise ReplacementRefusedError("replace readback filled quantity is stale")
        if not _covers_through(self.fill_economic_intervals, self.filled_quantity):
            raise ReplacementRefusedError("replace requires complete fill economics")
        if record.new_total_quantity <= self.filled_quantity:
            raise ReplacementRefusedError("replace total must exceed filled quantity")
        if record.replace_created_at < basis.locally_received_at:
            raise TemporalOrderError("replace predates its broker readback")
        return self._with_record(record)
