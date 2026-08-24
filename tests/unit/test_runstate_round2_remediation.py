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

import fcntl
import json
import os
import sys
import threading
from pathlib import Path
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import runstate_mark  # noqa: E402
from tree_options.data.digest import sha256_hex  # noqa: E402
from tree_options.runstate import RunIdentity, RunState, RunStore  # noqa: E402
from tree_options.runstate import heartbeat as HB_module  # noqa: E402
from tree_options.runstate import lease as L_module  # noqa: E402
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
    link = root / run_id
    link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(RunIdRefusedError):
            RunStore.open(root, run_id)
    finally:
        # pytest reclaims tmp_path (the link's target) at session end; a
        # dangling symlink left in repo scratch crashes the mutation
        # harness's disposable copy (copytree dereferences symlinks —
        # gate9 GATE_EXIT=1, 2026-08-23). Always take the link with us.
        link.unlink(missing_ok=True)


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


# --- R2-2: lease lock protocol covers ALL owner.json mutators (P1) ----------


def _lease_dir_with_owner() -> tuple[Path, L_module.LeaseOwner]:
    store_dir = _scratch() / "run"
    store_dir.mkdir()
    lease_dir = store_dir / L_module.LEASE_DIRNAME
    lease_dir.mkdir(parents=True)
    owner = L_module.LeaseOwner(
        pid=4242,
        pid_start_ticks=99,
        boot_id=BOOT,
        started_epoch=T0,
        argv_hash="0" * 64,
    )
    (lease_dir / L_module.OWNER_FILENAME).write_text(
        json.dumps(json.loads(owner.model_dump_json()), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return store_dir, owner


def test_release_blocks_while_adopt_lock_held() -> None:
    """Mechanism proof: release() mutates owner.json (unlink), so it must
    hold LOCK_EX on lease/adopt.lock. Round-2 probe
    (/tmp/pr-a-f2-lease-interleaving.log): A.release() unlinked WITHOUT the
    lock while adopter B classified STALE under it; a fresh acquirer C then
    O_EXCL-created and B's os.replace stomped C's file — two live owners."""
    store_dir, owner = _lease_dir_with_owner()
    lease_dir = store_dir / L_module.LEASE_DIRNAME
    owner_path = lease_dir / L_module.OWNER_FILENAME
    lock_fh = open(lease_dir / "adopt.lock", "w", encoding="utf-8")
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
    done = threading.Event()

    def worker() -> None:
        assert L_module.release(store_dir, owner)
        done.set()

    t = threading.Thread(target=worker)
    t.start()
    try:
        t.join(timeout=0.6)
        assert t.is_alive(), "release() completed while adopt.lock was held"
        assert not done.is_set()
        assert owner_path.exists(), "unlink happened under someone else's lock"
    finally:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        lock_fh.close()
    assert done.wait(timeout=5), "release() never completed after the lock freed"
    t.join(timeout=5)
    assert not owner_path.exists()


def test_fresh_acquire_blocks_while_adopt_lock_held() -> None:
    """Mechanism proof: the FRESH O_EXCL create path is also an owner.json
    mutation, so it must take the same lock — otherwise it can interleave
    with a locked adoption (the round-2 two-live-owners interleaving)."""
    store_dir = _scratch() / "run"
    store_dir.mkdir()
    (store_dir / L_module.LEASE_DIRNAME).mkdir(parents=True)
    lease_dir = store_dir / L_module.LEASE_DIRNAME
    owner_path = lease_dir / L_module.OWNER_FILENAME
    owner = L_module.LeaseOwner(
        pid=5555,
        pid_start_ticks=7,
        boot_id=BOOT,
        started_epoch=T0,
        argv_hash="1" * 64,
    )
    lock_fh = open(lease_dir / "adopt.lock", "w", encoding="utf-8")
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
    done = threading.Event()

    def worker() -> None:
        assert (
            L_module.acquire(store_dir, owner, boot_id_now=BOOT)
            is L_module.LeaseClassification.HELD
        )
        done.set()

    t = threading.Thread(target=worker)
    t.start()
    try:
        t.join(timeout=0.6)
        assert t.is_alive(), "fresh acquire() completed while adopt.lock was held"
        assert not done.is_set()
        assert not owner_path.exists(), "O_EXCL create happened under someone else's lock"
    finally:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        lock_fh.close()
    assert done.wait(timeout=5), "acquire() never completed after the lock freed"
    t.join(timeout=5)
    assert json.loads(owner_path.read_text(encoding="utf-8"))["pid"] == 5555


def test_mixed_acquire_release_stress_owner_file_stays_parseable() -> None:
    """Mixed stress: N threads race acquire(allow_stale_adopt=True) and
    release against one lease dir. Whatever the interleaving, the owner
    file afterwards must round-trip-parse as a LeaseOwner (or be absent
    after a final release) — never torn, never two live owners."""
    store_dir = _scratch() / "stress"
    store_dir.mkdir()
    (store_dir / L_module.LEASE_DIRNAME).mkdir(parents=True)

    def racer(label: str, iterations: int) -> None:
        owner = L_module.LeaseOwner(
            pid=7000 + int(label),
            pid_start_ticks=1,
            boot_id=BOOT,
            started_epoch=T0,
            argv_hash=sha256_hex(label.encode("utf-8")),
        )
        for _ in range(iterations):
            # No live pid 7000+ exists -> classification is STALE_DEAD_PID
            # (adoptable) whenever the file exists; fresh O_EXCL otherwise.
            L_module.acquire(store_dir, owner, boot_id_now=BOOT, allow_stale_adopt=True)
            L_module.release(store_dir, owner)

    threads = [threading.Thread(target=racer, args=(str(i), 25)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)
    owner_path = store_dir / L_module.LEASE_DIRNAME / L_module.OWNER_FILENAME
    if owner_path.exists():
        final = L_module._read_owner(store_dir)
        assert final is not None, "owner.json exists but does not parse as a LeaseOwner"


# --- R2-3: heartbeat mismatch fires in EVERY non-terminal state --------------


def test_nonprocess_state_with_mismatched_beat_requires_reconciliation() -> None:
    """Round-2 probe: journal BARS_READY (non-process) + heartbeat CAPTURING
    classified ALIVE, because the `state not in PROCESS_STATES -> ALIVE`
    early return fired BEFORE the journal/beat reconciliation check."""
    beat = HB_module.Heartbeat(
        state=RunState.CAPTURING,
        pid=os.getpid(),
        pid_start_ticks=0,
        boot_id=BOOT,
        at_epoch=T0,
    )
    cls = HB_module.classify(
        beat,
        RunState.BARS_READY,
        now_epoch=T0,
        boot_id_now=BOOT,
        proc_root=Path("/proc"),
        stale_after_s=900,
    )
    assert cls is HB_module.HeartbeatClass.UNKNOWN_RECONCILIATION_REQUIRED


def test_nonprocess_state_with_matching_beat_still_alive() -> None:
    """Regression: a non-process state with NO disagreement still expects no
    process — matching beat or not, it is ALIVE."""
    beat = HB_module.Heartbeat(
        state=RunState.BARS_READY,
        pid=os.getpid(),
        pid_start_ticks=0,
        boot_id=BOOT,
        at_epoch=T0,
    )
    cls = HB_module.classify(
        beat,
        RunState.BARS_READY,
        now_epoch=T0,
        boot_id_now=BOOT,
        proc_root=Path("/proc"),
        stale_after_s=900,
    )
    assert cls is HB_module.HeartbeatClass.ALIVE


# --- R2-4: runstate_mark maps the new library errors to exit codes ----------

RUN_ID = "m4-round2-mark-20260823-abcdef12"


def _mark(root: Path, *args: str) -> int:
    return runstate_mark.main(["--store-root", str(root), "--now-epoch", str(T0 + 10), *args])


def _identity_path(root: Path, run_id: str = RUN_ID) -> Path:
    path = root / "identity.json"
    path.write_text(json.dumps(json.loads(_identity(run_id).model_dump_json())))
    return path


def test_cli_tmp_store_root_exit_6_on_create() -> None:
    """StoreRootRefusedError used to traceback out of main(); the contract
    is a deterministic exit code (create path)."""
    scratch = _scratch()
    identity_path = _identity_path(scratch)
    assert (
        _mark(
            Path("/tmp/pr-a-cli-refused"),
            RUN_ID,
            "--create-identity",
            str(identity_path),
            "--reason",
            "g",
        )
        == 6
    )
    assert not (Path("/tmp/pr-a-cli-refused") / RUN_ID).exists()


def test_cli_absolute_run_id_exit_6_on_create_and_open() -> None:
    scratch = _scratch()
    absolute_id = str(Path("/tmp") / f"pr-a-cli-escape-{uuid4().hex}")
    identity_path = _identity_path(scratch, run_id=absolute_id)
    # Create path: the identity's run id is absolute.
    assert (
        _mark(scratch, absolute_id, "--create-identity", str(identity_path), "--reason", "g") == 6
    )
    assert not Path(absolute_id).exists()
    # Open path: the run-id argument escapes the root.
    root = _scratch()
    assert _mark(root, absolute_id, "CAPTURING", "--reason", "x") == 6
    assert _mark(root, "../escape", "CAPTURING", "--reason", "x") == 6
    assert _mark(root, "a/b", "CAPTURING", "--reason", "x") == 6


def test_cli_pin_second_different_hash_exit_7() -> None:
    root = _scratch()
    assert _mark(root, RUN_ID, "--create-identity", str(_identity_path(root)), "--reason", "g") == 0
    assert _mark(root, RUN_ID, "--pin-manifest", "e" * 64, "--reason", "pin 1") == 0
    assert _mark(root, RUN_ID, "--pin-manifest", "f" * 64, "--reason", "pin 2") == 7
    store = RunStore.open(root, RUN_ID)
    assert store.pinned_manifest_sha256 == "e" * 64


def test_cli_transition_after_torn_journal_tail_exit_8() -> None:
    """A torn final journal line blocks append (JournalConcurrentWriteError);
    the CLI maps it to exit 8 instead of a traceback."""
    root = _scratch()
    assert _mark(root, RUN_ID, "--create-identity", str(_identity_path(root)), "--reason", "g") == 0
    assert _mark(root, RUN_ID, "CAPTURING", "--reason", "era pass") == 0
    # Corrupt the tail like the round-1 probe: append an incomplete fragment.
    with open(root / RUN_ID / "journal.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"seq":3,"kind":"TRANSITION","kind_of_cha')
        fh.flush()
        os.fsync(fh.fileno())
    assert _mark(root, RUN_ID, "CAPTURE_COMPLETE", "--reason", "wrapper exit 0") == 8
    # The journal must not have grown past the torn line.
    store = RunStore.open(root, RUN_ID)
    assert store.state is RunState.CAPTURING
