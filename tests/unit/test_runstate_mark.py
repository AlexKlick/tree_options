"""runstate_mark CLI: exit-code contract + create/pin/transition flows."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import runstate_mark  # noqa: E402
from tree_options.runstate import RunIdentity, RunState, RunStore, compute_run_id  # noqa: E402

BOOT = "11111111-2222-3333-4444-555555555555"
T0 = 1_800_000_000
RUN_ID = compute_run_id(
    campaign="m4-marktest",
    protocol_hash="a" * 64,
    code_sha="b" * 40,
    provider="massive-polygon-free/1",
    capture_version="m4b-capture/1",
    universe_manifest_sha256="c" * 64,
    args_hash="d" * 64,
    started_epoch=T0,
)

# Round-1 review migration (2026-08-23): pytest's tmp_path lives under
# /tmp on this host; the runstate root refusal (RunStore.create/open)
# makes that path unusable for run-state stores. The scratch root
# therefore lives under the repo's gitignored artifacts/runstate-tests/,
# unique per-test.
_MIGRATION_TESTS_ROOT = REPO_ROOT / "artifacts" / "runstate-tests"


def _identity_path(root: Path) -> Path:
    identity = RunIdentity(
        run_id=RUN_ID,
        campaign="m4-marktest",
        protocol_hash="a" * 64,
        code_sha="b" * 40,
        provider="massive-polygon-free/1",
        capture_version="m4b-capture/1",
        universe_manifest_sha256="c" * 64,
        boot_id=BOOT,
        pid=12345,
        pid_start_ticks=99,
        started_epoch=T0,
        args_hash="d" * 64,
    )
    path = root / "identity.json"
    path.write_text(json.dumps(json.loads(identity.model_dump_json()), indent=2))
    return path


@pytest.fixture()
def root() -> Path:
    _MIGRATION_TESTS_ROOT.mkdir(parents=True, exist_ok=True)
    scratch = _MIGRATION_TESTS_ROOT / f"test-{uuid4().hex}"
    scratch.mkdir(parents=True)
    return scratch


def _mark(root: Path, *args: str) -> int:
    return runstate_mark.main(
        [
            "--store-root",
            str(root),
            "--now-epoch",
            str(T0 + 10),
            *args,
        ]
    )


def test_create_and_transition_flow(root, tmp_path, capsys):
    identity_path = _identity_path(root)
    assert (
        _mark(
            root,
            RUN_ID,
            "--create-identity",
            str(identity_path),
            "--reason",
            "genesis",
        )
        == 0
    )
    assert _mark(root, RUN_ID, "CAPTURING", "--reason", "era pass") == 0
    store = RunStore.open(root, RUN_ID)
    assert store.state is RunState.CAPTURING
    out = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert out["state"] == "CAPTURING"


def test_unknown_run_exit_4(root):
    assert _mark(root, "never-created", "CAPTURING", "--reason", "x") == 4


def test_illegal_transition_exit_2(root, tmp_path):
    _mark(root, RUN_ID, "--create-identity", str(_identity_path(root)), "--reason", "g")
    assert _mark(root, RUN_ID, "CAPTURING", "--reason", "go") == 0
    assert _mark(root, RUN_ID, "BARS_CAPTURING", "--reason", "skip") == 2


def test_pin_manifest_without_state(root, tmp_path, capsys):
    _mark(root, RUN_ID, "--create-identity", str(_identity_path(root)), "--reason", "g")
    assert (
        _mark(
            root,
            RUN_ID,
            "--pin-manifest",
            "e" * 64,
            "--reason",
            "wrapper exit 0",
        )
        == 0
    )
    store = RunStore.open(root, RUN_ID)
    assert store.pinned_manifest_sha256 == "e" * 64
    assert store.state is RunState.PLANNED


def test_missing_state_and_pin_refused(root, tmp_path, capsys):
    _mark(root, RUN_ID, "--create-identity", str(_identity_path(root)), "--reason", "g")
    capsys.readouterr()
    assert _mark(root, RUN_ID, "--reason", "nothing") == 2


def test_identity_mismatch_exit_5(root, tmp_path):
    path = _identity_path(root)
    assert (
        _mark(
            root,
            "other-run-id",
            "--create-identity",
            str(path),
            "--reason",
            "g",
        )
        == 5
    )


def test_cli_store_id_mismatch_exit_6(root, tmp_path, capsys):
    """Round-3 review fix (2026-08-23, finding 3): a store whose directory
    names run-B but whose run.json names run-A used to open — the CLI then
    journaled into run-A's journal while the operator asked for run-B.
    Refused with a deterministic exit (6, the run-id refusal family), the
    journal untouched, no lease taken."""
    assert _mark(root, RUN_ID, "--create-identity", str(_identity_path(root)), "--reason", "g") == 0
    misfiled = "m4-marktest-20260823-misfiled0"
    (root / RUN_ID).rename(root / misfiled)
    assert _mark(root, misfiled, "CAPTURING", "--reason", "era pass") == 6
    assert "STORE_ID_MISMATCH" in capsys.readouterr().err
    # nothing was journaled into the misfiled store and no lease was taken
    journal = root / misfiled / "journal.jsonl"
    assert journal.read_text().count("\n") == 1  # GENESIS only
    assert not (root / misfiled / "lease").exists()
