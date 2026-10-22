"""Wave 2b soak: the supervised paper desk end to end, driven by commands.

ONE owner process (``SupervisedDesk`` + ``run_loop``) over the shared Wave 2
fakes (``Wave2Gateway``, ``SessionClock``, ``FakeCommandAuthority``); every
command is queued by the web's own ``CommandStore`` (accepted, never
applied there) and applied by the desk. Oracles are the frozen fixture
effect (2 SPY at 671.50, stop 665.00, 3 confirm ticks: max loss $268.60, its
20% gap reserve), the operator profile (max open loss $400: one fixture
entry fits, two do not), broker wire facts (orders, tags, executions) and
the books' published files.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.unit.trex_fakes import CLOCK_START, SPY_CON_ID, FillStep, equity_effect
from tests.unit.wave2_owner_rig import ACCOUNT, EQUITY_PROGRAM, PROFILE, OwnerRig

from tree_options.action_graph.canary import equity_entry_window
from tree_options.trex import desk_commands
from tree_options.trex import supervised as supervised_core
from tree_options.trex.contracts import (
    Blocker,
    DeskEvent,
    LedgerEvent,
    Reconciliation,
    RiskSnapshot,
    SupervisedMandateV2,
    equity_max_loss_cents,
)
from tree_options.trex.desk_commands import CommandQueue
from tree_options.trex.ledger import HoldingsDocument, fold_ledger, read_ledger
from tree_options.trex.pause import pause_entries
from tree_options.trex.risk_snapshot import settled_cash_cents
from tree_options.trex.state import Status

pytest.importorskip("ib_async")

pytestmark = pytest.mark.soak

IID = "eq-spy-001"
STOP = 66500


@pytest.fixture()
def rig(tmp_path: Path) -> Iterator[OwnerRig]:
    r = OwnerRig(tmp_path)
    assert r.step() == 0  # first beat/tick, first books publish
    yield r
    r.close()


def _confirm_stop(rig: OwnerRig, bid: int = STOP) -> None:
    """``stop_confirm_ticks`` new real-time ticks at the stop, one loop each."""
    for _ in range(equity_effect().exit_policy.stop_confirm_ticks):
        rig.quote(bid, bid + 4)
        assert rig.step() == 0


def _open(rig: OwnerRig, effect: Any = None) -> Any:
    """compose -> approve -> full fill: an open desk-owned position."""
    effect = rig.draft_equity(effect)
    rig.quote()
    composed = rig.compose(effect.intent_id)
    assert composed.status == "applied", composed
    rig.gw.script_fills(effect.order_ref, [FillStep()])
    rig.quote()
    approved = rig.approve(composed)
    assert approved.status == "applied", approved
    rig.wait()  # the fill
    assert rig.step() == 0
    assert rig.step() == 0
    assert rig.book()[effect.intent_id]["status"] == Status.OPEN.value
    return effect


def _ledger(rig: OwnerRig) -> tuple[LedgerEvent, ...]:
    return read_ledger(rig.ledger_file("events.jsonl"))


def _reconciliation(rig: OwnerRig) -> Reconciliation:
    return Reconciliation.model_validate_json(rig.ledger_file("reconciliation.json").read_bytes())


def _risk(rig: OwnerRig) -> RiskSnapshot:
    return RiskSnapshot.model_validate_json(rig.ledger_file("risk-snapshot.json").read_bytes())


# ------------------------------------------------------------- the lifecycle


def test_compose_approve_partial_fill_stop_exit_flat_reconciled_with_commissions(rig):
    effect = rig.draft_equity()
    rig.quote()
    composed = rig.compose(IID)
    assert composed.status == "applied", composed
    facts = composed.result or {}
    proposal_path = rig.paths.root / "proposals" / f"{facts['proposal_id']}.json"
    assert proposal_path.exists(), "the proposal is written for the web to show"
    assert facts["max_loss_cents"] == equity_max_loss_cents(effect) == 26860
    assert facts["entries_this_session"] == 0
    assert rig.orders() == [], "composing never places an order"

    # approve: exactly one STK DAY limit, tagged, explicit paper account
    rig.gw.script_fills(effect.order_ref, [FillStep(1)])
    rig.quote()
    approved = rig.approve(composed)
    assert approved.status == "applied", approved
    assert approved.result["sent"] is True and approved.result["outcome"] == "acknowledged"
    (buy,) = rig.orders("BUY")
    assert (buy.order.orderRef, buy.order.account, buy.order.totalQuantity) == (
        "trex:sup:eq-spy-001",
        ACCOUNT,
        2,
    )
    assert (buy.order.tif, buy.order.outsideRth, buy.order.lmtPrice) == ("DAY", False, 671.5)
    assert rig.sup.entry_mandate_consumed(IID).exists(), "the approval minted its one permit"

    # a second approval of the same proposal answers the first; nothing re-sent
    again = rig.approve(composed)
    assert again.status == "applied"
    assert again.result["duplicate_of"] == approved.idempotency_key
    assert again.result["sent"] is False
    assert len(rig.orders("BUY")) == 1

    # partial fill: 1 of 2 (its commission report arrives a step later)
    rig.wait()
    assert rig.step() == 0
    st = rig.book()[IID]
    assert (st["status"], st["filled_qty"]) == (Status.ENTER_WORKING.value, 1)
    fills = [e for e in _ledger(rig) if e.kind == "fill"]
    assert [(e.quantity_delta, e.attribution.program_id) for e in fills] == [(1, EQUITY_PROGRAM)]

    # the stop confirms: the unfilled remainder is cancelled, the shares held are sold
    rig.gw.script_fills("trex:desk:eq-spy-001", [FillStep()])
    _confirm_stop(rig)
    assert rig.step() == 0
    assert buy.orderStatus.status == "Cancelled", "the remainder was cancelled"
    (sell,) = rig.orders("SELL")
    assert (sell.order.orderRef, sell.order.totalQuantity) == ("trex:desk:eq-spy-001", 1)
    assert DeskEvent.EQUITY_EXIT_STOP.value in rig.names()

    # the exit fills: flat at the broker, the book closes on the stop
    rig.wait()
    assert rig.step() == 0
    assert rig.step() == 0
    assert rig.held() == 0
    st = rig.book()[IID]
    assert (st["status"], st["close_reason"]) == (Status.CLOSED.value, "stop")

    # late commission reports, then the next status cadence: reconciled clean
    rig.gw.deliver_commissions()
    rig.clock.advance(61)
    assert rig.step() == 0
    ledger = _ledger(rig)
    kinds = sorted(e.kind for e in ledger if e.kind != "reconciliation")
    assert kinds == ["commission", "commission", "fill", "fill"], kinds
    fold = fold_ledger(ledger)
    assert fold.complete, fold.pending_commissions
    assert fold.positions(ACCOUNT) == {SPY_CON_ID: 0}
    assert sum(e.cash_delta_micros for e in ledger if e.kind == "commission") == -2_000_000
    reconciliation = _reconciliation(rig)
    assert (reconciliation.status, reconciliation.mismatches) == ("clean", ())
    risk = _risk(rig)
    assert risk.status == "complete" and risk.open_loss_cents == 0
    # W2c item 8: the broker's SettledCash shows neither of today's trades;
    # the published cash takes the share bought (1 x 671.50) and both 1.00
    # commissions out, and never adds the unsettled sale proceeds
    broker = settled_cash_cents(rig.gw.accountValues(), account_id=ACCOUNT)
    assert broker is not None
    assert risk.cash_cents == broker - (67_150 + 2 * 100)
    holdings = HoldingsDocument.model_validate_json(rig.ledger_file("holdings.json").read_bytes())
    assert holdings.holdings == ()
    # replaying the broker facts books nothing new (append-only, idempotent)
    before = rig.ledger_file("events.jsonl").read_bytes()
    assert rig.step() == 0
    assert rig.ledger_file("events.jsonl").read_bytes() == before
    names = rig.names()
    for name in (
        DeskEvent.PROPOSAL_COMPOSED,
        DeskEvent.ENTRY_APPROVED,
        DeskEvent.COMMAND_APPLIED,
        DeskEvent.RECONCILIATION_CLEAN,
    ):
        assert name.value in names, name
    assert "unknown_exposure" not in names and "legs_mismatch" not in names


# ------------------------------------------------------------- authority


def test_back_to_back_approvals_cannot_both_pass_when_only_one_fits(rig):
    """The first entry is sent but unfilled: the broker-priced snapshot does
    not show it yet, so only the pending-entry reservation stops the second
    ($268.60 + $268.60 > the $400 open-loss cap)."""
    first = rig.draft_equity(equity_effect(intent_id="eq-spy-a"))
    second = rig.draft_equity(equity_effect(intent_id="eq-spy-b"))
    rig.quote()
    composed_a = rig.compose(first.intent_id)
    rig.quote()
    composed_b = rig.compose(second.intent_id)
    assert (composed_a.status, composed_b.status) == ("applied", "applied")
    rig.quote()
    assert rig.approve(composed_a).status == "applied"  # rests unfilled
    assert rig.step() == 0
    rig.quote()
    refused = rig.approve(composed_b)
    assert (refused.status, refused.reason) == ("refused", "open_loss_cap_exceeded"), refused
    facts = refused.result or {}
    assert facts["reserved_intents"] == ["eq-spy-a"]
    assert facts["reserved_loss_cents"] == 26860
    assert facts["reserved_cash_cents"] == 2 * 67150
    assert len(rig.orders("BUY")) == 1
    assert not rig.sup.entry_mandate("eq-spy-b").exists(), "a refused approval grants nothing"


def test_a_pause_between_approval_and_send_refuses_the_send(rig, monkeypatch):
    """PAUSE_ENTRIES placed while the approval is being granted (an operator
    CLI racing the desk): nothing is sent. After a resume the owner's next
    approval of the same proposal sends it exactly once."""
    rig.draft_equity()
    rig.quote()
    composed = rig.compose(IID)
    real_grant = supervised_core.grant_entry_mandate

    def grant_then_pause(*args: Any, **kwargs: Any) -> SupervisedMandateV2:
        mandate = real_grant(*args, **kwargs)
        pause_entries(rig.paths.root, actor="operator-cli", now=rig.clock())
        return mandate

    monkeypatch.setattr(supervised_core, "grant_entry_mandate", grant_then_pause)
    rig.quote()
    stopped = rig.approve(composed)
    assert (stopped.status, stopped.reason) == ("refused", Blocker.ENTRIES_PAUSED.value)
    assert rig.orders() == [], "the send was refused"
    assert rig.sup.entry_mandate(IID).exists(), "the approval is not spent"
    monkeypatch.setattr(supervised_core, "grant_entry_mandate", real_grant)

    paused = rig.approve(composed)
    assert (paused.status, paused.reason) == ("refused", Blocker.ENTRIES_PAUSED.value)
    assert rig.run("resume_entries", {"reason": "checked"}).status == "applied"
    rig.quote()
    sent = rig.approve(composed)
    assert sent.status == "applied" and sent.result["sent"] is True, sent
    assert sent.result["mandate_approval_id"] == stopped.idempotency_key
    assert len(rig.orders("BUY")) == 1
    assert rig.approve(composed).result["duplicate_of"] == sent.idempotency_key
    assert len(rig.orders("BUY")) == 1


def test_a_restart_invalidates_a_pending_approval(rig, monkeypatch):
    rig.draft_equity()
    rig.quote()
    composed = rig.compose(IID)
    real_grant = supervised_core.grant_entry_mandate

    def grant_then_pause(*args: Any, **kwargs: Any) -> SupervisedMandateV2:
        mandate = real_grant(*args, **kwargs)
        pause_entries(rig.paths.root, actor="operator-cli", now=rig.clock())
        return mandate

    monkeypatch.setattr(supervised_core, "grant_entry_mandate", grant_then_pause)
    rig.quote()
    assert rig.approve(composed).reason == Blocker.ENTRIES_PAUSED.value
    monkeypatch.setattr(supervised_core, "grant_entry_mandate", real_grant)
    assert rig.sup.entry_mandate(IID).exists(), "a granted, unconsumed approval"
    old_epoch = rig.desk.owner_epoch

    rig.restart()
    assert rig.desk.owner_epoch != old_epoch
    assert rig.started["retired_mandates"] == [IID]
    assert rig.started["stale_proposals"] == [composed.result["proposal_id"]]
    (invalidated,) = [
        e
        for e in rig.events()
        if e["event"] == DeskEvent.APPROVALS_INVALIDATED.value and e["retired_mandates"]
    ]
    assert invalidated["owner_epoch"] == rig.desk.owner_epoch
    assert not rig.sup.entry_mandate(IID).exists()
    assert rig.run("resume_entries", {"reason": "after restart"}).status == "applied"
    rig.quote()
    for epoch in (old_epoch, rig.desk.owner_epoch):
        refused = rig.approve(composed, owner_epoch=epoch)
        assert (refused.status, refused.reason) == (
            "refused",
            Blocker.APPROVAL_EPOCH_MISMATCH.value,
        ), refused
    assert rig.orders() == []


def test_reject_tombstones_the_proposal_and_the_web_shows_it(rig):
    from tree_options.trex_web.commands_view import proposals_payload

    rig.draft_equity()
    rig.quote()
    composed = rig.compose(IID)
    pid = composed.result["proposal_id"]
    (row,) = proposals_payload(rig.paths.root, rig.sup.root, rig.clock())["proposals"]
    assert (row["proposal_id"], row["status"]) == (pid, "pending")
    assert row["proposal_sha256"] == composed.result["proposal_sha256"]
    rejected = rig.run("reject_entry", {"proposal_id": pid, "reason": "not today"})
    assert rejected.status == "applied", rejected
    assert (rig.paths.root / "proposals" / f"{pid}.rejected.json").exists()
    (row,) = proposals_payload(rig.paths.root, rig.sup.root, rig.clock())["proposals"]
    assert row["status"] == "rejected"
    rig.quote()
    refused = rig.approve(composed)
    assert (refused.status, refused.reason) == ("refused", "proposal_rejected")
    assert rig.orders() == []


def test_an_equity_entry_before_the_window_opens_is_refused_then_allowed(tmp_path):
    """The one scenario that starts at the session open (09:30 ET), before
    the equity entry window (open + 5 min): compose refuses
    ``entry_window_closed`` and places nothing; at 09:35 the same candidate
    composes and its approval sends exactly one BUY."""
    early = OwnerRig(tmp_path, start=CLOCK_START)
    try:
        assert early.step() == 0
        effect = early.draft_equity()
        early.quote()
        refused = early.compose(effect.intent_id)
        assert (refused.status, refused.reason) == (
            "refused",
            Blocker.ENTRY_WINDOW_CLOSED.value,
        ), refused
        assert early.orders() == []
        window_start, _ = equity_entry_window(early.clock.today())
        early.clock.to(window_start)
        early.quote()
        composed = early.compose(effect.intent_id)
        assert composed.status == "applied", composed
        early.quote()
        approved = early.approve(composed)
        assert approved.status == "applied" and approved.result["sent"] is True, approved
        assert len(early.orders("BUY")) == 1
    finally:
        early.close()


# ------------------------------------------------------------- facts


def test_a_reconnect_publishes_the_reconciliation_at_once(rig):
    first = _reconciliation(rig).as_of
    rig.clock.advance(10)
    assert rig.step() == 0
    assert _reconciliation(rig).as_of == first, "inside the 60 s cadence: not rewritten"
    rig.gw.is_connected = False
    rig.desk.runtime.observe_connection()
    rig.gw.is_connected = True
    rig.clock.advance(5)
    assert rig.step() == 0
    reconnected = _reconciliation(rig)
    assert reconnected.as_of > first, "a reconnect rewrites the status files at once"
    assert _risk(rig).as_of == reconnected.as_of
    edges = [e for e in rig.events() if e["event"] == "gateway_connected"]
    assert [e["first"] for e in edges] == [True, False]


def test_reconciliation_required_blocks_entries_never_exits(rig):
    _open(rig)
    rig.gw.override_position(SPY_CON_ID, 3.0)  # one share nobody booked
    rig.clock.advance(61)
    assert rig.step() == 0
    reconciliation = _reconciliation(rig)
    assert reconciliation.status == "reconciliation_required"
    (mismatch,) = reconciliation.mismatches
    assert (mismatch.local_qty, mismatch.broker_qty) == (2, 3)
    assert ("trex-desk reconciliation_required", "urgent") in [(t, p) for t, _, p in rig.notified]
    rig.draft_equity(equity_effect(intent_id="eq-spy-2", quantity=1))
    rig.quote()
    composed = rig.compose("eq-spy-2")
    assert (composed.status, composed.reason) == ("refused", "reconciliation_required")
    rig.gw.script_fills("trex:desk:eq-spy-001", [FillStep()])
    _confirm_stop(rig)
    assert rig.step() == 0
    (sell,) = rig.orders("SELL")
    assert sell.order.totalQuantity == 2, "the exit sells the desk's own shares only"


def test_the_session_entry_cap_counts_both_kinds(rig):
    """Three entries sent this session (any kind, from the outbox) refuse a
    fourth: ``session_entry_limit_reached``."""
    stamp = rig.clock.utc().isoformat()
    outbox = rig.sup.outbox_dir()
    outbox.mkdir(parents=True, exist_ok=True)
    for intent, outcome in (("canary-x", "acknowledged"), ("eq-x", "uncertain"), ("eq-y", "x")):
        doc = {"intent_id": intent, "outcome": outcome, "at": stamp}
        (outbox / f"{intent}.terminal.json").write_text(json.dumps(doc))
    rejected = {"intent_id": "eq-z", "outcome": "rejected", "at": stamp}
    (outbox / "eq-z.terminal.json").write_text(json.dumps(rejected))
    rig.draft_equity()
    rig.quote()
    composed = rig.compose(IID)
    assert (composed.status, composed.reason) == (
        "refused",
        Blocker.SESSION_ENTRY_LIMIT_REACHED.value,
    ), composed
    assert composed.result["entries_this_session"] == 3


# ------------------------------------------------------------- options


def test_an_option_debit_spread_goes_through_the_same_command_path(rig):
    effect = rig.draft_option()
    rig.option_quotes()
    composed = rig.compose(effect.intent_id, asset_kind="option")
    assert composed.status == "applied", composed
    assert composed.result["max_loss_cents"] == 9000
    rig.option_quotes()
    approved = rig.approve(composed)
    assert approved.status == "applied", approved
    (order,) = rig.orders()
    assert order.contract.secType == "BAG"
    assert (order.order.orderRef, order.order.account) == ("trex:sup:opt-qqq-001", ACCOUNT)
    spec = json.loads(rig.paths.spec("opt-qqq-001").read_text())
    assert (spec["schema"], spec["intent_id"]) == ("trex-desk-spec/1", "opt-qqq-001")
    assert rig.sup.entry_mandate_consumed("opt-qqq-001").exists()
    assert rig.step() == 0
    assert rig.book()["opt-qqq-001"]["status"] == Status.ENTER_WORKING.value


# ------------------------------------------------------------- kill switches


def test_halt_blocks_entries_and_exits_as_before(rig):
    _open(rig)
    halted = rig.run("halt", {"reason": "drill"})
    assert halted.status == "applied" and (rig.paths.root / "HALT").exists()
    rig.draft_equity(equity_effect(intent_id="eq-spy-2", quantity=1))
    rig.quote()
    composed = rig.compose("eq-spy-2")
    assert (composed.status, composed.reason) == ("refused", "halt")
    rig.gw.script_fills("trex:desk:eq-spy-001", [FillStep()])
    _confirm_stop(rig)
    assert rig.step() == 0
    assert rig.orders("SELL") == [], "HALT gates exit orders too"
    assert "halt_no_new_orders" in rig.names()
    resumed = rig.run("resume_all", {"reason": "drill over"})
    assert resumed.status == "applied" and resumed.result["removed"] == ["HALT"]
    assert rig.step() == 0
    (sell,) = rig.orders("SELL")
    assert sell.order.totalQuantity == 2


@pytest.fixture()
def roomy(tmp_path: Path) -> Iterator[OwnerRig]:
    """A rig whose open-loss cap fits two fixture entries ($268.60 + $134.30)."""
    r = OwnerRig(tmp_path, profile={**PROFILE, "max_open_loss": "1000"})
    assert r.step() == 0
    yield r
    r.close()


def test_a_same_day_buy_counts_against_cash_before_it_settles(roomy):
    """W2c item 8: the broker's SettledCash (2,000.00 here) does not show a
    stock purchase until it settles (T+1). After 2 SPY are bought at 671.50
    the published cash is 2,000.00 - 1,343.00 - its commission, so a second
    same-day purchase of 1 share (671.50) is refused insufficient_cash
    instead of being sent against cash already spent."""
    rig = roomy
    rig.gw.account_values["SettledCash"] = "2000.00"
    rig.desk.books.request_refresh()
    assert rig.step() == 0
    _open(rig)
    rig.gw.deliver_commissions()
    rig.clock.advance(61)
    assert rig.step() == 0
    assert _risk(rig).cash_cents == 200_000 - (2 * 67_150 + 100)
    rig.draft_equity(equity_effect(intent_id="eq-spy-2", quantity=1))
    rig.quote()
    composed = rig.compose("eq-spy-2")
    assert (composed.status, composed.reason) == ("refused", Blocker.INSUFFICIENT_CASH.value), (
        composed
    )
    assert len(rig.orders("BUY")) == 1


def test_a_standing_flatten_refuses_compose_and_approve(roomy):
    """W2c item 2: with FLATTEN standing an approval would send a BUY the
    runtime unwinds at once. A proposal composed before the FLATTEN is
    refused at approval (nothing granted, nothing sent), a new compose is
    refused, and the open position is still flattened."""
    rig = roomy
    _open(rig)
    second = rig.draft_equity(equity_effect(intent_id="eq-spy-2", quantity=1))
    rig.quote()
    composed = rig.compose(second.intent_id)
    assert composed.status == "applied", composed
    flattened = rig.run("flatten", {"reason": "close it all"})
    assert flattened.status == "applied" and (rig.paths.root / "FLATTEN").exists()
    rig.quote()
    refused = rig.approve(composed)
    assert (refused.status, refused.reason) == ("refused", Blocker.FLATTEN.value), refused
    assert not rig.sup.entry_mandate("eq-spy-2").exists(), "a refused approval grants nothing"
    assert len(rig.orders("BUY")) == 1, "only the first entry was ever sent"
    rig.draft_equity(equity_effect(intent_id="eq-spy-3", quantity=1))
    rig.quote()
    again = rig.compose("eq-spy-3")
    assert (again.status, again.reason) == ("refused", Blocker.FLATTEN.value), again
    rig.quote()
    assert rig.step() == 0
    sells = rig.orders("SELL")
    assert [(s.order.orderRef, s.order.totalQuantity) for s in sells] == [
        ("trex:desk:eq-spy-001", 2)
    ]


def test_a_flatten_accepted_through_a_gateway_outage_is_applied_not_expired(rig):
    """W2b review P1. The gateway drops with a position open. The operator
    clicks PAUSE (the web answers 202 accepted); the owner applies it on its
    way out (exit 6). The unit then waits for the gateway and the operator
    clicks FLATTEN, which outlives its 300 s TTL before any owner runs. It
    is not dropped as command_expired: the restarted owner applies it before
    its first tick, and that tick flattens (PAUSE never blocks exits)."""
    _open(rig)
    rig.gw.is_connected = False
    paused = rig.submit("pause_entries", {"reason": "gateway down"}, ttl_s=300)
    assert rig.step() == 6
    assert rig.result(paused).status == "applied"
    flatten = rig.submit("flatten", {"reason": "close it all"}, ttl_s=300)
    rig.gw.is_connected = True
    rig.restart(down_s=301)
    answer = rig.result(flatten)
    assert (answer.status, answer.result["applied_after_expiry"]) == ("applied", True)
    rig.quote()
    assert rig.step() == 0
    (sell,) = rig.orders("SELL")
    assert (sell.order.orderRef, sell.order.totalQuantity) == ("trex:desk:eq-spy-001", 2)


def test_a_halt_accepted_through_a_gateway_outage_still_gates_the_exits(rig):
    _open(rig)
    rig.gw.is_connected = False
    assert rig.step() == 6
    halt = rig.submit("halt", {"reason": "while the owner is down"}, ttl_s=300)
    rig.gw.is_connected = True
    rig.restart(down_s=301)
    assert rig.result(halt).status == "applied"
    assert (rig.paths.root / "HALT").exists()
    rig.gw.script_fills("trex:desk:eq-spy-001", [FillStep()])
    _confirm_stop(rig)
    assert rig.step() == 0
    assert rig.orders("SELL") == [], "the late HALT gates the stop exit as before"


def test_a_command_stage_failure_never_stops_the_exits(rig, monkeypatch):
    """W2c item 3: a command-stage error while the session is up (a file
    error, a bug) is one ``commands_failed`` event per error class and the
    loop goes on: the stop exit still fires. A lost session is still exit 6."""
    _open(rig)

    def unreadable(self: CommandQueue, kinds: Any = None) -> Any:
        raise PermissionError(13, "commands/ unreadable")

    monkeypatch.setattr(CommandQueue, "process", unreadable)
    rig.gw.script_fills("trex:desk:eq-spy-001", [FillStep()])
    _confirm_stop(rig)
    assert rig.step() == 0
    (sell,) = rig.orders("SELL")
    assert (sell.order.orderRef, sell.order.totalQuantity) == ("trex:desk:eq-spy-001", 2)
    failures = [e for e in rig.events() if e["event"] == DeskEvent.COMMANDS_FAILED.value]
    assert [(e["stage"], e["error"]) for e in failures] == [("commands", "PermissionError")]

    def bug(self: CommandQueue, kinds: Any = None) -> Any:
        raise ValueError("a bug")

    monkeypatch.setattr(CommandQueue, "process", bug)
    assert rig.step() == 0

    def lost(self: CommandQueue, kinds: Any = None) -> Any:
        rig.gw.is_connected = False
        raise ConnectionError("gone")

    monkeypatch.setattr(CommandQueue, "process", lost)
    assert rig.step() == 6


def test_a_burst_of_entry_commands_never_delays_a_kill_switch(rig):
    """A burst of compose commands queued before a HALT: the HALT is applied
    in the same loop iteration, ahead of them, and at most
    ``MAX_COMMANDS_PER_PASS`` composes run per iteration."""
    limit = desk_commands.MAX_COMMANDS_PER_PASS
    rig.draft_equity()
    rig.quote()
    keys = []
    for _ in range(limit + 2):  # issued one second apart: issue order is defined
        args = {"program_id": EQUITY_PROGRAM, "candidate_id": IID, "asset_kind": "equity"}
        keys.append(rig.submit("compose_entry", args))
        rig.clock.advance(1)
    halt = rig.submit("halt", {"reason": "stop everything"})
    assert rig.step() == 0
    assert rig.result(halt).status == "applied" and (rig.paths.root / "HALT").exists()
    answered = [k for k in keys if rig.result(k).status != "accepted"]
    assert answered == keys[:limit], "the rest wait, queued, for the next iteration"
    assert {rig.result(k).reason for k in answered} == {"halt"}, "applied first: HALT refuses"
    assert rig.step() == 0
    assert all(rig.result(k).status != "accepted" for k in keys)


def test_pause_blocks_entries_but_a_stop_exit_still_fires(rig):
    _open(rig)
    paused = rig.run("pause_entries", {"reason": "lunch"})
    assert paused.status == "applied" and paused.result["entries_paused"] is True
    audit = (rig.paths.root / "pause-entries.jsonl").read_text().splitlines()
    assert json.loads(audit[-1])["actor"] == "family:owner"
    rig.draft_equity(equity_effect(intent_id="eq-spy-2", quantity=1))
    rig.quote()
    composed = rig.compose("eq-spy-2")
    assert (composed.status, composed.reason) == ("refused", Blocker.ENTRIES_PAUSED.value)
    rig.gw.script_fills("trex:desk:eq-spy-001", [FillStep()])
    _confirm_stop(rig)
    assert rig.step() == 0
    (sell,) = rig.orders("SELL")
    assert (sell.order.orderRef, sell.order.totalQuantity) == ("trex:desk:eq-spy-001", 2)
    rig.wait()
    assert rig.step() == 0
    assert rig.step() == 0
    assert rig.book()[IID]["status"] == Status.CLOSED.value
