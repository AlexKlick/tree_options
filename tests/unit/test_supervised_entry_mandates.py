"""Per-trade authority: ``supervised-mandate/2`` grant, permit binding, send.

One owner approval authorizes exactly one order for exactly one effect.
Oracles are independent of the implementation: approvals and mandates come
from the shared C2.8 builders (``tests/unit/trex_fakes.py``) over the
frozen fixtures, refusals are pinned to the frozen ``Blocker`` wire strings
(C2.7) or the existing ``SupervisedRefused`` reasons, and consumption is
observed on disk (the C2.6 layout: ``mandates/<iid>.json`` renamed to
``mandates/<iid>.consumed.json``).
"""

from __future__ import annotations

import hashlib
import threading
from datetime import datetime
from decimal import Decimal

import pytest

from tests.unit.trex_fakes import (
    approve_command,
    contract_fixture,
    entry_proposal,
    equity_effect,
)
from tree_options.execution import BrokerAcknowledgement, OrderIntent
from tree_options.time.sessions import shift_instant
from tree_options.trex.contracts import (
    Blocker,
    EntryProposal,
    SupervisedEffect,
    SupervisedEquityEffect,
    SupervisedMandateV2,
    Wave2SupervisedDir,
    effect_v1_bytes,
    effect_v1_sha256,
    mandate_for_approval,
)
from tree_options.trex.supervised import (
    Acknowledged,
    ApprovalRefused,
    NotSubmitted,
    SupervisedIntent,
    SupervisedPaths,
    SupervisedRefused,
    active_mandate,
    entry_mandates,
    grant_entry_mandate,
    grant_mandate,
    invalidate_entry_mandates,
    issue_permit,
    record_intent,
    send,
)

PROPOSAL_DOC = contract_fixture("proposal_equity.json")
COMMAND_DOC = contract_fixture("command_approve_entry.json")
EPOCH = PROPOSAL_DOC["review"]["owner_epoch"]
DIGEST = PROPOSAL_DOC["review"]["profile_digest"]
ACCOUNT = PROPOSAL_DOC["effect"]["account_id"]
STRATEGY = PROPOSAL_DOC["strategy_version"]
SEND_DEADLINE = datetime.fromisoformat(PROPOSAL_DOC["send_deadline"])
COMMAND_EXPIRES = datetime.fromisoformat(COMMAND_DOC["expires_at"])
#: the fixture mandate's grant time (inside the command and proposal windows)
T_GRANT = datetime.fromisoformat(contract_fixture("mandate_v2.json")["granted_at"])
#: the approval TTL is an owner default (C2.3): 300 s, never the send deadline here
APPROVAL_TTL_S = 300
T_EXPIRY = shift_instant(T_GRANT, APPROVAL_TTL_S)
SCREENING_SHA = "b" * 64


@pytest.fixture()
def paths(tmp_path):
    supervised = SupervisedPaths(tmp_path / "supervised")
    supervised.prepare()
    return supervised


def _layout(paths: SupervisedPaths) -> Wave2SupervisedDir:
    return Wave2SupervisedDir(paths.root)


def _intent(
    effect: SupervisedEquityEffect | SupervisedEffect,
    *,
    source: str = STRATEGY,
    created_at: datetime = T_GRANT,
    send_deadline: datetime = SEND_DEADLINE,
) -> SupervisedIntent:
    if isinstance(effect, SupervisedEquityEffect):
        order = OrderIntent(
            intent_id=effect.intent_id,
            contract_id=f"STK:{effect.con_id}",
            side="BUY",
            position_effect="OPEN_LONG",
            quantity=effect.quantity,
            order_type="LIMIT",
            limit_price=Decimal(effect.limit_cents) / 100,
            intent_created_at=created_at,
            source=source,
            source_sequence_id=f"seq-{effect.intent_id}",
        )
    else:
        order = OrderIntent(
            intent_id=effect.intent_id,
            contract_id=f"BAG:{effect.structure.id}",
            side="BUY",
            position_effect="OPEN_LONG",
            quantity=effect.quantity,
            order_type="LIMIT",
            limit_price=effect.limit,
            execution_style="package",
            package_id=effect.structure.id,
            intent_created_at=created_at,
            source=source,
            source_sequence_id=f"seq-{effect.intent_id}",
        )
    return SupervisedIntent(
        intent=order,
        package_intent_sha256=effect_v1_sha256(effect),
        created_at=created_at,
        send_deadline=send_deadline,
    )


def _grant(paths, proposal=None, command=None, *, now=T_GRANT, epoch=EPOCH, digest=DIGEST):
    proposal = proposal if proposal is not None else entry_proposal()
    command = command if command is not None else approve_command(proposal)
    return grant_entry_mandate(
        paths,
        now=now,
        proposal=proposal,
        command=command,
        owner_epoch=epoch,
        profile_digest=digest,
    )


def _permit(paths, effect=None, *, now=T_GRANT, epoch=EPOCH, digest=DIGEST, payload=None, **kw):
    effect = effect if effect is not None else equity_effect()
    intent = kw.pop("intent", None) or _intent(effect)
    return issue_permit(
        paths,
        now=now,
        account_id=kw.pop("account_id", ACCOUNT),
        owner_epoch=epoch,
        intent=intent,
        canary_blockers=(),
        effect_payload=payload if payload is not None else effect_v1_bytes(effect),
        screening_sha256=SCREENING_SHA,
        profile_digest=digest,
        **kw,
    )


class RecordingBroker:
    """Acknowledges every submit and records the exact bytes it was handed."""

    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def submit(self, attempt, effect_payload: bytes):
        self.payloads.append(effect_payload)
        sequence = f"ack-{attempt.intent_id}"
        return Acknowledged(
            BrokerAcknowledgement(
                record_id=sequence,
                intent_id=attempt.intent_id,
                broker_order_id="4242",
                broker_acknowledged_at=attempt.send_attempt_at,
                locally_received_at=attempt.send_attempt_at,
                source="recording-broker",
                source_sequence_id=sequence,
                broker_sequence_id=sequence,
            )
        )

    def lookup(self, intent_id: str):
        return NotSubmitted()


# ------------------------------------------------------------------ grant


def test_grant_writes_the_per_trade_mandate_for_the_approval(paths):
    proposal = entry_proposal()
    command = approve_command(proposal)
    granted = _grant(paths, proposal, command)
    stored = _layout(paths).mandate(proposal.effect.intent_id)
    assert stored.exists()
    assert SupervisedMandateV2.model_validate_json(stored.read_bytes()) == granted
    # independent oracle: the contract's own builder over the same approval
    assert granted == mandate_for_approval(proposal, command, now=T_GRANT)
    assert granted.expires_at == min(SEND_DEADLINE, T_EXPIRY)
    assert granted.effect_sha256 == effect_v1_sha256(proposal.effect)
    # the v1 single mandate is untouched by a per-trade grant
    assert not paths.mandate().exists()


@pytest.mark.parametrize(
    ("live_epoch", "live_digest", "args", "now", "expected"),
    [
        ("owner-restarted-9", DIGEST, {}, T_GRANT, ("approval_epoch_mismatch",)),
        (EPOCH, "e" * 64, {}, T_GRANT, ("approval_profile_mismatch",)),
        (EPOCH, DIGEST, {"effect_sha256": "f" * 64}, T_GRANT, ("approval_effect_mismatch",)),
        (EPOCH, DIGEST, {"proposal_sha256": "0" * 64}, T_GRANT, ("approval_proposal_mismatch",)),
        (EPOCH, DIGEST, {}, COMMAND_EXPIRES, ("command_expired",)),
    ],
    ids=["epoch", "profile", "effect", "proposal", "command-expired"],
)
def test_grant_refuses_a_mismatched_approval_with_the_vocabulary_code(
    paths, live_epoch, live_digest, args, now, expected
):
    proposal = entry_proposal()
    command = approve_command(proposal, args=args)
    with pytest.raises(ApprovalRefused) as caught:
        _grant(paths, proposal, command, now=now, epoch=live_epoch, digest=live_digest)
    assert caught.value.blockers == expected
    assert caught.value.reason == expected[0]
    assert isinstance(caught.value, SupervisedRefused)
    assert not _layout(paths).mandate(proposal.effect.intent_id).exists()


def test_grant_refuses_once_the_send_deadline_is_reached(paths):
    # the command is still live (issued 14:01:30) but the proposal's send
    # deadline (14:01:40) has passed: the approval has nothing left to authorize
    deadline = shift_instant(datetime.fromisoformat(COMMAND_DOC["issued_at"]), 10)
    proposal = entry_proposal(send_deadline=deadline.isoformat())
    with pytest.raises(ApprovalRefused) as caught:
        _grant(paths, proposal, now=deadline)
    assert caught.value.blockers == ("approval_expired",)
    assert not _layout(paths).mandate(proposal.effect.intent_id).exists()


def test_grant_refused_while_halted(paths):
    (paths.root / "HALT").write_text("{}")
    with pytest.raises(SupervisedRefused) as caught:
        _grant(paths)
    assert caught.value.reason == "effects_halted"
    assert not _layout(paths).mandates().exists() or not any(_layout(paths).mandates().iterdir())


def test_replayed_approval_is_idempotent_and_consumes_once(paths):
    proposal = entry_proposal()
    command = approve_command(proposal)
    first = _grant(paths, proposal, command)
    replay = _grant(paths, proposal, command, now=shift_instant(T_GRANT, 30))
    assert replay == first  # the replay grants nothing new
    permit = _permit(paths)
    iid = proposal.effect.intent_id
    assert not _layout(paths).mandate(iid).exists()
    assert _layout(paths).mandate_consumed(iid).exists()
    assert paths.permit(permit.permit_id).exists()
    # a second permit for the same approval: the mandate is spent
    with pytest.raises(ApprovalRefused) as caught:
        _permit(paths, now=shift_instant(T_GRANT, 1))
    assert caught.value.blockers == (Blocker.MANDATE_ALREADY_CONSUMED,)
    assert caught.value.reason == "mandate_already_consumed"
    # and re-applying the approval after consumption cannot re-arm it
    with pytest.raises(ApprovalRefused) as again:
        _grant(paths, proposal, command, now=shift_instant(T_GRANT, 2))
    assert again.value.blockers == ("mandate_already_consumed",)
    assert len(list((paths.root / "permits").glob("*.json"))) == 1


def test_a_second_live_approval_for_the_same_intent_is_refused(paths):
    proposal = entry_proposal()
    _grant(paths, proposal)
    other = approve_command(proposal, idempotency_key="0b5d6f2a-1c3e-4a5b-8d7f-9e0a1b2c3d4e")
    with pytest.raises(SupervisedRefused) as caught:
        _grant(paths, proposal, other, now=shift_instant(T_GRANT, 5))
    assert caught.value.reason == "entry_mandate_already_granted"
    kept = SupervisedMandateV2.model_validate_json(
        _layout(paths).mandate(proposal.effect.intent_id).read_bytes()
    )
    assert kept.approval_id == COMMAND_DOC["idempotency_key"]


def test_an_expired_approval_can_be_replaced_by_a_fresh_one(paths):
    proposal = entry_proposal()
    _grant(paths, proposal)
    # 14:07:00 the first approval expired; the proposal lives until 14:10
    fresh_at = T_EXPIRY
    fresh_cmd = approve_command(
        proposal,
        idempotency_key="0b5d6f2a-1c3e-4a5b-8d7f-9e0a1b2c3d4e",
        issued_at=fresh_at.isoformat(),
        expires_at=shift_instant(fresh_at, 60).isoformat(),
    )
    fresh = _grant(paths, proposal, fresh_cmd, now=fresh_at)
    assert fresh.approval_id == "0b5d6f2a-1c3e-4a5b-8d7f-9e0a1b2c3d4e"
    assert fresh.expires_at == SEND_DEADLINE  # min(send_deadline, now + 300)
    permit = _permit(paths, now=shift_instant(fresh_at, 1))
    assert permit.expires_at <= fresh.expires_at


# ----------------------------------------------------------------- permit


def test_permit_binds_the_equity_effect_bytes_and_consumes_the_mandate(paths):
    effect = equity_effect()
    mandate = _grant(paths)
    permit = _permit(paths, effect)
    assert permit.effect_sha256 == hashlib.sha256(effect_v1_bytes(effect)).hexdigest()
    assert permit.effect_sha256 == mandate.effect_sha256
    assert permit.intent_id == effect.intent_id
    assert permit.strategy_version == STRATEGY
    assert permit.owner_epoch == EPOCH
    assert permit.screening_sha256 == SCREENING_SHA
    # the permit never outlives the approval
    assert permit.expires_at == min(mandate.expires_at, SEND_DEADLINE)
    consumed = _layout(paths).mandate_consumed(effect.intent_id)
    assert SupervisedMandateV2.model_validate_json(consumed.read_bytes()) == mandate


@pytest.mark.parametrize(
    ("now", "epoch", "digest", "expected"),
    [
        (T_EXPIRY, EPOCH, DIGEST, ("approval_expired",)),
        (T_GRANT, "owner-restarted-9", DIGEST, ("approval_epoch_mismatch",)),
        (T_GRANT, EPOCH, "e" * 64, ("approval_profile_mismatch",)),
        (T_GRANT, EPOCH, None, ("approval_profile_mismatch",)),
    ],
    ids=["expired", "restart-epoch", "profile", "profile-unknown"],
)
def test_permit_refuses_a_stale_mandate_without_consuming_it(paths, now, epoch, digest, expected):
    _grant(paths)
    with pytest.raises(ApprovalRefused) as caught:
        _permit(paths, now=now, epoch=epoch, digest=digest)
    assert caught.value.blockers == expected
    iid = equity_effect().intent_id
    assert _layout(paths).mandate(iid).exists()
    assert not _layout(paths).mandate_consumed(iid).exists()
    assert not list((paths.root / "permits").glob("*.json"))


def test_permit_refuses_other_effect_bytes_than_the_approved_ones(paths):
    _grant(paths)
    smaller = equity_effect(quantity=1)
    with pytest.raises(ApprovalRefused) as caught:
        _permit(paths, smaller)
    assert caught.value.blockers == ("approval_effect_mismatch",)
    assert _layout(paths).mandate(smaller.intent_id).exists()


def test_permit_refuses_account_and_scope_mismatches(paths):
    _grant(paths)
    with pytest.raises(SupervisedRefused) as account:
        _permit(paths, account_id="DU9999999")
    assert account.value.reason == "mandate_account_mismatch"
    with pytest.raises(SupervisedRefused) as scope:
        _permit(paths, intent=_intent(equity_effect(), source="some-other-strategy/1"))
    assert scope.value.reason == "mandate_scope_mismatch"
    assert _layout(paths).mandate(equity_effect().intent_id).exists()


def test_equity_effect_without_a_per_trade_approval_is_refused(paths):
    # a standing v1 mandate for the same strategy must not authorize equities
    grant_mandate(
        paths,
        now=T_GRANT,
        account_id=ACCOUNT,
        owner_epoch=EPOCH,
        strategy_version=STRATEGY,
        profile_digest=DIGEST,
        max_orders=3,
        ttl_seconds=3600,
        granted_by="operator-terminal",
    )
    with pytest.raises(SupervisedRefused) as caught:
        _permit(paths)
    assert caught.value.reason == "mandate_absent"
    v1 = active_mandate(
        paths, now=T_GRANT, account_id=ACCOUNT, owner_epoch=EPOCH, strategy_version=STRATEGY
    )
    assert v1.orders_used == 0


def test_non_canonical_equity_bytes_are_refused(paths):
    _grant(paths)
    canonical = effect_v1_bytes(equity_effect())
    spaced = canonical.replace(b",", b", ", 1)
    with pytest.raises(SupervisedRefused) as caught:
        _permit(paths, payload=spaced)
    assert caught.value.reason == "effect_invalid"


def test_two_mandates_for_different_intents_coexist(paths):
    first = entry_proposal()
    second_effect = equity_effect(intent_id="eq-spy-002", quantity=1)
    second = entry_proposal(second_effect)
    a = _grant(paths, first)
    b = _grant(
        paths,
        second,
        approve_command(second, idempotency_key="0b5d6f2a-1c3e-4a5b-8d7f-9e0a1b2c3d4e"),
    )
    assert a.intent_id != b.intent_id
    listed = {m["intent_id"]: m["state"] for m in entry_mandates(paths, now=T_GRANT)}
    assert listed == {"eq-spy-001": "granted", "eq-spy-002": "granted"}
    permit_b = _permit(paths, second_effect)
    # spending one leaves the other intact
    assert _layout(paths).mandate("eq-spy-001").exists()
    permit_a = _permit(paths)
    assert permit_a.permit_id != permit_b.permit_id
    listed = {m["intent_id"]: m["state"] for m in entry_mandates(paths, now=T_GRANT)}
    assert listed == {"eq-spy-001": "consumed", "eq-spy-002": "consumed"}


def test_concurrent_permits_for_one_approval_mint_exactly_one(paths):
    _grant(paths)
    outcomes: list[str] = []
    guard = threading.Lock()
    start = threading.Barrier(6)

    def race() -> None:
        start.wait()
        try:
            permit = _permit(paths)
            result = f"permit:{permit.permit_id}"
        except SupervisedRefused as refused:
            result = refused.reason
        with guard:
            outcomes.append(result)

    threads = [threading.Thread(target=race) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    minted = [o for o in outcomes if o.startswith("permit:")]
    assert len(minted) == 1
    assert (
        sorted(o for o in outcomes if not o.startswith("permit:"))
        == ["mandate_already_consumed"] * 5
    )


def test_restart_invalidates_pending_approvals(paths):
    _grant(paths)
    first = entry_proposal(equity_effect(intent_id="eq-spy-003"))
    _grant(
        paths,
        first,
        approve_command(first, idempotency_key="0b5d6f2a-1c3e-4a5b-8d7f-9e0a1b2c3d4e"),
    )
    _permit(paths, equity_effect(intent_id="eq-spy-003"))  # consumed before the restart
    invalidated = invalidate_entry_mandates(paths, now=T_GRANT, owner_epoch="owner-restarted-9")
    assert invalidated == ["eq-spy-001"]
    assert not _layout(paths).mandate("eq-spy-001").exists()
    assert _layout(paths).mandate_consumed("eq-spy-003").exists()  # history stays
    with pytest.raises(SupervisedRefused) as caught:
        _permit(paths, epoch="owner-restarted-9")
    assert caught.value.reason == "mandate_absent"
    # the same epoch invalidates nothing
    assert invalidate_entry_mandates(paths, now=T_GRANT, owner_epoch="owner-restarted-9") == []


def test_retire_entry_mandate_is_the_public_retirement(paths):
    """W2c item 6: ``retire_entry_mandate`` moves one unconsumed per-trade
    mandate out of the way under the state lock (the retirement record
    names the reason) and returns it; a mandate of another proposal, an
    absent one and a consumed one are left alone (None)."""
    from tree_options.trex.supervised import retire_entry_mandate

    proposal = entry_proposal()
    granted = _grant(paths, proposal)
    layout = _layout(paths)
    assert (
        retire_entry_mandate(paths, "eq-spy-001", now=T_GRANT, reason="x", proposal_id="0" * 64)
        is None
    )
    assert layout.mandate("eq-spy-001").exists(), "another proposal's approval stays"
    retired = retire_entry_mandate(
        paths, "eq-spy-001", now=T_GRANT, reason="rejected", proposal_id=proposal.proposal_id
    )
    assert retired == granted
    assert not layout.mandate("eq-spy-001").exists()
    assert paths.entry_mandate_retired("eq-spy-001", granted.approval_id).exists()
    record = paths.entry_mandate_retirements().read_text().splitlines()[-1]
    assert '"reason": "rejected"' in record or '"reason":"rejected"' in record
    assert retire_entry_mandate(paths, "eq-spy-001", now=T_GRANT, reason="again") is None
    with pytest.raises(SupervisedRefused) as caught:
        _permit(paths)
    assert caught.value.reason == "mandate_absent", "a retired approval can never mint"
    # a consumed mandate is history, never retired
    other = entry_proposal(equity_effect(intent_id="eq-spy-004"))
    _grant(
        paths, other, approve_command(other, idempotency_key="1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f")
    )
    _permit(paths, equity_effect(intent_id="eq-spy-004"))
    assert retire_entry_mandate(paths, "eq-spy-004", now=T_GRANT, reason="late") is None
    assert layout.mandate_consumed("eq-spy-004").exists()
    with pytest.raises(SupervisedRefused) as unsafe:
        retire_entry_mandate(paths, "../escape", now=T_GRANT, reason="x")
    assert unsafe.value.reason == "intent_id_not_path_safe"


def test_the_owner_flows_use_no_private_supervised_helpers():
    """W2c item 6: the reject flow retires through the public function, not
    the supervised core's private lock and retirement."""
    import ast
    from pathlib import Path

    import tree_options.trex.desk_entries as desk_entries

    tree = ast.parse(Path(desk_entries.__file__).read_text())
    private = sorted(
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "supervised_core"
        and node.attr.startswith("_")
    )
    assert private == []


# ------------------------------------------------------------------- send


def test_equity_send_hands_the_broker_the_permitted_bytes(paths):
    effect = equity_effect()
    _grant(paths)
    record_intent(paths, _intent(effect))
    permit = _permit(paths, effect)
    broker = RecordingBroker()
    with pytest.raises(SupervisedRefused) as tampered:
        send(
            paths,
            now=shift_instant(T_GRANT, 1),
            permit_id=permit.permit_id,
            effect_payload=effect_v1_bytes(equity_effect(quantity=1)),
            broker=broker,
        )
    assert tampered.value.reason == "effect_hash_mismatch"
    receipt = send(
        paths,
        now=shift_instant(T_GRANT, 2),
        permit_id=permit.permit_id,
        effect_payload=effect_v1_bytes(effect),
        broker=broker,
    )
    assert receipt["outcome"] == "acknowledged"
    assert broker.payloads == [effect_v1_bytes(effect)]
    assert paths.permit_consumed(permit.permit_id).exists()


def test_send_refuses_a_per_trade_permit_whose_mandate_vanished(paths):
    effect = equity_effect()
    _grant(paths)
    record_intent(paths, _intent(effect))
    permit = _permit(paths, effect)
    _layout(paths).mandate_consumed(effect.intent_id).unlink()
    with pytest.raises(SupervisedRefused) as caught:
        send(
            paths,
            now=shift_instant(T_GRANT, 1),
            permit_id=permit.permit_id,
            effect_payload=effect_v1_bytes(effect),
            broker=RecordingBroker(),
        )
    assert caught.value.reason == "mandate_absent"
    assert paths.pending(effect.intent_id).exists()


def test_send_of_a_per_trade_permit_stops_on_halt(paths):
    effect = equity_effect()
    _grant(paths)
    record_intent(paths, _intent(effect))
    permit = _permit(paths, effect)
    (paths.root / "HALT").write_text("{}")
    broker = RecordingBroker()
    with pytest.raises(SupervisedRefused) as caught:
        send(
            paths,
            now=shift_instant(T_GRANT, 1),
            permit_id=permit.permit_id,
            effect_payload=effect_v1_bytes(effect),
            broker=broker,
        )
    assert caught.value.reason == "effects_halted"
    assert broker.payloads == []


def test_send_expires_with_the_approval_not_the_intent_deadline(paths):
    effect = equity_effect()
    mandate = _grant(paths)
    record_intent(paths, _intent(effect))
    permit = _permit(paths, effect, now=shift_instant(T_GRANT, 200))
    assert permit.expires_at == mandate.expires_at
    with pytest.raises(SupervisedRefused) as caught:
        send(
            paths,
            now=mandate.expires_at,
            permit_id=permit.permit_id,
            effect_payload=effect_v1_bytes(effect),
            broker=RecordingBroker(),
        )
    assert caught.value.reason == "permit_expired"


# ------------------------------------------------- option effect, v1 path


def _option_proposal() -> EntryProposal:
    doc = contract_fixture("option_effect_canary.json")
    return entry_proposal(SupervisedEffect.model_validate(doc))


def test_option_effect_uses_a_per_trade_mandate_when_one_exists(paths):
    proposal = _option_proposal()
    effect = proposal.effect
    assert isinstance(effect, SupervisedEffect)
    granted_at = shift_instant(proposal.proposed_at, 60)
    command = approve_command(
        proposal,
        issued_at=granted_at.isoformat(),
        expires_at=shift_instant(granted_at, 120).isoformat(),
    )
    mandate = _grant(paths, proposal, command, now=granted_at)
    intent = _intent(
        effect,
        source=proposal.strategy_version,
        created_at=granted_at,
        send_deadline=proposal.send_deadline,
    )
    permit = _permit(paths, effect, now=granted_at, intent=intent)
    assert permit.effect_sha256 == mandate.effect_sha256
    assert _layout(paths).mandate_consumed(effect.intent_id).exists()
    assert not paths.mandate().exists()  # no v1 mandate was needed or touched


def test_v1_canary_flow_is_unchanged_and_binds_the_golden_bytes(paths):
    """The canary-001 option path: v1 mandate, permit, send; no mandate/2."""
    effect = SupervisedEffect.model_validate(contract_fixture("option_effect_canary.json"))
    payload = effect_v1_bytes(effect)
    # golden (tests/contracts): 636 bytes, sha256 9beed250...
    assert len(payload) == 636
    golden = "9beed2502c1d3c0950ed4adb94dac3881430ece3df8a464fb12007291cee13ea"
    assert hashlib.sha256(payload).hexdigest() == golden
    t0 = datetime.fromisoformat("2026-10-14T13:55:00+00:00")
    grant_mandate(
        paths,
        now=t0,
        account_id=effect.account_id,
        owner_epoch=EPOCH,
        strategy_version="operational-canary/1",
        profile_digest=DIGEST,
        max_orders=1,
        ttl_seconds=3600,
        granted_by="operator-terminal",
    )
    intent = _intent(
        effect,
        source="operational-canary/1",
        created_at=t0,
        send_deadline=shift_instant(t0, 300),
    )
    record_intent(paths, intent)
    permit = issue_permit(
        paths,
        now=t0,
        account_id=effect.account_id,
        owner_epoch=EPOCH,
        intent=intent,
        canary_blockers=(),
        effect_payload=payload,
        screening_sha256=SCREENING_SHA,
    )
    assert permit.effect_sha256 == golden
    spent = active_mandate(
        paths,
        now=t0,
        account_id=effect.account_id,
        owner_epoch=EPOCH,
        strategy_version="operational-canary/1",
    )
    assert spent.orders_used == 1
    assert permit.mandate_id == spent.mandate_id
    broker = RecordingBroker()
    receipt = send(paths, now=t0, permit_id=permit.permit_id, effect_payload=payload, broker=broker)
    assert receipt["outcome"] == "acknowledged"
    assert broker.payloads == [payload]
    assert not _layout(paths).mandates().exists()
