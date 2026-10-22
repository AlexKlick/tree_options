"""Wave 5: the owner's manual equity ticket (``compose_entry`` +
``manual_equity_ticket``), composed by the desk, end to end over the shared
Wave 2 fakes (``OwnerRig``: the web's own ``CommandStore`` queues, the
desk's ``run_loop`` applies).

Oracles are hand-computed from the ticket and the scripted NBBO (2 SPY at
the 671.52 ask, stop 665.00, target 680.00: notional 1,343.04 USD, max loss
its 20% gap reserve, ceil(134304 x 0.2) = 26861 cents), the broker wire
(orders placed) and the files on disk (proposals, mandates, outbox).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.unit.trex_fakes import SPY_CON_ID
from tests.unit.wave2_owner_rig import ACCOUNT, OwnerRig
from tree_options.time.sessions import shift_instant
from tree_options.trex.contracts import (
    Blocker,
    CommandResult,
    DeskEvent,
    EntryProposal,
    SupervisedEquityEffect,
    model_document,
    parse_command,
)
from tree_options.trex.contracts.commands import ManualLimit
from tree_options.trex.contracts.effects import EquityExitPolicy
from tree_options.trex.desk_entries import manual_limit_cents

pytest.importorskip("ib_async")

BID, ASK = 67148, 67152
PROGRAM = "owner-manual"


@pytest.fixture()
def rig(tmp_path: Path) -> Iterator[OwnerRig]:
    r = OwnerRig(tmp_path)
    assert r.step() == 0  # first beat/tick, first books publish
    yield r
    r.close()


def _ticket(rig: OwnerRig, **changes: Any) -> dict[str, Any]:
    ticket: dict[str, Any] = {
        "symbol": "SPY",
        "quantity": 2,
        "stop_price_cents": 66500,
        "target_price_cents": 68000,
        "exit_deadline_session": rig.clock.next_session().isoformat(),
        "limit_policy": "ask",
    }
    ticket.update(changes)
    return ticket


def _args(token: str, ticket: dict[str, Any]) -> dict[str, Any]:
    return {
        "program_id": PROGRAM,
        "candidate_id": f"manual:{token}",
        "asset_kind": "equity",
        "manual_equity_ticket": ticket,
    }


def _compose(rig: OwnerRig, ticket: dict[str, Any], token: str | None = None) -> CommandResult:
    rig.quote(BID, ASK)
    return rig.run("compose_entry", _args(token or str(uuid.uuid4()), ticket))


def _proposals(rig: OwnerRig) -> list[EntryProposal]:
    folder = rig.paths.root / "proposals"
    if not folder.is_dir():
        return []
    return [
        EntryProposal.model_validate_json(p.read_bytes())
        for p in sorted(folder.glob("*.json"))
        if not p.name.endswith(".rejected.json")
    ]


def _authority_files(rig: OwnerRig) -> list[str]:
    """Every file under the supervised dir's mandate and outbox trees."""
    found: list[str] = []
    for path in sorted(rig.sup.root.rglob("*")):
        if path.is_file() and ("mandate" in str(path) or "outbox" in str(path)):
            found.append(str(path.relative_to(rig.sup.root)))
    return found


# ------------------------------------------------------------- composes


def test_a_manual_spy_ticket_composes_a_proposal_with_the_bound_exit_policy(rig):
    token = str(uuid.uuid4())
    ticket = _ticket(rig)
    composed = _compose(rig, ticket, token)
    assert composed.status == "applied", composed
    facts = composed.result or {}
    (proposal,) = _proposals(rig)
    assert facts["proposal_id"] == proposal.proposal_id
    effect = proposal.effect
    assert isinstance(effect, SupervisedEquityEffect)
    iid = f"manual-{token}"
    assert facts["intent_id"] == effect.intent_id == iid
    assert (effect.symbol, effect.con_id, effect.quantity) == ("SPY", SPY_CON_ID, 2)
    assert effect.account_id == ACCOUNT, "the desk binds its own paper account"
    assert effect.order_ref == f"trex:sup:{iid}"
    assert effect.limit_cents == ASK, "limit_policy ask: the live ask"
    # the exit policy the desk will enforce is exactly the ticket's
    assert effect.exit_policy == EquityExitPolicy(
        stop_price_cents=66500,
        target_price_cents=68000,
        exit_deadline_session=rig.clock.next_session(),
    )
    assert (effect.exit_policy.stop_confirm_ticks, effect.exit_policy.forced_exit_et) == (
        3,
        "15:45",
    )
    assert proposal.strategy_version == "owner-manual/1"
    assert proposal.program_id == PROGRAM
    # review figures, hand computed: 2 x 671.52; the 20% gap reserve beats
    # the stop loss 2 x (671.52 - 665.00) = 13.04
    assert proposal.review.notional_cents == facts["notional_cents"] == 134304
    assert proposal.review.max_loss_cents == facts["max_loss_cents"] == 26861
    quote = proposal.review.quote
    assert (quote.bid_cents, quote.ask_cents, quote.market_data_type) == (BID, ASK, "realtime")
    composed_events = [e for e in rig.events() if e["event"] == "proposal_composed"]
    assert [e["candidate"] for e in composed_events] == [f"manual:{token}"]


def test_composing_never_grants_a_mandate_or_sends_and_approval_is_a_separate_act(rig):
    composed = _compose(rig, _ticket(rig))
    assert composed.status == "applied", composed
    for _ in range(3):
        rig.wait()
        assert rig.step() == 0
    assert rig.orders() == [], "composing never places an order"
    assert _authority_files(rig) == [], "no mandate, intent, permit or outbox entry"
    assert DeskEvent.ENTRY_APPROVED.value not in rig.names()
    # the owner's explicit approval of the proposal is what sends: once
    rig.quote(BID, ASK)
    approved = rig.approve(composed)
    assert approved.status == "applied", approved
    (buy,) = rig.orders("BUY")
    iid = (composed.result or {})["intent_id"]
    assert (buy.order.orderRef, buy.order.account, buy.order.totalQuantity) == (
        f"trex:sup:{iid}",
        ACCOUNT,
        2,
    )
    assert (buy.order.tif, buy.order.outsideRth, buy.order.lmtPrice) == ("DAY", False, 671.52)


def test_the_mid_and_an_explicit_limit_price_the_ticket():
    assert manual_limit_cents("ask", 67148, 67152) == 67152
    assert manual_limit_cents("mid", 67148, 67152) == 67150
    assert manual_limit_cents("mid", 67148, 67153) == 67151, "a half cent rounds up"
    assert manual_limit_cents(ManualLimit(limit_cents=67100), 67148, 67152) == 67100
    with pytest.raises(ValueError):
        manual_limit_cents("market", 1, 2)


def test_an_explicit_limit_composes_at_that_limit(rig):
    composed = _compose(rig, _ticket(rig, limit_policy={"limit_cents": 67140}))
    assert composed.status == "applied", composed
    (proposal,) = _proposals(rig)
    assert isinstance(proposal.effect, SupervisedEquityEffect)
    assert proposal.effect.limit_cents == 67140


# ------------------------------------------------------------- refused


def test_a_symbol_off_the_allowlist_is_refused_before_any_quote(rig):
    """QQQ qualifies at the broker (it is the option underlying's stream),
    but the equity allowlist is SPY only."""
    stocks_before = set(rig.ib._stocks)
    refused = _compose(
        rig, _ticket(rig, symbol="QQQ", stop_price_cents=100, target_price_cents=None)
    )
    assert (refused.status, refused.reason) == (
        "refused",
        Blocker.EQUITY_SYMBOL_NOT_QUALIFIED.value,
    )
    assert set(rig.ib._stocks) == stocks_before, "no stock prepared for a refused symbol"
    assert _proposals(rig) == []


@pytest.mark.parametrize("wall", ["15:31", "15:59"])
def test_inside_the_session_but_outside_the_entry_window_is_refused(rig, wall):
    rig.clock.to_et(wall)
    refused = _compose(rig, _ticket(rig))
    assert (refused.status, refused.reason) == ("refused", Blocker.ENTRY_WINDOW_CLOSED.value)
    assert _proposals(rig) == [] and rig.orders() == []


def test_before_the_entry_window_opens_is_refused(tmp_path):
    from tests.unit.trex_fakes import CLOCK_START

    early = OwnerRig(tmp_path, start=shift_instant(CLOCK_START, 60))  # 09:31 ET
    try:
        assert early.step() == 0
        refused = _compose(early, _ticket(early))
        assert (refused.status, refused.reason) == ("refused", Blocker.ENTRY_WINDOW_CLOSED.value)
        assert _proposals(early) == []
    finally:
        early.close()


def test_outside_the_regular_session_is_refused(rig):
    rig.clock.to_et("16:05")
    refused = _compose(rig, _ticket(rig))
    assert (refused.status, refused.reason) == ("refused", "outside_regular_session")


def test_a_delayed_quote_never_prices_a_ticket(rig):
    rig.gw.set_market_data_type("SPY", "delayed")
    refused = _compose(rig, _ticket(rig))
    assert (refused.status, refused.reason) == ("refused", Blocker.EQUITY_QUOTE_NOT_REALTIME.value)
    assert _proposals(rig) == []


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"stop_price_cents": ASK}, "the stop is not below the limit"),
        ({"target_price_cents": ASK}, "the target is not above the limit"),
        ({"exit_deadline_session": "2026-10-21"}, "exit_deadline_session"),
        ({"exit_deadline_session": "2026-10-24"}, "exit_deadline_session"),  # a Saturday
    ],
)
def test_a_ticket_the_live_price_cannot_bound_is_refused(rig, changes, detail):
    refused = _compose(rig, _ticket(rig, **changes))
    assert (refused.status, refused.reason) == ("refused", "manual_ticket_invalid")
    assert detail in refused.detail
    assert _proposals(rig) == []


def test_the_desks_screening_still_applies_cash_capital_and_the_session_cap(rig):
    big = _compose(rig, _ticket(rig, quantity=100))  # 67,152 USD of a 5,000 USD profile
    assert big.status == "refused"
    assert "equity_notional_exceeds_intended_capital" in (big.result or {})["blockers"]
    # three entries already sent this session: the per-session cap holds
    now = rig.clock.utc().isoformat()
    for n in range(3):
        path = rig.sup.terminal(f"sent-{n}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"intent_id": f"sent-{n}", "outcome": "acknowledged", "at": now})
        )
    capped = _compose(rig, _ticket(rig))
    assert capped.status == "refused"
    assert Blocker.SESSION_ENTRY_LIMIT_REACHED.value in (capped.result or {})["blockers"]
    assert _proposals(rig) == []


# ------------------------------------------------------------- idempotency


def _submit_same(rig: OwnerRig, doc: dict[str, Any], *, sub: str = "owner") -> CommandResult:
    """The exact body again (a browser retry: a new request, same bytes)."""
    body = json.dumps(doc).encode()
    actor = rig.auth.verify(rig.auth.make_request(body, family_sub=sub))
    command = parse_command({**doc, "issued_by": model_document(actor)})
    return rig.store.submit(command, now=rig.clock.utc()).result


def test_compose_is_idempotent_per_key(rig):
    rig.quote(BID, ASK)
    now = rig.clock.utc()
    key = str(uuid.uuid4())
    doc = {
        "schema": "trex-command/1",
        "idempotency_key": key,
        "kind": "compose_entry",
        "args": _args(str(uuid.uuid4()), _ticket(rig)),
        "issued_at": now.isoformat(),
        "expires_at": shift_instant(now, 120).isoformat(),
    }
    first = _submit_same(rig, doc)
    assert first.status == "accepted"
    assert _submit_same(rig, doc).status == "duplicate", "a retry before the desk applies it"
    assert rig.step() == 0
    applied = rig.result(key)
    assert applied.status == "applied", applied
    again = _submit_same(rig, doc)
    assert (again.status, again.reason) == ("duplicate", Blocker.DUPLICATE_COMMAND.value)
    first_result = (again.result or {})["first_result"]
    assert first_result["status"] == "applied"
    assert first_result["result"]["proposal_id"] == (applied.result or {})["proposal_id"]
    for _ in range(2):
        assert rig.step() == 0
    assert len(_proposals(rig)) == 1, "the same key composes once"
    assert rig.result(key) == applied, "the answer never changes"
    changed = {**doc, "args": {**doc["args"], "manual_equity_ticket": _ticket(rig, quantity=1)}}
    mismatch = _submit_same(rig, changed)
    assert (mismatch.status, mismatch.reason) == ("refused", Blocker.COMMAND_BODY_MISMATCH.value)
    assert len(_proposals(rig)) == 1
