"""The cockpit automation surface's entries-only pause functions.

These are what the desk OWNER process calls to apply a queued
``pause_entries`` / ``resume_entries`` command (the web only queues it);
no HTTP routing here.
Oracles: the run dir's files and the frozen command fixture.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tests.unit.trex_fakes import contract_fixture
from tree_options.time.sessions import shift_instant
from tree_options.trex import pause
from tree_options.trex.contracts import parse_command
from tree_options.trex_web import automation

T0 = datetime(2026, 10, 22, 15, 0, 30, tzinfo=UTC)


def _audit(run_dir):
    return [
        json.loads(line) for line in (run_dir / pause.PAUSE_AUDIT_FILE).read_text().splitlines()
    ]


def test_pause_and_resume_actions_flip_only_the_pause_file(tmp_path):
    doc = automation.pause_entries_action(tmp_path, actor="family:alex", reason="away", now=T0)
    assert doc == {
        "entries_paused": True,
        "changed": True,
        "kill_files": [],
        "pause": pause.pause_record(tmp_path),
    }
    assert doc["pause"]["actor"] == "family:alex"
    doc = automation.resume_entries_action(tmp_path, actor="family:alex", now=T0)
    assert doc == {"entries_paused": False, "changed": True, "kill_files": [], "pause": None}
    assert [a["event"] for a in _audit(tmp_path)] == ["entries_paused", "entries_resumed"]


def test_pause_beside_halt_reports_the_kill_files_and_leaves_them(tmp_path):
    (tmp_path / "HALT").touch()
    doc = automation.pause_entries_action(tmp_path, actor="a", now=T0)
    assert doc["kill_files"] == ["HALT"] and doc["entries_paused"] is True
    doc = automation.resume_entries_action(tmp_path, actor="a", now=T0)
    assert doc["kill_files"] == ["HALT"] and (tmp_path / "HALT").exists()


def test_the_cockpit_resume_verb_never_lifts_the_entries_pause(tmp_path):
    automation.pause_entries_action(tmp_path, actor="a", now=T0)
    automation.kill_file_action(tmp_path, "halt")
    doc = automation.kill_file_action(tmp_path, "resume")
    assert doc["kill_files"] == []
    assert pause.is_paused(tmp_path), "HALT behavior unchanged; resume_entries is separate"


@pytest.mark.parametrize("verb", ["halt", "flatten"])
def test_halt_and_flatten_never_create_a_pause(tmp_path, verb):
    automation.kill_file_action(tmp_path, verb)
    assert not pause.is_paused(tmp_path)


def test_a_pause_command_applies_with_its_actor_reason_and_key(tmp_path):
    command = parse_command(contract_fixture("command_pause_entries.json"))
    doc = automation.apply_entries_command(tmp_path, command, now=T0)
    assert doc["entries_paused"] is True and doc["changed"] is True
    (line,) = _audit(tmp_path)
    assert line["command"] == command.idempotency_key
    assert line["actor"] == f"{command.issued_by.ingress}:{command.issued_by.sub}"
    assert line["reason"] == command.args.reason


def test_a_resume_command_applies(tmp_path):
    automation.pause_entries_action(tmp_path, actor="a", now=T0)
    doc = contract_fixture("command_pause_entries.json")
    doc["kind"] = "resume_entries"
    doc["args"] = {"reason": "back"}
    command = parse_command(doc)
    result = automation.apply_entries_command(tmp_path, command, now=T0)
    assert result["entries_paused"] is False and not pause.is_paused(tmp_path)
    assert _audit(tmp_path)[-1]["command"] == command.idempotency_key


@pytest.mark.parametrize("kind", ["halt", "flatten", "resume_all"])
def test_other_command_kinds_are_not_entries_commands(tmp_path, kind):
    doc = contract_fixture("command_pause_entries.json")
    doc["kind"] = kind
    with pytest.raises(ValueError):
        automation.apply_entries_command(tmp_path, parse_command(doc), now=T0)
    assert list(tmp_path.iterdir()) == []


def test_a_command_outside_its_window_is_not_applied(tmp_path):
    """The one validity rule (the web's intake uses the same): a command
    applies while issued_at <= now + 60 s and now < expires_at."""
    command = parse_command(contract_fixture("command_pause_entries.json"))
    refusals = {
        command.expires_at: "command_expired",
        shift_instant(command.expires_at, 3600): "command_expired",
        shift_instant(command.issued_at, -61): "command_not_yet_valid",
    }
    for refused, reason in refusals.items():
        with pytest.raises(ValueError, match=reason):
            automation.apply_entries_command(tmp_path, command, now=refused)
    assert not pause.is_paused(tmp_path)
    assert not (tmp_path / pause.PAUSE_AUDIT_FILE).exists()
    for applied in (
        shift_instant(command.issued_at, -60),  # client clock 60 s ahead: tolerated
        command.issued_at,
        shift_instant(command.expires_at, -1),
    ):
        assert automation.apply_entries_command(tmp_path, command, now=applied)["entries_paused"]


def test_automation_status_reports_the_pause(tmp_path):
    def fake(args):
        return ""

    assert automation.automation_status(tmp_path, systemctl=fake)["entries_paused"] is False
    automation.pause_entries_action(tmp_path, actor="a", now=T0)
    doc = automation.automation_status(tmp_path, systemctl=fake)
    assert doc["entries_paused"] is True and doc["kill_files"] == []
