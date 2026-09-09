"""M6 paper-broker adapter: deterministic broker facts for paper execution.

One concrete, broker-NEUTRAL adapter: it consumes the local actions a paper
runner emits (``OrderIntent`` + ``SubmitAttempt``) plus a declared market
context (quote, fee schedule, fill plan) and produces exactly the broker
facts the lifecycle consumes — ``BrokerAcknowledgement``, a fill sequence,
and terminal ``BrokerReadback`` records.

Conventions pinned here: the adapter is DETERMINISTIC (no randomness — the
fill plan is declared, ids derive from the intent's own identity, and the
same inputs always produce byte-identical records); cumulative quantity
across emitted fills is STRICTLY increasing and sums to exactly the order's
quantity, with the last fill a ``CompleteFill`` whose cumulative equals the
total (fill economics are complete at termination by construction); broker
timestamps never precede the submit they answer and local receipt never
precedes the broker event it carries; and the emitted record sequence is
LIFECYCLE-CLEAN — applying it to an ``ExecutionLifecycle`` reaches
``FILLED`` with zero reconciliation reasons, so any drift in either layer
surfaces as a failed invariant rather than a silent discrepancy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from tree_options.execution.records import (
    BrokerAcknowledgement,
    BrokerReadback,
    BrokerReadbackStatus,
    CompleteFill,
    ExecutionRecord,
    OrderIntent,
    PartialFill,
    SubmitAttempt,
)
from tree_options.time.sessions import shift_instant


@dataclass(frozen=True)
class PaperQuote:
    """The market context one paper session executes against.

    ``bid``/``ask`` bracket every fill price; ``fee_per_contract`` is the
    declared all-in per-contract fee.  Prices are Decimal-exact by the
    records' own money discipline.
    """

    bid: Decimal
    ask: Decimal
    fee_per_contract: Decimal = Decimal("0.65")

    def __post_init__(self) -> None:
        for name, value in (("bid", self.bid), ("ask", self.ask)):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        if self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")
        if not isinstance(self.fee_per_contract, Decimal) or self.fee_per_contract < 0:
            raise ValueError("fee_per_contract must be a non-negative Decimal")


@dataclass(frozen=True)
class PaperFillPlan:
    """How the order's quantity splits across fills.

    ``fractions`` are parts of the whole (not percentages): ``[1]`` fills
    in one clip; ``[1, 1, 2]`` fills four contracts as 1+1+2.  The final
    clip is emitted as the ``CompleteFill``.  Empty means single-clip.
    """

    fractions: tuple[int, ...] = (1,)

    def __post_init__(self) -> None:
        materialized = self.fractions or (1,)
        if any(
            not isinstance(part, int) or isinstance(part, bool) or part < 1 for part in materialized
        ):
            raise ValueError("fill fractions must be positive ints")
        object.__setattr__(self, "fractions", materialized)

    def clips(self, quantity: int) -> tuple[int, ...]:
        """The per-fill clip sizes covering exactly ``quantity``.

        The declared fractions repeat as whole clips; the LAST clip is
        trimmed (never the earlier ones) so the sum is exact — a fill
        sequence that over- or under-shoots the order would violate the
        lifecycle's own economics.
        """
        clips: list[int] = []
        remaining = quantity
        index = 0
        while remaining > 0:
            clip = self.fractions[index % len(self.fractions)]
            clips.append(min(clip, remaining))
            remaining -= clips[-1]
            index += 1
        return tuple(clips)


@dataclass(frozen=True)
class PaperLag:
    """Declared clock offsets between local action, broker event, receipt.

    All offsets are WHOLE SECONDS applied through ``time.sessions.
    shift_instant`` (the repo's only sanctioned instant arithmetic —
    naive ``timedelta`` use outside ``time/`` is banned by the calendar's
    architectural test): broker events happen at ``send +
    broker_offset_seconds``, local receipt at ``event +
    receipt_offset_seconds``, and successive fills are spaced
    ``fill_spacing_seconds`` apart so exchange events are strictly ordered.
    """

    broker_offset_seconds: int = 1
    receipt_offset_seconds: int = 1
    fill_spacing_seconds: int = 1

    def __post_init__(self) -> None:
        for name, value in (
            ("broker_offset_seconds", self.broker_offset_seconds),
            ("receipt_offset_seconds", self.receipt_offset_seconds),
            ("fill_spacing_seconds", self.fill_spacing_seconds),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative int")


@dataclass
class PaperBroker:
    """Deterministic paper broker bound to one order's execution."""

    intent: OrderIntent
    quote: PaperQuote
    plan: PaperFillPlan = field(default_factory=PaperFillPlan)
    lag: PaperLag = field(default_factory=PaperLag)
    _broker_order_seq: int = 0
    _event_seq: int = 0

    def _next_event_seq(self) -> int:
        self._event_seq += 1
        return self._event_seq

    def acknowledge(self, attempt: SubmitAttempt) -> BrokerAcknowledgement:
        """The broker's answer to one submit attempt.

        The broker order id derives from the INTENT's identity, so retries
        of the same intent acknowledge the same broker order — the record
        contract's "send retries never mint another id" holds on the broker
        side too.
        """
        if attempt.intent_id != self.intent.intent_id:
            raise ValueError("attempt belongs to a different intent")
        self._broker_order_seq += 1
        acknowledged_at = shift_instant(attempt.send_attempt_at, self.lag.broker_offset_seconds)
        return BrokerAcknowledgement(
            record_id=f"paper-ack-{self.intent.intent_id}-{self._next_event_seq():04d}",
            intent_id=self.intent.intent_id,
            broker_order_id=f"paper-order-{self.intent.intent_id}",
            broker_acknowledged_at=acknowledged_at,
            locally_received_at=shift_instant(acknowledged_at, self.lag.receipt_offset_seconds),
            source="synthetic-paper-adapter",
            source_sequence_id=f"paper-ack-{self.intent.intent_id}-{self._next_event_seq():04d}",
            broker_sequence_id=f"paper-ack-{self.intent.intent_id}-{self._broker_order_seq}",
        )

    def fills(self, attempt: SubmitAttempt) -> tuple[PartialFill | CompleteFill, ...]:
        """The fill sequence for the acknowledged order.

        Prices walk the quote's inside: each clip executes at the midpoint
        (buy and sell symmetric), cumulative quantity is strictly
        increasing, the final clip is the ``CompleteFill`` closing at
        exactly the order quantity.
        """
        clips = self.plan.clips(self.intent.quantity)
        midpoint = (self.quote.bid + self.quote.ask) / Decimal(2)
        exchange_at = shift_instant(attempt.send_attempt_at, self.lag.broker_offset_seconds)
        cumulative = 0
        fills: list[PartialFill | CompleteFill] = []
        for clip in clips:
            cumulative += clip
            is_final = cumulative == self.intent.quantity
            sequence = self._next_event_seq()
            fill_type = CompleteFill if is_final else PartialFill
            fills.append(
                fill_type(
                    record_id=f"paper-fill-{self.intent.intent_id}-{sequence:04d}",
                    intent_id=self.intent.intent_id,
                    broker_order_id=f"paper-order-{self.intent.intent_id}",
                    fill_quantity=clip,
                    cumulative_quantity=cumulative,
                    unit_price=midpoint,
                    fees=self.quote.fee_per_contract * clip,
                    exchange_event_at=exchange_at,
                    locally_received_at=shift_instant(exchange_at, self.lag.receipt_offset_seconds),
                    source="synthetic-paper-adapter",
                    source_sequence_id=f"paper-fill-{self.intent.intent_id}-{sequence:04d}",
                    broker_sequence_id=f"paper-fill-{self.intent.intent_id}-{sequence:04d}",
                )
            )
            exchange_at = shift_instant(exchange_at, self.lag.fill_spacing_seconds)
        return tuple(fills)

    def readbacks(self, attempt: SubmitAttempt) -> tuple[BrokerReadback, ...]:
        """Terminal readback for the fully filled order.

        Exactly one snapshot, taken after the last fill's exchange event,
        reporting the FILLED status with the order's total and cumulative —
        the minimal broker confirmation that closes the lifecycle.
        """
        if not self.intent.quantity >= 1:
            raise ValueError("intent quantity must be positive")
        fills = self.fills(attempt)
        snapshot_at = shift_instant(fills[-1].exchange_event_at, self.lag.fill_spacing_seconds)
        sequence = self._next_event_seq()
        return (
            BrokerReadback(
                record_id=f"paper-readback-{self.intent.intent_id}-{sequence:04d}",
                intent_id=self.intent.intent_id,
                status=BrokerReadbackStatus.FILLED,
                broker_order_id=f"paper-order-{self.intent.intent_id}",
                total_quantity=self.intent.quantity,
                cumulative_quantity=self.intent.quantity,
                broker_snapshot_at=snapshot_at,
                locally_received_at=shift_instant(snapshot_at, self.lag.receipt_offset_seconds),
                source="synthetic-paper-adapter",
                source_sequence_id=f"paper-readback-{self.intent.intent_id}-{sequence:04d}",
                broker_sequence_id=f"paper-readback-{self.intent.intent_id}-{sequence:04d}",
            ),
        )

    def broker_facts(self, attempt: SubmitAttempt) -> tuple[ExecutionRecord, ...]:
        """The full lifecycle-clean broker-fact sequence for one submit."""
        return (self.acknowledge(attempt), *self.fills(attempt), *self.readbacks(attempt))
