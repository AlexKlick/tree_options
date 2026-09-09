"""M6 paper-broker adapter: deterministic broker facts for paper execution.

One concrete, broker-NEUTRAL adapter: it consumes the local actions a paper
runner emits (``OrderIntent`` + ``SubmitAttempt``) plus a declared market
context (quote, fee schedule, fill plan) and produces exactly the broker
facts the lifecycle consumes — ``BrokerAcknowledgement``, a fill sequence,
and terminal ``BrokerReadback`` records.

Conventions pinned here: the adapter is DETERMINISTIC and IDEMPOTENT — ids
derive from stable identity (intent + slot/role), fill economics anchor to
the FIRST acknowledged submit, and re-requesting the same order's facts
returns byte-identical records (a lifecycle no-op by the exact-duplicate
rule), so a send retry can never duplicate economics; cumulative quantity
across emitted fills is STRICTLY increasing and sums to exactly the order's
quantity, with the last fill a ``CompleteFill`` whose cumulative equals the
total (fill economics are complete at termination by construction); broker
timestamps never precede the submit they answer and local receipt never
precedes the broker event it carries, and successive fills are spaced
``fill_spacing_seconds >= 1`` apart so exchange events are strictly ordered;
the executable price is the quote midpoint QUANTIZED to the record schema's
8-decimal bound (always representable, always within the bracket); and the
emitted record sequence is LIFECYCLE-CLEAN — applying it to an
``ExecutionLifecycle`` reaches ``FILLED`` with zero reconciliation reasons,
so any drift in either layer surfaces as a failed invariant rather than a
silent discrepancy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

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

#: The record schema's bound on price precision (ExactPrice decimal_places).
_PRICE_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True)
class PaperQuote:
    """The market context one paper session executes against.

    ``bid``/``ask`` bracket every fill price; ``fee_per_contract`` is the
    declared all-in per-contract fee.  Prices are Decimal-exact and finite
    by the records' own money discipline.
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
        if (
            not isinstance(self.fee_per_contract, Decimal)
            or not self.fee_per_contract.is_finite()
            or self.fee_per_contract < 0
        ):
            raise ValueError("fee_per_contract must be a non-negative finite Decimal")

    @property
    def executable_price(self) -> Decimal:
        """The midpoint quantized to the record schema's 8-decimal bound.

        Quantization is half-up and can never leave the bracket: the true
        midpoint sits inside ``[bid, ask]`` with at least half a quantum of
        room on each side unless the bracket itself is sub-quantum wide, in
        which case the rounded price lands on one of the two quotes.
        """
        return ((self.bid + self.ask) / Decimal(2)).quantize(_PRICE_QUANTUM, ROUND_HALF_UP)


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
    ``fill_spacing_seconds`` apart.  Spacing must be at least one second —
    the adapter's declared convention is STRICTLY ordered exchange events.
    """

    broker_offset_seconds: int = 1
    receipt_offset_seconds: int = 1
    fill_spacing_seconds: int = 1

    def __post_init__(self) -> None:
        for name, value in (
            ("broker_offset_seconds", self.broker_offset_seconds),
            ("receipt_offset_seconds", self.receipt_offset_seconds),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative int")
        spacing = self.fill_spacing_seconds
        if not isinstance(spacing, int) or isinstance(spacing, bool) or spacing < 1:
            raise ValueError("fill_spacing_seconds must be an int >= 1 (strict event order)")


@dataclass
class PaperBroker:
    """Deterministic paper broker bound to one order's execution.

    Fill economics anchor to the FIRST acknowledged submit: a retried
    submit is answered with a fresh acknowledgement for the SAME broker
    order plus the byte-identical fill history (an identity-preserving
    no-op in the lifecycle), never a second set of economics.
    """

    intent: OrderIntent
    quote: PaperQuote
    plan: PaperFillPlan = field(default_factory=PaperFillPlan)
    lag: PaperLag = field(default_factory=PaperLag)
    _anchor_attempt: SubmitAttempt | None = None

    def acknowledge(self, attempt: SubmitAttempt) -> BrokerAcknowledgement:
        """The broker's answer to one submit attempt.

        The broker order id derives from the INTENT's identity, so retries
        of the same intent acknowledge the same broker order — the record
        contract's "send retries never mint another id" holds on the broker
        side too.  The first acknowledged attempt becomes the anchor the
        fill history's timestamps derive from.
        """
        if attempt.intent_id != self.intent.intent_id:
            raise ValueError("attempt belongs to a different intent")
        if self._anchor_attempt is None:
            self._anchor_attempt = attempt
        acknowledged_at = shift_instant(attempt.send_attempt_at, self.lag.broker_offset_seconds)
        return BrokerAcknowledgement(
            record_id=f"paper-ack-{self.intent.intent_id}-{attempt.record_id}",
            intent_id=self.intent.intent_id,
            broker_order_id=f"paper-order-{self.intent.intent_id}",
            broker_acknowledged_at=acknowledged_at,
            locally_received_at=shift_instant(acknowledged_at, self.lag.receipt_offset_seconds),
            source="synthetic-paper-adapter",
            source_sequence_id=f"paper-ack-{self.intent.intent_id}-{attempt.record_id}",
            broker_sequence_id=f"paper-ack-{self.intent.intent_id}-{attempt.record_id}",
        )

    def _require_anchor(self) -> SubmitAttempt:
        if self._anchor_attempt is None:
            raise ValueError("acknowledge a submit before requesting broker facts")
        return self._anchor_attempt

    def fills(self) -> tuple[PartialFill | CompleteFill, ...]:
        """The fill sequence for the acknowledged order.

        Cumulative quantity is strictly increasing, each clip executes at
        the quote's executable (quantized midpoint) price with fees scaled
        to the clip, and the final clip is the ``CompleteFill`` closing at
        exactly the order quantity.  Record ids derive from the intent and
        slot, so repeated calls return byte-identical records.
        """
        anchor = self._require_anchor()
        clips = self.plan.clips(self.intent.quantity)
        price = self.quote.executable_price
        exchange_at = shift_instant(anchor.send_attempt_at, self.lag.broker_offset_seconds)
        cumulative = 0
        fills: list[PartialFill | CompleteFill] = []
        for slot, clip in enumerate(clips, start=1):
            cumulative += clip
            is_final = cumulative == self.intent.quantity
            fill_type = CompleteFill if is_final else PartialFill
            fills.append(
                fill_type(
                    record_id=f"paper-fill-{self.intent.intent_id}-{slot:04d}",
                    intent_id=self.intent.intent_id,
                    broker_order_id=f"paper-order-{self.intent.intent_id}",
                    fill_quantity=clip,
                    cumulative_quantity=cumulative,
                    unit_price=price,
                    fees=self.quote.fee_per_contract * clip,
                    exchange_event_at=exchange_at,
                    locally_received_at=shift_instant(exchange_at, self.lag.receipt_offset_seconds),
                    source="synthetic-paper-adapter",
                    source_sequence_id=f"paper-fill-{self.intent.intent_id}-{slot:04d}",
                    broker_sequence_id=f"paper-fill-{self.intent.intent_id}-{slot:04d}",
                )
            )
            exchange_at = shift_instant(exchange_at, self.lag.fill_spacing_seconds)
        return tuple(fills)

    def readbacks(self) -> tuple[BrokerReadback, ...]:
        """Terminal readback for the fully filled order.

        Exactly one snapshot, taken after the last fill's exchange event,
        reporting the FILLED status with the order's total and cumulative —
        the minimal broker confirmation that closes the lifecycle.
        """
        fills = self.fills()
        snapshot_at = shift_instant(fills[-1].exchange_event_at, self.lag.fill_spacing_seconds)
        return (
            BrokerReadback(
                record_id=f"paper-readback-{self.intent.intent_id}",
                intent_id=self.intent.intent_id,
                status=BrokerReadbackStatus.FILLED,
                broker_order_id=f"paper-order-{self.intent.intent_id}",
                total_quantity=self.intent.quantity,
                cumulative_quantity=self.intent.quantity,
                broker_snapshot_at=snapshot_at,
                locally_received_at=shift_instant(snapshot_at, self.lag.receipt_offset_seconds),
                source="synthetic-paper-adapter",
                source_sequence_id=f"paper-readback-{self.intent.intent_id}",
                broker_sequence_id=f"paper-readback-{self.intent.intent_id}",
            ),
        )

    def broker_facts(self, attempt: SubmitAttempt) -> tuple[ExecutionRecord, ...]:
        """The full lifecycle-clean broker-fact sequence for one submit.

        For a RETRY this is a fresh acknowledgement plus the unchanged
        fill history — applying it after the original sequence is an
        identity-preserving no-op, never duplicate economics.
        """
        return (self.acknowledge(attempt), *self.fills(), *self.readbacks())
