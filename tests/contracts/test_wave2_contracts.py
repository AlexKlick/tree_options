"""Wave 2 frozen contracts: fixture round-trips, canonical hashing, validators.

Oracles are independent of the implementation where it matters: canonical
bytes are re-derived with stdlib ``json.dumps``; the option golden hash is
a literal computed once (2026-10-21) by the EXISTING ``effect_bytes`` over
the canary-001 effect with a synthetic account id; the blocker wire strings
are a literal list.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import tree_options.trex.contracts as c
from tree_options.trex.desk_spec import DeskSpec
from tree_options.trex.supervised_ibkr import SupervisedEffect, effect_bytes

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[2]

#: sha256(effect_bytes(canary-001 effect with account DU0000001)), pinned.
OPTION_GOLDEN_SHA256 = "9beed2502c1d3c0950ed4adb94dac3881430ece3df8a464fb12007291cee13ea"
OPTION_GOLDEN_LEN = 636


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def stdlib_canonical(document: Any) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def reorder(value: Any) -> Any:
    """The same document with every object's keys in reverse order."""
    if isinstance(value, dict):
        return {k: reorder(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [reorder(v) for v in value]
    return value


def put(document: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    out = copy.deepcopy(document)
    *parents, leaf = path.split(".")
    node = out
    for key in parents:
        node = node[key]
    node[leaf] = value
    return out


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def body_of(proposal_doc: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in proposal_doc.items() if k != "proposal_id"}


def mint(body: dict[str, Any]) -> c.EntryProposal:
    return c.EntryProposal.create(c.EntryProposalBody.model_validate(body))


# ---------------------------------------------------------------- fixtures

ROUND_TRIPS: list[tuple[str, Callable[[dict[str, Any]], Any], str | None]] = [
    ("option_effect_canary.json", SupervisedEffect.model_validate, "supervised-effect/1"),
    ("equity_effect.json", c.SupervisedEquityEffect.model_validate, c.EQUITY_EFFECT_SCHEMA),
    ("proposal_equity.json", c.EntryProposal.model_validate, c.PROPOSAL_SCHEMA),
    ("proposal_option.json", c.EntryProposal.model_validate, c.PROPOSAL_SCHEMA),
    ("command_approve_entry.json", c.parse_command, c.COMMAND_SCHEMA),
    ("command_pause_entries.json", c.parse_command, c.COMMAND_SCHEMA),
    ("command_result_applied.json", c.CommandResult.model_validate, c.COMMAND_RESULT_SCHEMA),
    ("mandate_v2.json", c.SupervisedMandateV2.model_validate, c.MANDATE_V2_SCHEMA),
    ("broker_execution.json", c.BrokerExecution.model_validate, None),
    ("broker_portfolio_item.json", c.BrokerPortfolioItem.model_validate, None),
    ("ledger_event_fill.json", c.LedgerEvent.model_validate, c.LEDGER_EVENT_SCHEMA),
    ("holding.json", c.Holding.model_validate, None),
    ("risk_snapshot.json", c.RiskSnapshot.model_validate, c.RISK_SNAPSHOT_SCHEMA),
    ("reconciliation_required.json", c.Reconciliation.model_validate, c.RECONCILIATION_SCHEMA),
    ("desk_spec_v2_equity.json", c.parse_desk_spec, c.DESK_SPEC_V2_SCHEMA),
    ("desk_spec_v2_option.json", c.parse_desk_spec, c.DESK_SPEC_V2_SCHEMA),
    ("desk_spec_v1_option.json", c.parse_desk_spec, "trex-desk-spec/1"),
]


@pytest.mark.parametrize(("name", "parse", "schema"), ROUND_TRIPS, ids=[r[0] for r in ROUND_TRIPS])
def test_fixture_round_trips(
    name: str, parse: Callable[[dict[str, Any]], Any], schema: str | None
) -> None:
    document = load(name)
    model = parse(document)
    assert c.model_document(model) == document
    assert parse(c.model_document(model)) == model
    assert document.get("schema") == schema


def test_every_fixture_is_covered() -> None:
    assert {p.name for p in FIXTURES.glob("*.json")} == {r[0] for r in ROUND_TRIPS}


EXTRA_FIELD_CASES = [
    ("equity_effect.json", c.SupervisedEquityEffect.model_validate, ""),
    ("equity_effect.json", c.SupervisedEquityEffect.model_validate, "exit_policy"),
    ("proposal_equity.json", c.EntryProposal.model_validate, ""),
    ("proposal_equity.json", c.EntryProposal.model_validate, "review.quote"),
    ("command_approve_entry.json", c.parse_command, ""),
    ("command_approve_entry.json", c.parse_command, "args"),
    ("command_approve_entry.json", c.parse_command, "issued_by"),
    ("command_result_applied.json", c.CommandResult.model_validate, ""),
    ("mandate_v2.json", c.SupervisedMandateV2.model_validate, ""),
    ("broker_execution.json", c.BrokerExecution.model_validate, ""),
    ("ledger_event_fill.json", c.LedgerEvent.model_validate, "attribution"),
    ("holding.json", c.Holding.model_validate, ""),
    ("risk_snapshot.json", c.RiskSnapshot.model_validate, ""),
    ("reconciliation_required.json", c.Reconciliation.model_validate, ""),
    ("desk_spec_v2_equity.json", c.parse_desk_spec, "position"),
]


@pytest.mark.parametrize(("name", "parse", "where"), EXTRA_FIELD_CASES)
def test_extra_fields_refused(
    name: str, parse: Callable[[dict[str, Any]], Any], where: str
) -> None:
    document = load(name)
    target = put(document, f"{where}.unexpected" if where else "unexpected", 1)
    with pytest.raises(ValidationError):
        parse(target)


# ------------------------------------------------------- canonical bytes


def test_effect_bytes_are_sorted_compact_json() -> None:
    document = load("equity_effect.json")
    effect = c.SupervisedEquityEffect.model_validate(document)
    assert c.effect_v1_bytes(effect) == stdlib_canonical(document)
    assert c.effect_v1_sha256(effect) == hashlib.sha256(stdlib_canonical(document)).hexdigest()


def test_canonical_bytes_independent_of_key_order() -> None:
    document = load("equity_effect.json")
    shuffled = c.SupervisedEquityEffect.model_validate(reorder(document))
    assert list(reorder(document)) != list(document)
    assert c.effect_v1_bytes(shuffled) == stdlib_canonical(document)
    proposal_doc = load("proposal_equity.json")
    assert (
        c.EntryProposal.model_validate(reorder(proposal_doc)).proposal_id
        == (proposal_doc["proposal_id"])
    )


def test_proposal_id_independent_of_instant_spelling() -> None:
    document = load("proposal_equity.json")
    # the same instants written with the New York offset
    respelled = put(document, "proposed_at", "2026-10-22T10:00:00-04:00")
    respelled = put(respelled, "send_deadline", "2026-10-22T10:10:00-04:00")
    respelled = put(respelled, "review.quote.as_of", "2026-10-22T09:59:58-04:00")
    proposal = c.EntryProposal.model_validate(respelled)
    assert proposal.proposal_id == document["proposal_id"]
    assert c.model_document(proposal) == document


def test_proposal_hashes_match_stdlib_oracle() -> None:
    document = load("proposal_equity.json")
    proposal = c.EntryProposal.model_validate(document)
    assert (
        document["proposal_id"] == hashlib.sha256(stdlib_canonical(body_of(document))).hexdigest()
    )
    assert c.proposal_bytes(proposal) == stdlib_canonical(document)
    assert c.proposal_sha256(proposal) == hashlib.sha256(stdlib_canonical(document)).hexdigest()


def test_decode_refuses_non_canonical_encoding() -> None:
    document = load("equity_effect.json")
    effect = c.decode_effect_v1(stdlib_canonical(document))
    assert isinstance(effect, c.SupervisedEquityEffect)
    with pytest.raises(ValueError, match="canonical"):
        c.decode_effect_v1(json.dumps(document, indent=2).encode())


EFFECT_ECONOMIC_CHANGES = [
    ("quantity", 3),
    ("limit_cents", 67151),
    ("symbol", "QQQ"),
    ("con_id", 320227571),
    ("account_id", "DU0000002"),
    ("exit_policy.stop_price_cents", 66400),
    ("exit_policy.target_price_cents", 68100),
    ("exit_policy.target_price_cents", None),
    ("exit_policy.exit_deadline_session", "2026-10-26"),
    ("exit_policy.stop_confirm_ticks", 4),
]


@pytest.mark.parametrize(("path", "value"), EFFECT_ECONOMIC_CHANGES)
def test_effect_sha_changes_with_each_economic_field(path: str, value: Any) -> None:
    document = load("equity_effect.json")
    base = c.effect_v1_sha256(c.SupervisedEquityEffect.model_validate(document))
    changed = c.SupervisedEquityEffect.model_validate(put(document, path, value))
    assert c.effect_v1_sha256(changed) != base


def test_effect_sha_changes_with_intent_identity() -> None:
    document = load("equity_effect.json")
    base = c.effect_v1_sha256(c.SupervisedEquityEffect.model_validate(document))
    other = put(put(document, "intent_id", "eq-spy-002"), "order_ref", "trex:sup:eq-spy-002")
    assert c.effect_v1_sha256(c.SupervisedEquityEffect.model_validate(other)) != base


def test_effect_sha_stable_across_reparse() -> None:
    document = load("equity_effect.json")
    effect = c.SupervisedEquityEffect.model_validate(document)
    again = c.decode_effect_v1(c.effect_v1_bytes(effect))
    assert c.effect_v1_sha256(again) == c.effect_v1_sha256(effect)


PROPOSAL_FIELD_CHANGES = [
    ("strategy_version", "equity-pilot/2"),
    ("program_id", "prog-other"),
    ("sleeve_ref", None),
    ("proposed_at", "2026-10-22T14:00:01Z"),
    ("send_deadline", "2026-10-22T14:09:59Z"),
    ("review.quote.bid_cents", 67147),
    ("review.quote.ask_cents", 67153),
    ("review.quote.market_data_type", "delayed"),
    ("review.quote.as_of", "2026-10-22T13:59:57Z"),
    ("review.max_loss_cents", 26861),
    ("review.screening_sha256", "0" * 64),
    ("review.owner_epoch", "owner-other"),
    ("review.profile_digest", "1" * 64),
]


@pytest.mark.parametrize(("path", "value"), PROPOSAL_FIELD_CHANGES)
def test_proposal_id_changes_with_each_field(path: str, value: Any) -> None:
    document = load("proposal_equity.json")
    changed = mint(put(body_of(document), path, value))
    assert changed.proposal_id != document["proposal_id"]
    # the old id no longer validates over the changed body
    with pytest.raises(ValidationError, match="proposal_id"):
        c.EntryProposal.model_validate(put(document, path, value))


def test_proposal_id_changes_with_the_effect() -> None:
    document = load("proposal_equity.json")
    body = put(body_of(document), "effect.quantity", 3)
    effect = c.SupervisedEquityEffect.model_validate(body["effect"])
    body = put(body, "effect_sha256", c.effect_v1_sha256(effect))
    body = put(body, "review.notional_cents", c.equity_notional_cents(effect))
    body = put(body, "review.max_loss_cents", c.equity_max_loss_cents(effect))
    assert mint(body).proposal_id != document["proposal_id"]


def test_proposal_effect_sha_must_match_the_effect() -> None:
    body = put(body_of(load("proposal_equity.json")), "effect_sha256", "2" * 64)
    with pytest.raises(ValidationError, match="effect_sha256"):
        c.EntryProposalBody.model_validate(body)


# -------------------------------------------------------- equity effect


@pytest.mark.parametrize("quantity", [0, -1, 1.5, 2.0, "2", True])
def test_equity_quantity_is_a_whole_positive_int(quantity: Any) -> None:
    with pytest.raises(ValidationError):
        c.SupervisedEquityEffect.model_validate(
            put(load("equity_effect.json"), "quantity", quantity)
        )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("side", "SELL"),
        ("tif", "GTC"),
        ("outside_rth", True),
        ("currency", "EUR"),
        ("exchange", "ARCA"),
        ("order_ref", "trex:sup:someone-else"),
        ("account_id", "U1234567"),
        ("limit_cents", 0),
        ("con_id", 0),
        ("symbol", "spy"),
        ("intent_id", "../escape"),
        ("exit_policy.stop_confirm_ticks", 0),
        ("exit_policy.forced_exit_et", "15:50"),
    ],
)
def test_equity_effect_field_refusals(path: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        c.SupervisedEquityEffect.model_validate(put(load("equity_effect.json"), path, value))


@pytest.mark.parametrize("stop", [67150, 67151])
def test_stop_must_be_below_limit(stop: int) -> None:
    document = put(load("equity_effect.json"), "exit_policy.stop_price_cents", stop)
    with pytest.raises(ValidationError, match="stop_price_cents"):
        c.SupervisedEquityEffect.model_validate(document)


@pytest.mark.parametrize("target", [67150, 67149])
def test_target_must_be_above_limit(target: int) -> None:
    document = put(load("equity_effect.json"), "exit_policy.target_price_cents", target)
    with pytest.raises(ValidationError, match="target_price_cents"):
        c.SupervisedEquityEffect.model_validate(document)


def test_equity_risk_arithmetic() -> None:
    effect = c.SupervisedEquityEffect.model_validate(load("equity_effect.json"))
    assert c.equity_notional_cents(effect) == 2 * 67150
    assert c.equity_stop_loss_cents(effect) == 2 * (67150 - 66500)
    assert c.equity_max_loss_cents(effect) == 26860  # 20% of 134300
    assert c.equity_max_loss_cents(effect, gap_reserve_bps=0) == 1300
    small = c.SupervisedEquityEffect.model_validate(
        put(
            put(load("equity_effect.json"), "limit_cents", 101),
            "exit_policy",
            {
                "stop_price_cents": 100,
                "exit_deadline_session": "2026-10-23",
            },
        )
        | {"quantity": 1}
    )
    assert c.equity_max_loss_cents(small) == 21  # ceil(20.2)
    with pytest.raises(ValueError):
        c.equity_max_loss_cents(effect, gap_reserve_bps=10_001)


# ----------------------------------------------------- option golden


def test_option_effect_golden_bytes_unchanged() -> None:
    document = load("option_effect_canary.json")
    effect = SupervisedEffect.model_validate(document)
    payload = effect_bytes(effect)
    assert len(payload) == OPTION_GOLDEN_LEN
    assert hashlib.sha256(payload).hexdigest() == OPTION_GOLDEN_SHA256
    assert payload == stdlib_canonical(document)
    via_union = c.EFFECT_V1_ADAPTER.validate_python(document)
    assert type(via_union) is SupervisedEffect
    assert c.effect_v1_bytes(via_union) == payload
    assert c.effect_v1_sha256(via_union) == OPTION_GOLDEN_SHA256
    assert c.decode_effect_v1(payload) == effect


@pytest.mark.parametrize("schema", [None, "supervised-effect/2", "supervised-effect/1"])
def test_effect_union_dispatches_on_schema(schema: str | None) -> None:
    document = load("equity_effect.json")
    if schema is None:
        document.pop("schema")
    else:
        document["schema"] = schema
    with pytest.raises(ValidationError):
        c.EFFECT_V1_ADAPTER.validate_python(document)


# -------------------------------------------------------- proposals


@pytest.mark.parametrize(
    ("path", "value", "match"),
    [
        ("review.notional_cents", 134301, "notional"),
        ("review.max_loss_cents", 1299, "stop loss"),
        ("review.quote.as_of", "2026-10-22T14:00:01Z", "postdate"),
        ("send_deadline", "2026-10-22T14:15:01Z", "send_deadline"),
        ("send_deadline", "2026-10-22T14:00:00Z", "send_deadline"),
        ("effect.exit_policy.exit_deadline_session", "2026-10-21", "exit_deadline_session"),
        ("review.quote.bid_cents", 67153, "bid_cents"),
        ("review.quote.market_data_type", "streaming", "market_data_type"),
    ],
)
def test_proposal_body_refusals(path: str, value: Any, match: str) -> None:
    body = put(body_of(load("proposal_equity.json")), path, value)
    if path.startswith("effect."):
        effect = c.SupervisedEquityEffect.model_validate(body["effect"])
        body["effect_sha256"] = c.effect_v1_sha256(effect)
    with pytest.raises(ValidationError, match=match):
        c.EntryProposalBody.model_validate(body)


def test_proposal_created_from_body_carries_its_id() -> None:
    document = load("proposal_equity.json")
    assert c.model_document(mint(body_of(document))) == document
    assert c.proposal_id_of(body_of(document)) == document["proposal_id"]


# --------------------------------------------------------- commands


def test_unknown_command_kind_refused() -> None:
    document = put(load("command_pause_entries.json"), "kind", "send_order")
    with pytest.raises(ValidationError):
        c.parse_command(document)


def test_args_validated_per_kind() -> None:
    # pause args under an approve kind refuse (args are kind-specific)
    document = put(load("command_pause_entries.json"), "kind", "approve_entry")
    with pytest.raises(ValidationError):
        c.parse_command(document)


@pytest.mark.parametrize(
    "field", ["proposal_id", "proposal_sha256", "effect_sha256", "owner_epoch", "profile_digest"]
)
def test_approve_entry_args_require_every_binding(field: str) -> None:
    document = load("command_approve_entry.json")
    del document["args"][field]
    with pytest.raises(ValidationError):
        c.parse_command(document)


@pytest.mark.parametrize("field", ["proposal_id", "proposal_sha256", "effect_sha256"])
def test_approve_entry_hashes_are_lowercase_sha256(field: str) -> None:
    document = load("command_approve_entry.json")
    document["args"][field] = document["args"][field].upper()
    with pytest.raises(ValidationError):
        c.parse_command(document)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("expires_at", "2026-10-22T14:06:31Z"),  # 301 s
        ("expires_at", "2026-10-22T14:01:30Z"),  # not after issued_at
        ("idempotency_key", "not-a-uuid"),
        ("idempotency_key", "6F1C2B8E-3D4A-4F5B-9C6D-7E8F9A0B1C2D"),
        ("issued_by.ingress", "public"),
        ("issued_at", "2026-10-22T14:01:30"),  # naive
    ],
)
def test_command_envelope_refusals(path: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        c.parse_command(put(load("command_approve_entry.json"), path, value))


def test_command_body_hash_ignores_transport_identity_only() -> None:
    document = load("command_approve_entry.json")
    base = c.command_body_sha256(c.parse_command(document))
    retry = put(
        put(document, "issued_by.request_id", "req-0009"), "issued_by.request_sha256", "3" * 64
    )
    assert c.command_body_sha256(c.parse_command(retry)) == base
    for path, value in (
        ("issued_by.sub", "someone-else"),
        ("issued_by.ingress", "loopback"),
        ("args.owner_epoch", "owner-other"),
        ("expires_at", "2026-10-22T14:06:00Z"),
    ):
        assert c.command_body_sha256(c.parse_command(put(document, path, value))) != base


def test_actor_binds_its_request() -> None:
    body = b'{"kind":"approve_entry"}'
    actor = c.parse_command(load("command_approve_entry.json")).issued_by
    raw = c.RawCommandRequest(
        method="POST",
        path="/trex/commands",
        headers={},
        body=body,
        peer_host="127.0.0.1",
        request_id="req-0001",
    )
    assert c.actor_binds_request(actor, raw)
    assert not c.actor_binds_request(actor, dataclasses.replace(raw, body=b"x"))
    assert not c.actor_binds_request(actor, dataclasses.replace(raw, request_id="req-0002"))


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("refused", None),
        ("failed", None),
        ("duplicate", None),
        ("duplicate", "command_body_mismatch"),
        ("applied", "entries_paused"),
        ("accepted", "entries_paused"),
    ],
)
def test_command_result_reason_matches_status(status: str, reason: str | None) -> None:
    document = put(put(load("command_result_applied.json"), "status", status), "reason", reason)
    with pytest.raises(ValidationError):
        c.CommandResult.model_validate(document)


def test_command_result_duplicate_and_refused_shapes() -> None:
    document = load("command_result_applied.json")
    duplicate = put(put(document, "status", "duplicate"), "reason", "duplicate_command")
    assert c.CommandResult.model_validate(duplicate).reason == c.Blocker.DUPLICATE_COMMAND
    refused = put(put(document, "status", "refused"), "reason", "approval_epoch_mismatch")
    assert c.CommandResult.model_validate(refused).status == "refused"


# --------------------------------------------------------- mandates


def test_mandate_expires_at_after_send_deadline_refused() -> None:
    document = put(load("mandate_v2.json"), "send_deadline", "2026-10-22T14:06:59Z")
    with pytest.raises(ValidationError, match="send_deadline"):
        c.SupervisedMandateV2.model_validate(document)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("expires_at", "2026-10-22T14:02:00Z"),  # == granted_at
        ("granted_at", "2026-10-22T14:01:59Z"),  # 301 s window
        ("max_orders", 2),
        ("environment", "live"),
        ("approval_id", "approval-1"),
    ],
)
def test_mandate_v2_refusals(path: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        c.SupervisedMandateV2.model_validate(put(load("mandate_v2.json"), path, value))


def test_approval_expiry_is_min_of_deadline_and_ttl() -> None:
    deadline = at("2026-10-22T14:10:00+00:00")
    assert c.approval_expiry(at("2026-10-22T14:02:00+00:00"), deadline) == at(
        "2026-10-22T14:07:00+00:00"
    )
    assert c.approval_expiry(at("2026-10-22T14:08:00+00:00"), deadline) == deadline


def _approval_case() -> tuple[c.EntryProposal, dict[str, Any]]:
    return c.EntryProposal.model_validate(load("proposal_equity.json")), load(
        "command_approve_entry.json"
    )


LIVE_EPOCH = "owner-20261022T133000Z-4242"
LIVE_PROFILE = hashlib.sha256(b"synthetic-profile").hexdigest()


def test_approval_blockers_clean_and_mandate_matches_fixture() -> None:
    proposal, document = _approval_case()
    command = c.parse_command(document)
    assert isinstance(command, c.ApproveEntryCommand)
    now = at("2026-10-22T14:02:00+00:00")
    assert (
        c.approval_blockers(
            proposal, command, now=now, owner_epoch=LIVE_EPOCH, profile_digest=LIVE_PROFILE
        )
        == ()
    )
    mandate = c.mandate_for_approval(proposal, command, now=now)
    assert c.model_document(mandate) == load("mandate_v2.json")
    assert (
        c.mandate_blockers(
            mandate,
            now=now,
            effect_sha256=proposal.effect_sha256,
            owner_epoch=LIVE_EPOCH,
            profile_digest=LIVE_PROFILE,
        )
        == ()
    )


@pytest.mark.parametrize(
    ("path", "value", "live_epoch", "now", "expected"),
    [
        (
            "args.effect_sha256",
            "4" * 64,
            LIVE_EPOCH,
            "14:02:00",
            c.Blocker.APPROVAL_EFFECT_MISMATCH,
        ),
        (
            "args.proposal_sha256",
            "5" * 64,
            LIVE_EPOCH,
            "14:02:00",
            c.Blocker.APPROVAL_PROPOSAL_MISMATCH,
        ),
        (
            "args.proposal_id",
            "6" * 64,
            LIVE_EPOCH,
            "14:02:00",
            c.Blocker.APPROVAL_PROPOSAL_MISMATCH,
        ),
        (
            "args.owner_epoch",
            "owner-old",
            LIVE_EPOCH,
            "14:02:00",
            c.Blocker.APPROVAL_EPOCH_MISMATCH,
        ),
        (None, None, "owner-restarted", "14:02:00", c.Blocker.APPROVAL_EPOCH_MISMATCH),
        (
            "args.profile_digest",
            "7" * 64,
            LIVE_EPOCH,
            "14:02:00",
            c.Blocker.APPROVAL_PROFILE_MISMATCH,
        ),
        (None, None, LIVE_EPOCH, "14:06:30", c.Blocker.COMMAND_EXPIRED),
    ],
)
def test_approval_blockers_name_each_mismatch(
    path: str | None, value: Any, live_epoch: str, now: str, expected: c.Blocker
) -> None:
    proposal, document = _approval_case()
    if path is not None:
        document = put(document, path, value)
    command = c.parse_command(document)
    assert isinstance(command, c.ApproveEntryCommand)
    blockers = c.approval_blockers(
        proposal,
        command,
        now=at(f"2026-10-22T{now}+00:00"),
        owner_epoch=live_epoch,
        profile_digest=LIVE_PROFILE,
    )
    assert blockers == (expected,)


def test_approval_after_send_deadline_is_expired() -> None:
    proposal, document = _approval_case()
    document = put(
        put(document, "issued_at", "2026-10-22T14:09:00Z"), "expires_at", "2026-10-22T14:14:00Z"
    )
    command = c.parse_command(document)
    assert isinstance(command, c.ApproveEntryCommand)
    blockers = c.approval_blockers(
        proposal,
        command,
        now=at("2026-10-22T14:10:00+00:00"),
        owner_epoch=LIVE_EPOCH,
        profile_digest=LIVE_PROFILE,
    )
    assert blockers == (c.Blocker.APPROVAL_EXPIRED,)


def test_mandate_blockers() -> None:
    mandate = c.SupervisedMandateV2.model_validate(load("mandate_v2.json"))
    sha = mandate.effect_sha256
    late = at("2026-10-22T14:07:00+00:00")
    kwargs = {"owner_epoch": LIVE_EPOCH, "profile_digest": LIVE_PROFILE}
    assert c.mandate_blockers(mandate, now=late, effect_sha256=sha, **kwargs) == (
        c.Blocker.APPROVAL_EXPIRED,
    )
    early = at("2026-10-22T14:03:00+00:00")
    assert c.mandate_blockers(mandate, now=early, effect_sha256="8" * 64, **kwargs) == (
        c.Blocker.APPROVAL_EFFECT_MISMATCH,
    )
    assert c.mandate_blockers(
        mandate, now=early, effect_sha256=sha, owner_epoch="x", profile_digest=LIVE_PROFILE
    ) == (c.Blocker.APPROVAL_EPOCH_MISMATCH,)


# ------------------------------------------------------------- books


def test_base_exec_id_rule() -> None:
    assert c.base_exec_id_of("0000e0d5.6576d6a5.01.01") == "0000e0d5.6576d6a5.01"
    assert c.base_exec_id_of("0000e0d5.6576d6a5.01.02") == c.base_exec_id_of(
        "0000e0d5.6576d6a5.01.01"
    )
    assert c.base_exec_id_of("nodot") == "nodot"
    assert c.base_exec_id_of(".leading") == ".leading"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("base_exec_id", "0000e0d5.6576d6a5"),
        ("quantity", 2.5),
        ("quantity", 0),
        ("sec_type", "FUT"),
        ("multiplier", 100),
        ("side", "BUY"),
        ("time", "2026-10-22T14:03:05"),
    ],
)
def test_broker_execution_refusals(path: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        c.BrokerExecution.model_validate(put(load("broker_execution.json"), path, value))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("event_id", "fill:other"),
        ("exec_id", None),
        ("quantity_delta", 0),
        ("supersedes_event_id", "fill:x"),
        ("ref_sha256", "9" * 64),
        ("perm_id", None),
        ("kind", "commission"),  # a commission moves no quantity (and has its own id)
    ],
)
def test_ledger_event_shape_refusals(path: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        c.LedgerEvent.model_validate(put(load("ledger_event_fill.json"), path, value))


def test_ledger_event_other_kinds() -> None:
    fill = load("ledger_event_fill.json")
    correction = put(
        put(put(fill, "kind", "correction"), "event_id", "correction:0000e0d5.6576d6a5.01.02"),
        "exec_id",
        "0000e0d5.6576d6a5.01.02",
    )
    correction = put(put(correction, "supersedes_event_id", fill["event_id"]), "quantity_delta", 0)
    assert c.LedgerEvent.model_validate(correction).kind == "correction"
    with pytest.raises(ValidationError):
        c.LedgerEvent.model_validate(put(correction, "supersedes_event_id", None))
    commission = put(
        put(put(fill, "kind", "commission"), "event_id", "commission:0000e0d5.6576d6a5.01.01"),
        "quantity_delta",
        0,
    )
    paid = c.LedgerEvent.model_validate(put(commission, "cash_delta_micros", -1_000_000))
    assert paid.kind == "commission"
    with pytest.raises(ValidationError):
        c.LedgerEvent.model_validate(put(commission, "quantity_delta", 2))
    recon_sha = "a" * 64
    reconciliation = {
        "schema": "trex-ledger-event/1",
        "event_id": f"reconciliation:{recon_sha}",
        "kind": "reconciliation",
        "account_id": "DU0000001",
        "at": "2026-10-22T14:04:00Z",
        "recorded_at": "2026-10-22T14:04:01Z",
        "attribution": {"intent_id": None, "strategy_version": None, "program_id": None},
        "quantity_delta": 0,
        "cash_delta_micros": 0,
        "ref_sha256": recon_sha,
    }
    assert c.LedgerEvent.model_validate(reconciliation).attribution.sleeve_ref == "unassigned"
    with pytest.raises(ValidationError):
        c.LedgerEvent.model_validate(put(reconciliation, "cash_delta_micros", 5))
    with pytest.raises(ValidationError):
        c.LedgerEvent.model_validate(put(reconciliation, "ref_sha256", None))


def test_attribution_is_ours_or_outside() -> None:
    ours = load("ledger_event_fill.json")["attribution"]
    assert c.Attribution.model_validate(ours).intent_id == "eq-spy-001"
    with pytest.raises(ValidationError):
        c.Attribution.model_validate({**ours, "program_id": None})
    outside = c.Attribution.model_validate(
        {"intent_id": None, "strategy_version": None, "program_id": None}
    )
    assert outside.sleeve_ref == c.UNASSIGNED_SLEEVE


@pytest.mark.parametrize(
    ("path", "value"),
    [("quantity", -2), ("quantity", 0), ("quantity", 3), ("lots", [])],
)
def test_holding_refusals(path: str, value: Any) -> None:
    document = load("holding.json")
    if path == "quantity" and value == -2:
        document["lots"][0]["quantity"] = -2
    with pytest.raises(ValidationError):
        c.Holding.model_validate(put(document, path, value))


def test_order_ref_parsing_and_desk_prefix() -> None:
    from tree_options.trex.desk_runtime import DESK_REF_PREFIX, desk_order_ref

    assert c.DESK_REF_PREFIX == DESK_REF_PREFIX
    assert c.desk_order_ref("eq-spy-001") == desk_order_ref("eq-spy-001")
    assert c.parse_order_ref("trex:sup:eq-spy-001") == c.OrderRefOwner("sup", "eq-spy-001")
    assert c.parse_order_ref("trex:desk:canary-001") == c.OrderRefOwner("desk", "canary-001")
    for other in (None, "", "trex:sup:", "trex:desk:", "trex:spread-1", "manual"):
        assert c.parse_order_ref(other) is None


# -------------------------------------------------------------- risk


def test_risk_snapshot_freshness() -> None:
    snapshot = c.RiskSnapshot.model_validate(load("risk_snapshot.json"))
    stale = (c.Blocker.RISK_SNAPSHOT_STALE,)
    assert c.risk_snapshot_blockers(snapshot, at("2026-10-22T14:06:00+00:00")) == ()
    assert c.risk_snapshot_blockers(snapshot, at("2026-10-22T14:06:01+00:00")) == stale
    assert c.risk_snapshot_blockers(snapshot, at("2026-10-22T14:03:59+00:00")) == stale
    assert c.risk_snapshot_blockers(None, at("2026-10-22T14:04:00+00:00")) == stale
    unavailable = c.RiskSnapshot.model_validate(
        {
            **load("risk_snapshot.json"),
            "status": "unavailable",
            "open_loss_cents": None,
            "day_realized_loss_cents": None,
            "cash_cents": None,
            "equity_gross_cents": None,
            "outside_positions": [],
        }
    )
    assert c.risk_snapshot_blockers(unavailable, at("2026-10-22T14:04:00+00:00")) == stale


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("open_loss_cents", None),
        ("open_loss_cents", -1),
        ("status", "unavailable"),
        ("stale_after_s", 600),
        ("outside_positions", [{"con_id": 1, "sec_type": "STK", "symbol": "X", "quantity": 0}]),
    ],
)
def test_risk_snapshot_refusals(path: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        c.RiskSnapshot.model_validate(put(load("risk_snapshot.json"), path, value))


def test_risk_snapshot_cash_source_is_an_additive_optional_field() -> None:
    """Amendment 2026-10-21 (cash tag): ``cash_source`` names the IBKR
    account-value tag ``cash_cents`` was read from. Optional: a document
    without it (every earlier one, the frozen fixture) reads and writes
    unchanged; only a complete snapshot carries it."""
    document = load("risk_snapshot.json")
    assert "cash_source" not in document
    plain = c.RiskSnapshot.model_validate(document)
    assert plain.cash_source is None
    assert c.model_document(plain) == document
    for source in ("SettledCash", "TotalCashValue"):
        tagged = {**document, "cash_source": source}
        snapshot = c.RiskSnapshot.model_validate(tagged)
        assert snapshot.cash_source == source
        assert c.model_document(snapshot) == tagged
    with pytest.raises(ValidationError):
        c.RiskSnapshot.model_validate({**document, "cash_source": "NetLiquidation"})
    unavailable = {
        **document,
        "status": "unavailable",
        "open_loss_cents": None,
        "day_realized_loss_cents": None,
        "cash_cents": None,
        "equity_gross_cents": None,
        "outside_positions": [],
    }
    assert c.RiskSnapshot.model_validate(unavailable).cash_source is None
    with pytest.raises(ValidationError):
        c.RiskSnapshot.model_validate({**unavailable, "cash_source": "TotalCashValue"})


@pytest.mark.parametrize(
    ("local", "broker", "kind", "ok"),
    [
        (0, 3, "unexplained", True),
        (2, 0, "missing", True),
        (2, 3, "quantity_mismatch", True),
        (2, 3, "missing", False),
        (0, 3, "quantity_mismatch", False),
        (2, 2, "quantity_mismatch", False),
    ],
)
def test_mismatch_kind_follows_quantities(local: int, broker: int, kind: str, ok: bool) -> None:
    document = {"con_id": 756733, "local_qty": local, "broker_qty": broker, "kind": kind}
    if ok:
        assert c.PositionMismatch.model_validate(document).kind == kind
    else:
        with pytest.raises(ValidationError):
            c.PositionMismatch.model_validate(document)


def test_reconciliation_status_shape_and_blockers() -> None:
    required = load("reconciliation_required.json")
    with pytest.raises(ValidationError):
        c.Reconciliation.model_validate(put(required, "status", "clean"))
    with pytest.raises(ValidationError):
        c.Reconciliation.model_validate(put(required, "mismatches", []))
    with pytest.raises(ValidationError):
        c.Reconciliation.model_validate(put(required, "mismatches", required["mismatches"] * 2))
    clean = c.Reconciliation.model_validate(put(put(required, "status", "clean"), "mismatches", []))
    fresh = at("2026-10-22T14:05:00+00:00")
    assert c.reconciliation_blockers(clean, fresh) == ()
    blocked = (c.Blocker.RECONCILIATION_REQUIRED,)
    assert c.reconciliation_blockers(clean, at("2026-10-22T14:06:01+00:00")) == blocked
    assert c.reconciliation_blockers(c.Reconciliation.model_validate(required), fresh) == blocked
    assert c.reconciliation_blockers(None, fresh) == blocked


# ------------------------------------------------------- desk spec v2


def test_desk_spec_v1_still_parses_through_existing_model() -> None:
    document = load("desk_spec_v1_option.json")
    assert DeskSpec.model_validate(document) == c.parse_desk_spec(document)
    assert type(c.parse_desk_spec(document)) is DeskSpec


def test_desk_spec_v2_kinds() -> None:
    equity = c.parse_desk_spec(load("desk_spec_v2_equity.json"))
    option = c.parse_desk_spec(load("desk_spec_v2_option.json"))
    assert isinstance(equity, c.EquityDeskSpecV2)
    assert isinstance(option, c.OptionDeskSpecV2)
    effect = c.SupervisedEquityEffect.model_validate(load("equity_effect.json"))
    assert equity.position.exit_policy == effect.exit_policy
    assert equity.position.entry_limit_cents == effect.limit_cents
    assert (
        option.structure
        == SupervisedEffect.model_validate(load("option_effect_canary.json")).structure
    )


@pytest.mark.parametrize(
    ("name", "path", "value"),
    [
        ("desk_spec_v2_equity.json", "schema", "trex-desk-spec/3"),
        ("desk_spec_v2_equity.json", "asset_kind", "future"),
        ("desk_spec_v2_equity.json", "asset_kind", "option"),
        ("desk_spec_v2_equity.json", "entry_order_ref", "trex:sup:other"),
        ("desk_spec_v2_equity.json", "position.position_id", "other"),
        ("desk_spec_v2_equity.json", "position.exit_policy.stop_price_cents", 67150),
        ("desk_spec_v2_option.json", "asset_kind", "equity"),
    ],
)
def test_desk_spec_v2_refusals(name: str, path: str, value: Any) -> None:
    with pytest.raises(ValueError):
        c.parse_desk_spec(put(load(name), path, value))


# ---------------------------------------------- vocabulary, layout, import

#: C2.7 ``Blocker`` as frozen by the Wave 2 plan and the contracts lane.
PLAN_BLOCKERS = frozenset(
    {
        "entries_paused",
        "reconciliation_required",
        "risk_snapshot_stale",
        "equity_quote_not_realtime",
        "equity_symbol_not_qualified",
        "insufficient_cash",
        "approval_expired",
        "approval_epoch_mismatch",
        "approval_profile_mismatch",
        "approval_effect_mismatch",
        "mandate_already_consumed",
        "duplicate_command",
        "command_body_mismatch",
        "approval_proposal_mismatch",
        "command_expired",
        "command_unauthorized",
        "proposal_unknown",
        "session_entry_limit_reached",
    }
)
#: Amendment 2026-10-21 (lane K): the wire strings the merged Wave 2 lanes
#: emit as blockers, refusal reasons or view reasons, spelled as they do.
AMENDED_BLOCKERS = frozenset(
    {
        # kill files and owner facts (trex.pause, trex.admission, desk.enter)
        "halt",
        "auto_off",
        "exit_owner_not_ready",
        "shadow_only_build",
        "selection_rule_not_activated",
        # entry screening (trex.supervised_canary, action_graph.canary)
        "credit_open_not_supported_v1",
        "risk_snapshot_account_mismatch",
        "profile_limits_missing",
        "effect_account_not_mandate_account",
        "equity_not_whole_share_long",
        "quote_stale_or_future",
        "broker_owner_unhealthy",
        "protective_exit_unavailable",
        "equity_notional_exceeds_intended_capital",
        "trade_exceeds_intended_capital",
        "trade_loss_cap_exceeded",
        "open_exposure_unknown",
        "open_loss_cap_exceeded",
        "daily_loss_unknown",
        "daily_loss_cap_exceeded",
        # equity preflight (trex.supervised_ibkr)
        "gateway_disconnected",
        "not_paper_gateway_port",
        "wrong_client_id",
        "account_not_managed_by_session",
        "account_not_paper",
        "equity_con_id_mismatch",
        "equity_quote_incomplete",
        "broker_view_unreadable:account_values",
        "broker_view_unreadable:open_orders",
        "cash_commitment_unbounded",
        # per-trade authority refusals (trex.supervised, trex.grant_policy)
        "effect_invalid",
        "effects_halted",
        "entry_mandate_already_granted",
        "intent_id_not_path_safe",
        "mandate_absent",
        "mandate_account_mismatch",
        "mandate_binding_mismatch",
        "mandate_scope_mismatch",
        "permit_already_issued",
        "product_strategy_requires_approval",
        # holdings checks before a desk equity sell (trex.desk_runtime ``why``)
        "holdings_mismatch",
        "holdings_excess",
        "not_a_stock_row",
        "fractional_position",
        "order_fill_not_whole_shares",
        # web command ingress and store (trex_web.commands_view, command_store)
        "body_too_large",
        "invalid_json",
        "issued_by_is_stamped_by_the_backend",
        "invalid_command",
        "command_not_yet_valid",
        "unknown_command",
        "command_store_unavailable",
        "commands_dir_unwritable",
        "command_store_unreadable",
        "command_store_foreign_file",
        "command_result_invalid",
        "staged_command_invalid",
        "staged_command_mismatch",
        # web views (trex_web.commands_view proposals, trex_web.portfolio_view)
        "proposal_unreadable",
        "proposal_invalid",
        "proposal_not_canonical",
        "proposal_id_mismatch",
        "mandate_unreadable",
        "holdings_missing",
        "holdings_unreadable",
        "holdings_invalid",
        "risk_snapshot_missing",
        "risk_snapshot_unreadable",
        "risk_snapshot_invalid",
        "risk_snapshot_unavailable",
        "reconciliation_missing",
        "reconciliation_unreadable",
        "reconciliation_invalid",
        "reconciliation_stale",
        "reconciliation_unavailable",
        "account_mismatch",
        "non_paper_account",
    }
)
#: Amendment 2026-10-21: reserved for the Wave 2b lanes (not yet emitted).
RESERVED_BLOCKERS = frozenset({"entry_window_closed", "outside_undefined_risk", "exit_quote_blind"})
#: Amendment 2026-10-21 (W2c integration): additive members, each spelled
#: exactly as its producer emits it (``W2C_PRODUCERS``): the owner lane's
#: plain-string refusal codes, and a standing FLATTEN.
W2C_BLOCKERS = frozenset(
    {
        # kill files (trex.admission)
        "flatten",
        # the owner's entry flows (trex.desk_entries)
        "outside_regular_session",
        "profile_absent",
        "profile_invalid",
        "account_unbound",
        "candidate_unknown",
        "candidate_blocked",
        "candidate_invalid",
        "candidate_program_mismatch",
        "candidate_kind_mismatch",
        "candidate_expired",
        "intent_already_used",
        "quote_unavailable",
        "proposal_rejected",
        "broker_rejected",
        "send_uncertain",
        "desk_book_unreadable",
        # the owner's command consumer (trex.desk_commands)
        "command_invalid",
        "command_key_mismatch",
        "apply_error",
        "owner_stopped_before_result",
    }
)
#: The modules whose string literals the W2c members must match.
W2C_PRODUCERS = (
    "trex/admission.py",
    "trex/desk_entries.py",
    "trex/desk_commands.py",
    "trex/desk_books.py",
    "trex/desk_runtime.py",
    "trex/supervised_desk.py",
)
#: Built by the portfolio view as ``f"{label}_<state>"``, so never a literal.
PORTFOLIO_LABEL_REASONS = frozenset(
    f"{label}_{state}"
    for label in ("holdings", "risk_snapshot", "reconciliation")
    for state in ("missing", "unreadable", "invalid")
)
#: C2.7 ``DeskEvent`` as frozen by the contracts lane.
PLAN_EVENTS = frozenset(
    {
        "proposal_composed",
        "entry_approved",
        "entry_rejected",
        "entries_paused",
        "entries_resumed",
        "approvals_invalidated",
        "command_applied",
        "command_refused",
        "equity_exit_stop",
        "equity_exit_target",
        "equity_exit_deadline",
        "equity_exit_forced",
        "reconciliation_clean",
        "reconciliation_required",
    }
)
#: Amendment 2026-10-21: event names the desk runtime's equity path added.
AMENDED_EVENTS = frozenset(
    {
        "equity_contract_mismatch",
        "equity_unqualified",
        "exit_evidence_inconclusive",
        "holdings_unverifiable",
        "structure_tick_failed",
    }
)
#: Amendment 2026-10-21 (W2c integration): additive event names.
W2C_EVENTS = frozenset({"commands_failed", "gateway_connected", "books_problem", "books_failed"})
#: Amendment 2026-10-21 (Wave 3/4 integration): the desk records exit_quote_blind.
W34_EVENTS = frozenset({"exit_quote_blind"})
#: The strings the coordinator and the lane handoffs named (2026-10-21).
NAMED_LANE_CODES: list[tuple[type[c.Blocker] | type[c.DeskEvent], str]] = [
    (c.Blocker, "entry_mandate_already_granted"),
    (c.Blocker, "mandate_absent"),
    (c.Blocker, "mandate_binding_mismatch"),
    (c.Blocker, "exit_owner_not_ready"),
    (c.Blocker, "halt"),
    (c.Blocker, "auto_off"),
    (c.Blocker, "shadow_only_build"),
    (c.Blocker, "selection_rule_not_activated"),
    (c.DeskEvent, "holdings_unverifiable"),
    (c.DeskEvent, "equity_contract_mismatch"),
    (c.DeskEvent, "equity_unqualified"),
    (c.DeskEvent, "exit_evidence_inconclusive"),
    (c.Blocker, "equity_con_id_mismatch"),
    (c.Blocker, "equity_quote_incomplete"),
    (c.Blocker, "broker_view_unreadable:account_values"),
    (c.Blocker, "broker_view_unreadable:open_orders"),
    (c.Blocker, "cash_commitment_unbounded"),
    (c.Blocker, "entry_window_closed"),
    (c.Blocker, "outside_undefined_risk"),
    (c.Blocker, "exit_quote_blind"),
    (c.DeskEvent, "approvals_invalidated"),
]
#: The merged modules whose string literals the amended values must match.
PRODUCERS = (
    "trex/admission.py",
    "trex/pause.py",
    "trex/supervised.py",
    "trex/grant_policy.py",
    "trex/supervised_canary.py",
    "trex/supervised_ibkr.py",
    "trex/desk_runtime.py",
    "action_graph/canary.py",
    "desk/enter.py",
    "trex_web/commands_view.py",
    "trex_web/command_store.py",
    "trex_web/portfolio_view.py",
)


def _member_name(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value).upper()


def test_blocker_wire_strings_are_frozen() -> None:
    assert {b.value for b in c.Blocker} == (
        PLAN_BLOCKERS | AMENDED_BLOCKERS | RESERVED_BLOCKERS | W2C_BLOCKERS
    )
    # the amendments are additive: no earlier value is re-added or renamed
    assert not PLAN_BLOCKERS & (AMENDED_BLOCKERS | RESERVED_BLOCKERS | W2C_BLOCKERS)
    assert not AMENDED_BLOCKERS & (RESERVED_BLOCKERS | W2C_BLOCKERS)
    assert not RESERVED_BLOCKERS & W2C_BLOCKERS
    assert PORTFOLIO_LABEL_REASONS <= AMENDED_BLOCKERS


def test_desk_event_wire_strings_are_frozen() -> None:
    assert {e.value for e in c.DeskEvent} == (
        PLAN_EVENTS | AMENDED_EVENTS | W2C_EVENTS | W34_EVENTS
    )
    assert not PLAN_EVENTS & (AMENDED_EVENTS | W2C_EVENTS | W34_EVENTS)
    assert not AMENDED_EVENTS & (W2C_EVENTS | W34_EVENTS)
    assert not W2C_EVENTS & W34_EVENTS


def test_w34_exit_quote_blind_is_the_desk_runtimes_event_name() -> None:
    """Wave 3/4 integration: the event the desk runtime records and pushes is
    the vocabulary member, equal to the reserved Blocker of the same name."""
    from tree_options.trex import desk_runtime, equity_exits

    assert c.DeskEvent("exit_quote_blind") == equity_exits.EXIT_QUOTE_BLIND
    assert c.DeskEvent.EXIT_QUOTE_BLIND == c.Blocker.EXIT_QUOTE_BLIND
    assert desk_runtime._NOTIFY_EVENTS[c.DeskEvent.EXIT_QUOTE_BLIND.value] == "urgent"
    test_named_lane_codes_round_trip(c.DeskEvent, "exit_quote_blind")


@pytest.mark.parametrize("vocabulary", [c.Blocker, c.DeskEvent], ids=["Blocker", "DeskEvent"])
def test_vocabulary_values_are_unique(vocabulary: Any) -> None:
    # ``__members__`` keeps aliases (a repeated value), iteration hides them
    values = [member.value for member in vocabulary.__members__.values()]
    assert len(values) == len(set(values))
    assert len(values) == len(list(vocabulary))
    # names are the values spelled as identifiers (``:`` becomes ``_``)
    assert {name: m.value for name, m in vocabulary.__members__.items()} == {
        _member_name(value): value for value in values
    }


@pytest.mark.parametrize(
    ("vocabulary", "code"), NAMED_LANE_CODES, ids=[code for _, code in NAMED_LANE_CODES]
)
def test_named_lane_codes_round_trip(vocabulary: Any, code: str) -> None:
    member = vocabulary(code)
    assert member.value == code
    assert member == code
    assert str(member) == code
    assert f"{member}" == code
    assert json.dumps({"reason": member}) == json.dumps({"reason": code})
    assert vocabulary[_member_name(code)] is member
    assert vocabulary(json.loads(json.dumps(member))) is member


@pytest.mark.parametrize(
    ("vocabulary", "code"),
    [
        *((c.Blocker, code) for code in sorted(W2C_BLOCKERS)),
        *((c.DeskEvent, code) for code in sorted(W2C_EVENTS)),
    ],
    ids=[*sorted(W2C_BLOCKERS), *sorted(W2C_EVENTS)],
)
def test_w2c_codes_round_trip(vocabulary: Any, code: str) -> None:
    """W2c item 7: the owner lane's plain strings are members, values unchanged."""
    test_named_lane_codes_round_trip(vocabulary, code)


def test_w2c_values_are_their_producers_literals() -> None:
    literals = _producer_literals(W2C_PRODUCERS)
    # ``commands_failed`` is new in W2c: its producer spells the member itself
    plain = (W2C_BLOCKERS | W2C_EVENTS) - {c.DeskEvent.COMMANDS_FAILED.value}
    assert sorted(plain - literals) == []


def test_w2c_producer_constants_are_vocabulary_members() -> None:
    from tree_options.trex import desk_books, desk_commands

    assert desk_commands.COMMAND_NOT_YET_VALID == c.Blocker.COMMAND_NOT_YET_VALID
    assert desk_commands.INTERRUPTED == c.Blocker.OWNER_STOPPED_BEFORE_RESULT
    assert desk_books.BOOKS_PROBLEM_EVENT == c.DeskEvent.BOOKS_PROBLEM


def _producer_literals(producers: tuple[str, ...] = PRODUCERS) -> set[str]:
    import ast

    found: set[str] = set()
    for relative in producers:
        tree = ast.parse((REPO / "src" / "tree_options" / relative).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
                # desk.enter lowercases its kill-file names into blockers
                if node.value.isupper():
                    found.add(node.value.lower())
    return found


def test_amended_values_are_the_producers_literals() -> None:
    literals = _producer_literals()
    expected = (AMENDED_BLOCKERS - PORTFOLIO_LABEL_REASONS) | AMENDED_EVENTS
    assert sorted(expected - literals) == []


def test_producer_constants_are_vocabulary_members() -> None:
    from tree_options.trex import admission, pause, supervised_canary, supervised_ibkr

    assert pause.HALTED == c.Blocker.HALT
    assert admission.HALTED == c.Blocker.HALT
    assert admission.FLATTENING == c.Blocker.FLATTEN
    assert admission.EXIT_OWNER_NOT_READY == c.Blocker.EXIT_OWNER_NOT_READY
    assert supervised_canary.CREDIT_OPEN_BLOCKER == c.Blocker.CREDIT_OPEN_NOT_SUPPORTED_V1
    assert supervised_ibkr.EQUITY_CON_ID_MISMATCH == c.Blocker.EQUITY_CON_ID_MISMATCH
    assert supervised_ibkr.EQUITY_QUOTE_INCOMPLETE == c.Blocker.EQUITY_QUOTE_INCOMPLETE
    assert (
        supervised_ibkr.ACCOUNT_VALUES_UNREADABLE == c.Blocker.BROKER_VIEW_UNREADABLE_ACCOUNT_VALUES
    )
    assert supervised_ibkr.OPEN_ORDERS_UNREADABLE == c.Blocker.BROKER_VIEW_UNREADABLE_OPEN_ORDERS
    assert supervised_ibkr.CASH_COMMITMENT_UNBOUNDED == c.Blocker.CASH_COMMITMENT_UNBOUNDED


def test_admission_blockers_are_vocabulary(tmp_path: Path) -> None:
    from tree_options.trex.admission import entry_blockers

    (tmp_path / "PAUSE_ENTRIES").write_text("{}")
    (tmp_path / "HALT").write_text("{}")
    (tmp_path / "FLATTEN").write_text("{}")
    blockers = entry_blockers(tmp_path, at("2026-10-22T14:04:30+00:00"))
    assert set(blockers) == {
        c.Blocker.ENTRIES_PAUSED,
        c.Blocker.HALT,
        c.Blocker.FLATTEN,
        c.Blocker.RISK_SNAPSHOT_STALE,
        c.Blocker.RECONCILIATION_REQUIRED,
        c.Blocker.EXIT_OWNER_NOT_READY,
    }


def test_equity_review_blockers_are_vocabulary() -> None:
    from decimal import Decimal

    from tree_options.action_graph.canary import EquityEntryFacts, review_equity_entry
    from tree_options.action_graph.capital import CapitalProfile

    effect = c.SupervisedEquityEffect.model_validate(load("equity_effect.json"))
    now = at("2026-10-22T14:04:30+00:00")

    def profile(cap: Decimal | None) -> CapitalProfile:
        return CapitalProfile(
            profile_id="paper-tiny",
            revision=1,
            intended_capital=Decimal("1"),
            risk_style="defined-risk",
            goals=("equity-pilot",),
            allowed_strategy_versions=("equity-pilot/1",),
            max_loss_per_trade=cap,
            max_open_loss=cap,
            max_daily_loss=cap,
            horizon_days=30,
        )

    def facts(loss: int | None) -> EquityEntryFacts:
        return EquityEntryFacts(
            mandate_account_id="DU0000009",
            checked_at=now,
            market_data_type="delayed",
            quote_observed_at=None,
            settled_cash_cents=None,
            open_loss_cents=loss,
            day_realized_loss_cents=loss,
            entries_this_session=3,
            owner_healthy=False,
            protective_exit_ready=False,
        )

    capped = review_equity_entry(profile(Decimal("1")), effect, facts(0))
    unknown = review_equity_entry(
        profile(None),
        effect.model_copy(update={"quantity": 0}),
        facts(None),
        symbol_allowlist=frozenset(),
    )
    seen = set(capped) | set(unknown)
    assert seen <= {b.value for b in c.Blocker}
    assert seen == {
        c.Blocker.PROFILE_LIMITS_MISSING,
        c.Blocker.EFFECT_ACCOUNT_NOT_MANDATE_ACCOUNT,
        c.Blocker.EQUITY_SYMBOL_NOT_QUALIFIED,
        c.Blocker.EQUITY_NOT_WHOLE_SHARE_LONG,
        c.Blocker.EQUITY_QUOTE_NOT_REALTIME,
        c.Blocker.QUOTE_STALE_OR_FUTURE,
        c.Blocker.BROKER_OWNER_UNHEALTHY,
        c.Blocker.PROTECTIVE_EXIT_UNAVAILABLE,
        c.Blocker.INSUFFICIENT_CASH,
        c.Blocker.EQUITY_NOTIONAL_EXCEEDS_INTENDED_CAPITAL,
        c.Blocker.TRADE_EXCEEDS_INTENDED_CAPITAL,
        c.Blocker.TRADE_LOSS_CAP_EXCEEDED,
        c.Blocker.OPEN_EXPOSURE_UNKNOWN,
        c.Blocker.OPEN_LOSS_CAP_EXCEEDED,
        c.Blocker.DAILY_LOSS_UNKNOWN,
        c.Blocker.DAILY_LOSS_CAP_EXCEEDED,
        c.Blocker.SESSION_ENTRY_LIMIT_REACHED,
    }


def _reasons(document: Any) -> set[str]:
    """Every non-null ``reason`` anywhere in a JSON-shaped document."""
    if isinstance(document, dict):
        own = {document["reason"]} if isinstance(document.get("reason"), str) else set()
        return own.union(*(_reasons(v) for v in document.values()))
    if isinstance(document, list):
        return set().union(*(_reasons(v) for v in document))
    return set()


def test_portfolio_view_reasons_are_vocabulary(tmp_path: Path) -> None:
    from tree_options.trex_web.portfolio_view import portfolio_payload

    now = at("2026-10-22T14:04:30+00:00")
    risk_unavailable = {
        **load("risk_snapshot.json"),
        "account_id": "U0000009",
        "status": "unavailable",
        "open_loss_cents": None,
        "day_realized_loss_cents": None,
        "cash_cents": None,
        "equity_gross_cents": None,
        "outside_positions": [],
    }
    rec = load("reconciliation_required.json")
    rec_unavailable = {**rec, "status": "unavailable", "mismatches": []}
    rec_clean = {**rec, "status": "clean", "mismatches": []}
    scenarios: list[tuple[dict[str, bytes], datetime]] = [
        ({}, now),
        ({name: b"{" for name in ("holdings", "risk-snapshot", "reconciliation")}, now),
        ({name: b"{}" for name in ("holdings", "risk-snapshot", "reconciliation")}, now),
        (
            {
                "holdings": b'{"holdings": []}',
                "risk-snapshot": json.dumps(risk_unavailable).encode(),
                "reconciliation": json.dumps(rec_unavailable).encode(),
            },
            now,
        ),
        ({"reconciliation": json.dumps(rec_clean).encode()}, at("2026-10-22T15:00:00+00:00")),
    ]
    seen: set[str] = set()
    for index, (files, when) in enumerate(scenarios):
        run_dir = tmp_path / f"run-{index}"
        (run_dir / "ledger").mkdir(parents=True)
        for name, payload in files.items():
            (run_dir / "ledger" / f"{name}.json").write_bytes(payload)
        seen |= _reasons(portfolio_payload(run_dir, when))
    assert seen <= {b.value for b in c.Blocker}
    assert seen >= PORTFOLIO_LABEL_REASONS | {
        "risk_snapshot_unavailable",
        "reconciliation_stale",
        "reconciliation_unavailable",
        "account_mismatch",
        "non_paper_account",
    }


def test_layout_agrees_with_the_existing_run_dir() -> None:
    from tree_options.trex.desk_runtime import DeskPaths
    from tree_options.trex.supervised import SupervisedPaths

    root = Path("/state/desk-paper")
    run = c.Wave2RunDir(root)
    desk = DeskPaths(root)
    assert run.halt() == desk.halt()
    assert run.flatten() == desk.flatten()
    assert run.spec("eq-spy-001") == desk.spec("eq-spy-001")
    assert run.pause_entries() == root / "PAUSE_ENTRIES"
    assert run.command("k") == root / "commands" / "k.json"
    assert run.command_result("k") == root / "commands" / "k.result.json"
    assert run.proposal("p") == root / "proposals" / "p.json"
    assert run.ledger_events() == root / "ledger" / "events.jsonl"
    sup_root = Path("/state/supervised")
    sup = c.Wave2SupervisedDir(sup_root)
    assert sup.mandate("eq-spy-001").parent == SupervisedPaths(sup_root).root / "mandates"
    assert sup.mandate_consumed("eq-spy-001").name == "eq-spy-001.consumed.json"


def test_contracts_import_without_a_broker_library() -> None:
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    probe = (
        "import sys, tree_options.trex.contracts; sys.exit(1 if 'ib_async' in sys.modules else 0)"
    )
    done = subprocess.run(
        [sys.executable, "-B", "-c", probe], cwd=REPO, env=env, capture_output=True, timeout=120
    )
    assert done.returncode == 0, done.stderr.decode()[-2000:]


def test_instants_normalize_to_utc() -> None:
    proposal = c.EntryProposal.model_validate(load("proposal_equity.json"))
    assert proposal.send_deadline.tzinfo is UTC
