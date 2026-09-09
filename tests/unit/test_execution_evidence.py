"""M6 evidence gate: verify the lifecycle, then admit or refuse it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tree_options.execution import (
    BrokerAcknowledgement,
    BrokerReadback,
    BrokerReadbackStatus,
    CompleteFill,
    EvidenceBlockerKind,
    EvidenceVerdict,
    ExecutionLifecycle,
    ExecutionState,
    OrderIntent,
    PaperBroker,
    PaperFillPlan,
    PaperLag,
    PaperQuote,
    PartialFill,
    SubmitAttempt,
    assess_evidence,
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


def _attempt(**over: object) -> SubmitAttempt:
    fields: dict[str, object] = {
        "record_id": "attempt-001",
        "intent_id": "intent-001",
        "send_attempt_at": _at(1),
        "source": "executor",
        "source_sequence_id": "attempt-seq-001",
    }
    fields.update(over)
    return SubmitAttempt(**fields)


def _ack(**over: object) -> BrokerAcknowledgement:
    fields: dict[str, object] = {
        "record_id": "ack-001",
        "intent_id": "intent-001",
        "broker_order_id": "paper-order-001",
        "broker_acknowledged_at": _at(2),
        "locally_received_at": _at(3),
        "source": "synthetic-paper-adapter",
        "source_sequence_id": "source-ack-001",
        "broker_sequence_id": "broker-ack-001",
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
        "fill_quantity": 2,
        "cumulative_quantity": 3,
        "unit_price": Decimal("1.22"),
        "fees": Decimal("2.60"),
        "exchange_event_at": _at(9),
        "locally_received_at": _at(10),
        "source": "synthetic-paper-adapter",
        "source_sequence_id": "source-fill-003",
        "broker_sequence_id": "broker-fill-003",
    }
    fields.update(over)
    return CompleteFill(**fields)


def _filled_lifecycle() -> ExecutionLifecycle:
    """submit -> ack -> 1@1.20 -> 2@1.22: FILLED, zero reasons.

    Two fills only — cumulative arithmetic demands each fill's interval
    (cum - qty, cum) tile from the prior cumulative, so a qty-2 closer
    follows a single qty-1 partial: (0,1) + (1,3)."""
    return (
        ExecutionLifecycle.start(_intent())
        .apply(_attempt())
        .apply(_ack())
        .apply(_partial(1))
        .apply(_complete())
    )


def _canceled_readback(number: int = 1, cumulative: int = 1) -> BrokerReadback:
    return BrokerReadback(
        record_id=f"readback-{number:03d}",
        intent_id="intent-001",
        status=BrokerReadbackStatus.CANCELED,
        broker_order_id="paper-order-001",
        total_quantity=3,
        cumulative_quantity=cumulative,
        broker_snapshot_at=_at(12 + number),
        locally_received_at=_at(13 + number),
        source="synthetic-paper-adapter",
        source_sequence_id=f"readback-seq-{number:03d}",
        broker_sequence_id=f"broker-readback-{number:03d}",
    )


def test_clean_filled_lifecycle_is_admissible_with_exact_economics() -> None:
    receipt = assess_evidence(_filled_lifecycle())
    assert receipt.verdict is EvidenceVerdict.ADMISSIBLE
    assert receipt.is_admissible
    assert receipt.blockers == ()
    assert receipt.economics is not None
    # per-fill economics, summed exactly: 1x1.20 + 2x1.22
    assert receipt.economics.gross_amount == Decimal("3.64")
    assert receipt.economics.total_fees == Decimal("3.25")
    assert receipt.economics.net_cash_flow == Decimal("-6.89")  # BUY: -(gross + fees)
    assert receipt.economics.filled_quantity == 3
    assert receipt.economically_covered_quantity == 3
    assert receipt.basis == (
        "terminal FILLED with exact fill economics ((0, 3),) and zero retained reconciliation reasons"
    )


def test_paper_broker_sequence_is_admissible_end_to_end() -> None:
    intent = OrderIntent(
        intent_id="paper-001",
        contract_id="O:XYZ260918C00100000",
        side="BUY",
        position_effect="OPEN_LONG",
        quantity=4,
        order_type="LIMIT",
        limit_price=Decimal("1.25"),
        execution_style="single",
        package_id=None,
        intent_created_at=T0,
        source="paper-runner",
        source_sequence_id="intent-seq-001",
    )
    broker = PaperBroker(
        intent,
        PaperQuote(bid=Decimal("1.20"), ask=Decimal("1.30")),
        PaperFillPlan((1, 1, 2)),
        PaperLag(),
    )
    lifecycle = ExecutionLifecycle.start(intent).apply(
        SubmitAttempt(
            record_id="attempt-001",
            intent_id="paper-001",
            send_attempt_at=_at(5),
            source="paper-runner",
            source_sequence_id="attempt-seq-001",
        )
    )
    for fact in broker.broker_facts(
        SubmitAttempt(
            record_id="attempt-001",
            intent_id="paper-001",
            send_attempt_at=_at(5),
            source="paper-runner",
            source_sequence_id="attempt-seq-001",
        )
    ):
        lifecycle = lifecycle.apply(fact)
    receipt = assess_evidence(lifecycle)
    assert receipt.verdict is EvidenceVerdict.ADMISSIBLE
    assert receipt.economics is not None
    assert receipt.economics.gross_amount == Decimal("5.00")  # 4 x 1.25
    assert receipt.economics.total_fees == Decimal("2.60")  # 0.65 x 4
    assert receipt.economics.net_cash_flow == Decimal("-7.60")


def test_non_terminal_states_are_refused_without_economics() -> None:
    submitting = ExecutionLifecycle.start(_intent()).apply(_attempt())
    receipt = assess_evidence(submitting)
    assert receipt.verdict is EvidenceVerdict.REFUSED
    assert receipt.economics is None
    assert receipt.blockers[0].kind is EvidenceBlockerKind.NON_TERMINAL_STATE
    assert receipt.blockers[0].detail == "state SUBMITTING is not terminal"


def test_retained_reasons_block_admission_individually() -> None:
    # overlapping fills: FILL_ECONOMIC_OVERLAP retained
    overlapped = (
        ExecutionLifecycle.start(_intent())
        .apply(_attempt())
        .apply(_ack())
        .apply(_partial(1))
        .apply(_partial(2, fill_quantity=2, cumulative_quantity=2))
    )
    receipt = assess_evidence(overlapped)
    assert receipt.verdict is EvidenceVerdict.REFUSED
    assert receipt.economics is None
    retained = [
        b for b in receipt.blockers if b.kind is EvidenceBlockerKind.RECONCILIATION_RETAINED
    ]
    assert retained, "every retained reason must appear as its own blocker"
    assert any(b.detail.startswith("ECONOMIC: FILL_ECONOMIC_OVERLAP — ") for b in retained)
    # this fixture DID retain a submit, so MISSING_SUBMIT must not fire here
    assert not any("MISSING_SUBMIT" in b.detail for b in retained)


def test_filled_claim_with_partial_economics_is_refused() -> None:
    """A lifecycle CLAIMING FILLED with only (0, 1) covered of 3 is refused —
    the gate re-checks completeness instead of trusting the state."""
    records = (
        ExecutionLifecycle.start(_intent())
        .apply(_attempt())
        .apply(_ack())
        .apply(_partial(1))
        .records
    )
    claimed = ExecutionLifecycle(
        intent=_intent(),
        state=ExecutionState.FILLED,
        broker_state=ExecutionState.FILLED,
        records=records,
    )
    receipt = assess_evidence(claimed)
    assert receipt.verdict is EvidenceVerdict.REFUSED
    assert receipt.economics is None
    assert any(b.kind is EvidenceBlockerKind.INCOMPLETE_FILL_ECONOMICS for b in receipt.blockers)


def test_derivation_drift_between_records_and_claims_is_refused() -> None:
    """The claimed intervals must match what the retained records re-derive."""
    records = _filled_lifecycle().records
    drifted = ExecutionLifecycle(
        intent=_intent(),
        state=ExecutionState.FILLED,
        broker_state=ExecutionState.FILLED,
        records=records,
        fill_economic_intervals=((0, 2),),
    )
    receipt = assess_evidence(drifted)
    assert receipt.verdict is EvidenceVerdict.REFUSED
    assert any(b.kind is EvidenceBlockerKind.DERIVATION_MISMATCH for b in receipt.blockers)
    # an EQUAL-SUM drift (the unmerged adjacent form) can only be caught by
    # comparing the interval SHAPE — the covered-quantity check stays silent
    unmerged = ExecutionLifecycle(
        intent=_intent(),
        state=ExecutionState.FILLED,
        broker_state=ExecutionState.FILLED,
        records=records,
        fill_economic_intervals=((0, 1), (1, 3)),
    )
    assert unmerged.economically_covered_quantity == 3  # same sum, wrong shape
    receipt = assess_evidence(unmerged)
    assert receipt.verdict is EvidenceVerdict.REFUSED
    assert any(b.kind is EvidenceBlockerKind.DERIVATION_MISMATCH for b in receipt.blockers)


def test_canceled_with_partial_fills_hanging_from_zero_is_admissible() -> None:
    lifecycle = (
        ExecutionLifecycle.start(_intent())
        .apply(_attempt())
        .apply(_ack())
        .apply(_partial(1))
        .apply(_canceled_readback())
    )
    receipt = assess_evidence(lifecycle)
    assert receipt.state is ExecutionState.CANCELED
    assert receipt.verdict is EvidenceVerdict.ADMISSIBLE
    assert receipt.economics is not None
    assert receipt.economics.filled_quantity == 1
    assert receipt.economics.gross_amount == Decimal("1.20")


def test_canceled_economics_not_hanging_from_zero_are_refused() -> None:
    records = (
        ExecutionLifecycle.start(_intent())
        .apply(_attempt())
        .apply(_ack())
        .apply(_partial(2, fill_quantity=1, cumulative_quantity=2))
        .records
    )
    canceled = ExecutionLifecycle(
        intent=_intent(),
        state=ExecutionState.CANCELED,
        broker_state=ExecutionState.CANCELED,
        records=records,
    )
    receipt = assess_evidence(canceled)
    assert receipt.verdict is EvidenceVerdict.REFUSED
    assert any(b.kind is EvidenceBlockerKind.INCOMPLETE_FILL_ECONOMICS for b in receipt.blockers)


def test_sell_side_net_cash_flow_is_proceeds_net_of_fees() -> None:
    sell = _intent(side="SELL", position_effect="CLOSE_LONG")
    lifecycle = (
        ExecutionLifecycle.start(sell)
        .apply(_attempt())
        .apply(_ack())
        .apply(_partial(1))
        .apply(_complete())
    )
    assert lifecycle.state is ExecutionState.FILLED
    receipt = assess_evidence(lifecycle)
    assert receipt.verdict is EvidenceVerdict.ADMISSIBLE
    assert receipt.economics is not None
    # SELL: +(gross - fees) — money in
    assert receipt.economics.net_cash_flow == Decimal("0.39")  # 3.64 - 3.25
    assert receipt.economics.gross_amount == Decimal("3.64")


def test_receipts_are_deterministic() -> None:
    lifecycle = _filled_lifecycle()
    assert assess_evidence(lifecycle) == assess_evidence(lifecycle)
    refused = assess_evidence(ExecutionLifecycle.start(_intent()))
    assert refused == assess_evidence(ExecutionLifecycle.start(_intent()))
    assert refused.blockers[0].detail == "state CREATED is not terminal"
