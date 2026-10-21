"""Pure parts of the owner's entry flows (``trex.desk_entries``).

The flows themselves run end to end in tests/soak/test_wave2_end_to_end.py;
these pin the session count, the pending-entry reservation and the equity
draft against hand-computed oracles from the frozen fixture effect (2 SPY at
671.50, stop 665.00: 20% gap reserve 268.60 > stop loss 13.00).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tests.unit.trex_fakes import contract_fixture, equity_effect, ledger_event
from tree_options.desk.enter_supervised import complete_draft
from tree_options.trex.contracts import (
    EquityDeskSpecV2,
    Quote,
    SupervisedEffect,
    desk_spec_v2_for,
    equity_max_loss_cents,
)
from tree_options.trex.desk_entries import (
    entries_this_session,
    equity_draft,
    equity_loss_cents,
    pending_reservation,
)
from tree_options.trex.desk_spec import DeskSpec
from tree_options.trex.ledger import fold_ledger
from tree_options.trex.state import BookState, Status
from tree_options.trex.supervised import SupervisedPaths

SESSION = date(2026, 10, 22)
AT = "2026-10-22T14:00:00+00:00"


def _terminal(sup: SupervisedPaths, intent: str, outcome: str, at: str = AT) -> None:
    path = sup.terminal(intent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"intent_id": intent, "outcome": outcome, "at": at}))


def test_entries_this_session_counts_sends_of_both_kinds(tmp_path: Path) -> None:
    sup = SupervisedPaths(tmp_path)
    assert entries_this_session(sup, SESSION) == 0
    _terminal(sup, "canary-1", "acknowledged")
    _terminal(sup, "eq-1", "uncertain")
    _terminal(sup, "eq-2", "rejected")  # the broker refused it: no entry
    _terminal(sup, "eq-3", "acknowledged", at="2026-10-21T19:00:00+00:00")  # yesterday
    sending = sup.sending("eq-4")
    sending.write_text(json.dumps({"claimed_at": "2026-10-22T09:45:00-04:00"}))
    _terminal(sup, "eq-5", "uncertain")
    sup.reconciled("eq-5").write_text(json.dumps({"verdict": "confirmed_not_submitted"}))
    assert entries_this_session(sup, SESSION) == 3  # canary-1, eq-1, eq-4


def test_an_unreadable_outbox_count_is_unknown(tmp_path: Path) -> None:
    sup = SupervisedPaths(tmp_path)
    _terminal(sup, "eq-1", "acknowledged")
    sup.terminal("eq-2").write_text("{not json")
    assert entries_this_session(sup, SESSION) is None


def test_equity_loss_cents_is_the_contract_arithmetic() -> None:
    effect = equity_effect()
    assert equity_loss_cents(2, 67150, 66500) == equity_max_loss_cents(effect) == 26860
    assert equity_loss_cents(1, 10000, 5000) == 5000, "a deep stop beats the gap reserve"


def _book(**states: Status) -> BookState:
    book = BookState(list(states))
    for sid, status in states.items():
        book.structures[sid].status = status
    return book


def test_pending_reservation_holds_the_unfilled_part_of_working_entries() -> None:
    working = equity_effect(intent_id="eq-a")
    planned = equity_effect(intent_id="eq-b", quantity=1)
    opened = equity_effect(intent_id="eq-c")
    now = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
    equities: dict[str, EquityDeskSpecV2] = {}
    for effect in (working, planned, opened):
        spec = desk_spec_v2_for(effect, now)
        assert isinstance(spec, EquityDeskSpecV2)
        equities[effect.intent_id] = spec
    book = _book(**{"eq-a": Status.ENTER_WORKING, "eq-b": Status.PLANNED, "eq-c": Status.OPEN})
    full = pending_reservation(book, {}, equities, None)
    assert full.intents == ("eq-a", "eq-b"), "an open position is in the broker's snapshot"
    assert full.loss_cents == 26860 + equity_loss_cents(1, 67150, 66500)
    # the ledger booked one share of eq-a: only the other share stays reserved
    fill = ledger_event(
        quantity_delta=1,
        cash_delta_micros=-671_500_000,
        attribution={"intent_id": "eq-a", "strategy_version": "s/1", "program_id": "p"},
    )
    reservation = pending_reservation(book, {}, equities, fold_ledger([fill]))
    assert reservation.loss_cents == 2 * equity_loss_cents(1, 67150, 66500) == 2 * 13430
    assert reservation.cash_cents == 67150 + 67150


def test_pending_reservation_of_a_debit_option_entry() -> None:
    effect = SupervisedEffect.model_validate(contract_fixture("option_effect_canary.json"))
    spec = DeskSpec(
        intent_id=effect.intent_id,
        account_id=effect.account_id,
        structure=effect.structure,
        entry_order_ref=effect.order_ref,
        registered_at=datetime(2026, 10, 22, 14, 0, tzinfo=UTC),
    )
    sid = effect.structure.id
    book = _book(**{sid: Status.ENTER_WORKING})
    reservation = pending_reservation(book, {sid: spec}, {}, None)
    structure = effect.structure
    cap = structure.max_loss_per_package() * 100 * structure.quantity * 100
    assert reservation.loss_cents == int(cap.to_integral_value())
    assert reservation.cash_cents == int(structure.limit * 100 * structure.quantity * 100)
    assert pending_reservation(_book(**{sid: Status.OPEN}), {sid: spec}, {}, None).loss_cents == 0


def test_an_equity_draft_completes_into_a_valid_proposal() -> None:
    effect = equity_effect()
    valid_until = datetime(2026, 10, 22, 20, 0, tzinfo=UTC)
    draft = equity_draft(
        effect,
        program_id="prog-equity-pilot",
        strategy_version="equity-pilot/1",
        valid_until=valid_until,
    )
    assert draft["candidate_id"] == "eq-spy-001"
    assert draft["body"]["review"]["max_loss_cents"] == 26860
    proposed = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
    proposal = complete_draft(
        draft,
        proposed_at=proposed,
        send_deadline=datetime(2026, 10, 22, 14, 10, tzinfo=UTC),
        quote=Quote(bid_cents=67148, ask_cents=67152, market_data_type="realtime", as_of=proposed),
        screening_sha256="e" * 64,
        owner_epoch="owner-x",
        profile_digest="d" * 64,
    )
    assert proposal.effect == effect
    assert proposal.review.notional_cents == 134300
    with pytest.raises(ValueError, match="horizon"):
        complete_draft(
            draft,
            proposed_at=proposed,
            send_deadline=datetime(2026, 10, 22, 20, 0, 1, tzinfo=UTC),
            quote=Quote(
                bid_cents=67148, ask_cents=67152, market_data_type="realtime", as_of=proposed
            ),
            screening_sha256="e" * 64,
            owner_epoch="owner-x",
            profile_digest="d" * 64,
        )
    with pytest.raises(ValueError, match="timezone"):
        equity_draft(
            effect,
            program_id="p",
            strategy_version="s/1",
            valid_until=datetime(2026, 10, 22, 20, 0),
        )
    assert Decimal(draft["body"]["review"]["notional_cents"]) == 134300
