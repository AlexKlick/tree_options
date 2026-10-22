"""Wave 5: ``GET /api/ideas`` (``trex-idea-list/1``), what the owner may compose.

Built over temp run and supervised dirs only (never the host's state). The
drafts are written the way their producers write them (the miner's option
draft shape, ``desk_entries.equity_draft``, a BLOCKED diagnostic); the
proposals and mandates are the frozen contract fixtures. Oracles: the
fixture ``tests/contracts/fixtures/wave5/idea_list.json`` (the document this
world serves, pinned), the contract models, and the desk's own file names.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.unit.trex_fakes import entry_proposal, equity_effect, mandate_v2
from tree_options.desk.enter_supervised import DRAFT_SCHEMA, LIVE_BINDINGS, draft_directory
from tree_options.trex import pause
from tree_options.trex.contracts.canonical import model_bytes, model_document
from tree_options.trex.contracts.effects import SupervisedEffect, effect_v1_sha256
from tree_options.trex.contracts.ideas import IdeaList
from tree_options.trex.contracts.layout import Wave2RunDir, Wave2SupervisedDir
from tree_options.trex.contracts.proposals import PROPOSAL_SCHEMA, proposal_bytes
from tree_options.trex.desk_entries import equity_draft
from tree_options.trex.plan import LegStructure
from tree_options.trex.supervised_ibkr import supervised_order_ref
from tree_options.trex_web.app import create_app
from tree_options.trex_web.entry_ideas_view import ideas_payload

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "contracts" / "fixtures" / "wave5" / "idea_list.json"
#: 10:02 ET on Thursday 2026-10-22: inside the session and the equity window
NOW = datetime(2026, 10, 22, 14, 2, tzinfo=UTC)
WRITTEN = datetime(2026, 10, 22, 13, 57, tzinfo=UTC)
VALID_UNTIL = datetime(2026, 10, 22, 15, 30, tzinfo=UTC)


def _write(path: Path, payload: bytes, at: datetime = WRITTEN) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    stamp = at.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def option_draft(intent_id: str = "opt-qqq-001") -> dict[str, Any]:
    """The miner's draft shape (``enter_supervised._preview_doc``)."""
    structure = LegStructure.model_validate(
        {
            "id": intent_id,
            "underlying": "QQQ",
            "kind": "debit_vertical",
            "legs": [
                {"right": "P", "action": "BUY", "strike": "500", "expiry": "2026-11-20"},
                {"right": "P", "action": "SELL", "strike": "495", "expiry": "2026-11-20"},
            ],
            "quantity": 1,
            "entry_date": "2026-10-22",
            "exit_deadline": "2026-10-23",
            "limit": "1.00",
            "exits": {"touch": False, "breach": False},
        }
    )
    effect = SupervisedEffect(
        intent_id=intent_id,
        account_id="DUT143714",
        structure=structure,
        side=structure.open_side,
        quantity=1,
        limit=Decimal("0.90"),
        order_ref=supervised_order_ref(intent_id),
    )
    return {
        "schema": DRAFT_SCHEMA,
        "draft_of": PROPOSAL_SCHEMA,
        "deal_id": intent_id,
        "candidate_id": intent_id,
        "row": "R1",
        "kind": "debit_vertical",
        "entry_session": "2026-10-22",
        "valid_until": "2026-10-22T15:30:00Z",
        "requested_by": "desk-enter-shadow",
        "execution_enabled": False,
        "missing": list(LIVE_BINDINGS),
        "body": {
            "schema": PROPOSAL_SCHEMA,
            "effect": model_document(effect),
            "effect_sha256": effect_v1_sha256(effect),
            "strategy_version": "desk-row/R1",
            "program_id": "desk-miner",
            "sleeve_ref": None,
            "proposed_at": None,
            "send_deadline": None,
            "review": {
                "quote": None,
                "max_loss_cents": 9000,
                "notional_cents": 9000,
                "screening_sha256": None,
                "owner_epoch": None,
                "profile_digest": None,
            },
        },
    }


def world(root: Path) -> tuple[Path, Path]:
    """One option draft, one equity draft with a pending proposal, one
    BLOCKED diagnostic, one unparseable file."""
    run, sup = root / "run", root / "supervised"
    drafts = draft_directory(run)
    _write(drafts / "opt-qqq-001.json", json.dumps(option_draft()).encode())
    doc = equity_draft(
        equity_effect(),
        program_id="prog-equity-pilot",
        strategy_version="equity-pilot/1",
        valid_until=VALID_UNTIL,
    )
    _write(drafts / "eq-spy-001.json", json.dumps(doc).encode())
    blocked = {
        "schema": "supervised-preview-blocked/1",
        "deal_id": "d-credit",
        "candidate_id": "d-credit",
        "row": "R2",
        "kind": "credit_vertical",
        "reason": "credit_open_not_supported_v1",
        "execution_enabled": False,
    }
    _write(drafts / "d-credit.BLOCKED.json", json.dumps(blocked).encode())
    _write(drafts / "broken.json", b"{not json")
    _write(drafts / "opt-qqq-001.json.tmp", b"half written")  # a writer's temp file
    proposal = entry_proposal()
    _write(Wave2RunDir(run).proposal(proposal.proposal_id), proposal_bytes(proposal))
    sup.mkdir(parents=True, exist_ok=True)
    return run, sup


def items(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["candidate_id"]: item for item in payload["drafts"]["items"]}


# ------------------------------------------------------------- the document


def test_the_fixture_is_what_the_view_serves(tmp_path):
    run, sup = world(tmp_path)
    payload = ideas_payload(run, sup, NOW)
    assert payload == json.loads(FIXTURE.read_text())
    assert model_document(IdeaList.model_validate(payload)) == payload


def test_each_idea_repeats_the_compose_args_the_desk_reads(tmp_path):
    run, sup = world(tmp_path)
    payload = ideas_payload(run, sup, NOW)
    found = items(payload)
    assert sorted(found) == ["eq-spy-001", "opt-qqq-001"], "temp files and BLOCKED are not ideas"
    for candidate, item in found.items():
        args = item["compose_args"]
        assert args == {
            "program_id": item["program_id"],
            "candidate_id": candidate,
            "asset_kind": item["asset_kind"],
            "sleeve_ref": None,
        }
        # the desk's compose reads exactly proposal-drafts/<candidate_id>.json
        assert (draft_directory(run) / f"{args['candidate_id']}.json").is_file()
    option, equity = found["opt-qqq-001"], found["eq-spy-001"]
    assert (option["asset_kind"], option["underlying"], option["equity"]) == ("option", "QQQ", None)
    assert option["option"]["limit_cents"] == 90
    assert [leg["strike_cents"] for leg in option["option"]["legs"]] == [50000, 49500]
    assert option["max_loss_cents"] == option["notional_cents"] == 9000
    assert (equity["asset_kind"], equity["underlying"], equity["option"]) == ("equity", "SPY", None)
    # the contract arithmetic: 2 x 671.50; its 20% gap reserve beats the stop loss
    assert (equity["notional_cents"], equity["max_loss_cents"]) == (134300, 26860)
    assert equity["equity"]["stop_price_cents"] == 66500
    assert (option["age_s"], option["seconds_to_expiry"]) == (300, 5280)
    assert payload["drafts"]["blocked"] == [
        {
            "candidate_id": "d-credit",
            "kind": "credit_vertical",
            "reason": "credit_open_not_supported_v1",
            "written_at": "2026-10-22T13:57:00Z",
        }
    ]
    assert payload["drafts"]["problems"] == [{"file": "broken.json", "reason": "candidate_invalid"}]
    assert payload["execution_enabled"] is False


def test_a_live_proposal_marks_its_idea_proposed_and_a_consumed_one_used(tmp_path):
    run, sup = world(tmp_path)
    proposal = entry_proposal()
    equity = items(ideas_payload(run, sup, NOW))["eq-spy-001"]
    assert (equity["status"], equity["proposal_status"]) == ("proposed", "pending")
    assert equity["proposal_id"] == proposal.proposal_id
    option = items(ideas_payload(run, sup, NOW))["opt-qqq-001"]
    assert (option["status"], option["proposal_status"], option["proposal_id"]) == (
        "available",
        "none",
        None,
    )
    # the approval was spent: the intent can never be sent again
    consumed = Wave2SupervisedDir(sup).mandate_consumed("eq-spy-001")
    _write(consumed, model_bytes(mandate_v2(proposal)))
    equity = items(ideas_payload(run, sup, NOW))["eq-spy-001"]
    assert (equity["status"], equity["proposal_status"]) == ("used", "consumed")
    assert "intent_already_used" in equity["hints"]


def test_a_rejected_proposal_frees_the_idea_and_an_outbox_entry_spends_it(tmp_path):
    run, sup = world(tmp_path)
    proposal = entry_proposal()
    _write(Wave2RunDir(run).proposal_rejected(proposal.proposal_id), b"{}")
    equity = items(ideas_payload(run, sup, NOW))["eq-spy-001"]
    assert (equity["status"], equity["proposal_status"]) == ("available", "rejected")
    from tree_options.trex.supervised import SupervisedPaths

    _write(SupervisedPaths(sup).terminal("eq-spy-001"), b"{}")
    equity = items(ideas_payload(run, sup, NOW))["eq-spy-001"]
    assert (equity["status"], equity["proposal_status"]) == ("used", "rejected")


def test_an_expired_draft_is_expired_with_its_hint(tmp_path):
    run, sup = world(tmp_path)
    later = datetime(2026, 10, 22, 15, 30, tzinfo=UTC)  # exactly valid_until
    option = items(ideas_payload(run, sup, later))["opt-qqq-001"]
    assert (option["status"], option["seconds_to_expiry"]) == ("expired", 0)
    assert "candidate_expired" in option["hints"]


# ------------------------------------------------------------- hints


def test_hints_follow_the_stops_the_profile_and_the_window(tmp_path):
    run, sup = world(tmp_path)
    (run / "HALT").write_text("{}")
    pause.pause_entries(run, actor="owner", now=NOW)
    payload = ideas_payload(run, sup, NOW)
    for item in items(payload).values():
        assert {"halt", "entries_paused", "profile_absent"} <= set(item["hints"])
    assert {"halt", "entries_paused"} <= set(payload["manual_equity"]["hints"])
    # a profile whose per-trade cap the equity idea exceeds (26860 > 10000)
    (run / "profile.json").write_text(
        json.dumps(
            {
                "profile_id": "owner-paper",
                "revision": 1,
                "intended_capital": "5000",
                "risk_style": "defined-risk",
                "goals": ["paper-desk"],
                "allowed_strategy_versions": ["equity-pilot/1"],
                "max_loss_per_trade": "100",
                "max_open_loss": "400",
                "max_daily_loss": "600",
                "horizon_days": 30,
            }
        )
    )
    found = items(ideas_payload(run, sup, NOW))
    assert "trade_loss_cap_exceeded" in found["eq-spy-001"]["hints"]
    assert "trade_loss_cap_exceeded" not in found["opt-qqq-001"]["hints"], "9000 <= 10000"
    assert "profile_absent" not in found["eq-spy-001"]["hints"]
    # 15:45 ET: the session is open, the equity entry window is closed
    late = datetime(2026, 10, 22, 19, 45, tzinfo=UTC)
    payload = ideas_payload(run, sup, late)
    late_items = items(payload)
    assert "entry_window_closed" in late_items["eq-spy-001"]["hints"]
    assert "entry_window_closed" not in late_items["opt-qqq-001"]["hints"]
    assert payload["manual_equity"]["window_status"] == "closed"
    assert "entry_window_closed" in payload["manual_equity"]["hints"]


def test_the_manual_terms_name_the_allowlist_and_todays_window(tmp_path):
    run, sup = world(tmp_path)
    terms = ideas_payload(run, sup, NOW)["manual_equity"]
    assert terms["candidate_prefix"] == "manual:"
    assert (terms["program_id"], terms["strategy_version"]) == ("owner-manual", "owner-manual/1")
    assert terms["symbols"] == ["SPY"]
    assert terms["limit_policies"] == ["ask", "mid", "limit_cents"]
    assert terms["max_entries_per_session"] == 3
    assert (terms["window_status"], terms["window_session"]) == ("open", "2026-10-22")
    # 09:35 to 15:30 ET (13:35Z to 19:30Z in EDT)
    assert (terms["window_opens_at"], terms["window_closes_at"]) == (
        "2026-10-22T13:35:00Z",
        "2026-10-22T19:30:00Z",
    )
    saturday = ideas_payload(run, sup, datetime(2026, 10, 24, 15, 0, tzinfo=UTC))["manual_equity"]
    assert (saturday["window_status"], saturday["window_session"]) == ("no_session", None)
    assert "outside_regular_session" in saturday["hints"]


# ------------------------------------------------------------- unreadable


def test_unreadable_entries_are_problems_never_dropped(tmp_path):
    run, sup = world(tmp_path)
    drafts = draft_directory(run)
    os.symlink(drafts / "opt-qqq-001.json", drafts / "linked.json")
    _write(drafts / "huge.json", b" " * (256 * 1024 + 1))
    _write(drafts / "other.json", json.dumps({"schema": "something-else/1"}).encode())
    bad_effect = option_draft("bad-001")
    bad_effect["body"]["effect"]["quantity"] = 0
    _write(drafts / "bad-001.json", json.dumps(bad_effect).encode())
    payload = ideas_payload(run, sup, NOW)
    assert payload["drafts"]["problems"] == [
        {"file": "bad-001.json", "reason": "candidate_invalid"},
        {"file": "broken.json", "reason": "candidate_invalid"},
        {"file": "huge.json", "reason": "draft_unreadable"},
        {"file": "linked.json", "reason": "draft_unreadable"},
        {"file": "other.json", "reason": "candidate_invalid"},
    ]
    assert sorted(items(payload)) == ["eq-spy-001", "opt-qqq-001"]


def test_a_missing_drafts_dir_is_empty_and_an_unlistable_one_unavailable(tmp_path):
    run, sup = tmp_path / "run", tmp_path / "supervised"
    empty = ideas_payload(run, sup, NOW)["drafts"]
    assert empty == {
        "status": "ok",
        "unavailable_reason": None,
        "items": [],
        "blocked": [],
        "problems": [],
        "truncated": False,
    }
    _write(draft_directory(run), b"a file, not a directory")
    down = ideas_payload(run, sup, NOW)
    assert down["drafts"] == {
        "status": "unavailable",
        "unavailable_reason": "drafts_dir_unreadable",
        "items": None,
        "blocked": None,
        "problems": None,
        "truncated": None,
    }
    assert down["manual_equity"]["symbols"] == ["SPY"], "the ticket terms stay known"
    draft_directory(run).unlink()
    real = tmp_path / "elsewhere"
    real.mkdir()
    os.symlink(real, draft_directory(run))
    linked = ideas_payload(run, sup, NOW)["drafts"]
    assert linked["unavailable_reason"] == "drafts_dir_unreadable"


def test_an_unlistable_proposals_dir_makes_the_drafts_unavailable(tmp_path):
    run, sup = world(tmp_path)
    proposals = Wave2RunDir(run).proposals()
    for path in proposals.iterdir():
        path.unlink()
    proposals.rmdir()
    _write(proposals, b"a file, not a directory")
    drafts = ideas_payload(run, sup, NOW)["drafts"]
    assert (drafts["status"], drafts["unavailable_reason"]) == (
        "unavailable",
        "proposals_dir_unreadable",
    )


# ------------------------------------------------------------- the route


def _client(root: Path, now: datetime = NOW) -> TestClient:
    app = create_app(
        state_dir=str(root / "legacy"),
        plans_dir=str(root / "plans"),
        discovery_dir=str(root / "discovery"),
        desk_state_dir=str(root / "desk-state"),
        desk_store_dir=str(root / "desk-store"),
        desk_run_dir=str(root / "run"),
        supervised_dir=str(root / "supervised"),
        command_clock=lambda: now,
    )
    return TestClient(app, base_url="http://127.0.0.1:8090", client=("127.0.0.1", 1))


def test_create_app_serves_the_ideas_read_only(tmp_path):
    run, sup = world(tmp_path)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    response = _client(tmp_path).get("/api/ideas")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == ideas_payload(run, sup, NOW)
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert [p for p in after if p.startswith(("run", "supervised"))] == [
        p for p in before if p.startswith(("run", "supervised"))
    ], "a read never writes the desk's directories"


def test_serving_the_ideas_never_imports_a_broker_library(tmp_path):
    world(tmp_path)
    script = f"""
import json, sys
from datetime import UTC, datetime
from fastapi.testclient import TestClient
from tree_options.trex_web.app import create_app
root = {str(tmp_path)!r}
app = create_app(state_dir=root + "/legacy", plans_dir=root + "/plans",
                 discovery_dir=root + "/discovery", desk_state_dir=root + "/desk-state",
                 desk_store_dir=root + "/desk-store", desk_run_dir=root + "/run",
                 supervised_dir=root + "/supervised",
                 command_clock=lambda: datetime(2026, 10, 22, 14, 2, tzinfo=UTC))
client = TestClient(app, base_url="http://127.0.0.1:8090", client=("127.0.0.1", 1))
response = client.get("/api/ideas")
loaded = sorted(m for m in sys.modules if m.split(".")[0] in ("ib_async", "ib_insync", "eventkit"))
print(json.dumps({{"code": response.status_code, "items": len(response.json()["drafts"]["items"]),
                   "loaded": loaded}}))
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(REPO / "src"),
    }
    done = subprocess.run(
        [sys.executable, "-B", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report == {"code": 200, "items": 2, "loaded": []}


@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_the_ideas_route_takes_no_writes(tmp_path, method):
    world(tmp_path)
    response = getattr(_client(tmp_path), method)("/api/ideas")
    assert response.status_code == 405
