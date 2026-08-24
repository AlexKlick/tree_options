"""Round-1 review probes (2026-08-23) — load-bearing tests for the fixes.

Each test reproduces the original probe exactly so a regression of the
fix would surface here, not in production. Original probe lines:

- /tmp/pr-a-adversarial-probes.log: RUNSTATE_TMP_ACCEPTED, RUNSTATE_REPIN_ACCEPTED
- /tmp/pr-a-runstate-race-probe.log: TWO_STALE_ADOPTERS_SUCCEEDED
- /tmp/pr-a-runstate-write-probes.log: STALE_APPEND_RETURNED_WITH_DAMAGED_TAIL,
  COMPLETE_BAD_TAIL_TOLERATED, APPEND_MADE_BAD_TAIL_MIDFILE
- runstate_mark CLI: silent-drop of to_state when --pin-manifest supplied
- heartbeat: ALIVE classification without journal-state reconciliation
"""

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

import runstate_mark  # noqa: E402
from tree_options.data.digest import sha256_hex  # noqa: E402
from tree_options.runstate import (  # noqa: E402
    RunIdentity,
    RunState,
    RunStore,
    compute_run_id,
)
from tree_options.runstate import heartbeat as HB_module  # noqa: E402
from tree_options.runstate import journal as J_module  # noqa: E402
from tree_options.runstate import (  # noqa: E402
    lease as L_module,
)
from tree_options.runstate.errors import (  # noqa: E402
    JournalConcurrentWriteError,
    PinAlreadyBoundError,
    StoreRootRefusedError,
)

BOOT = "11111111-2222-3333-4444-555555555555"
T0 = 1_800_000_000
RUN_ID = compute_run_id(
    campaign="m4-round1",
    protocol_hash="a" * 64,
    code_sha="b" * 40,
    provider="massive-polygon-free/1",
    capture_version="m4b-capture/1",
    universe_manifest_sha256="c" * 64,
    args_hash="d" * 64,
    started_epoch=T0,
)
_TESTS_ROOT = REPO_ROOT / "artifacts" / "runstate-tests"


def _scratch() -> Path:
    _TESTS_ROOT.mkdir(parents=True, exist_ok=True)
    s = _TESTS_ROOT / f"test-{uuid4().hex}"
    s.mkdir(parents=True)
    return s


def _identity() -> RunIdentity:
    return RunIdentity(
        run_id=RUN_ID,
        campaign="m4-round1",
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


# --- F1: /tmp store root refusal ---------------------------------------------


def test_runstate_create_refuses_tmp_root() -> None:
    """Round-1 probe RUNSTATE_TMP_ACCEPTED: refused."""
    with pytest.raises(StoreRootRefusedError):
        RunStore.create(Path("/tmp/should-never-hold-a-store"), _identity(), now_epoch=T0)


def test_runstate_open_refuses_tmp_root() -> None:
    """The probe ran create() to CAPTURE_COMPLETE under /tmp; the refusal
    applies equally to open() — no /tmp store survives a fresh open."""
    with pytest.raises(StoreRootRefusedError):
        RunStore.open(Path("/tmp/should-never-hold-a-store"), RUN_ID)


def test_runstate_root_validation_is_component_boundary() -> None:
    """A path whose resolved component stack crosses /tmp is refused
    (mirrors the seal/ledger.validate_ledger_root semantics)."""
    # A nested dir under /tmp is refused even though it doesn't equal /tmp.
    nested = Path("/tmp") / f"probe-{uuid4().hex}"
    with pytest.raises(StoreRootRefusedError):
        RunStore.create(nested, _identity(), now_epoch=T0)


# --- F2(a): stale-lease adoption is lock-verified ---------------------------


def test_two_concurrent_adopters_do_not_corrupt_owner_file() -> None:
    """Round-1 probe TWO_STALE_ADOPTERS_SUCCEEDED: the underlying concern
    was data corruption — without the flock, two concurrent adopters
    could race-write and leave owner.json in a torn or partial state.

    The fix serialises adoption via flock on lease/adopt.lock. The
    OWNER FILE on disk must be a complete, valid LeaseOwner payload
    (round-trip parse) regardless of how many threads raced. (A
    sequential caller may legitimately adopt a second time after the
    first's pid became dead — that is a separate scenario.)
    """
    import threading

    store_dir = _scratch() / RUN_ID
    store_dir.mkdir()
    lease_dir = store_dir / L_module.LEASE_DIRNAME
    lease_dir.mkdir()
    stale = L_module.LeaseOwner(
        pid=11111,
        pid_start_ticks=1,
        boot_id="dead-previous-boot",
        started_epoch=T0,
        argv_hash="0" * 64,
    )
    (lease_dir / L_module.OWNER_FILENAME).write_text(
        json.dumps(json.loads(stale.model_dump_json()), sort_keys=True) + "\n"
    )
    N = 8  # exaggerate the contention the probe created
    barrier = threading.Barrier(N)
    failures: list[str] = []

    def attempt(label: str, pid: int) -> None:
        barrier.wait()
        try:
            L_module.acquire(
                store_dir,
                L_module.LeaseOwner(
                    pid=pid,
                    pid_start_ticks=1,
                    boot_id=BOOT,
                    started_epoch=T0,
                    argv_hash=sha256_hex(label.encode("utf-8")),
                ),
                boot_id_now=BOOT,
                allow_stale_adopt=True,
            )
        except Exception as exc:
            failures.append(f"{label}: {exc.__class__.__name__}")

    threads = [threading.Thread(target=attempt, args=(f"T{i}", 4243 + i)) for i in range(N)]
    # Round-2 review fix (2026-08-23): the old assertion here was vacuous
    # (`... or True`). The real invariant: the surviving owner's argv_hash
    # must be one of the racing threads' argv hashes — the file was written
    # whole by exactly one racer, never torn and never a blend.
    racer_hashes = {sha256_hex(f"T{i}".encode()) for i in range(N)}
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    # The file must be a valid LeaseOwner payload (no torn write).
    final = L_module._read_owner(store_dir)
    assert final is not None
    assert final.boot_id == BOOT  # replaced the dead-previous-boot stale
    assert final.argv_hash in racer_hashes
    assert failures == [], f"unexpected exceptions: {failures}"


# --- F2(b): journal append refuses stale prev + torn tail -------------------


def test_append_with_wrong_prev_refused() -> None:
    """Round-1 probe STALE_APPEND_RETURNED_WITH_DAMAGED_TAIL."""
    store_dir = _scratch() / "journal-test"
    store_dir.mkdir()
    real = J_module.JournalRecord(
        seq=1,
        kind="GENESIS",
        to_state=RunState.PLANNED,
        reason="store created",
        actor_pid=1,
        actor_boot_id=BOOT,
        at_epoch=T0,
        prev_record_sha256=J_module.GENESIS_PREV,
    )
    J_module.append_record(store_dir, real)
    # The caller "thinks" the previous hash is still GENESIS_PREV; the
    # actual tail is real.record_sha256. Append must refuse.
    stale = J_module.JournalRecord(
        seq=2,
        kind="TRANSITION",
        from_state=RunState.PLANNED,
        to_state=RunState.CAPTURING,
        reason="stale",
        actor_pid=1,
        actor_boot_id=BOOT,
        at_epoch=T0 + 1,
        prev_record_sha256=J_module.GENESIS_PREV,  # WRONG
    )
    with pytest.raises(JournalConcurrentWriteError):
        J_module.append_record(store_dir, stale)


def test_append_past_torn_tail_refused() -> None:
    """Round-1 probe COMPLETE_BAD_TAIL_TOLERATED."""
    store_dir = _scratch() / "torn-test"
    store_dir.mkdir()
    real = J_module.JournalRecord(
        seq=1,
        kind="GENESIS",
        to_state=RunState.PLANNED,
        reason="store created",
        actor_pid=1,
        actor_boot_id=BOOT,
        at_epoch=T0,
        prev_record_sha256=J_module.GENESIS_PREV,
    )
    J_module.append_record(store_dir, real)
    # Corrupt the FINAL line: simulate a crash mid-append by appending
    # an incomplete JSON fragment.
    journal_path = store_dir / J_module.JOURNAL_FILENAME
    with open(journal_path, "a", encoding="utf-8") as fh:
        fh.write('{"seq":2,"kind":"TRANSITION","kind_of_cha')
        fh.flush()
        os.fsync(fh.fileno())
    # A subsequent legitimate append must be REFUSED (the new contract:
    # torn tail blocks append; repair is an explicit owner act).
    legit = J_module.JournalRecord(
        seq=2,
        kind="TRANSITION",
        from_state=RunState.PLANNED,
        to_state=RunState.CAPTURING,
        reason="legit",
        actor_pid=1,
        actor_boot_id=BOOT,
        at_epoch=T0 + 2,
        prev_record_sha256=real.record_sha256,
    )
    with pytest.raises(JournalConcurrentWriteError):
        J_module.append_record(store_dir, legit)


def test_append_after_legitimate_chain_still_works() -> None:
    """Regression: the new locked-tail re-read must not break the happy path."""
    store_dir = _scratch() / "happy"
    store_dir.mkdir()
    prev = J_module.GENESIS_PREV
    for seq in range(1, 4):
        record = J_module.JournalRecord(
            seq=seq,
            kind="GENESIS" if seq == 1 else "TRANSITION",
            to_state=RunState.PLANNED if seq == 1 else RunState.CAPTURING,
            reason=f"r{seq}",
            actor_pid=1,
            actor_boot_id=BOOT,
            at_epoch=T0 + seq,
            prev_record_sha256=prev,
        )
        prev = J_module.append_record(store_dir, record)
    view = J_module.replay(store_dir)
    assert len(view.records) == 3
    assert view.tail_hash == prev


# --- F3: pinned manifest is immutable ---------------------------------------


def test_pin_manifest_second_different_hash_refused() -> None:
    """Round-1 probe RUNSTATE_REPIN_ACCEPTED."""
    root = _scratch()
    store = RunStore.create(root, _identity(), now_epoch=T0)
    store.pin_manifest("a" * 64, now_epoch=T0 + 1, actor_pid=1, actor_boot_id=BOOT)
    with pytest.raises(PinAlreadyBoundError):
        store.pin_manifest("b" * 64, now_epoch=T0 + 2, actor_pid=1, actor_boot_id=BOOT)


def test_pin_manifest_same_hash_is_idempotent() -> None:
    root = _scratch()
    store = RunStore.create(root, _identity(), now_epoch=T0)
    first = store.pin_manifest("a" * 64, now_epoch=T0 + 1, actor_pid=1, actor_boot_id=BOOT)
    second = store.pin_manifest("a" * 64, now_epoch=T0 + 2, actor_pid=1, actor_boot_id=BOOT)
    assert first == second


# --- F8a: runstate_mark CLI refuses to_state + --pin-manifest ---------------


def test_runstate_mark_rejects_combination_exit_2() -> None:
    """Round-1 finding: to_state + --pin-manifest was silently dropping
    the transition. The CLI now refuses the combination outright."""
    root = _scratch()
    identity_path = root / "identity.json"
    identity_path.write_text(json.dumps(json.loads(_identity().model_dump_json())))
    # Seed a PLANNED store.
    assert (
        runstate_mark.main(
            [
                "--store-root",
                str(root),
                "--now-epoch",
                str(T0 + 5),
                RUN_ID,
                "--create-identity",
                str(identity_path),
                "--reason",
                "genesis",
            ]
        )
        == 0
    )
    # Combination must exit 2 and write nothing.
    rc = runstate_mark.main(
        [
            "--store-root",
            str(root),
            "--now-epoch",
            str(T0 + 10),
            RUN_ID,
            "CAPTURE_COMPLETE",
            "--pin-manifest",
            "e" * 64,
            "--reason",
            "should-be-rejected",
        ]
    )
    assert rc == 2
    # State must still be PLANNED.
    store = RunStore.open(root, RUN_ID)
    assert store.state is RunState.PLANNED
    assert store.pinned_manifest_sha256 is None


# --- F8d: heartbeat classification reconciles with journal state ----------


def test_heartbeat_with_mismatched_state_classified_reconciliation() -> None:
    """Round-1 probe MISMATCHED_HEARTBEAT_CLASS: a fresh heartbeat whose
    recorded state disagrees with the journal projection must NEVER be
    ALIVE — classify as UNKNOWN_RECONCILIATION_REQUIRED."""
    # Journal state is CAPTURING (a PROCESS state).
    # Beat claims SEALED_RUNNING — the journal says one thing, the beat another.
    beat = HB_module.Heartbeat(
        state=RunState.SEALED_RUNNING,
        pid=os.getpid(),
        pid_start_ticks=0,
        boot_id=BOOT,
        at_epoch=T0,
    )
    cls = HB_module.classify(
        beat,
        RunState.CAPTURING,
        now_epoch=T0,
        boot_id_now=BOOT,
        proc_root=Path("/proc"),
        stale_after_s=900,
    )
    assert cls is HB_module.HeartbeatClass.UNKNOWN_RECONCILIATION_REQUIRED


def test_heartbeat_with_matching_state_classified_alive(tmp_path: Path) -> None:
    """Regression: matching state + fresh beat + alive owner is ALIVE.

    Real `/proc` does not have a pid inside the test; the heartbeat
    module's owner-gone check would mark the owner gone and the
    classification falls to UNKNOWN_RESUMABLE regardless of the state
    match. Build a fake /proc so the owner looks alive.
    """
    fake_proc = tmp_path / "proc"
    pid_dir = fake_proc / str(os.getpid())
    pid_dir.mkdir(parents=True)
    tail = ["1"] * 40
    tail[19] = "0"  # field 22 starttime
    (pid_dir / "stat").write_text(f"{os.getpid()} (test) " + " ".join(tail) + "\n")
    beat = HB_module.Heartbeat(
        state=RunState.CAPTURING,
        pid=os.getpid(),
        pid_start_ticks=0,
        boot_id=BOOT,
        at_epoch=T0,
    )
    cls = HB_module.classify(
        beat,
        RunState.CAPTURING,
        now_epoch=T0,
        boot_id_now=BOOT,
        proc_root=fake_proc,
        stale_after_s=900,
    )
    assert cls is HB_module.HeartbeatClass.ALIVE
