"""The entries-only pause: ``PAUSE_ENTRIES`` in the desk run dir.

Oracles are the on-disk files (the pause file, the audit log, the kill
files the desk runtime reads) and the frozen contracts (layout names,
``Blocker`` / ``DeskEvent`` wire strings), never pause internals.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tree_options.trex import pause
from tree_options.trex.contracts import Blocker, DeskEvent, Wave2RunDir
from tree_options.trex.desk_runtime import DeskPaths

T0 = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 22, 15, 7, 30, tzinfo=UTC)


def _audit(run_dir: Path) -> list[dict]:
    lines = (run_dir / pause.PAUSE_AUDIT_FILE).read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_pause_writes_the_layout_file_with_actor_reason_and_time(tmp_path):
    doc = pause.pause_entries(tmp_path, actor="operator", reason="lunch", now=T0)
    path = Wave2RunDir(tmp_path).pause_entries()
    assert path.name == "PAUSE_ENTRIES" and path.exists()
    record = json.loads(path.read_text())
    assert record["schema"] == pause.PAUSE_RECORD_SCHEMA
    assert (record["actor"], record["reason"]) == ("operator", "lunch")
    assert datetime.fromisoformat(record["at"]) == T0
    assert pause.is_paused(tmp_path) is True
    assert pause.pause_record(tmp_path) == record
    assert doc["entries_paused"] is True and doc["changed"] is True


def test_resume_removes_the_file_and_both_steps_are_audited(tmp_path):
    pause.pause_entries(tmp_path, actor="operator", reason="lunch", now=T0)
    doc = pause.resume_entries(tmp_path, actor="owner", reason="back", now=T1)
    assert doc["entries_paused"] is False and doc["changed"] is True
    assert not Wave2RunDir(tmp_path).pause_entries().exists()
    assert pause.is_paused(tmp_path) is False and pause.pause_record(tmp_path) is None
    first, second = _audit(tmp_path)
    assert first["event"] == DeskEvent.ENTRIES_PAUSED.value
    assert (first["actor"], first["reason"], first["changed"]) == ("operator", "lunch", True)
    assert datetime.fromisoformat(first["at"]) == T0
    assert second["event"] == DeskEvent.ENTRIES_RESUMED.value
    assert (second["actor"], second["reason"], second["changed"]) == ("owner", "back", True)
    assert datetime.fromisoformat(second["at"]) == T1


def test_repeated_requests_are_idempotent_and_still_audited(tmp_path):
    pause.pause_entries(tmp_path, actor="a", reason="first", now=T0)
    original = Wave2RunDir(tmp_path).pause_entries().read_bytes()
    again = pause.pause_entries(tmp_path, actor="b", reason="second", now=T1)
    assert again["changed"] is False and again["entries_paused"] is True
    # the standing pause keeps who placed it and when
    assert Wave2RunDir(tmp_path).pause_entries().read_bytes() == original
    pause.resume_entries(tmp_path, actor="a", now=T1)
    idle = pause.resume_entries(tmp_path, actor="a", now=T1)
    assert idle["changed"] is False and idle["entries_paused"] is False
    assert [(a["event"], a["changed"]) for a in _audit(tmp_path)] == [
        ("entries_paused", True),
        ("entries_paused", False),
        ("entries_resumed", True),
        ("entries_resumed", False),
    ]


def test_the_audit_is_append_only(tmp_path):
    pause.pause_entries(tmp_path, actor="a", now=T0)
    before = (tmp_path / pause.PAUSE_AUDIT_FILE).read_bytes()
    pause.resume_entries(tmp_path, actor="a", now=T1)
    pause.pause_entries(tmp_path, actor="a", now=T1)
    after = (tmp_path / pause.PAUSE_AUDIT_FILE).read_bytes()
    assert after.startswith(before) and len(_audit(tmp_path)) == 3


@pytest.mark.parametrize("present", [True, False])
def test_pause_and_resume_never_touch_halt_or_flatten(tmp_path, present):
    halt, flatten = tmp_path / "HALT", tmp_path / "FLATTEN"
    if present:
        halt.write_text("operator halt")
        flatten.write_text("operator flatten")
    pause.pause_entries(tmp_path, actor="a", now=T0)
    pause.resume_entries(tmp_path, actor="a", now=T1)
    pause.pause_entries(tmp_path, actor="a", now=T1)
    assert halt.exists() is present and flatten.exists() is present
    if present:
        assert halt.read_text() == "operator halt"
        assert flatten.read_text() == "operator flatten"


def test_a_pause_refuses_entries_and_leaves_every_exit_input_unchanged(tmp_path):
    """Runtime-free: the desk runtime's exit lane reads exactly two files
    (``DeskPaths.halt`` and ``DeskPaths.flatten``, desk_runtime._tick).
    A pause changes neither, and the exit gate never names it."""
    desk = DeskPaths(tmp_path)
    exit_inputs = (desk.halt().exists(), desk.flatten().exists())
    assert pause.exit_blockers(tmp_path) == ()
    pause.pause_entries(tmp_path, actor="a", reason="x", now=T0)
    assert pause.pause_blockers(tmp_path) == (Blocker.ENTRIES_PAUSED.value,)
    assert pause.exit_blockers(tmp_path) == ()
    assert (desk.halt().exists(), desk.flatten().exists()) == exit_inputs


def test_halt_still_gates_exits_and_resume_entries_does_not_lift_it(tmp_path):
    (tmp_path / "HALT").touch()
    pause.pause_entries(tmp_path, actor="a", now=T0)
    assert pause.exit_blockers(tmp_path) == (pause.HALTED,)
    pause.resume_entries(tmp_path, actor="a", now=T1)
    assert (tmp_path / "HALT").exists()
    assert pause.exit_blockers(tmp_path) == (pause.HALTED,)
    assert pause.pause_blockers(tmp_path) == ()


def test_a_failed_write_leaves_no_partial_pause_and_no_audit(tmp_path, monkeypatch):
    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(pause.os, "replace", broken_replace)
    with pytest.raises(OSError):
        pause.pause_entries(tmp_path, actor="a", now=T0)
    assert not os.path.lexists(Wave2RunDir(tmp_path).pause_entries())
    assert not (tmp_path / pause.PAUSE_AUDIT_FILE).exists(), "nothing happened, nothing audited"
    assert sorted(p.name for p in tmp_path.iterdir()) == [], "no temp file left behind"


def test_an_unauditable_resume_never_stands(tmp_path, monkeypatch):
    """Resume is the unsafe direction: if its audit line cannot be written,
    the pause is restored (byte-identical) and the error surfaces."""
    pause.pause_entries(tmp_path, actor="a", reason="keep", now=T0)
    record = Wave2RunDir(tmp_path).pause_entries().read_bytes()

    def broken_audit(run_dir, line):
        raise OSError("audit unwritable")

    monkeypatch.setattr(pause, "_append_audit", broken_audit)
    with pytest.raises(OSError):
        pause.resume_entries(tmp_path, actor="a", now=T1)
    assert pause.is_paused(tmp_path)
    assert Wave2RunDir(tmp_path).pause_entries().read_bytes() == record


def test_an_unauditable_pause_still_pauses(tmp_path, monkeypatch):
    """Pause is the safe direction: a failing audit never delays it."""

    def broken_audit(run_dir, line):
        raise OSError("audit unwritable")

    monkeypatch.setattr(pause, "_append_audit", broken_audit)
    with pytest.raises(OSError):
        pause.pause_entries(tmp_path, actor="a", now=T0)
    assert pause.is_paused(tmp_path)


def test_a_dangling_pause_symlink_still_pauses_and_resume_removes_only_the_link(tmp_path):
    target = tmp_path / "elsewhere"
    os.symlink(target, Wave2RunDir(tmp_path).pause_entries())
    assert pause.is_paused(tmp_path)
    assert pause.pause_blockers(tmp_path) == (Blocker.ENTRIES_PAUSED.value,)
    assert pause.pause_record(tmp_path) == {"unreadable": True}
    target.write_text("not ours")
    pause.resume_entries(tmp_path, actor="a", now=T1)
    assert not os.path.lexists(Wave2RunDir(tmp_path).pause_entries())
    assert target.read_text() == "not ours"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"actor": "", "now": T0},
        {"actor": "   ", "now": T0},
        {"actor": "a", "now": datetime(2026, 10, 22, 15, 0)},  # naive
        {"actor": "a", "reason": "x" * 501, "now": T0},
        {"actor": "a\nb", "now": T0},
    ],
)
def test_invalid_requests_refuse_before_any_file(tmp_path, kwargs):
    with pytest.raises(ValueError):
        pause.pause_entries(tmp_path, **kwargs)
    with pytest.raises(ValueError):
        pause.resume_entries(tmp_path, **kwargs)
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


def test_a_reason_of_the_contract_maximum_is_accepted(tmp_path):
    pause.pause_entries(tmp_path, actor="a", reason="x" * 500, now=T0)
    assert pause.pause_record(tmp_path)["reason"] == "x" * 500


def test_the_command_key_rides_along_in_the_audit(tmp_path):
    key = "0a9b8c7d-6e5f-4a3b-8c1d-0e9f8a7b6c5d"
    pause.pause_entries(tmp_path, actor="a", now=T0, command_key=key)
    pause.resume_entries(tmp_path, actor="a", now=T1)
    first, second = _audit(tmp_path)
    assert first["command"] == key and second["command"] is None
    assert pause.pause_record(tmp_path) is None


def test_the_run_dir_is_created_on_demand(tmp_path):
    run_dir = tmp_path / "not" / "yet"
    pause.pause_entries(run_dir, actor="a", now=T0)
    assert pause.is_paused(run_dir)


def test_a_resume_issued_before_the_pause_command_leaves_it(tmp_path):
    """W2c refix: a pause placed by a queued command records its issue time;
    a queued resume issued earlier (delivered late) leaves it standing and
    says so, audited. The terminal's resume (no issue time) lifts it."""
    issued = datetime(2026, 10, 22, 14, 59, 50, tzinfo=UTC)
    pause.pause_entries(tmp_path, actor="web:owner", now=T0, issued_at=issued)
    assert pause.pause_record(tmp_path)["issued_at"] == "2026-10-22T14:59:50Z"
    older = datetime(2026, 10, 22, 14, 59, 40, tzinfo=UTC)
    doc = pause.resume_entries(tmp_path, actor="web:owner", now=T1, issued_at=older)
    assert doc == {"entries_paused": True, "changed": False, "superseded_by_newer_stop": True}
    assert pause.is_paused(tmp_path)
    assert _audit(tmp_path)[-1]["superseded_by_newer_stop"] is True
    newer = datetime(2026, 10, 22, 15, 0, 0, tzinfo=UTC)
    pause.pause_entries(tmp_path, actor="web:owner", now=T1, issued_at=newer)
    record = pause.pause_record(tmp_path)
    assert record["issued_at"] == "2026-10-22T15:00:00Z", "restamped"
    assert datetime.fromisoformat(record["at"]) == T0, "placed once, at T0"
    doc = pause.resume_entries(tmp_path, actor="operator", now=T1)
    assert doc["changed"] is True and not pause.is_paused(tmp_path)
