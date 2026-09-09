"""M6 paper adapter: deterministic lifecycle-clean broker facts, pinned exactly."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tree_options.execution import (
    BrokerReadback,
    CompleteFill,
    ExecutionLifecycle,
    ExecutionState,
    OrderIntent,
    PartialFill,
    SubmitAttempt,
)
from tree_options.execution.paper import PaperBroker, PaperFillPlan, PaperLag, PaperQuote

T0 = datetime(2026, 9, 9, 20, 0, tzinfo=UTC)


def _intent(quantity: int = 4) -> OrderIntent:
    return OrderIntent(
        intent_id="paper-001",
        contract_id="O:XYZ260918C00100000",
        side="BUY",
        position_effect="OPEN_LONG",
        quantity=quantity,
        order_type="LIMIT",
        limit_price=Decimal("1.25"),
        execution_style="single",
        package_id=None,
        intent_created_at=T0,
        source="paper-runner",
        source_sequence_id="intent-seq-001",
    )


def _attempt(number: int = 1, *, send_at: int = 5) -> SubmitAttempt:
    return SubmitAttempt(
        record_id=f"attempt-{number:03d}",
        intent_id="paper-001",
        send_attempt_at=T0 + timedelta(seconds=send_at),
        source="paper-runner",
        source_sequence_id=f"attempt-seq-{number:03d}",
    )


def _broker(quantity: int = 4, fractions: tuple[int, ...] = (1, 1, 2)) -> PaperBroker:
    return PaperBroker(
        _intent(quantity),
        PaperQuote(bid=Decimal("1.20"), ask=Decimal("1.30")),
        PaperFillPlan(fractions),
    )


def test_lifecycle_clean_fill_path() -> None:
    broker = _broker()
    attempt = _attempt()
    facts = broker.broker_facts(attempt)
    assert [type(fact).__name__ for fact in facts] == [
        "BrokerAcknowledgement",
        "PartialFill",
        "PartialFill",
        "CompleteFill",
        "BrokerReadback",
    ]
    lifecycle = ExecutionLifecycle.start(broker.intent).apply(attempt)
    for fact in facts:
        lifecycle = lifecycle.apply(fact)
    assert lifecycle.state is ExecutionState.FILLED
    assert lifecycle.filled_quantity == 4
    assert lifecycle.reconciliation_reasons == frozenset()
    assert lifecycle.fill_economic_intervals == ((0, 4),)


def test_fill_plan_clips_cover_exactly_with_last_clip_trimmed() -> None:
    assert PaperFillPlan((2,)).clips(5) == (2, 2, 1)  # last clip trimmed
    assert PaperFillPlan((1, 1, 2)).clips(4) == (1, 1, 2)  # exact cover
    assert PaperFillPlan((3,)).clips(2) == (2,)  # single clip trimmed
    assert PaperFillPlan().clips(3) == (1, 1, 1)  # default single-contract clips
    assert PaperFillPlan(()).clips(1) == (1,)  # empty plan means single clip
    assert sum(PaperFillPlan((2, 3)).clips(7)) == 7  # never over- or under-shoots
    with pytest.raises(ValueError, match="positive ints"):
        PaperFillPlan((0, 1))
    with pytest.raises(ValueError, match="positive ints"):
        PaperFillPlan((True,))  # bools are not clip sizes


def test_fills_are_strictly_cumulative_at_the_midpoint_with_scaled_fees() -> None:
    broker = _broker()
    fills = broker.fills(_attempt())
    midpoint = (Decimal("1.20") + Decimal("1.30")) / Decimal(2)
    assert [fill.cumulative_quantity for fill in fills] == [1, 2, 4]  # strictly increasing
    assert [fill.fill_quantity for fill in fills] == [1, 1, 2]
    assert all(fill.unit_price == midpoint for fill in fills)
    assert fills[0].fees == Decimal("0.65")
    assert fills[2].fees == Decimal("1.30")  # fee per contract scales with the clip
    assert isinstance(fills[-1], CompleteFill)
    assert all(isinstance(fill, PartialFill) for fill in fills[:-1])
    assert fills[-1].cumulative_quantity == broker.intent.quantity


def test_retries_acknowledge_the_same_broker_order() -> None:
    broker = _broker()
    first = broker.acknowledge(_attempt(1, send_at=5))
    retry = broker.acknowledge(_attempt(2, send_at=9))
    assert first.broker_order_id == retry.broker_order_id == "paper-order-paper-001"
    for ack, send_at in ((first, 5), (retry, 9)):
        assert ack.broker_acknowledged_at >= T0 + timedelta(seconds=send_at)
        assert ack.locally_received_at >= ack.broker_acknowledged_at
    with pytest.raises(ValueError, match="different intent"):
        broker.acknowledge(
            SubmitAttempt(
                record_id="attempt-999",
                intent_id="other-intent",
                send_attempt_at=T0,
                source="paper-runner",
                source_sequence_id="attempt-seq-999",
            )
        )


def test_paper_broker_is_deterministic() -> None:
    attempt = _attempt()
    first = PaperBroker(_intent(), PaperQuote(Decimal("1.20"), Decimal("1.30"))).broker_facts(
        attempt
    )
    second = PaperBroker(_intent(), PaperQuote(Decimal("1.20"), Decimal("1.30"))).broker_facts(
        attempt
    )
    assert [fact.model_dump(mode="json") for fact in first] == [
        fact.model_dump(mode="json") for fact in second
    ]
    # and byte-identical per record: canonical serialization round-trips
    assert all(
        a.model_dump_json() == b.model_dump_json() for a, b in zip(first, second, strict=True)
    )


def test_timestamps_never_precede_their_cause() -> None:
    broker = _broker()
    attempt = _attempt()
    ack, *rest = broker.broker_facts(attempt)
    fills = tuple(fact for fact in rest if isinstance(fact, (PartialFill, CompleteFill)))
    readback = rest[-1]
    assert isinstance(readback, BrokerReadback)
    # every record is received at or after the submit instant it answers
    for fact in (ack, *fills, readback):
        assert fact.locally_received_at >= attempt.send_attempt_at
    for fill in fills:
        assert fill.exchange_event_at >= ack.broker_acknowledged_at
        assert fill.locally_received_at >= fill.exchange_event_at
    # the terminal snapshot is taken after the LAST fill's exchange event
    assert readback.broker_snapshot_at >= fills[-1].exchange_event_at
    assert readback.broker_snapshot_at > fills[0].exchange_event_at
    assert readback.locally_received_at >= readback.broker_snapshot_at
    # fill exchange events are strictly ordered (spacing > 0)
    assert fills[0].exchange_event_at < fills[1].exchange_event_at < fills[2].exchange_event_at


def test_quote_and_lag_validations() -> None:
    with pytest.raises(ValueError, match="bid cannot exceed ask"):
        PaperQuote(Decimal("1.30"), Decimal("1.20"))
    with pytest.raises(ValueError, match="positive finite Decimal"):
        PaperQuote(Decimal("0"), Decimal("1.20"))
    with pytest.raises(ValueError, match="positive finite Decimal"):
        PaperQuote(1.20, Decimal("1.30"))  # binary floats refused
    with pytest.raises(ValueError, match="non-negative Decimal"):
        PaperQuote(Decimal("1.20"), Decimal("1.30"), fee_per_contract=Decimal("-1"))
    with pytest.raises(ValueError, match="non-negative"):
        PaperLag(broker_offset_seconds=-1)
    with pytest.raises(ValueError, match="non-negative"):
        PaperLag(fill_spacing_seconds=-1)
    with pytest.raises(ValueError, match="non-negative"):
        PaperLag(receipt_offset_seconds=True)  # bools are not second counts
