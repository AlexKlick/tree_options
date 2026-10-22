"""Wave 3 lane D: ``GET /api/activity`` (``trex-activity/1``, C3.2).

The feed merges what the producers already write: the web's command store
and the desk owner's answers (``commands/``), the desk's ``events.jsonl``,
the pause and kill-file audits (``pause-entries.jsonl``,
``automation.jsonl``) and lane H's drill records. Every file here is
written by its REAL producer (``CommandStore.submit``, the owner's
``apply_file_commands`` / ``CommandQueue`` with the desk's own event writer,
``trex.pause``, ``automation.kill_file_action``, the ``DrillRecord``
contract model). Oracles are those files read back plainly (never through
the view's code) and the contract model ``ActivityPage``, which every
response must validate as.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.trex_fakes import SessionClock
from tree_options.time.sessions import shift_instant
from tree_options.trex import pause
from tree_options.trex.contracts.acceptance import DrillRecord, EvidenceRef
from tree_options.trex.contracts.canonical import model_bytes, model_document, sha256_hex
from tree_options.trex.contracts.commands import Actor, AnyCommand, parse_command
from tree_options.trex.contracts.wave3 import (
    ACTIVITY_PATH,
    ACTIVITY_SOURCES,
    DESK_SYSTEM_SUB,
    AcceptanceDir,
    ActivityPage,
)
from tree_options.trex.desk_commands import CommandQueue, Outcome, applied
from tree_options.trex.desk_runtime import DeskPaths
from tree_options.trex.supervised_desk import _run_dir_event, apply_file_commands
from tree_options.trex_web import activity_view
from tree_options.trex_web.automation import kill_file_action
from tree_options.trex_web.command_store import CommandStore

OWNER = "owner@example.com"
FAMILY = {"sub": OWNER, "ingress": "family"}
DESK = {"sub": DESK_SYSTEM_SUB, "ingress": "system"}
PAPER_ACCOUNT = "DU7654321"


def instant(text: str) -> datetime:
    """A wire instant (``...Z`` or an offset) as an aware datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def jsonl(path: Path) -> list[dict[str, Any]]:
    """A producer's JSONL file read back plainly (the oracle)."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class EntryFlows:
    """The owner's entry flows: answers what it is told (the desk's real
    consumer applies it and writes the answer and its event)."""

    def __init__(self) -> None:
        self.answer: Outcome = applied()

    def compose(self, command: Any, now: datetime) -> Outcome:
        return self.answer

    def approve(self, command: Any, now: datetime) -> Outcome:
        return self.answer

    def reject(self, command: Any, now: datetime) -> Outcome:
        return self.answer

    def interrupted(self, command: AnyCommand) -> dict[str, Any]:
        return {}


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.clock = SessionClock()
        self.run_dir = tmp / "desk-paper"
        self.run_dir.mkdir()
        self.acceptance = tmp / "acceptance"
        self.acceptance.mkdir()
        self.paths = DeskPaths(self.run_dir)
        self.store = CommandStore(self.run_dir)
        self.flows = EntryFlows()
        self.client = self.mount(lambda: self.run_dir, lambda: self.acceptance)

    def mount(self, run_dir: Any, acceptance_dir: Any) -> TestClient:
        app = FastAPI()
        activity_view.attach(app, run_dir=run_dir, clock=self.clock, acceptance_dir=acceptance_dir)
        return TestClient(app)

    # -- producers -------------------------------------------------------------

    def command(
        self,
        kind: str,
        args: dict[str, Any],
        *,
        sub: str = OWNER,
        ingress: str = "family",
        ttl_s: int = 120,
        key: str | None = None,
    ) -> AnyCommand:
        now = self.clock.utc()
        body = {
            "schema": "trex-command/1",
            "idempotency_key": key or str(uuid.uuid4()),
            "kind": kind,
            "args": args,
            "issued_at": now.isoformat(),
            "expires_at": shift_instant(now, ttl_s).isoformat(),
        }
        raw = json.dumps(body).encode()
        actor = Actor.model_validate(
            {
                "sub": sub,
                "ingress": ingress,
                "request_id": f"web-{uuid.uuid4().hex}",
                "request_sha256": sha256_hex(raw),
            }
        )
        return parse_command({**body, "issued_by": model_document(actor)})

    def submit(self, kind: str, args: dict[str, Any], **kw: Any) -> AnyCommand:
        command = self.command(kind, args, **kw)
        self.store.submit(command, now=self.clock.utc())
        return command

    def apply_files(self) -> None:
        """The desk owner's kill-switch pass (the real one, no broker)."""
        assert (
            apply_file_commands(self.paths, owner_epoch="owner-test", clock=self.clock) is not None
        )

    def apply_all(self) -> None:
        """The owner's full pass, entry kinds included, with its event writer."""
        CommandQueue(
            self.run_dir,
            flows=self.flows,
            clock=self.clock,
            owner_epoch="owner-test",
            event=_run_dir_event(self.paths, self.clock),
        ).process()

    def desk_event(self, name: str, **payload: Any) -> None:
        _run_dir_event(self.paths, self.clock)(name, **payload)

    def drill(self, drill_id: str, status: str, *, evidence: str = "") -> DrillRecord:
        ran = status != "not_run"
        record = DrillRecord.model_validate(
            {
                "id": drill_id,
                "status": status,
                "evidence": [EvidenceRef(path=evidence, sha256="0" * 64)] if ran else [],
                "attested_by": OWNER if ran else None,
                "at": self.clock.utc() if ran else None,
                "note": "",
            }
        )
        path = AcceptanceDir(self.acceptance).drill(drill_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(model_bytes(record))
        return record

    # -- the view ----------------------------------------------------------------

    def get(self, client: TestClient | None = None, **params: Any) -> dict[str, Any]:
        response = (client or self.client).get(ACTIVITY_PATH, params=params)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        document = response.json()
        ActivityPage.model_validate(document)  # the contract is the oracle
        return document

    def items(self, **params: Any) -> list[dict[str, Any]]:
        return self.get(limit=200, **params)["items"]


@pytest.fixture()
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in items}


def statuses(document: dict[str, Any]) -> dict[str, tuple[str, str | None]]:
    return {s["source"]: (s["status"], s["unavailable_reason"]) for s in document["sources"]}


def all_ok(document: dict[str, Any]) -> bool:
    return all(status == ("ok", None) for status in statuses(document).values())


# -- the merged feed -----------------------------------------------------------------


def test_a_pause_from_the_phone_shows_as_its_command_the_desk_events_and_the_audit(rig):
    command = rig.submit("pause_entries", {"reason": "lunch"})
    rig.clock.advance(3)
    rig.apply_files()
    key = command.idempotency_key

    document = rig.get(limit=200)
    assert all_ok(document) and document["execution_enabled"] is False
    items = document["items"]
    answer = rig.store.read_result(key)
    assert answer is not None and answer.status == "applied"

    item = by_id(items)[f"command:{key}"]
    assert (item["source"], item["kind"], item["command_key"]) == ("command", "pause_entries", key)
    assert (item["command_status"], item["reason"]) == ("applied", None)
    assert item["actor"] == FAMILY and item["actor_unavailable_reason"] is None
    assert instant(item["at"]) == answer.at

    events = jsonl(rig.paths.events())
    desk = [i for i in items if i["source"] == "desk_event"]
    assert sorted(i["kind"] for i in desk) == sorted(e["event"] for e in events)
    assert {"entries_paused", "command_applied"} <= {i["kind"] for i in desk}
    assert all(i["actor"] == DESK and i["command_key"] is None for i in desk)
    assert sorted(instant(i["at"]) for i in desk) == sorted(instant(e["ts"]) for e in events)

    (audit_line,) = jsonl(rig.run_dir / pause.PAUSE_AUDIT_FILE)
    (audit,) = [i for i in items if i["source"] == "audit"]
    assert audit["kind"] == audit_line["event"] == "entries_paused"
    assert audit["actor"] == FAMILY  # the audit's ``family:<sub>`` actor
    assert instant(audit["at"]) == instant(audit_line["at"])

    assert len(items) == 1 + len(events) + 1
    assert [instant(i["at"]) for i in items] == sorted(
        (instant(i["at"]) for i in items), reverse=True
    )


def test_a_command_reads_queued_then_claimed_then_answered(rig):
    halt = rig.submit("halt", {"reason": ""})
    key = halt.idempotency_key
    queued = by_id(rig.items())[f"command:{key}"]
    assert queued["command_status"] == "accepted" and queued["actor"] == FAMILY
    assert "queued" in queued["summary"].lower()
    assert queued["summary"].startswith("Emergency stop (also stops exits)")

    folder = rig.run_dir / "commands"
    os.rename(folder / f"{key}.json", folder / f"{key}.claimed.json")  # the owner's claim
    claimed = by_id(rig.items())[f"command:{key}"]
    assert claimed["command_status"] == "accepted" and claimed["actor"] == FAMILY
    assert "being applied" in claimed["summary"].lower()
    os.rename(folder / f"{key}.claimed.json", folder / f"{key}.json")

    rig.clock.advance(2)
    rig.apply_files()
    answered = by_id(rig.items())[f"command:{key}"]
    assert answered["command_status"] == "applied"
    assert answered["actor"] == FAMILY  # read from the archived claim (commands/done/)
    assert (folder / "done" / f"{key}.claimed.json").exists()
    assert answered["refs"] == {"path": "HALT"}


def test_a_loopback_command_names_its_ingress(rig):
    command = rig.submit("flatten", {"reason": ""}, sub="loopback-operator", ingress="loopback")
    item = by_id(rig.items())[f"command:{command.idempotency_key}"]
    assert item["actor"] == {"sub": "loopback-operator", "ingress": "loopback"}


def test_a_command_refused_at_intake_has_its_reason_and_no_recorded_actor(rig):
    command = rig.command("pause_entries", {"reason": ""}, ttl_s=1)
    rig.clock.advance(5)
    submission = rig.store.submit(command, now=rig.clock.utc())
    assert submission.result.status == "refused"
    item = by_id(rig.items())[f"command:{command.idempotency_key}"]
    assert (item["command_status"], item["reason"]) == ("refused", submission.result.reason)
    assert item["reason"] == "command_expired"
    assert item["actor"] is None and item["actor_unavailable_reason"] == "actor_not_recorded"


def test_an_entry_answer_carries_its_refs_and_the_desk_reason(rig):
    proposal_id = "ab" * 32
    rig.flows.answer = Outcome(
        "refused", "approval_epoch_mismatch", "", {"intent_id": "eq-spy-001"}
    )
    command = rig.submit(
        "approve_entry",
        {
            "proposal_id": proposal_id,
            "proposal_sha256": "cd" * 32,
            "effect_sha256": "ef" * 32,
            "owner_epoch": "owner-old",
            "profile_digest": "01" * 32,
        },
    )
    rig.clock.advance(1)
    rig.apply_all()
    item = by_id(rig.items())[f"command:{command.idempotency_key}"]
    assert (item["command_status"], item["reason"]) == ("refused", "approval_epoch_mismatch")
    assert item["refs"] == {"proposal_id": proposal_id, "intent_id": "eq-spy-001"}
    assert item["actor"] == FAMILY


def test_an_answer_never_borrows_the_actor_of_a_different_document(rig):
    command = rig.submit("pause_entries", {"reason": "mine"})
    key = command.idempotency_key
    forged = rig.command("pause_entries", {"reason": "forged"}, sub="intruder@example.com", key=key)
    (rig.run_dir / "commands" / f"{key}.json").write_bytes(model_bytes(forged))
    rig.clock.advance(1)
    rig.apply_files()
    answer = rig.store.read_result(key)
    assert answer is not None and answer.reason == "command_body_mismatch"

    response = rig.client.get(ACTIVITY_PATH, params={"limit": 200})
    item = by_id(response.json()["items"])[f"command:{key}"]
    assert (item["command_status"], item["reason"]) == ("refused", "command_body_mismatch")
    assert item["actor"] is None and item["actor_unavailable_reason"] == "actor_not_recorded"
    assert "intruder" not in response.text and "forged" not in response.text


def test_desk_events_carry_their_ids_as_refs_and_never_the_account(rig):
    proposal_id = "ab" * 32
    rig.desk_event(
        "proposal_composed",
        proposal_id=proposal_id,
        intent="eq-spy-001",
        candidate="cand-1",
        program="prog-equity-1",
        asset_kind="equity",
        actor=f"family:{OWNER}",
        account=PAPER_ACCOUNT,
    )
    response = rig.client.get(ACTIVITY_PATH)
    (item,) = response.json()["items"]
    assert item["id"].startswith("desk_event:") and item["kind"] == "proposal_composed"
    assert item["actor"] == DESK
    assert item["refs"] == {
        "proposal_id": proposal_id,
        "intent_id": "eq-spy-001",
        "program_id": "prog-equity-1",
    }
    assert PAPER_ACCOUNT not in response.text


def test_free_text_is_shown_with_account_ids_and_secrets_redacted(rig):
    token = "sk_live_" + "Abc123XyZ" * 5
    reason = f"moving cash from {PAPER_ACCOUNT} and U1234567 token={token}"
    rig.submit("pause_entries", {"reason": reason})
    rig.clock.advance(1)
    rig.apply_files()
    pause.pause_entries(
        rig.run_dir, actor="operator-terminal", reason=f"secret: {token}", now=rig.clock.utc()
    )
    response = rig.client.get(ACTIVITY_PATH, params={"limit": 200})
    assert response.status_code == 200
    for leaked in (PAPER_ACCOUNT, "U1234567", token, token[8:]):
        assert leaked not in response.text
    summaries = " ".join(i["summary"] for i in response.json()["items"])
    assert "moving cash from" in summaries  # the owner's words stay


def test_kill_file_verbs_at_the_terminal_are_audit_items_without_an_actor(rig):
    kill_file_action(rig.run_dir, "halt")
    kill_file_action(rig.run_dir, "resume")
    lines = jsonl(rig.run_dir / "automation.jsonl")
    audits = [i for i in rig.items() if i["source"] == "audit"]
    assert len(audits) == len(lines) == 2
    by_kind = {i["kind"]: i for i in audits}
    placed, removed = by_kind["kill_file_placed"], by_kind["kill_files_removed"]
    assert placed["refs"] == {"path": "HALT"}
    assert "HALT" in removed["summary"] and "FLATTEN" in removed["summary"]
    for item in audits:
        assert item["actor"] is None and item["actor_unavailable_reason"] == "actor_not_recorded"
        assert item["command_key"] is None and item["command_status"] is None
    assert sorted(instant(i["at"]) for i in audits) == sorted(instant(x["at"]) for x in lines)


def test_a_terminal_pause_is_audited_without_a_verified_actor(rig):
    pause.pause_entries(rig.run_dir, actor="operator-terminal", reason="", now=rig.clock.utc())
    (item,) = rig.items(source="audit")
    assert item["kind"] == "entries_paused"
    assert item["actor"] is None and item["actor_unavailable_reason"] == "actor_not_recorded"
    assert "operator-terminal" in item["summary"]


def test_drills_that_ran_are_listed_from_the_acceptance_dir(rig):
    passed = rig.drill("relogin", "passed", evidence="drills/relogin.log")
    rig.clock.advance(60)
    failed = rig.drill("gateway_outage", "failed", evidence="drills/outage.log")
    rig.drill("notification", "not_run")
    document = rig.get(limit=200)
    assert all_ok(document)
    drills = by_id(document["items"])
    assert set(drills) == {"drill:relogin", "drill:gateway_outage"}
    item = drills["drill:relogin"]
    assert (item["source"], item["kind"]) == ("drill", "drill_passed")
    assert item["refs"] == {"drill_id": "relogin", "path": "drills/relogin.log"}
    assert instant(item["at"]) == passed.at
    assert OWNER in item["summary"]  # who attested it
    assert item["actor"] is None and item["actor_unavailable_reason"] == "actor_not_recorded"
    assert drills["drill:gateway_outage"]["kind"] == "drill_failed"
    assert instant(drills["drill:gateway_outage"]["at"]) == failed.at
    assert [i["id"] for i in document["items"]] == ["drill:gateway_outage", "drill:relogin"]


# -- unknown is unavailable, never empty --------------------------------------------


def test_missing_roots_report_unavailable_sources_never_an_empty_feed(rig, tmp_path):
    client = rig.mount(lambda: tmp_path / "no-desk", lambda: tmp_path / "no-acceptance")
    document = rig.get(client)
    assert document["items"] == [] and document["has_more"] is False
    assert statuses(document) == {
        "command": ("unavailable", "activity_source_unreadable"),
        "desk_event": ("unavailable", "activity_source_unreadable"),
        "audit": ("unavailable", "activity_source_unreadable"),
        "drill": ("unavailable", "acceptance_missing"),
    }
    assert [s["source"] for s in document["sources"]] == list(ACTIVITY_SOURCES)


def test_existing_roots_with_nothing_recorded_yet_are_ok_and_empty(rig):
    document = rig.get()
    assert document["items"] == [] and all_ok(document)


def test_an_unreadable_line_marks_desk_events_but_keeps_the_readable_ones(rig):
    rig.desk_event("gateway_connected")
    with rig.paths.events().open("a", encoding="utf-8") as stream:
        stream.write("{not json\n")
    rig.clock.advance(1)
    rig.desk_event("reconciliation_clean")
    document = rig.get()
    assert statuses(document)["desk_event"] == ("unavailable", "activity_source_unreadable")
    assert [i["kind"] for i in document["items"]] == ["reconciliation_clean", "gateway_connected"]


def test_a_line_still_being_written_is_not_an_error(rig):
    rig.desk_event("gateway_connected")
    with rig.paths.events().open("a", encoding="utf-8") as stream:
        stream.write('{"event": "entries_pau')  # no newline yet: an append in flight
    document = rig.get()
    assert statuses(document)["desk_event"] == ("ok", None)
    assert [i["kind"] for i in document["items"]] == ["gateway_connected"]


def test_an_unreadable_result_marks_commands_and_a_bad_drill_marks_drills(rig, tmp_path):
    good = rig.submit("pause_entries", {"reason": ""})
    other = tmp_path / "elsewhere.json"
    other.write_text("{}")
    os.symlink(other, rig.run_dir / "commands" / f"{uuid.uuid4()}.result.json")
    bad = AcceptanceDir(rig.acceptance).drill("relogin")
    bad.parent.mkdir(parents=True)
    bad.write_text('{"id": "relogin", "status": "passed"}')
    document = rig.get()
    assert statuses(document)["command"] == ("unavailable", "activity_source_unreadable")
    assert statuses(document)["drill"] == ("unavailable", "activity_source_unreadable")
    assert [i["id"] for i in document["items"]] == [f"command:{good.idempotency_key}"]


def test_the_feed_never_writes(rig):
    rig.submit("halt", {"reason": ""})
    rig.desk_event("gateway_connected")
    kill_file_action(rig.run_dir, "flatten")
    rig.drill("relogin", "passed", evidence="drills/relogin.log")

    def tree() -> dict[str, tuple[int, int]]:
        return {
            str(p): (p.lstat().st_size, p.lstat().st_mtime_ns)
            for root in (rig.run_dir, rig.acceptance)
            for p in sorted(root.rglob("*"))
        }

    before = tree()
    rig.get(limit=200)
    rig.get(limit=1, source="command")
    assert tree() == before


# -- paging ---------------------------------------------------------------------------


def busy_rig(rig: Rig) -> None:
    """Items from every source, some sharing a timestamp (ties)."""
    for n in range(4):
        rig.submit("pause_entries" if n % 2 == 0 else "resume_entries", {"reason": f"n{n}"})
        rig.clock.advance(1)
        rig.apply_files()
        for name in ("gateway_connected", "reconciliation_clean", "books_problem"):
            rig.desk_event(name)  # same instant: a tie broken by the id
        rig.clock.advance(7)
    rig.drill("relogin", "passed", evidence="drills/relogin.log")


def walk(rig: Rig, limit: int, **params: Any) -> list[list[dict[str, Any]]]:
    """Every page, following the cursors; an item served twice fails at once
    (a cursor that does not advance can never loop)."""
    pages: list[list[dict[str, Any]]] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        query = {"limit": limit, **params, **({"cursor": cursor} if cursor else {})}
        document = rig.get(**query)
        ids = [item["id"] for item in document["items"]]
        assert not seen & set(ids), f"page {len(pages) + 1} repeats {sorted(seen & set(ids))}"
        seen.update(ids)
        pages.append(document["items"])
        assert document["limit"] == limit
        if not document["has_more"]:
            assert document["next_cursor"] is None
            return pages
        assert document["next_cursor"] and len(document["items"]) == limit
        cursor = document["next_cursor"]


def test_pages_walk_the_whole_feed_newest_first_without_repeats_or_gaps(rig):
    busy_rig(rig)
    full = rig.items()
    assert len(full) > 20 and len({i["id"] for i in full}) == len(full)
    for limit in (1, 3, 7, 50):
        pages = walk(rig, limit)
        flat = [item for page in pages for item in page]
        assert flat == full
    ats = [instant(i["at"]) for i in full]
    assert ats == sorted(ats, reverse=True)
    assert len(ats) > len(set(ats))  # ties exist and still page cleanly


def test_a_cursor_stays_put_while_new_activity_arrives(rig):
    busy_rig(rig)
    full = rig.items()
    first = rig.get(limit=5)
    rig.clock.advance(30)
    rig.desk_event("gateway_connected")  # newer than every page already served
    rig.submit("halt", {"reason": "late"})
    second = rig.get(limit=5, cursor=first["next_cursor"])
    assert first["items"] + second["items"] == full[:10]


def test_the_source_filter_keeps_every_source_status(rig):
    busy_rig(rig)
    full = rig.items()
    document = rig.get(limit=200, source=["command", "drill"])
    assert [s["source"] for s in document["sources"]] == list(ACTIVITY_SOURCES)
    assert document["items"] == [i for i in full if i["source"] in ("command", "drill")]
    pages = walk(rig, 2, source="audit")
    assert [i for p in pages for i in p] == [i for i in full if i["source"] == "audit"]


def test_the_default_page_is_fifty_and_the_maximum_two_hundred(rig):
    for _ in range(205):
        rig.desk_event("gateway_connected")
        rig.clock.advance(1)
    document = rig.get()
    assert document["limit"] == 50 and len(document["items"]) == 50 and document["has_more"]
    assert len(rig.get(limit=200)["items"]) == 200
    assert len(rig.get(limit=1)["items"]) == 1


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "0"},
        {"limit": "201"},
        {"limit": "-1"},
        {"limit": "ten"},
        {"source": "orders"},
        {"cursor": "not a cursor!"},
        {"cursor": base64.urlsafe_b64encode(b"2026-10-22|nothing").decode().rstrip("=")},
        {"cursor": "A" * 201},
        [("limit", "5"), ("limit", "6")],
        [("source", "command"), ("source", "trades")],
    ],
)
def test_a_bad_query_is_refused_never_guessed(rig, params):
    response = rig.client.get(ACTIVITY_PATH, params=params)
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_query"
    assert response.headers["cache-control"] == "no-store"


def test_a_long_log_is_read_from_its_tail(rig, monkeypatch):
    monkeypatch.setattr(activity_view, "MAX_LOG_TAIL_BYTES", 600)
    for _ in range(30):
        rig.desk_event("gateway_connected")
        rig.clock.advance(1)
    events = jsonl(rig.paths.events())
    document = rig.get(limit=200)
    assert statuses(document)["desk_event"] == ("ok", None)
    shown = [instant(i["at"]) for i in document["items"]]
    newest = sorted((instant(e["ts"]) for e in events), reverse=True)
    assert 0 < len(shown) < len(events)
    assert shown == newest[: len(shown)]  # the newest lines, none torn


def test_past_the_command_horizon_the_newest_results_are_kept(rig, monkeypatch):
    monkeypatch.setattr(activity_view, "MAX_COMMAND_RESULTS", 3)
    keys = []
    for n in range(5):
        command = rig.submit("pause_entries", {"reason": f"n{n}"})
        keys.append(command.idempotency_key)
        result = rig.run_dir / "commands" / f"{command.idempotency_key}.result.json"
        os.utime(result, ns=(1_000_000_000 * (n + 1), 1_000_000_000 * (n + 1)))
        rig.clock.advance(1)
    document = rig.get(limit=200)
    assert statuses(document)["command"] == ("ok", None)
    assert [i["command_key"] for i in document["items"]] == keys[::-1][:3]
