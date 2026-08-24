"""Round-2 review remediation (2026-08-23) — load-bearing tests for the fixes.

Each test reproduces the round-2 probe exactly so a regression of the fix
surfaces here, not in production:

- run_id path escape (P1): an absolute or parent-bearing run id joined onto
  the validated root REPLACED the root (`root / "/tmp/x"` == `/tmp/x`), so a
  store could land outside the durable root. Covered by RunIdRefusedError
  plus the final-resolved-dir descendant check.
- lease lock protocol (P1): only the adoption branch of acquire() and never
  release() took the flock on lease/adopt.lock — an interleaving could
  leave two live owners.
- heartbeat mismatch ordering (P1 in effect): classify() returned ALIVE at
  the `state not in PROCESS_STATES` early return BEFORE the journal/beat
  reconciliation check.
- runstate_mark CLI: new library errors traced back instead of mapping to
  deterministic exit codes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from tree_options.runstate import RunIdentity, RunState, RunStore  # noqa: E402
from tree_options.runstate.errors import RunIdRefusedError  # noqa: E402

BOOT = "11111111-2222-3333-4444-555555555555"
T0 = 1_800_000_000
_TESTS_ROOT = REPO_ROOT / "artifacts" / "runstate-tests"


def _scratch() -> Path:
    _TESTS_ROOT.mkdir(parents=True, exist_ok=True)
    s = _TESTS_ROOT / f"r2-{uuid4().hex}"
    s.mkdir(parents=True)
    return s


def _identity(run_id: str = "m4-round2-20260823-abcdef12") -> RunIdentity:
    return RunIdentity(
        run_id=run_id,
        campaign="m4-round2",
        protocol_hash="a" * 64,
        code_sha="b" * 40,
        provider="massive-polygon-free/1",
        capture_version="m4b-capture/1",
        universe_manifest_sha256="c" * 64,
        boot_id=BOOT,
        pid=4242,
        pid_start_ticks=99,
        started_epoch=T0,
        args_hash="d" * 64,
    )


# --- R2-1: run_id path escape (P1) -------------------------------------------


def test_create_refuses_absolute_run_id_and_creates_nothing_there() -> None:
    """Round-2 probe: `root / identity.run_id` with an ABSOLUTE run id
    REPLACES the base — the store landed at /tmp/pr-a-escaped."""
    escaped = Path("/tmp") / f"pr-a-escaped-{uuid4().hex}"
    root = _scratch()
    with pytest.raises(RunIdRefusedError) as excinfo:
        RunStore.create(root, _identity(run_id=str(escaped)), now_epoch=T0)
    assert excinfo.value.code == "RUN_ID_REFUSED"
    assert str(escaped) in str(excinfo.value)
    # Not only refused: nothing may exist at the escaped location, and the
    # durable root must not have gained a stray entry either.
    assert not escaped.exists()
    assert list(root.iterdir()) == []


def test_create_refuses_parent_bearing_run_id() -> None:
    root = _scratch()
    with pytest.raises(RunIdRefusedError):
        RunStore.create(root, _identity(run_id="../escape"), now_epoch=T0)
    assert not (root.parent / "escape").exists()


def test_create_refuses_multi_component_run_id() -> None:
    root = _scratch()
    with pytest.raises(RunIdRefusedError):
        RunStore.create(root, _identity(run_id="a/b"), now_epoch=T0)
    assert not (root / "a").exists()


def test_create_refuses_dot_and_empty_run_id() -> None:
    root = _scratch()
    with pytest.raises(RunIdRefusedError):
        RunStore.create(root, _identity(run_id="."), now_epoch=T0)
    with pytest.raises(RunIdRefusedError):
        RunStore.create(root, _identity(run_id=".."), now_epoch=T0)
    assert list(root.iterdir()) == []


def test_open_refuses_absolute_run_id() -> None:
    root = _scratch()
    with pytest.raises(RunIdRefusedError):
        RunStore.open(root, "/tmp/pr-a-escaped")
    with pytest.raises(RunIdRefusedError):
        RunStore.open(root, "../escape")
    with pytest.raises(RunIdRefusedError):
        RunStore.open(root, "a/b")


def test_open_refuses_symlinked_run_dir_pointing_outside_root(tmp_path: Path) -> None:
    """Final-resolved-dir descendant check: a symlink at `<root>/<run-id>`
    pointing at a directory under /tmp must not open, even though the run id
    itself is a single clean component."""
    root = _scratch()
    run_id = "m4-round2-symlinked"
    outside = tmp_path / f"outside-{uuid4().hex}"
    outside.mkdir()
    (root / run_id).symlink_to(outside, target_is_directory=True)
    with pytest.raises(RunIdRefusedError):
        RunStore.open(root, run_id)


def test_single_component_run_id_still_creates_and_opens() -> None:
    """Regression: the ordinary canonical id (campaign-date-hash, one path
    component) keeps working through both create() and open()."""
    root = _scratch()
    run_id = "m4-round2-20260823-0123456789abcdef"
    store = RunStore.create(root, _identity(run_id=run_id), now_epoch=T0)
    assert store.state is RunState.PLANNED
    reopened = RunStore.open(root, run_id)
    assert reopened.identity.run_id == run_id
    assert reopened.state is RunState.PLANNED
