"""Workstream C: American exercise + expiry settlement + ledger extension
(M3 plan §3.C)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tree_options.data.bars import BarRecord
from tree_options.ledger.book import LedgerBook, LedgerViolation
from tree_options.options import (
    ExerciseElectionInputs,
    SettlementMintError,
    intrinsic_value,
    mint_settlement,
    should_elect_exercise,
)
from tree_options.schemas.common import FEE_TICK
from tree_options.schemas.ledger import EntryKind
from tree_options.schemas.options import DeliverableSpec, OptionContract

D = Decimal


def contract(
    *,
    call_put: str = "C",
    strike: str = "100",
    expiration: date = date(2019, 3, 15),
    style: str = "american",
    listing_start: date = date(2019, 1, 7),
) -> OptionContract:
    return OptionContract(
        contract_id=f"OPT-SYN-0001-{expiration:%y%m%d}-{call_put}-{int(D(strike) * 100):08d}",
        option_root="SYN-0001",
        underlying_security_id="SYN-0001",
        expiration=expiration,
        strike=D(strike),
        call_put=call_put,  # type: ignore[arg-type]
        multiplier=100,
        exercise_style=style,  # type: ignore[arg-type]
        listing_start=listing_start,
        listing_end=expiration,
        deliverable=DeliverableSpec(shares_per_contract=D(100)),
        standard_contract_flag=True,
    )


def reference_bar(
    *,
    close: str,
    session: date,
    security_id: str = "SYN-0001",
    ref_id: str = "RAW-1",
) -> BarRecord:
    return BarRecord(
        security_id=security_id,
        session=session,
        open=D(close),
        high=D(close),
        low=D(close),
        close=D(close),
        volume=1000,
        source="synthetic/v1",
        source_record_id=ref_id,
        source_row_hash="0" * 64,
        snapshot_id="x",
        available_at=datetime(session.year, session.month, session.day, 23, 0, tzinfo=UTC),
    )


def fill(
    *,
    fill_id: str,
    contract_id: str,
    quantity: int = 1,
    price: str = "2.50",
    at: datetime | None = None,
    session: date = date(2019, 1, 7),
    side: str = "buy",
) -> object:
    from tree_options.schemas.trading import Fill

    return Fill(
        fill_id=fill_id,
        order_id=f"ORD-{fill_id}",
        contract_id=contract_id,
        side=side,  # type: ignore[arg-type]
        quantity=quantity,
        price=D(price),
        multiplier=100,
        deliverable_shares_per_contract=D(100),
        fees=D("0.65"),
        execution_at=at or datetime(2019, 1, 7, 15, 0, tzinfo=UTC),
        execution_session=session,
    )


def settle(
    *,
    contract: OptionContract,
    quantity: int = 1,
    reference_close: str,
    session: date,
    kind: str = "expiry",
    settlement_id: str = "STL-1",
) -> object:
    return mint_settlement(
        contract=contract,
        settlement_id=settlement_id,
        kind=kind,  # type: ignore[arg-type]
        quantity=quantity,
        session=session,
        reference_bar=reference_bar(close=reference_close, session=session),
    )


# ---- intrinsic + minting --------------------------------------------------


def test_intrinsic_for_calls_and_puts() -> None:
    assert intrinsic_value("C", D("100"), D("110")) == D("10")
    assert intrinsic_value("C", D("100"), D("90")) == D("0")
    assert intrinsic_value("P", D("100"), D("90")) == D("10")
    assert intrinsic_value("P", D("100"), D("110")) == D("0")


def test_expiry_settlement_cash_is_exact_intrinsic() -> None:
    s = settle(contract=contract(), reference_close="110", session=date(2019, 3, 15))
    assert s.cash == (D("10") * 1 * 100).quantize(FEE_TICK)
    assert s.kind == "expiry"
    assert s.ref_id == "RAW-1"


def test_otm_expiry_settles_to_zero_cash() -> None:
    s = settle(contract=contract(call_put="P"), reference_close="110", session=date(2019, 3, 15))
    assert s.cash == D("0.00")


def test_early_exercise_of_european_refused() -> None:
    with pytest.raises(SettlementMintError, match="no early exercise right"):
        settle(
            contract=contract(style="european"),
            reference_close="110",
            session=date(2019, 3, 8),
            kind="early_exercise",
        )


def test_early_exercise_on_expiration_session_refused() -> None:
    with pytest.raises(SettlementMintError, match="must precede"):
        settle(
            contract=contract(),
            reference_close="110",
            session=date(2019, 3, 15),
            kind="early_exercise",
        )


def test_expiry_kind_must_land_on_expiration() -> None:
    with pytest.raises(SettlementMintError, match="must land on"):
        settle(contract=contract(), reference_close="110", session=date(2019, 3, 14))


def test_settlement_before_listing_refused() -> None:
    with pytest.raises(SettlementMintError, match="before listing_start"):
        settle(contract=contract(), reference_close="110", session=date(2018, 12, 3))


def test_model_rejects_inconsistent_cash() -> None:
    from tree_options.options.settlement import ExerciseSettlement

    good = settle(contract=contract(), reference_close="110", session=date(2019, 3, 15))
    with pytest.raises(ValueError, match="intrinsic arithmetic"):
        ExerciseSettlement(**{**good.model_dump(), "cash": D("999.00")})


# ---- ledger application ----------------------------------------------------


def book_with_position() -> tuple[LedgerBook, OptionContract]:
    c = contract()
    book = LedgerBook(D("100000.00"))
    book.apply(fill(fill_id="F1", contract_id=c.contract_id, quantity=2, price="2.50"))
    return book, c


def test_settlement_closes_lots_and_conserves() -> None:
    book, c = book_with_position()
    book.apply_settlement(
        settle(
            contract=c,
            quantity=2,
            reference_close="110",
            session=c.expiration,
            settlement_id="STL-1",
        )
    )
    assert book.quantity(c.contract_id) == 0
    # cash: initial - 2*2.50*100 - 0.65 fees + 10*2*100 settlement
    assert book.cash == D("100000.00") - D("500.00") - D("0.65") + D("2000.00")
    book.assert_conservation()
    entries = book.entries
    assert entries[-1].kind == "exercise_settlement"
    assert entries[-1].amount == D("2000.00")
    assert entries[-1].ref_id == "STL-1"
    kinds = {e.kind for e in entries}
    assert kinds == {"fill_notional", "fee", "exercise_settlement"}


def test_partial_settlement_then_sell() -> None:
    book, c = book_with_position()
    book.apply_settlement(
        settle(
            contract=c,
            quantity=1,
            reference_close="110",
            session=date(2019, 3, 14),
            kind="early_exercise",
            settlement_id="STL-1",
        )
    )
    assert book.quantity(c.contract_id) == 1
    from tree_options.schemas.trading import Fill

    book.apply(
        Fill(
            fill_id="F2",
            order_id="ORD-F2",
            contract_id=c.contract_id,
            side="sell",
            quantity=1,
            price=D("0.05"),
            multiplier=100,
            deliverable_shares_per_contract=D(100),
            fees=D("0.65"),
            execution_at=datetime(2019, 3, 15, 15, 0, tzinfo=UTC),
            execution_session=date(2019, 3, 15),
        )
    )
    book.assert_conservation()
    assert book.quantity(c.contract_id) == 0


def test_duplicate_settlement_refused() -> None:
    book, c = book_with_position()
    s = settle(contract=c, quantity=1, reference_close="110", session=c.expiration)
    book.apply_settlement(s)
    with pytest.raises(LedgerViolation, match="DUPLICATE_SETTLEMENT"):
        book.apply_settlement(s)


def test_settlement_over_position_refused() -> None:
    book, c = book_with_position()
    with pytest.raises(LedgerViolation, match="POSITION_UNDERFLOW"):
        book.apply_settlement(
            settle(contract=c, quantity=3, reference_close="110", session=c.expiration)
        )


def test_settlement_without_position_refused() -> None:
    book = LedgerBook(D("1000.00"))
    c = contract()
    with pytest.raises(LedgerViolation, match="POSITION_UNDERFLOW"):
        book.apply_settlement(
            settle(contract=c, quantity=1, reference_close="110", session=c.expiration)
        )


def test_out_of_order_settlement_refused() -> None:
    book, c = book_with_position()
    book.apply_settlement(
        settle(
            contract=c,
            quantity=1,
            reference_close="110",
            session=date(2019, 3, 14),
            kind="early_exercise",
            settlement_id="STL-1",
        )
    )
    # a settlement stamped BEFORE the last applied event (23:00 UTC on 3/14)
    stale = settle(
        contract=contract(expiration=date(2019, 4, 19), strike="100"),
        quantity=1,
        reference_close="110",
        session=date(2019, 3, 13),
        kind="early_exercise",
        settlement_id="STL-2",
    )
    # give the stale one an earlier ts by minting against an earlier reference
    with pytest.raises(LedgerViolation, match="OUT_OF_ORDER"):
        stale2 = stale.model_copy(update={"ts": datetime(2019, 3, 13, 23, 0, tzinfo=UTC)})
        book.apply_settlement(stale2)  # type: ignore[arg-type]


def test_settlement_cannot_validate_broken_lots() -> None:
    """Oracle independence: corrupting the realized accumulator (not the
    event streams) must still fail conservation after settlements."""
    book, c = book_with_position()
    book.apply_settlement(
        settle(contract=c, quantity=1, reference_close="110", session=c.expiration)
    )
    book._realized[c.contract_id] += D("1.00")  # corrupt outside the API
    with pytest.raises(LedgerViolation):
        book.assert_conservation()


# ---- election policy --------------------------------------------------------


def inputs(**kw: object) -> ExerciseElectionInputs:
    params: dict[str, object] = {
        "exercise_style": "american",
        "call_put": "C",
        "expiration_seen": True,
        "mid_premium": D("1.50"),
        "bid": D("1.40"),
        "intrinsic": D("1.00"),
        "pending_dividend_per_share": None,
    }
    params.update(kw)
    return ExerciseElectionInputs(**params)  # type: ignore[arg-type]


def test_dividend_branch_elects_for_calls() -> None:
    assert should_elect_exercise(inputs(pending_dividend_per_share=D("0.60")))
    # dividend below the remaining time value: no election
    assert not should_elect_exercise(inputs(pending_dividend_per_share=D("0.40")))


def test_dividend_branch_ignores_puts() -> None:
    assert not should_elect_exercise(inputs(call_put="P", pending_dividend_per_share=D("0.60")))


def test_market_underpricing_branch_elects() -> None:
    # intrinsic 1.00, 98% bound 0.98 — a 0.90 bid means selling loses
    assert should_elect_exercise(inputs(bid=D("0.90")))
    assert not should_elect_exercise(inputs(bid=D("0.99")))
    # zero-bid deep ITM: the classic exercise case
    assert should_elect_exercise(inputs(bid=D("0.00")))
    # no intrinsic: never elect on this branch
    assert not should_elect_exercise(inputs(intrinsic=D("0"), bid=D("0")))


def test_european_never_elects_early() -> None:
    assert not should_elect_exercise(
        inputs(exercise_style="european", bid=D("0.00"), pending_dividend_per_share=D("5.00"))
    )


def test_election_uses_only_file_prev_facts() -> None:
    """The inputs are a closed dataclass: file(t) facts (session-t close,
    session-t quotes) are structurally absent — a file(t)-only signal
    cannot reach the policy. Assert the shape stays file(t-1)-scoped."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(ExerciseElectionInputs)}
    assert field_names == {
        "exercise_style",
        "call_put",
        "expiration_seen",
        "mid_premium",
        "bid",
        "intrinsic",
        "pending_dividend_per_share",
    }


# ---- randomized conservation property ----------------------------------------


@settings(max_examples=50, deadline=None)
@given(
    n_lots=st.lists(
        st.tuples(st.integers(1, 5), st.decimals(min_value=D("0.01"), max_value=D("10"), places=2)),
        min_size=1,
        max_size=6,
    ),
    settle_after=st.integers(0, 5),
    closes=st.lists(
        st.decimals(min_value=D("1.00"), max_value=D("300"), places=2), min_size=1, max_size=4
    ),
)
def test_randomized_fill_settlement_streams_conserve(n_lots, settle_after, closes) -> None:
    """Random buy streams with interleaved settlements (and a final
    everything-flat close) conserve to the penny under the extended
    oracle."""
    c = contract()
    book = LedgerBook(D("1000000.00"))
    seq = 0
    day = date(2019, 1, 7)
    remaining = 0
    for qty, price in n_lots:
        seq += 1
        day = date(2019, 1, 7 + seq)
        book.apply(
            fill(
                fill_id=f"F{seq}",
                contract_id=c.contract_id,
                quantity=qty,
                price=str(price),
                at=datetime(2019, 1, 7 + seq, 15, 0, tzinfo=UTC),
                session=day,
            )
        )
        remaining += qty
        if settle_after > 0 and seq % settle_after == 0 and remaining > 0:
            take = min(remaining, 1 + seq % 2)
            close = closes[seq % len(closes)]
            book.apply_settlement(
                mint_settlement(
                    contract=c,
                    settlement_id=f"STL{seq}",
                    kind="early_exercise",
                    quantity=take,
                    session=day,
                    reference_bar=reference_bar(close=str(close), session=day, ref_id=f"RAW-{seq}"),
                )
            )
            remaining -= take
    if remaining > 0:  # flatten via expiry settlement
        book.apply_settlement(
            mint_settlement(
                contract=c,
                settlement_id="STL-FINAL",
                kind="expiry",
                quantity=remaining,
                session=c.expiration,
                reference_bar=reference_bar(close=str(closes[0]), session=c.expiration),
            )
        )
    book.assert_conservation()
    assert book.quantity(c.contract_id) == 0
    assert book.cash == book.initial_cash + sum(book._realized.values()) - book.total_fees
    kinds: set[EntryKind] = {e.kind for e in book.entries}
    assert "exercise_settlement" in kinds


# ---- review r1 remediation (P1-4/5/6/7) ----------------------------------


def test_tied_timestamp_buys_replay_in_application_order() -> None:
    """P1-4: two buys at the SAME instant applied Z-then-A must conserve —
    the oracle replays the accepted sequence, never a re-derived ordering
    that would walk the lots differently."""
    c = contract()
    book = LedgerBook(D("10000.00"))
    ts = datetime(2019, 1, 7, 15, 0, tzinfo=UTC)
    book.apply(fill(fill_id="Z-BUY", contract_id=c.contract_id, quantity=1, price="1.00", at=ts))
    book.apply(fill(fill_id="A-BUY", contract_id=c.contract_id, quantity=1, price="2.00", at=ts))
    book.apply_settlement(
        settle(contract=c, quantity=1, reference_close="110", session=c.expiration)
    )
    # book FIFO removed the Z lot ($1): realized = 1000 - 100 = 900
    assert book.realized_pnl(c.contract_id) == D("900.00")
    book.assert_conservation()  # must not raise despite tied timestamps


def test_rejected_settlement_does_not_poison_the_timeline() -> None:
    """P1-5: an over-position settlement is rejected WITHOUT advancing the
    merged timeline — a later valid settlement at an earlier instant still
    applies."""
    book, c = book_with_position()
    with pytest.raises(LedgerViolation, match="POSITION_UNDERFLOW"):
        book.apply_settlement(
            settle(contract=c, quantity=5, reference_close="110", session=c.expiration)
        )
    # this March 14 early exercise is EARLIER than the rejected March 15
    # expiry — it must still be accepted
    book.apply_settlement(
        settle(
            contract=c,
            quantity=1,
            reference_close="110",
            session=date(2019, 3, 14),
            kind="early_exercise",
            settlement_id="STL-OK",
        )
    )
    book.assert_conservation()


def test_mint_binds_reference_bar_session_and_underlying() -> None:
    """P1-6: the reference is an authoritative BarRecord — a bar from the
    wrong session or the wrong underlying is refused."""
    c = contract()
    with pytest.raises(SettlementMintError, match="not the settlement session"):
        mint_settlement(
            contract=c,
            settlement_id="STL-X",
            kind="expiry",
            quantity=1,
            session=c.expiration,
            reference_bar=reference_bar(close="110", session=date(2019, 3, 14)),
        )
    with pytest.raises(SettlementMintError, match="not underlying"):
        mint_settlement(
            contract=c,
            settlement_id="STL-X",
            kind="expiry",
            quantity=1,
            session=c.expiration,
            reference_bar=reference_bar(close="110", session=c.expiration, security_id="SYN-0099"),
        )


def test_oracle_recomputes_settlement_cash_independently() -> None:
    """P1-7: a model_copy-tampered settlement cash (bypassing construction
    validation) fails conservation — the oracle derives intrinsic cash
    itself instead of trusting the recorded field."""
    book, c = book_with_position()
    good = settle(contract=c, quantity=1, reference_close="110", session=c.expiration)
    tampered = good.model_copy(update={"cash": D("9999.00")})  # type: ignore[attr-defined]
    book.apply_settlement(tampered)  # type: ignore[arg-type]
    with pytest.raises(LedgerViolation, match="SETTLEMENT_CASH_MISMATCH"):
        book.assert_conservation()


def test_unrepresentable_notional_fill_is_atomic_rejection() -> None:
    """Review r2 P1: a SCHEMA-VALID sell whose notional exceeds Money's
    18-digit bound is rejected at the STAGED ledger-entry construction —
    BEFORE the lot walk, cash, realized, or applied-IDs change. The book
    must remain exactly as it was."""
    from pydantic import ValidationError

    from tree_options.schemas.trading import Fill

    c = contract()
    book = LedgerBook(D("1000000.00"))
    book.apply(fill(fill_id="F1", contract_id=c.contract_id, quantity=1_000_000, price="0.01"))
    cash_before = book.cash
    qty_before = book.quantity(c.contract_id)
    realized_before = book.realized_pnl(c.contract_id)
    entries_before = len(book.entries)
    huge = Fill(
        fill_id="F2",
        order_id="ORD-F2",
        contract_id=c.contract_id,
        side="sell",
        quantity=1_000_000,
        price=D("9999999999.99"),  # 12 digits, 2dp: a VALID Price
        multiplier=100,
        deliverable_shares_per_contract=D(100),
        fees=D("0.65"),
        execution_at=datetime(2019, 1, 8, 15, 0, tzinfo=UTC),
        execution_session=date(2019, 1, 8),
    )
    with pytest.raises(ValidationError):  # the staged entry, not the book
        book.apply(huge)
    # the book is EXACTLY as before the rejected sell
    assert book.cash == cash_before
    assert book.quantity(c.contract_id) == qty_before
    assert book.realized_pnl(c.contract_id) == realized_before
    assert len(book.entries) == entries_before
    assert "F2" not in book._applied_fill_ids
    book.assert_conservation()  # still clean
    # and the sequence is unpoisoned: a later valid fill still applies
    book.apply(
        fill(
            fill_id="F3",
            contract_id=c.contract_id,
            quantity=1,
            price="0.01",
            at=datetime(2019, 1, 9, 15, 0, tzinfo=UTC),
            session=date(2019, 1, 9),
        )
    )
    book.assert_conservation()
