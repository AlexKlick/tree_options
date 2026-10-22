"""E6 shadow drafts: miner deals become entry PROPOSAL drafts, never requests.

A draft carries the ``trex-entry-proposal/1`` body with every live-only
binding (quote, screening digest, owner epoch, profile digest, proposal
times) left null, so it is never approvable as written; the owner flow
completes it from live facts. Oracles: the frozen contract models
(``EntryProposalBody`` / ``EntryProposal`` / ``EFFECT_V1_ADAPTER``) and the
desk's own spec builder, never enter_supervised internals.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tree_options.desk import enter_supervised
from tree_options.time.sessions import shift_instant
from tree_options.trex import pause
from tree_options.trex.contracts import (
    EFFECT_V1_ADAPTER,
    PROPOSAL_SCHEMA,
    Blocker,
    EntryProposal,
    EntryProposalBody,
    Quote,
    SupervisedEffect,
    desk_spec_v2_for,
    effect_v1_sha256,
)
from tree_options.trex.plan import LegStructure
from tree_options.trex.supervised_desk import EntryRequest

PATH_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


def run(world, **kwargs):
    return enter_supervised.write_previews(
        now=kwargs.pop("now", world.now),
        cal=world.cal,
        database=world.db,
        queue_dir=kwargs.pop("queue_dir", world.queues),
        run_dir=kwargs.pop("run_dir", world.root / "execution"),
        **kwargs,
    )


def drafts_dir(world):
    return world.root / "execution" / "proposal-drafts"


def the_draft(world, doc) -> dict:
    (candidate,) = doc["written"]
    return json.loads((drafts_dir(world) / f"{candidate}.json").read_text())


def live_bindings(world, *, ttl_s: int = 300) -> dict:
    proposed_at = world.now.astimezone(UTC)
    return {
        "proposed_at": proposed_at,
        "send_deadline": shift_instant(proposed_at, ttl_s),
        "quote": Quote(
            bid_cents=215, ask_cents=225, market_data_type="realtime", as_of=proposed_at
        ),
        "screening_sha256": "a" * 64,
        "owner_epoch": "owner-20261022T133000Z-4242",
        "profile_digest": "b" * 64,
    }


def test_a_debit_deal_becomes_a_proposal_draft(world):
    world.queue_file()
    doc = run(world)
    assert doc["status"] == "ok" and len(doc["written"]) == 1
    draft = the_draft(world, doc)
    assert draft["schema"] == enter_supervised.DRAFT_SCHEMA
    assert draft["execution_enabled"] is False
    body = draft["body"]
    assert body["schema"] == PROPOSAL_SCHEMA
    effect = EFFECT_V1_ADAPTER.validate_python(body["effect"])
    assert isinstance(effect, SupervisedEffect)
    assert body["effect_sha256"] == effect_v1_sha256(effect)
    deal = world.queue["admissible"][0]
    assert draft["deal_id"] == deal["deal_id"] == effect.structure.deal_id
    assert effect.side == "BUY" and effect.quantity == 1
    assert effect.limit == Decimal(deal["fill"])
    assert effect.order_ref == f"trex:sup:{effect.intent_id}"
    assert effect.intent_id == effect.structure.id == draft["candidate_id"]
    assert body["strategy_version"] == "desk-row/R1"
    # the reviewed cost: the order's limit x100 per package x quantity, in cents
    cost = Decimal(deal["fill"]) * 100 * deal["quantity"] * 100
    cents = int(cost.to_integral_value(rounding=ROUND_CEILING))
    assert body["review"]["max_loss_cents"] == body["review"]["notional_cents"] == cents


def test_a_draft_is_never_approvable_as_written(world):
    world.queue_file()
    draft = the_draft(world, run(world))
    assert sorted(draft["missing"]) == sorted(enter_supervised.LIVE_BINDINGS)
    with pytest.raises(ValidationError):
        EntryProposalBody.model_validate(draft["body"])
    with pytest.raises(ValidationError):
        EntryProposal.model_validate(draft["body"])
    with pytest.raises(ValidationError):
        EntryRequest.model_validate(draft)  # and never a direct request


def test_live_bindings_complete_a_draft_into_a_contract_proposal(world):
    world.queue_file()
    draft = the_draft(world, run(world))
    proposal = enter_supervised.complete_draft(draft, **live_bindings(world))
    assert isinstance(proposal, EntryProposal)
    # completing never changes what the owner approves
    assert proposal.effect_sha256 == draft["body"]["effect_sha256"]
    assert effect_v1_sha256(proposal.effect) == draft["body"]["effect_sha256"]
    assert proposal.review.max_loss_cents == draft["body"]["review"]["max_loss_cents"]
    # an independent re-validation of the minted document agrees
    assert EntryProposal.model_validate(proposal.model_dump(mode="json", by_alias=True))


def test_a_completion_past_the_candidate_horizon_refuses(world):
    world.queue_file()
    draft = the_draft(world, run(world))
    bindings = live_bindings(world)
    horizon = datetime.fromisoformat(world.queue["valid_until"]).astimezone(UTC)
    bindings["proposed_at"] = shift_instant(horizon, -60)
    bindings["quote"] = bindings["quote"].model_copy(update={"as_of": bindings["proposed_at"]})
    bindings["send_deadline"] = shift_instant(horizon, 1)
    with pytest.raises(ValueError, match="horizon"):
        enter_supervised.complete_draft(draft, **bindings)
    bindings["send_deadline"] = horizon
    assert enter_supervised.complete_draft(draft, **bindings).send_deadline == horizon


def test_the_candidate_id_is_path_safe_so_mandate_and_spec_v2_accept_it(world):
    """Miner deal ids carry ':'; mandate/2 and spec/2 ids are path-safe."""
    world.queue_file()
    assert not PATH_SAFE.match(world.queue["admissible"][0]["deal_id"])
    draft = the_draft(world, run(world))
    assert PATH_SAFE.match(draft["candidate_id"])
    effect = EFFECT_V1_ADAPTER.validate_python(draft["body"]["effect"])
    spec = desk_spec_v2_for(effect, world.now)
    assert spec.intent_id == draft["candidate_id"]


@pytest.mark.parametrize(
    "deal_id",
    ["plain-id", "2025-03-03:R1:AAPL:example", "::weird::", "x" * 200, ".hidden"],
)
def test_candidate_ids_are_path_safe_and_distinct(deal_id):
    cid = enter_supervised.candidate_id(deal_id)
    assert PATH_SAFE.match(cid)
    assert cid == enter_supervised.candidate_id(deal_id)  # deterministic
    if PATH_SAFE.match(deal_id):
        assert cid == deal_id
    else:
        assert cid != enter_supervised.candidate_id(deal_id + "x")


def test_the_live_inbox_and_the_proposals_dir_are_never_touched(world):
    world.queue_file()
    run(world)
    for name in ("requests", "proposals", "requests-preview"):
        path = world.root / "execution" / name
        assert not path.exists() or list(path.iterdir()) == []


def test_operator_flags_stop_drafts(world):
    world.queue_file()
    (world.root / "execution").mkdir(parents=True, exist_ok=True)
    (world.root / "execution" / "HALT").touch()
    doc = run(world)
    assert doc["status"] == "halted" and doc["written"] == []
    assert not drafts_dir(world).exists()


def test_an_entries_pause_stops_drafts_with_its_blocker(world):
    world.queue_file()
    pause.pause_entries(world.root / "execution", actor="owner", now=world.now)
    doc = run(world)
    assert doc["status"] == "paused" and doc["written"] == []
    assert doc["blockers"] == [Blocker.ENTRIES_PAUSED.value]
    assert not drafts_dir(world).exists()


def test_off_window_and_missing_queue_are_statuses_not_errors(world):
    assert run(world, now=world.now.replace(hour=12))["status"] == "outside_entry_window"
    assert run(world)["status"] == "queue_not_ready"


def test_dry_run_writes_no_files_but_records_evidence(world):
    world.queue_file()
    doc = run(world, dry_run=True)
    assert doc["written"] == [
        enter_supervised.candidate_id(world.queue["admissible"][0]["deal_id"])
    ]
    assert not drafts_dir(world).exists()


def _credit_deal():
    spec = LegStructure(
        id="d-credit",
        underlying="AAPL",
        kind="credit_vertical",
        legs=[
            {"right": "P", "action": "SELL", "strike": "100", "expiry": date(2025, 4, 28)},
            {"right": "P", "action": "BUY", "strike": "95", "expiry": date(2025, 4, 28)},
        ],
        quantity=1,
        entry_date=date(2025, 3, 4),
        exit_deadline=date(2025, 3, 12),
        limit="1.00",
        exits={"touch": False, "breach": True},
    )
    return SimpleNamespace(deal_id="d-credit", spec=spec, fill=spec.limit, raw={"row": "R2"})


def test_a_credit_kind_writes_a_blocked_diagnostic_never_a_draft():
    doc = enter_supervised._preview_doc(_credit_deal(), date(2025, 3, 4), world_now(), "DUT143714")
    assert doc["schema"] == "supervised-preview-blocked/1"
    assert doc["reason"] == "credit_open_not_supported_v1"
    assert "body" not in doc
    with pytest.raises(ValueError):
        EntryRequest.model_validate(doc)  # a diagnostic is never a request
    with pytest.raises(ValueError):
        enter_supervised.complete_draft(doc, **live_bindings(SimpleNamespace(now=world_now())))


def test_a_credit_deal_in_the_queue_lands_as_blocked(world):
    deal = world.queue["admissible"][0]
    # a bear call credit vertical the miner contract accepts: floor credit
    # 2.00 on width 5, so max loss (5 - 2.00) x 100
    legs = [
        {**deal["legs"][0], "action": "SELL", "strike": "100"},
        {**deal["legs"][1], "action": "BUY", "strike": "105"},
    ]
    prices = {"limit": "2.00", "ref_mid": "2.05"}
    deal.update(kind="credit_vertical", legs=legs, fill="2.10", max_loss="300.00", **prices)
    deal["structure"].update(
        kind="credit_vertical", legs=legs, exits={"touch": False, "breach": True}, **prices
    )
    world.queue_file()
    doc = run(world)
    assert doc["written"] == [] and len(doc["blocked"]) == 1
    (blocked,) = list(drafts_dir(world).glob("*.BLOCKED.json"))
    assert json.loads(blocked.read_text())["reason"] == "credit_open_not_supported_v1"


def world_now():
    from tree_options.trex.clock import ET

    return datetime(2025, 3, 4, 10, 0, tzinfo=ET)


def test_the_ideas_view_lists_exactly_what_the_writer_wrote(world):
    """Wave 5: the drafts this writer leaves are what ``GET /api/ideas``
    lists, each named by the compose_entry args the desk's compose reads
    (``proposal-drafts/<candidate_id>.json``)."""
    from tree_options.trex_web.entry_ideas_view import ideas_payload

    world.queue_file()
    doc = run(world)
    payload = ideas_payload(world.root / "execution", world.root / "supervised", world.now)
    drafts = payload["drafts"]
    assert drafts["status"] == "ok" and drafts["problems"] == []
    assert [item["candidate_id"] for item in drafts["items"]] == doc["written"]
    (item,) = drafts["items"]
    draft = the_draft(world, doc)
    assert item["compose_args"] == {
        "program_id": enter_supervised.PROGRAM_ID,
        "candidate_id": draft["candidate_id"],
        "asset_kind": "option",
        "sleeve_ref": None,
    }
    assert item["intent_id"] == draft["body"]["effect"]["intent_id"]
    assert item["strategy_version"] == draft["body"]["strategy_version"]
    assert item["entry_session"] == draft["entry_session"]
    assert item["status"] == "available" and item["proposal_status"] == "none"


def test_the_ideas_view_lists_a_blocked_deal_as_blocked_never_as_an_idea(world):
    from tree_options.trex_web.entry_ideas_view import ideas_payload

    deal = world.queue["admissible"][0]
    legs = [
        {**deal["legs"][0], "action": "SELL", "strike": "100"},
        {**deal["legs"][1], "action": "BUY", "strike": "105"},
    ]
    prices = {"limit": "2.00", "ref_mid": "2.05"}
    deal.update(kind="credit_vertical", legs=legs, fill="2.10", max_loss="300.00", **prices)
    deal["structure"].update(
        kind="credit_vertical", legs=legs, exits={"touch": False, "breach": True}, **prices
    )
    world.queue_file()
    doc = run(world)
    drafts = ideas_payload(world.root / "execution", world.root / "supervised", world.now)["drafts"]
    assert drafts["items"] == []
    assert [(b["candidate_id"], b["reason"]) for b in drafts["blocked"]] == [
        (doc["blocked"][0], "credit_open_not_supported_v1")
    ]


def test_advice_from_the_lab_annotates_the_result(world, tmp_path, monkeypatch):
    """Three lab runs qualify a policy; the annotation is never promoted."""
    import tree_options.desk.lab as lab_module

    lab = tmp_path / "lab"
    for i in range(3):
        run_dir = lab / f"20260928T00000{i}Z-model-zai"
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps(
                {
                    "policy": "model:zai",
                    "boards_shown": 12,
                    "model_calls": 12,
                    "model_failures": 0,
                    "summary": {
                        "entered": 10,
                        "modeled_wins": 6,
                        "modeled_losses": 4,
                        "closed_capital_proxy": "5100",
                        "minimum_closed_capital_proxy": "5050",
                    },
                }
            )
        )
    monkeypatch.setattr(lab_module, "default_root", lambda: lab)
    world.queue_file()
    doc = run(world)
    advice = doc["advisory"]
    assert advice is not None and advice["policy"] == "model:zai"
    assert advice["promoted"] is False
    assert advice["stats"]["runs"] == 3
    assert doc["decisions"][0]["advice"] == advice
    from tree_options.desk.evidence import EvidenceStore

    with EvidenceStore(world.db, readonly=True) as store:
        records = store.all("supervised_preview")
    assert records and records[0]["advice"] == advice
