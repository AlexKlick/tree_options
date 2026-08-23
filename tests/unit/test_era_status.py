"""era_status CLI: read-only proof, legacy detection, exit-code contract."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import era_status  # noqa: E402
from tree_options.runstate import RunIdentity, RunState, RunStore  # noqa: E402

BOOT = "11111111-2222-3333-4444-555555555555"
T0 = 1_800_000_000
RUN_ID = "m4-statustest-20260823-abcdef12"


def _create_store(root: Path, *, to: RunState = RunState.PLANNED) -> RunStore:
    identity = RunIdentity(
        run_id=RUN_ID,
        campaign="m4-statustest",
        protocol_hash="a" * 64,
        code_sha="b" * 40,
        provider="massive-polygon-free/1",
        capture_version="m4b-capture/1",
        universe_manifest_sha256="c" * 64,
        boot_id=BOOT,
        pid=1,
        pid_start_ticks=1,
        started_epoch=T0,
        args_hash="d" * 64,
    )
    store = RunStore.create(root, identity, now_epoch=T0)
    for state in _path_to(to):
        store.transition(state, reason="test", now_epoch=T0 + 1, actor_pid=1, actor_boot_id=BOOT)
    return store


def _path_to(target: RunState) -> list[RunState]:
    """Legal intermediate states from PLANNED to the requested target."""
    if target is RunState.PLANNED:
        return []
    chain: dict[RunState, list[RunState]] = {
        RunState.CAPTURING: [RunState.CAPTURING],
        RunState.CAPTURE_COMPLETE: [RunState.CAPTURING, RunState.CAPTURE_COMPLETE],
    }
    return chain[target]


def _status(root: Path, *args: str) -> int:
    return era_status.main(
        [
            "--store-root",
            str(root),
            "--run-id",
            RUN_ID,
            "--boot-id-override",
            BOOT,
            "--now-epoch",
            str(T0 + 60),
            *args,
        ]
    )


def test_status_is_read_only_no_mtime_changes(tmp_path, capsys):
    root = tmp_path / "runstate"
    _create_store(root)
    store_dir = root / RUN_ID
    before = {
        p: (p.stat().st_mtime_ns, p.stat().st_size)
        for p in sorted(store_dir.rglob("*"))
        if p.is_file()
    }
    assert _status(root) == 0
    capsys.readouterr()
    after = {
        p: (p.stat().st_mtime_ns, p.stat().st_size)
        for p in sorted(store_dir.rglob("*"))
        if p.is_file()
    }
    assert before == after


def test_status_reports_state_and_exits_zero(tmp_path, capsys):
    root = tmp_path / "runstate"
    _create_store(root, to=RunState.CAPTURE_COMPLETE)
    assert _status(root) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["state"] == "CAPTURE_COMPLETE"
    assert payload["classification"] == "ALIVE"  # no process expected
    assert payload["seq"] == 3  # GENESIS + CAPTURING + CAPTURE_COMPLETE


def test_unknown_process_state_exits_3(tmp_path, capsys):
    root = tmp_path / "runstate"
    _create_store(root, to=RunState.CAPTURING)  # process state, no heartbeat
    assert _status(root) == 3
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["classification"] == "UNKNOWN_RESUMABLE"


def test_torn_projection_reported_not_repaired(tmp_path, capsys):
    root = tmp_path / "runstate"
    _create_store(root)
    (root / RUN_ID / "current.json").write_text("{ torn")
    before = (root / RUN_ID / "current.json").read_text()
    assert _status(root) == 2
    capsys.readouterr()
    assert (root / RUN_ID / "current.json").read_text() == before


def test_no_run_found_exit_4(tmp_path):
    assert (
        era_status.main(
            [
                "--store-root",
                str(tmp_path / "empty"),
                "--now-epoch",
                str(T0),
                "--boot-id-override",
                BOOT,
            ]
        )
        == 4
    )


def test_legacy_prejournal_era_detected_exit_3(tmp_path, capsys):
    fake_proc = tmp_path / "proc"
    pid_dir = fake_proc / str(os.getpid())
    pid_dir.mkdir(parents=True)
    (pid_dir / "cmdline").write_bytes(
        b"python\x00scripts/capture_massive_structural.py\x00"
        b"--out-dir\x00artifacts/m4b-coverage-era\x00"
    )
    assert (
        era_status.main(
            [
                "--store-root",
                str(tmp_path / "runstate"),
                "--now-epoch",
                str(T0),
                "--boot-id-override",
                BOOT,
                "--capture-dir",
                "artifacts/m4b-coverage-era",
                "--proc-root",
                str(fake_proc),
            ]
        )
        == 3
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["state"] == "UNKNOWN"
    assert "pre-journal" in payload["note"]
