"""Supervised paper path: authority chain, outbox durability, reconcile rules.

Oracles are independent of the implementation: the projection assertions
come from the execution package's own lifecycle semantics exercised via
the deterministic PaperBroker, and every refusal is pinned to its
machine-readable reason string.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tree_options.execution import (
    ExecutionLifecycle,
    ExecutionState,
    OrderIntent,
    OrderReject,
)
from tree_options.execution.paper import PaperBroker, PaperQuote
from tree_options.time.sessions import shift_instant
from tree_options.trex.supervised import (
    Acknowledged,
    LookupUnknown,
    NotSubmitted,
    Refused,
    Submitted,
    SupervisedIntent,
    SupervisedPaths,
    SupervisedRefused,
    active_mandate,
    grant_mandate,
    issue_permit,
    project_intent,
    reconcile_intent,
    record_intent,
    recover,
    revoke_mandate,
    send,
    status,
)

T0 = datetime(2026, 9, 28, 14, 30, tzinfo=UTC)
ACCOUNT = "DU1234567"
OWNER = "gateway-epoch-1"
STRATEGY = "operational-canary/1"
PACKAGE_SHA = "a" * 64
SCREENING_SHA = "b" * 64
EFFECT = b'{"order":"BUY 1 VERTICAL LIMIT 1.25"}'


def _order_intent(intent_id: str = "sup-001", *, source: str = STRATEGY) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        contract_id="O:XYZ270116C00100000",
        side="BUY",
        position_effect="OPEN_LONG",
        quantity=1,
        order_type="LIMIT",
        limit_price=Decimal("1.25"),
        execution_style="single",
        intent_created_at=T0,
        source=source,
        source_sequence_id=f"seq-{intent_id}",
    )


def _sup_intent(intent_id: str = "sup-001", *, sha: str = PACKAGE_SHA,
                source: str = STRATEGY) -> SupervisedIntent:
    return SupervisedIntent(
        intent=_order_intent(intent_id, source=source),
        package_intent_sha256=sha,
        created_at=T0,
        send_deadline=shift_instant(T0, 120),
    )


@pytest.fixture()
def paths(tmp_path):
    supervised = SupervisedPaths(tmp_path / "supervised")
    supervised.prepare()
    return supervised


@pytest.fixture()
def mandate(paths):
    return grant_mandate(
        paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
        strategy_version=STRATEGY, profile_digest="c" * 64,
        max_orders=2, ttl_seconds=3600, granted_by="operator-terminal")


class PaperPortBroker:
    """Bridges the deterministic PaperBroker to the SupervisedBroker port."""

    def __init__(self, quote: PaperQuote | None = None) -> None:
        self._quote = quote or PaperQuote(bid=Decimal("1.10"), ask=Decimal("1.40"))
        self._paper: PaperBroker | None = None
        self.submitted: list[object] = []

    def submit(self, attempt):
        self.submitted.append(attempt)
        self._paper = PaperBroker(intent=_order_intent(attempt.intent_id),
                                  quote=self._quote)
        ack = self._paper.acknowledge(attempt)
        return Acknowledged(ack, self._paper.fills() + self._paper.readbacks())

    def lookup(self, intent_id: str):
        if self._paper is None or intent_id != self._paper.intent.intent_id:
            return NotSubmitted()
        return Submitted(f"paper-order-{intent_id}")


class ExplodingBroker:
    def submit(self, attempt):
        raise ConnectionResetError("gateway vanished mid-submit")

    def lookup(self, intent_id: str):
        return LookupUnknown("gateway down")


# ------------------------------------------------------------- mandate


def test_mandate_grant_and_active_roundtrip(paths):
    granted = grant_mandate(
        paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
        strategy_version=STRATEGY, profile_digest="c" * 64,
        max_orders=1, ttl_seconds=3600, granted_by="operator-terminal")
    assert (paths.root / "mandate.json").exists()
    loaded = active_mandate(paths, now=shift_instant(T0, 60), account_id=ACCOUNT,
                            owner_epoch=OWNER, strategy_version=STRATEGY)
    assert loaded.mandate_id == granted.mandate_id
    assert loaded.expires_at == shift_instant(T0, 3600)


def test_second_grant_while_active_refused(paths, mandate):
    with pytest.raises(SupervisedRefused) as caught:
        grant_mandate(paths, now=shift_instant(T0, 60), account_id=ACCOUNT,
                      owner_epoch=OWNER, strategy_version=STRATEGY,
                      profile_digest="c" * 64, max_orders=1,
                      ttl_seconds=3600, granted_by="operator-terminal")
    assert caught.value.reason == "mandate_already_active"


def test_expired_mandate_is_replaceable(paths):
    grant_mandate(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                  strategy_version=STRATEGY, profile_digest="c" * 64,
                  max_orders=1, ttl_seconds=60, granted_by="operator-terminal")
    later = shift_instant(T0, 61)
    fresh = grant_mandate(paths, now=later, account_id=ACCOUNT, owner_epoch=OWNER,
                          strategy_version=STRATEGY, profile_digest="d" * 64,
                          max_orders=1, ttl_seconds=3600, granted_by="operator-terminal")
    assert fresh.granted_at == later
    assert list(paths.root.glob("mandate.expired-*.json")), "archive must remain"


@pytest.mark.parametrize("ttl", [59, 8 * 60 * 60 + 1])
def test_mandate_ttl_bounds(paths, ttl):
    with pytest.raises(SupervisedRefused) as caught:
        grant_mandate(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                      strategy_version=STRATEGY, profile_digest="c" * 64,
                      max_orders=1, ttl_seconds=ttl, granted_by="operator-terminal")
    assert caught.value.reason == "mandate_ttl_out_of_bounds"


@pytest.mark.parametrize("kwargs, reason", [
    ({"account_id": "OTHER"}, "mandate_account_mismatch"),
    ({"owner_epoch": "gateway-epoch-2"}, "mandate_owner_mismatch"),
    ({"strategy_version": "other-strategy/1"}, "mandate_scope_mismatch"),
])
def test_active_mandate_mismatches(paths, mandate, kwargs, reason):
    base = {"account_id": ACCOUNT, "owner_epoch": OWNER, "strategy_version": STRATEGY}
    base.update(kwargs)
    with pytest.raises(SupervisedRefused) as caught:
        active_mandate(paths, now=T0, **base)
    assert caught.value.reason == reason


def test_active_mandate_absent(paths):
    with pytest.raises(SupervisedRefused) as caught:
        active_mandate(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                       strategy_version=STRATEGY)
    assert caught.value.reason == "mandate_absent"


def test_active_mandate_expired(paths):
    grant_mandate(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                  strategy_version=STRATEGY, profile_digest="c" * 64,
                  max_orders=1, ttl_seconds=60, granted_by="operator-terminal")
    with pytest.raises(SupervisedRefused) as caught:
        active_mandate(paths, now=shift_instant(T0, 61), account_id=ACCOUNT,
                       owner_epoch=OWNER, strategy_version=STRATEGY)
    assert caught.value.reason == "mandate_expired"


def test_revoke_tombstone_blocks_forever(paths, mandate):
    revoke_mandate(paths, now=T0, reason="operator halt")
    assert not paths.mandate().exists()
    assert paths.mandate_revoked().exists()
    with pytest.raises(SupervisedRefused) as caught:
        active_mandate(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                       strategy_version=STRATEGY)
    assert caught.value.reason == "mandate_revoked"
    with pytest.raises(SupervisedRefused) as caught:
        grant_mandate(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                      strategy_version=STRATEGY, profile_digest="c" * 64,
                      max_orders=1, ttl_seconds=3600, granted_by="operator-terminal")
    assert caught.value.reason == "mandate_revoked_permanent"


# -------------------------------------------------------------- intent


def test_intent_record_is_idempotent_for_identical_bytes(paths):
    first = record_intent(paths, _sup_intent())
    second = record_intent(paths, _sup_intent())
    assert first == second


def test_intent_id_collision_refused(paths):
    record_intent(paths, _sup_intent())
    other = SupervisedIntent(intent=_order_intent(), package_intent_sha256=PACKAGE_SHA,
                             created_at=T0, send_deadline=shift_instant(T0, 999))
    with pytest.raises(SupervisedRefused) as caught:
        record_intent(paths, other)
    assert caught.value.reason == "intent_id_collision"


def test_intent_deadline_must_follow_creation():
    with pytest.raises(ValueError):
        SupervisedIntent(intent=_order_intent(), package_intent_sha256=PACKAGE_SHA,
                         created_at=T0, send_deadline=T0)


def test_second_intent_same_package_refused_while_in_flight(paths):
    record_intent(paths, _sup_intent("sup-001"))
    with pytest.raises(SupervisedRefused) as caught:
        record_intent(paths, _sup_intent("sup-002"))
    assert caught.value.reason == "package_already_in_flight"


# -------------------------------------------------------------- permit


def test_permit_issue_spends_mandate_budget(paths, mandate):
    permit = issue_permit(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                          intent=_sup_intent(), canary_blockers=(),
                          effect_payload=EFFECT, screening_sha256=SCREENING_SHA)
    assert paths.permit(permit.permit_id).exists()
    spent = active_mandate(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                           strategy_version=STRATEGY)
    assert spent.orders_used == mandate.orders_used + 1


def test_permit_refused_on_canary_blockers(paths, mandate):
    with pytest.raises(SupervisedRefused) as caught:
        issue_permit(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                     intent=_sup_intent(), canary_blockers=("paper_account_mismatch_or_unverified",
                                                            "legacy_book_not_flat"),
                     effect_payload=EFFECT, screening_sha256=SCREENING_SHA)
    assert caught.value.reason == "canary_blockers"
    assert "legacy_book_not_flat" in caught.value.detail
    spent = active_mandate(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                           strategy_version=STRATEGY)
    assert spent.orders_used == mandate.orders_used


def test_permit_refused_past_send_deadline(paths, mandate):
    with pytest.raises(SupervisedRefused) as caught:
        issue_permit(paths, now=shift_instant(T0, 121), account_id=ACCOUNT,
                     owner_epoch=OWNER, intent=_sup_intent(),
                     canary_blockers=(), effect_payload=EFFECT,
                     screening_sha256=SCREENING_SHA)
    assert caught.value.reason == "intent_deadline_passed"


def test_permit_double_issue_refused(paths, mandate):
    record_intent(paths, _sup_intent())
    issue_permit(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                 intent=_sup_intent(), canary_blockers=(), effect_payload=EFFECT,
                 screening_sha256=SCREENING_SHA)
    with pytest.raises(SupervisedRefused) as caught:
        issue_permit(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                     intent=_sup_intent(), canary_blockers=(), effect_payload=EFFECT,
                     screening_sha256=SCREENING_SHA)
    assert caught.value.reason == "permit_already_issued"


def test_permit_budget_exhausted(paths):
    grant_mandate(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                  strategy_version=STRATEGY, profile_digest="c" * 64,
                  max_orders=1, ttl_seconds=3600, granted_by="operator-terminal")
    record_intent(paths, _sup_intent())
    issue_permit(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                 intent=_sup_intent(), canary_blockers=(), effect_payload=EFFECT,
                 screening_sha256=SCREENING_SHA)
    record_intent(paths, _sup_intent("sup-002", sha="e" * 64))
    with pytest.raises(SupervisedRefused) as caught:
        issue_permit(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                     intent=_sup_intent("sup-002", sha="e" * 64), canary_blockers=(),
                     effect_payload=EFFECT, screening_sha256=SCREENING_SHA)
    assert caught.value.reason == "mandate_budget_exhausted"


# ----------------------------------------------------------------- send


def _armed(paths, intent_id: str = "sup-001", sha: str = PACKAGE_SHA):
    record_intent(paths, _sup_intent(intent_id, sha=sha))
    permit = issue_permit(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                          intent=_sup_intent(intent_id, sha=sha), canary_blockers=(),
                          effect_payload=EFFECT, screening_sha256=SCREENING_SHA)
    return permit


def test_send_happy_path_projects_to_filled(paths, mandate):
    permit = _armed(paths)
    receipt = send(paths, now=shift_instant(T0, 5), permit_id=permit.permit_id,
                   effect_payload=EFFECT, broker=PaperPortBroker())
    assert receipt["outcome"] == "acknowledged"
    assert receipt["broker_order_id"] == "paper-order-sup-001"
    assert not paths.pending("sup-001").exists()
    assert not paths.sending("sup-001").exists()
    terminal = paths.terminal("sup-001").read_bytes()
    assert b"acknowledged" in terminal
    # Independent oracle: the journal folds to FILLED through the pure
    # lifecycle projection, exactly as the deterministic PaperBroker builds it.
    lifecycle = project_intent(paths, "sup-001")
    assert lifecycle.state == ExecutionState.FILLED
    assert isinstance(lifecycle, ExecutionLifecycle)
    assert lifecycle.filled_quantity == 1
    assert not paths.permit(permit.permit_id).exists()
    assert paths.permit_consumed(permit.permit_id).exists()


def test_send_refuses_wrong_effect_bytes(paths, mandate):
    permit = _armed(paths)
    with pytest.raises(SupervisedRefused) as caught:
        send(paths, now=T0, permit_id=permit.permit_id,
             effect_payload=b'{"order":"tampered"}', broker=PaperPortBroker())
    assert caught.value.reason == "effect_hash_mismatch"
    assert paths.pending("sup-001").exists()


def test_send_refuses_consumed_permit(paths, mandate):
    permit = _armed(paths)
    send(paths, now=T0, permit_id=permit.permit_id, effect_payload=EFFECT,
         broker=PaperPortBroker())
    with pytest.raises(SupervisedRefused) as caught:
        send(paths, now=T0, permit_id=permit.permit_id, effect_payload=EFFECT,
             broker=PaperPortBroker())
    assert caught.value.reason == "permit_consumed"


def test_send_refused_when_mandate_expired(paths):
    grant_mandate(paths, now=T0, account_id=ACCOUNT, owner_epoch=OWNER,
                  strategy_version=STRATEGY, profile_digest="c" * 64,
                  max_orders=1, ttl_seconds=60, granted_by="operator-terminal")
    permit = _armed(paths)
    # +120s: the 60s mandate is expired while the 15-minute permit is not;
    # the send boundary must re-check the mandate, not trust the permit.
    with pytest.raises(SupervisedRefused) as caught:
        send(paths, now=shift_instant(T0, 120), permit_id=permit.permit_id,
             effect_payload=EFFECT, broker=PaperPortBroker())
    assert caught.value.reason == "mandate_expired"
    # Nothing was claimed: the intent is still pending, untouched.
    assert paths.pending("sup-001").exists()


# ---------------------------------------------------- uncertainty + reconcile


def test_uncertain_send_preserves_and_blocks_retry(paths, mandate):
    permit = _armed(paths)
    receipt = send(paths, now=T0, permit_id=permit.permit_id,
                   effect_payload=EFFECT, broker=ExplodingBroker())
    assert receipt["outcome"] == "uncertain"
    assert receipt["reason"] == "broker_transport_error"
    with pytest.raises(SupervisedRefused) as caught:
        send(paths, now=shift_instant(T0, 2), permit_id=permit.permit_id,
             effect_payload=EFFECT, broker=PaperPortBroker())
    assert caught.value.reason == "permit_consumed"
    with pytest.raises(SupervisedRefused) as caught:
        record_intent(paths, _sup_intent("sup-002"))
    assert caught.value.reason == "package_already_in_flight"
    lifecycle = project_intent(paths, "sup-001")
    assert lifecycle.state in (ExecutionState.UNKNOWN,
                               ExecutionState.RECONCILIATION_REQUIRED)


def test_reconcile_not_submitted_clears_the_package(paths, mandate):
    permit = _armed(paths)
    send(paths, now=T0, permit_id=permit.permit_id, effect_payload=EFFECT,
         broker=ExplodingBroker())
    verdict = reconcile_intent(paths, now=shift_instant(T0, 30), intent_id="sup-001",
                               broker=PaperPortBroker())
    assert verdict["verdict"] == "confirmed_not_submitted"
    # A fresh intent for the same package is admissible again.
    record_intent(paths, _sup_intent("sup-002"))
    # Re-reconciling with the same verdict is idempotent.
    again = reconcile_intent(paths, now=shift_instant(T0, 60), intent_id="sup-001",
                             broker=PaperPortBroker())
    assert again["verdict"] == "confirmed_not_submitted"


def test_reconcile_still_unknown_keeps_package_held(paths, mandate):
    permit = _armed(paths)
    send(paths, now=T0, permit_id=permit.permit_id, effect_payload=EFFECT,
         broker=ExplodingBroker())
    verdict = reconcile_intent(paths, now=shift_instant(T0, 30), intent_id="sup-001",
                               broker=ExplodingBroker())
    assert verdict["verdict"] == "still_uncertain"
    with pytest.raises(SupervisedRefused) as caught:
        record_intent(paths, _sup_intent("sup-002"))
    assert caught.value.reason == "package_already_in_flight"


def test_reconcile_conflicting_verdict_refused(paths, mandate):
    permit = _armed(paths)
    send(paths, now=T0, permit_id=permit.permit_id, effect_payload=EFFECT,
         broker=ExplodingBroker())
    reconcile_intent(paths, now=shift_instant(T0, 30), intent_id="sup-001",
                     broker=ExplodingBroker())
    with pytest.raises(SupervisedRefused) as caught:
        reconcile_intent(paths, now=shift_instant(T0, 60), intent_id="sup-001",
                         broker=PaperPortBroker())
    assert caught.value.reason == "reconcile_verdict_conflict"


def test_reconcile_refuses_non_uncertain_intent(paths, mandate):
    permit = _armed(paths)
    send(paths, now=T0, permit_id=permit.permit_id, effect_payload=EFFECT,
         broker=PaperPortBroker())
    with pytest.raises(SupervisedRefused) as caught:
        reconcile_intent(paths, now=shift_instant(T0, 30), intent_id="sup-001",
                         broker=PaperPortBroker())
    assert caught.value.reason == "intent_not_uncertain"


def test_rejected_send_allows_new_intent_for_package(paths, mandate):
    class RejectingBroker:
        def submit(self, attempt):
            return Refused(OrderReject(
                record_id=f"rej-{attempt.record_id}", intent_id=attempt.intent_id,
                reason_code="INSUFFICIENT_MARGIN",
                broker_acknowledged_at=shift_instant(attempt.send_attempt_at, 1),
                locally_received_at=shift_instant(attempt.send_attempt_at, 2),
                source="supervised-test", source_sequence_id=f"rej-{attempt.record_id}",
                broker_sequence_id=f"rej-{attempt.record_id}"))

        def lookup(self, intent_id):
            return LookupUnknown("never submitted")

    permit = _armed(paths)
    receipt = send(paths, now=T0, permit_id=permit.permit_id, effect_payload=EFFECT,
                   broker=RejectingBroker())
    assert receipt["outcome"] == "rejected"
    assert receipt["reason_code"] == "INSUFFICIENT_MARGIN"
    record_intent(paths, _sup_intent("sup-002"))


# ---------------------------------------------------------------- recover


def test_recover_reclassifies_orphan_sending(paths):
    intent = _sup_intent()
    record_intent(paths, intent)
    # Simulate the real crash window: the pending file was atomically
    # renamed to its claim content but no terminal was ever written.
    claim = intent.model_dump(mode="json", by_alias=True)
    paths.sending(intent.intent.intent_id).write_text(json.dumps(claim))
    paths.pending(intent.intent.intent_id).unlink()
    recovered = recover(paths, now=T0)
    assert recovered == ["sup-001"]
    terminal = json.loads(paths.terminal("sup-001").read_bytes())
    assert terminal["reason"] == "sending_orphan_recovered"
    assert terminal["package_intent_sha256"] == PACKAGE_SHA
    # The preserved package sha keeps the package in flight after recovery.
    with pytest.raises(SupervisedRefused) as caught:
        record_intent(paths, _sup_intent("sup-002"))
    assert caught.value.reason == "package_already_in_flight"
    assert recover(paths, now=shift_instant(T0, 1)) == []


def test_recover_drops_sending_when_terminal_exists(paths, mandate):
    permit = _armed(paths)
    send(paths, now=T0, permit_id=permit.permit_id, effect_payload=EFFECT,
         broker=PaperPortBroker())
    paths.sending("sup-001").write_text('{"late": true}')
    assert recover(paths, now=shift_instant(T0, 1)) == []
    assert not paths.sending("sup-001").exists()


# ---------------------------------------------------------------- status


def test_status_reports_mandate_and_outbox(paths, mandate):
    permit = _armed(paths)
    send(paths, now=T0, permit_id=permit.permit_id, effect_payload=EFFECT,
         broker=PaperPortBroker())
    report = status(paths, now=shift_instant(T0, 1))
    assert report["mandate"]["state"] == "active"
    assert report["mandate"]["orders_used"] == 1
    entry = next(e for e in report["outbox"] if e["intent_id"] == "sup-001")
    assert entry["state"] == "receipt"
    assert entry["outcome"] == "acknowledged"
    assert entry["execution_state"] == ExecutionState.FILLED.value
