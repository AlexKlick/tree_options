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


def _attempt(number: int = 1, *, send_at: int = 5, intent_id: str = "paper-001") -> SubmitAttempt:
    return SubmitAttempt(
        record_id=f"attempt-{number:03d}",
        intent_id=intent_id,
        send_attempt_at=T0 + timedelta(seconds=send_at),
        source="paper-runner",
        source_sequence_id=f"attempt-seq-{number:03d}",
    )


def _broker(
    quantity: int = 4,
    fractions: tuple[int, ...] = (1, 1, 2),
    lag: PaperLag | None = None,
    quote: PaperQuote | None = None,
) -> PaperBroker:
    return PaperBroker(
        _intent(quantity),
        quote or PaperQuote(bid=Decimal("1.20"), ask=Decimal("1.30")),
        PaperFillPlan(fractions),
        lag or PaperLag(),
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


def test_fills_are_strictly_cumulative_at_the_executable_price_with_scaled_fees() -> None:
    broker = _broker()
    broker.acknowledge(_attempt())
    fills = broker.fills()
    assert [fill.cumulative_quantity for fill in fills] == [1, 2, 4]  # strictly increasing
    assert [fill.fill_quantity for fill in fills] == [1, 1, 2]
    assert all(fill.unit_price == Decimal("1.25") for fill in fills)  # (1.20+1.30)/2
    assert fills[0].fees == Decimal("0.65")
    assert fills[2].fees == Decimal("1.30")  # fee per contract scales with the clip
    assert isinstance(fills[-1], CompleteFill)
    assert all(isinstance(fill, PartialFill) for fill in fills[:-1])
    assert fills[-1].cumulative_quantity == broker.intent.quantity


def test_retry_answers_with_identical_economics_not_duplicates() -> None:
    """Codex round-1 P1: a retried submit must never duplicate fill economics.

    The retry's acknowledgement is a NEW record for the SAME broker order;
    the fill history is byte-identical (ids anchor to the FIRST submit), so
    applying the retry's sequence after the original is an
    identity-preserving no-op — not FILL_ECONOMIC_OVERLAP.
    """
    broker = _broker(
        lag=PaperLag(broker_offset_seconds=2, receipt_offset_seconds=10, fill_spacing_seconds=3)
    )
    first = broker.broker_facts(_attempt(1, send_at=5))
    retry = broker.broker_facts(_attempt(2, send_at=6))
    # fresh acknowledgement, same broker order, UNCHANGED fills + readback
    assert retry[0].record_id != first[0].record_id
    assert retry[0].broker_order_id == first[0].broker_order_id
    assert [f.model_dump_json() for f in retry[1:]] == [f.model_dump_json() for f in first[1:]]
    lifecycle = ExecutionLifecycle.start(broker.intent).apply(_attempt(1, send_at=5))
    for fact in first:
        lifecycle = lifecycle.apply(fact)
    assert lifecycle.state is ExecutionState.FILLED
    lifecycle = lifecycle.apply(_attempt(2, send_at=6))
    for fact in retry:
        lifecycle = lifecycle.apply(fact)
    assert lifecycle.state is ExecutionState.FILLED
    assert lifecycle.reconciliation_reasons == frozenset()
    assert lifecycle.fill_economic_intervals == ((0, 4),)


def test_fills_require_acknowledgement_and_refuse_foreign_intents() -> None:
    """Codex round-1 P1: broker facts never leak to another intent."""
    broker = _broker()
    with pytest.raises(ValueError, match="acknowledge a submit"):
        broker.fills()
    with pytest.raises(ValueError, match="different intent"):
        broker.acknowledge(_attempt(9, intent_id="other-intent"))
    # the refusal anchored nothing; a legal acknowledgement then opens facts
    with pytest.raises(ValueError, match="acknowledge a submit"):
        broker.fills()
    broker.acknowledge(_attempt())
    fills = broker.fills()
    assert all(fill.intent_id == "paper-001" for fill in fills)


def test_executable_price_is_quantized_within_the_bracket() -> None:
    """Codex round-1 P1: a sub-quantum bracket must still produce a
    schema-representable executable price."""
    subquantum = PaperQuote(Decimal("1.00000001"), Decimal("1.00000002"))
    assert subquantum.executable_price == Decimal("1.00000002")  # half-up, inside bracket
    normal = PaperQuote(Decimal("1.20"), Decimal("1.30"))
    assert normal.executable_price == Decimal("1.25")
    wide = PaperQuote(Decimal("1.00000000"), Decimal("1.00000003"))
    assert wide.executable_price == Decimal("1.00000002")  # 1.5e-8 -> half-up
    # and the fill schema accepts it end to end
    broker = PaperBroker(_intent(), subquantum)
    broker.acknowledge(_attempt())
    assert all(fill.unit_price == subquantum.executable_price for fill in broker.fills())


def test_quote_and_lag_validations() -> None:
    with pytest.raises(ValueError, match="bid cannot exceed ask"):
        PaperQuote(Decimal("1.30"), Decimal("1.20"))
    with pytest.raises(ValueError, match="positive finite Decimal"):
        PaperQuote(Decimal("0"), Decimal("1.20"))
    with pytest.raises(ValueError, match="positive finite Decimal"):
        PaperQuote(1.20, Decimal("1.30"))  # binary floats refused
    with pytest.raises(ValueError, match="non-negative finite Decimal"):
        PaperQuote(Decimal("1.20"), Decimal("1.30"), fee_per_contract=Decimal("-1"))
    with pytest.raises(ValueError, match="non-negative finite Decimal"):
        PaperQuote(Decimal("1.20"), Decimal("1.30"), fee_per_contract=Decimal("Infinity"))
    with pytest.raises(ValueError, match="non-negative int"):
        PaperLag(broker_offset_seconds=-1)
    with pytest.raises(ValueError, match="non-negative int"):
        PaperLag(receipt_offset_seconds=True)  # bools are not second counts
    with pytest.raises(ValueError, match="strict event order"):
        PaperLag(fill_spacing_seconds=0)  # exchange events must be strictly ordered


def test_nondefault_lag_offsets_are_exact() -> None:
    """Codex round-1 P2: the declared offsets are pinned to exact instants
    (broker 2s, receipt 3s, spacing 5s) — not merely 'some offsets'."""
    broker = _broker(
        lag=PaperLag(broker_offset_seconds=2, receipt_offset_seconds=3, fill_spacing_seconds=5)
    )
    ack, *rest = broker.broker_facts(_attempt(send_at=10))
    # send T0+10 -> ack event T0+12, received T0+15
    assert ack.broker_acknowledged_at == T0 + timedelta(seconds=12)
    assert ack.locally_received_at == T0 + timedelta(seconds=15)
    fills = rest[:3]
    readback = rest[-1]
    assert isinstance(readback, BrokerReadback)
    # fills spaced 5s from T0+12, each received 3s after its exchange event
    assert [f.exchange_event_at - T0 for f in fills] == [timedelta(seconds=s) for s in (12, 17, 22)]
    assert all(
        fill.locally_received_at - fill.exchange_event_at == timedelta(seconds=3) for fill in fills
    )
    # terminal snapshot one spacing past the LAST fill, received 3s later
    assert readback.broker_snapshot_at == T0 + timedelta(seconds=27)
    assert readback.locally_received_at == T0 + timedelta(seconds=30)


def test_paper_broker_is_deterministic() -> None:
    attempt = _attempt()
    first = PaperBroker(_intent(), PaperQuote(Decimal("1.20"), Decimal("1.30")))
    second = PaperBroker(_intent(), PaperQuote(Decimal("1.20"), Decimal("1.30")))
    assert [fact.model_dump(mode="json") for fact in first.broker_facts(attempt)] == [
        fact.model_dump(mode="json") for fact in second.broker_facts(attempt)
    ]
    # same broker, repeated requests: byte-identical economics
    again = first.broker_facts(attempt)
    assert [fact.model_dump_json() for fact in first.broker_facts(attempt)] == [
        fact.model_dump_json() for fact in again
    ]
