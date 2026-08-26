"""era_status CLI: read-only proof, legacy detection, exit-code contract."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import era_status  # noqa: E402
from tree_options.runstate import RunIdentity, RunState, RunStore, compute_run_id  # noqa: E402

BOOT = "11111111-2222-3333-4444-555555555555"
T0 = 1_800_000_000
RUN_ID = compute_run_id(
    campaign="m4-statustest",
    protocol_hash="a" * 64,
    code_sha="b" * 40,
    provider="massive-polygon-free/1",
    capture_version="m4b-capture/1",
    universe_manifest_sha256="c" * 64,
    args_hash="d" * 64,
    started_epoch=T0,
)

# Round-1 review migration (2026-08-23): pytest's tmp_path lives under
# /tmp; the runstate root refusal applies. Scratch roots for runstate
# live under the repo's gitignored artifacts/runstate-tests/.
_ERA_TESTS_ROOT = REPO_ROOT / "artifacts" / "runstate-tests"


@pytest.fixture()
def root() -> Path:
    _ERA_TESTS_ROOT.mkdir(parents=True, exist_ok=True)
    scratch = _ERA_TESTS_ROOT / f"test-{uuid4().hex}"
    scratch.mkdir(parents=True)
    return scratch


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


def test_status_is_read_only_no_mtime_changes(root, tmp_path, capsys):
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


def test_status_reports_state_and_exits_zero(root, tmp_path, capsys):
    _create_store(root, to=RunState.CAPTURE_COMPLETE)
    assert _status(root) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["state"] == "CAPTURE_COMPLETE"
    assert payload["classification"] == "ALIVE"  # no process expected
    assert payload["seq"] == 3  # GENESIS + CAPTURING + CAPTURE_COMPLETE


def test_unknown_process_state_exits_3(root, tmp_path, capsys):
    _create_store(root, to=RunState.CAPTURING)  # process state, no heartbeat
    assert _status(root) == 3
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["classification"] == "UNKNOWN_RESUMABLE"


def test_torn_projection_reported_not_repaired(root, tmp_path, capsys):
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


def _partial_store(root: Path) -> Path:
    """run.json WITHOUT journal.jsonl — the crash window between
    RunStore.create()'s two writes (run.json first, GENESIS second)."""
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
    store_dir = root / RUN_ID
    store_dir.mkdir(parents=True)
    (store_dir / "run.json").write_text(
        json.dumps(json.loads(identity.model_dump_json()), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return store_dir


def test_partially_created_store_default_selection_reports_unknown(root, tmp_path, capsys):
    """Round-3 review fix (2026-08-23, finding 4): _latest_run_id()
    unconditionally stat()ed journal.jsonl, so a crash between create()'s
    run.json write and the GENESIS append made the DEFAULT (--json, no
    --run-id) invocation raise FileNotFoundError. A partially-created store
    is UNKNOWN — structured output plus the documented exit, never a
    traceback."""
    _partial_store(root)
    assert (
        era_status.main(
            [
                "--store-root",
                str(root),
                "--now-epoch",
                str(T0 + 60),
                "--boot-id-override",
                BOOT,
                "--json",
            ]
        )
        == 3
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["state"] == "UNKNOWN"
    assert "partially created" in payload["note"]


def test_partially_created_store_explicit_run_id_reports_unknown(root, tmp_path, capsys):
    """Same crash-window store addressed by --run-id: structured UNKNOWN
    (exit 3), not the projection-torn refusal and never a traceback."""
    _partial_store(root)
    assert _status(root) == 3
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["state"] == "UNKNOWN"
    assert payload["run_id"] == RUN_ID


def test_misfiled_store_dir_refused_without_traceback(root, tmp_path, capsys):
    """Round-3 review fix (2026-08-23, finding 3): opening a store whose
    directory names ANOTHER run must refuse with the documented
    read-failure exit (2), never a traceback — era_status is the read-only
    observer of misfiled evidence."""
    _create_store(root)
    misfiled = "m4-statustest-20260823-misfiled0"
    (root / RUN_ID).rename(root / misfiled)
    assert (
        era_status.main(
            [
                "--store-root",
                str(root),
                "--run-id",
                misfiled,
                "--boot-id-override",
                BOOT,
                "--now-epoch",
                str(T0 + 60),
            ]
        )
        == 2
    )
    assert "STORE ID MISMATCH" in capsys.readouterr().err


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
                str(root),
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
