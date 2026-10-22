"""Wave 4 lane H: drill checks and the owner's drill record (``trex.drills``).

A drill is the owner's act: a machine check gathers what the desk's own
files show over the drill's window, and the record is written only for an
owner email from ``TREX_OWNER_EMAILS`` with evidence under the acceptance
evidence root, every file hashed by this producer (oracle: ``hashlib`` over
the bytes on disk). A ``passed`` record needs a check of the same drill
whose every requirement held; the verdict itself is the contract's
(``contracts.acceptance``), never this module's.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_trex_acceptance import (
    every_30s,
    replace_minutes,
    sample,
    write_session_samples,
)
from tests.unit.trex_fakes import approve_command, contract_fixture, entry_proposal
from tree_options.trex import acceptance as acc
from tree_options.trex import drills
from tree_options.trex.clock import session_calendar
from tree_options.trex.contracts import acceptance as contract
from tree_options.trex.contracts.canonical import model_bytes
from tree_options.trex.contracts.commands import CommandResult
from tree_options.trex.contracts.wave3 import AcceptanceDir

OWNER = "owner@example.com"
OWNERS = frozenset({OWNER})
CAL = session_calendar()
NOV16 = date(2026, 11, 16)


def Z(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@pytest.fixture
def adir(tmp_path) -> AcceptanceDir:
    return AcceptanceDir(tmp_path / "acceptance")


@pytest.fixture
def sources(tmp_path) -> acc.Sources:
    run = tmp_path / "desk-paper"
    (run / "commands" / "done").mkdir(parents=True)
    sup = tmp_path / "supervised"
    sup.mkdir()
    return acc.Sources(run_dir=run, supervised_dir=sup, gateway_state=tmp_path / "gateway.json")


def desk_events(sources: acc.Sources, *events: dict[str, Any]) -> None:
    with (sources.run_dir / "events.jsonl").open("a") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


def gateway(sources: acc.Sources, *transitions: tuple[str, str, str]) -> None:
    events = [
        {"at": Z(at).timestamp(), "kind": "status", "detail": f"{a} -> {b}"}
        for at, a, b in transitions
    ]
    events.append({"at": Z("2026-11-16T15:00:00Z").timestamp(), "kind": "restart", "detail": "x"})
    sources.gateway_state.write_text(json.dumps({"status": "ok", "events": events}))


def check(
    drill_id: str, sources, adir, since="2026-11-16T15:00:00Z", until="2026-11-16T16:00:00Z", **kw
):
    facts = drills.load_facts(adir, sources, Z(since), Z(until), **kw)
    return drills.evaluate(drill_id, facts, since=Z(since), until=Z(until), checked_at=Z(until))


def met(result: drills.DrillCheck) -> dict[str, bool]:
    return {r.name: r.met for r in result.requirements}


# =============================================================================
# machine checks over the desk's own files
# =============================================================================


def test_relogin_needs_a_login_state_back_to_ok_and_a_desk_reconnect(sources, adir) -> None:
    gateway(
        sources,
        ("2026-11-16T15:10:00Z", "ok", "needs_login"),
        ("2026-11-16T15:14:00Z", "needs_login", "ok"),
    )
    got = check("relogin", sources, adir)
    assert met(got) == {
        "gateway_needed_login": True,
        "gateway_back_ok": True,
        "desk_reconnected": False,
    }
    assert got.ok is False
    desk_events(
        sources, {"ts": "2026-11-16T10:14:30-05:00", "event": "gateway_connected", "first": False}
    )
    got = check("relogin", sources, adir)
    assert got.ok is True and all(met(got).values())
    # a transition outside the window proves nothing
    assert check("relogin", sources, adir, since="2026-11-16T15:20:00Z").ok is False


def test_gateway_outage_needs_api_down_then_ok_and_a_reconnect(sources, adir) -> None:
    gateway(
        sources,
        ("2026-11-16T15:10:00Z", "ok", "api_down"),
        ("2026-11-16T15:12:00Z", "api_down", "ok"),
    )
    desk_events(
        sources, {"ts": "2026-11-16T10:12:30-05:00", "event": "gateway_connected", "first": False}
    )
    got = check("gateway_outage", sources, adir)
    assert met(got) == {
        "gateway_went_down": True,
        "gateway_back_ok": True,
        "desk_reconnected": True,
    }
    # a login prompt is the relogin drill, not an outage
    assert check("relogin", sources, adir).ok is False


def test_gateway_transitions_also_come_from_recorder_samples(sources, adir) -> None:
    for at, status in (("15:05:00", "ok"), ("15:06:00", "down"), ("15:08:00", "ok")):
        acc.append_sample(adir, sample(Z(f"2026-11-16T{at}Z"), gateway=status))
    desk_events(
        sources, {"ts": "2026-11-16T10:08:30-05:00", "event": "gateway_connected", "first": False}
    )
    assert check("gateway_outage", sources, adir).ok is True


def test_restart_mid_position(sources, adir) -> None:
    samples = every_30s(NOV16, exposed=True)
    # the owner is down 15:30:00Z..15:32:30Z (stale), back 15:33:00Z: 180 s unguarded
    samples = replace_minutes(samples, NOV16, range(60, 63), exposed=True, beat_age=200)
    write_session_samples(adir, samples)
    desk_events(
        sources, {"ts": "2026-11-16T10:32:40-05:00", "event": "gateway_connected", "first": True}
    )
    got = check("restart_mid_position", sources, adir)
    assert met(got) == {
        "position_open_before_restart": True,
        "desk_restarted": True,
        "exit_owner_fresh_after_restart": True,
        "exit_unguarded_within_limit": True,
    }
    assert got.ok is True
    assert {f.kind: f.detail for f in got.findings}["max_exit_unguarded_s"] == "180"


def test_restart_mid_position_too_long_down_or_flat(sources, adir) -> None:
    samples = replace_minutes(
        every_30s(NOV16, exposed=True), NOV16, range(60, 66), exposed=True, beat_age=200
    )
    write_session_samples(adir, samples)
    desk_events(
        sources, {"ts": "2026-11-16T10:35:40-05:00", "event": "gateway_connected", "first": True}
    )
    got = check("restart_mid_position", sources, adir)
    assert met(got)["exit_unguarded_within_limit"] is False  # 360 s > 300 s
    flat = AcceptanceDir(adir.root.parent / "flat")
    write_session_samples(flat, every_30s(NOV16))
    assert (
        met(check("restart_mid_position", sources, flat))["position_open_before_restart"] is False
    )


def write_result(
    sources: acc.Sources, key: str, status: str, at: str, kind: str = "approve_entry"
) -> None:
    doc = contract_fixture("command_result_applied.json")
    doc.update(idempotency_key=key, status=status, at=at, kind=kind)
    if status != "applied":
        doc["reason"] = "duplicate_command" if status == "duplicate" else "command_expired"
    result = CommandResult.model_validate(doc)
    (sources.run_dir / "commands" / f"{key}.result.json").write_bytes(model_bytes(result))


KEY = "6f1c2b8e-3d4a-4f5b-9c6d-7e8f9a0b1c2d"


def test_duplicate_command_applied_once(sources, adir) -> None:
    assert met(check("duplicate_command", sources, adir))["command_answered"] is False
    write_result(sources, KEY, "applied", "2026-11-16T15:10:00Z")
    desk_events(
        sources, {"ts": "2026-11-16T10:10:00-05:00", "event": "command_applied", "command": KEY}
    )
    (sources.run_dir / "commands" / "done" / f"{KEY}.duplicate-1.json").write_text("{}")
    got = check("duplicate_command", sources, adir)
    assert got.ok is True
    assert {f.kind for f in got.findings} >= {"duplicate_set_aside"}
    desk_events(
        sources, {"ts": "2026-11-16T10:10:05-05:00", "event": "command_applied", "command": KEY}
    )
    assert met(check("duplicate_command", sources, adir))["applied_at_most_once"] is False


def test_unassisted_use_needs_an_owner_command_through_the_portal(sources, adir) -> None:
    command = approve_command(entry_proposal())  # issued_by: the fixture's family actor
    (sources.run_dir / "commands" / f"{command.idempotency_key}.json").write_bytes(
        model_bytes(command)
    )
    write_result(sources, command.idempotency_key, "applied", "2026-11-16T15:10:00Z")
    assert check("unassisted_use", sources, adir).ok is (command.issued_by.ingress == "family")
    loopback = command.model_copy(
        update={"issued_by": command.issued_by.model_copy(update={"ingress": "loopback"})}
    )
    (sources.run_dir / "commands" / f"{command.idempotency_key}.json").write_bytes(
        model_bytes(loopback)
    )
    assert check("unassisted_use", sources, adir).ok is False


def test_notification_needs_an_alerting_event(sources, adir) -> None:
    assert check("notification", sources, adir).ok is False
    desk_events(
        sources, {"ts": "2026-11-16T10:20:00-05:00", "event": "exit_begin", "structure": "x"}
    )
    assert check("notification", sources, adir).ok is True


def test_notification_events_are_the_desk_runtime_ones() -> None:
    from tree_options.trex.desk_runtime import _NOTIFY_EVENTS

    assert drills.ALERTING_DESK_EVENTS == frozenset(_NOTIFY_EVENTS)


def write_receipt(path: Path, restored_at: str, restored=("run/book.json",)) -> None:
    path.mkdir(parents=True)
    receipt = {
        "schema": "trex-desk-restore/1",
        "backup": "/x",
        "restored_at": restored_at,
        "restored": list(restored),
        "quarantined": [],
    }
    (path / "RESTORE.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


def test_backup_restore_needs_a_receipt_from_the_window(sources, adir, tmp_path) -> None:
    assert check("backup_restore", sources, adir).ok is False
    write_receipt(tmp_path / "scratch", "2026-11-16T15:30:00+00:00")
    got = check("backup_restore", sources, adir, restore_dir=tmp_path / "scratch")
    assert got.ok is True
    write_receipt(tmp_path / "old", "2026-11-15T15:30:00+00:00")
    assert check("backup_restore", sources, adir, restore_dir=tmp_path / "old").ok is False
    write_receipt(tmp_path / "empty", "2026-11-16T15:30:00+00:00", restored=())
    assert check("backup_restore", sources, adir, restore_dir=tmp_path / "empty").ok is False


def test_run_check_writes_hashed_evidence_and_copies_the_receipt(sources, adir, tmp_path) -> None:
    write_receipt(tmp_path / "scratch", "2026-11-16T15:30:00+00:00")
    result, ref = drills.run_check(
        adir,
        sources,
        "backup_restore",
        since=Z("2026-11-16T15:00:00Z"),
        until=Z("2026-11-16T16:00:00Z"),
        now=Z("2026-11-16T16:05:00Z"),
        restore_dir=tmp_path / "scratch",
    )
    assert ref.path == "drills/backup_restore/20261116T160500Z-check.json"
    written = adir.evidence() / ref.path
    assert hashlib.sha256(written.read_bytes()).hexdigest() == ref.sha256
    assert drills.DrillCheck.model_validate_json(written.read_bytes()) == result
    (copy,) = result.artifacts
    assert copy.path == "drills/backup_restore/20261116T160500Z-RESTORE.json"
    copied = adir.evidence() / copy.path
    assert copied.read_bytes() == (tmp_path / "scratch/RESTORE.json").read_bytes()
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == copy.sha256


# =============================================================================
# the owner's record
# =============================================================================


def evidence_file(adir: AcceptanceDir, rel: str, text: str = "notes") -> str:
    path = adir.evidence() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return rel


def ok_check(adir, sources, drill_id: str = "relogin") -> str:
    gateway(
        sources,
        ("2026-11-16T15:10:00Z", "ok", "needs_login"),
        ("2026-11-16T15:14:00Z", "needs_login", "ok"),
    )
    desk_events(
        sources, {"ts": "2026-11-16T10:14:30-05:00", "event": "gateway_connected", "first": False}
    )
    _, ref = drills.run_check(
        adir,
        sources,
        drill_id,
        since=Z("2026-11-16T15:00:00Z"),
        until=Z("2026-11-16T16:00:00Z"),
        now=Z("2026-11-16T16:05:00Z"),
    )
    return ref.path


def record(adir, **kw: Any) -> contract.DrillRecord:
    values: dict[str, Any] = {
        "status": "passed",
        "attested_by": OWNER,
        "owners": OWNERS,
        "evidence": [],
        "copy": [],
        "at": Z("2026-11-16T16:10:00Z"),
        "now": Z("2026-11-16T16:30:00Z"),
        "note": "Logged back in from the phone.",
    }
    values.update(kw)
    drill_id = values.pop("drill_id", "relogin")
    return drills.record_drill(adir, drill_id, **values)


def test_passed_record_with_an_ok_check_and_owner_attestation(adir, sources) -> None:
    check_path = ok_check(adir, sources)
    notes = evidence_file(adir, "drills/relogin/notes.md")
    got = record(adir, evidence=[check_path, notes], attested_by=" Owner@Example.com ")
    stored = contract.DrillRecord.model_validate_json(adir.drill("relogin").read_bytes())
    assert stored == got
    assert (got.status, got.attested_by, got.at) == ("passed", OWNER, Z("2026-11-16T16:10:00Z"))
    for ref in got.evidence:
        assert hashlib.sha256((adir.evidence() / ref.path).read_bytes()).hexdigest() == ref.sha256
    assert [r.path for r in got.evidence] == [check_path, notes]
    doc = contract.compute_acceptance(
        sessions=[],
        lifecycles=[],
        drills=[stored],
        owners=OWNERS,
        calendar=CAL,
        computed_at=Z("2026-11-16T22:00:00Z"),
    )
    assert doc.drill_results[0].passed is True and doc.drills_passed == 1


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"attested_by": "someone@example.com"}, "attester_not_owner"),
        ({"owners": frozenset()}, "owners_not_configured"),
        ({"evidence": ["../outside.txt"]}, "evidence_path_invalid"),
        ({"evidence": ["drills/relogin/absent.md"]}, "evidence_missing"),
        ({"evidence": []}, "evidence_required"),
        ({"at": Z("2026-11-16T16:10:00Z"), "now": Z("2026-11-16T16:00:00Z")}, "drill_at_in_future"),
        ({"drill_id": "fire_drill"}, "drill_unknown"),
    ],
)
def test_record_refusals_write_nothing(adir, sources, change, code) -> None:
    with pytest.raises(drills.DrillRefused, match=code):
        record(adir, **{"evidence": [evidence_file(adir, "drills/relogin/n.md")], **change})
    assert not adir.drill("relogin").exists()


def test_symlinked_evidence_is_refused(adir, tmp_path) -> None:
    real = tmp_path / "real.md"
    real.write_text("x")
    (adir.evidence() / "drills/relogin").mkdir(parents=True)
    (adir.evidence() / "drills/relogin/link.md").symlink_to(real)
    with pytest.raises(drills.DrillRefused, match="evidence_not_regular_file"):
        record(adir, status="failed", evidence=["drills/relogin/link.md"])


def test_passed_needs_an_ok_check_of_the_same_drill(adir, sources) -> None:
    notes = evidence_file(adir, "drills/relogin/notes.md")
    with pytest.raises(drills.DrillRefused, match="drill_check_not_ok"):
        record(adir, evidence=[notes])
    other = ok_check(adir, sources, "gateway_outage")  # not ok, and another drill
    with pytest.raises(drills.DrillRefused, match="drill_check_not_ok"):
        record(adir, evidence=[notes, other])
    # a failed drill is recorded on the owner's word with its evidence
    failed = record(adir, status="failed", evidence=[notes])
    assert failed.status == "failed"


def test_rerecording_keeps_the_previous_record_in_history(adir, sources) -> None:
    notes = evidence_file(adir, "drills/relogin/notes.md")
    first = record(adir, status="failed", evidence=[notes])
    second = record(adir, evidence=[ok_check(adir, sources), notes])
    history = adir.evidence() / "drills/relogin/history.jsonl"
    (line,) = history.read_bytes().splitlines()
    assert contract.DrillRecord.model_validate_json(line) == first
    assert contract.DrillRecord.model_validate_json(adir.drill("relogin").read_bytes()) == second


def test_copied_evidence_lands_under_the_evidence_root(adir, sources, tmp_path) -> None:
    shot = tmp_path / "phone screenshot.png"
    shot.write_bytes(b"\x89PNG fake")
    got = record(adir, status="failed", copy=[shot], now=Z("2026-11-16T16:20:00Z"))
    (ref,) = got.evidence
    assert ref.path == "drills/relogin/20261116T162000Z-phone_screenshot.png"
    assert (adir.evidence() / ref.path).read_bytes() == b"\x89PNG fake"
    assert ref.sha256 == hashlib.sha256(b"\x89PNG fake").hexdigest()


def test_cli_check_and_record(adir, sources, capsys) -> None:
    gateway(
        sources,
        ("2026-11-16T15:10:00Z", "ok", "needs_login"),
        ("2026-11-16T15:14:00Z", "needs_login", "ok"),
    )
    desk_events(
        sources, {"ts": "2026-11-16T10:14:30-05:00", "event": "gateway_connected", "first": False}
    )
    common = [
        "--acceptance-dir",
        str(adir.root),
        "--run-dir",
        str(sources.run_dir),
        "--gateway-state",
        str(sources.gateway_state),
    ]
    env = {"TREX_OWNER_EMAILS": OWNER}
    code = drills.main(
        [
            "check",
            "relogin",
            "--since",
            "2026-11-16T15:00:00Z",
            "--until",
            "2026-11-16T16:00:00Z",
            *common,
        ],
        env=env,
        now=Z("2026-11-16T16:05:00Z"),
    )
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and out["ok"] is True
    code = drills.main(
        [
            "record",
            "relogin",
            "--status",
            "passed",
            "--attested-by",
            OWNER,
            "--evidence",
            out["path"],
            "--note",
            "ok",
            *common,
        ],
        env=env,
        now=Z("2026-11-16T16:10:00Z"),
    )
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and out["status"] == "passed" and out["live_authorized"] is False
    code = drills.main(
        [
            "record",
            "relogin",
            "--status",
            "passed",
            "--attested-by",
            "x@example.com",
            "--evidence",
            "a",
            *common,
        ],
        env=env,
        now=Z("2026-11-16T16:10:00Z"),
    )
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 2 and out["refused"] == "attester_not_owner"
