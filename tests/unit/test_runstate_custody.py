"""External PR #13: complete run-state no-follow custody probes.

Every run-state authority name must be opened relative to held, component-wise
directory custody.  A refusal must leave any link/hard-link target byte-identical.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from tree_options.runstate import RunIdentity, RunState, RunStore, compute_run_id
from tree_options.runstate import heartbeat as H
from tree_options.runstate import journal as J
from tree_options.runstate import lease as L
from tree_options.runstate.errors import JournalCorruptError, StoreCustodyError

BOOT = "11111111-2222-3333-4444-555555555555"
T0 = 1_800_000_000
REPO_ROOT = Path(__file__).resolve().parents[2]
RUNSTATE_TESTS_ROOT = REPO_ROOT / "artifacts" / "runstate-tests"


@pytest.fixture()
def scratch() -> Path:
    RUNSTATE_TESTS_ROOT.mkdir(parents=True, exist_ok=True)
    path = RUNSTATE_TESTS_ROOT / f"custody-{uuid4().hex}"
    path.mkdir()
    return path


def _identity(*, run_nonce: str | None = None) -> RunIdentity:
    run_id = compute_run_id(
        campaign="m4-custody-test",
        protocol_hash="a" * 64,
        code_sha="b" * 40,
        provider="massive-polygon-free/1",
        capture_version="m4b-capture/1",
        universe_manifest_sha256="c" * 64,
        args_hash="d" * 64,
        started_epoch=T0,
        run_nonce=run_nonce,
    )
    return RunIdentity(
        run_id=run_id,
        campaign="m4-custody-test",
        protocol_hash="a" * 64,
        code_sha="b" * 40,
        provider="massive-polygon-free/1",
        capture_version="m4b-capture/1",
        universe_manifest_sha256="c" * 64,
        boot_id=BOOT,
        pid=123,
        pid_start_ticks=99,
        started_epoch=T0,
        args_hash="d" * 64,
        run_nonce=run_nonce,
    )


def _store(scratch: Path) -> RunStore:
    root = scratch / "runstate"
    return RunStore.create(root, _identity(), now_epoch=T0)


def _owner(pid: int = 42) -> L.LeaseOwner:
    return L.LeaseOwner(
        pid=pid,
        pid_start_ticks=100,
        boot_id=BOOT,
        started_epoch=T0,
        argv_hash="e" * 64,
    )


def _beat() -> H.Heartbeat:
    return H.Heartbeat(
        state=RunState.CAPTURING,
        pid=42,
        pid_start_ticks=100,
        boot_id=BOOT,
        at_epoch=T0,
    )


def _authority_payload(store: RunStore, name: str) -> bytes:
    if name in {"run.json", J.JOURNAL_FILENAME, J.PROJECTION_FILENAME}:
        return (store.dir / name).read_bytes()
    if name == L.OWNER_FILENAME:
        return (json.dumps(json.loads(_owner().model_dump_json()), sort_keys=True) + "\n").encode()
    if name == "adopt.lock":
        return b"lock bytes are not authority, but its inode is the lock domain\n"
    if name == H.HEARTBEAT_FILENAME:
        return (json.dumps(json.loads(_beat().model_dump_json()), sort_keys=True) + "\n").encode()
    raise AssertionError(name)


def _authority_path(store: RunStore, name: str) -> Path:
    if name in {L.OWNER_FILENAME, "adopt.lock"}:
        lease_dir = store.dir / L.LEASE_DIRNAME
        lease_dir.mkdir(exist_ok=True)
        return lease_dir / name
    return store.dir / name


def _exercise_authority_name(store: RunStore, name: str) -> None:
    if name == "run.json":
        RunStore.open(store.dir.parent, store.identity.run_id)
    elif name == J.JOURNAL_FILENAME:
        J.replay(store.dir, run_id=store.identity.run_id)
    elif name == J.PROJECTION_FILENAME:
        J.load_projection(store.dir, run_id=store.identity.run_id)
    elif name == L.OWNER_FILENAME:
        L.classify_existing(store.dir, boot_id_now=BOOT)
    elif name == "adopt.lock":
        L.acquire(store.dir, _owner(), boot_id_now=BOOT)
    elif name == H.HEARTBEAT_FILENAME:
        H.read(store.dir)
    else:  # pragma: no cover - test table exhaustiveness
        raise AssertionError(name)


@pytest.mark.parametrize(
    "name",
    [
        "run.json",
        J.JOURNAL_FILENAME,
        J.PROJECTION_FILENAME,
        L.OWNER_FILENAME,
        "adopt.lock",
        H.HEARTBEAT_FILENAME,
    ],
)
@pytest.mark.parametrize("plant", ["symlink", "dangling-symlink", "hardlink"])
def test_every_authority_name_refuses_final_links_without_target_mutation(
    scratch: Path,
    name: str,
    plant: str,
) -> None:
    """The final-name matrix applies to all six files, not just journal.jsonl."""
    store = _store(scratch)
    authority = _authority_path(store, name)
    target = scratch / f"protected-{name}-{plant}"
    original = _authority_payload(store, name)
    authority.unlink(missing_ok=True)
    if plant != "dangling-symlink":
        target.write_bytes(original)
    if plant == "hardlink":
        os.link(target, authority)
    else:
        authority.symlink_to(target)

    with pytest.raises(StoreCustodyError, match=name.replace(".", r"\.")):
        _exercise_authority_name(store, name)

    if plant == "dangling-symlink":
        assert not target.exists()
    else:
        assert target.read_bytes() == original


def test_create_refuses_intermediate_ancestor_symlink_without_writing_target(
    scratch: Path,
) -> None:
    target = scratch / "target"
    target.mkdir()
    linked_parent = scratch / "linked-parent"
    linked_parent.symlink_to(target, target_is_directory=True)
    root = linked_parent / "runstate"

    with pytest.raises(StoreCustodyError, match="component"):
        RunStore.create(root, _identity(), now_epoch=T0)

    assert list(target.iterdir()) == []


@pytest.mark.parametrize("dangling", [False, True], ids=["existing-target", "dangling-target"])
def test_open_refuses_run_json_symlink_by_name(scratch: Path, dangling: bool) -> None:
    store = _store(scratch)
    run_path = store.dir / "run.json"
    original = run_path.read_bytes()
    target = scratch / "protected-run.json"
    if not dangling:
        target.write_bytes(original)
    run_path.unlink()
    run_path.symlink_to(target)

    with pytest.raises(StoreCustodyError, match=r"run\.json"):
        RunStore.open(store.dir.parent, store.identity.run_id)

    if not dangling:
        assert target.read_bytes() == original
    else:
        assert not target.exists()


def test_open_refuses_run_json_hard_link(scratch: Path) -> None:
    store = _store(scratch)
    run_path = store.dir / "run.json"
    original = run_path.read_bytes()
    target = scratch / "protected-run.json"
    target.write_bytes(original)
    run_path.unlink()
    os.link(target, run_path)

    with pytest.raises(StoreCustodyError, match="link count"):
        RunStore.open(store.dir.parent, store.identity.run_id)

    assert target.read_bytes() == original


def test_journal_hard_link_refuses_append_without_mutating_target(scratch: Path) -> None:
    store = _store(scratch)
    journal = store.dir / J.JOURNAL_FILENAME
    original = journal.read_bytes()
    target = scratch / "protected-journal.jsonl"
    target.write_bytes(original)
    journal.unlink()
    os.link(target, journal)

    with pytest.raises(StoreCustodyError, match="link count"):
        store.transition(
            RunState.CAPTURING,
            reason="must refuse planted hard link",
            now_epoch=T0 + 1,
            actor_pid=123,
            actor_boot_id=BOOT,
        )

    assert target.read_bytes() == original


def test_projection_final_symlink_refuses_without_mutating_repo_target(scratch: Path) -> None:
    store = _store(scratch)
    projection = store.dir / J.PROJECTION_FILENAME
    tracked_target = REPO_ROOT / "pyproject.toml"
    original = tracked_target.read_bytes()
    projection.unlink()
    projection.symlink_to(tracked_target)

    with pytest.raises(StoreCustodyError, match=r"current\.json"):
        store.rebuild_projection(now_epoch=T0 + 1)

    assert tracked_target.read_bytes() == original


def test_projection_temp_symlink_refuses_without_mutating_target(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(scratch)
    target = scratch / "protected-projection-temp"
    original = b"protected projection temp bytes\n"
    target.write_bytes(original)
    planted = store.dir / ".attacker-projection.tmp"
    planted.symlink_to(target)
    monkeypatch.setattr(
        "tree_options.runstate.custody._new_temp_name",
        lambda _name: planted.name,
    )

    with pytest.raises(StoreCustodyError, match="temporary"):
        store.rebuild_projection(now_epoch=T0 + 1)

    assert target.read_bytes() == original


def test_create_run_json_temp_symlink_refuses_without_mutating_target(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = scratch / "runstate"
    identity = _identity(run_nonce="run-json-temp")
    target = scratch / "protected-run-json-temp"
    original = b"protected immutable identity temp bytes\n"
    target.write_bytes(original)

    def plant_temp(_name: str) -> str:
        planted = root / identity.run_id / ".attacker-run-json.tmp"
        planted.symlink_to(target)
        return planted.name

    monkeypatch.setattr("tree_options.runstate.custody._new_temp_name", plant_temp)
    with pytest.raises(StoreCustodyError, match="temporary"):
        RunStore.create(root, identity, now_epoch=T0)

    assert target.read_bytes() == original


@pytest.mark.parametrize("authority", ["heartbeat", "owner"])
def test_other_atomic_writers_refuse_planted_temp_name(
    scratch: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
) -> None:
    store = _store(scratch)
    directory = store.dir if authority == "heartbeat" else store.dir / L.LEASE_DIRNAME
    directory.mkdir(exist_ok=True)
    target = scratch / f"protected-{authority}-temp"
    original = f"protected {authority} temp bytes\n".encode()
    target.write_bytes(original)
    planted = directory / f".attacker-{authority}.tmp"
    planted.symlink_to(target)
    monkeypatch.setattr(
        "tree_options.runstate.custody._new_temp_name",
        lambda _name: planted.name,
    )

    with pytest.raises(StoreCustodyError, match="temporary"):
        if authority == "heartbeat":
            store.write_heartbeat(_beat())
        else:
            L.acquire(store.dir, _owner(), boot_id_now=BOOT)

    assert target.read_bytes() == original


def test_heartbeat_final_hard_link_refuses_without_mutating_target(scratch: Path) -> None:
    store = _store(scratch)
    target = scratch / "protected-heartbeat"
    original = b"protected heartbeat bytes\n"
    target.write_bytes(original)
    os.link(target, store.dir / H.HEARTBEAT_FILENAME)

    with pytest.raises(StoreCustodyError, match="link count"):
        store.write_heartbeat(_beat())

    assert target.read_bytes() == original


def test_lease_lock_symlink_refuses_before_target_truncation(scratch: Path) -> None:
    store = _store(scratch)
    lease_dir = store.dir / L.LEASE_DIRNAME
    lease_dir.mkdir()
    target = scratch / "protected-lock-target"
    original = b"must not be truncated\n"
    target.write_bytes(original)
    (lease_dir / "adopt.lock").symlink_to(target)

    with pytest.raises(StoreCustodyError, match=r"adopt\.lock"):
        L.acquire(store.dir, _owner(), boot_id_now=BOOT)

    assert target.read_bytes() == original


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_lease_owner_links_refuse_classification_without_mutating_target(
    scratch: Path, link_kind: str
) -> None:
    store = _store(scratch)
    lease_dir = store.dir / L.LEASE_DIRNAME
    lease_dir.mkdir()
    target = scratch / "protected-owner.json"
    payload = json.dumps(json.loads(_owner().model_dump_json()), sort_keys=True) + "\n"
    original = payload.encode()
    target.write_bytes(original)
    owner_path = lease_dir / L.OWNER_FILENAME
    if link_kind == "symlink":
        owner_path.symlink_to(target)
    else:
        os.link(target, owner_path)

    with pytest.raises(StoreCustodyError, match=r"owner\.json"):
        L.classify_existing(store.dir, boot_id_now=BOOT)

    assert target.read_bytes() == original


def test_projection_in_place_rewrite_after_publish_is_refused(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(scratch)
    real_replace = os.replace
    armed = {"done": False}

    def replace_then_rewrite(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if not armed["done"] and Path(os.fsdecode(dst)).name == J.PROJECTION_FILENAME:
            armed["done"] = True
            fd = os.open(dst, os.O_WRONLY | os.O_TRUNC, dir_fd=dst_dir_fd)
            try:
                os.write(fd, b'{"attacker":"rewrote published inode"}\n')
                os.fsync(fd)
            finally:
                os.close(fd)

    monkeypatch.setattr(os, "replace", replace_then_rewrite)
    with pytest.raises(StoreCustodyError, match="published bytes"):
        store.rebuild_projection(now_epoch=T0 + 1)


def test_projection_deletion_recreation_after_publish_is_refused(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(scratch)
    real_replace = os.replace
    armed = {"done": False}

    def replace_then_clone(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if not armed["done"] and Path(os.fsdecode(dst)).name == J.PROJECTION_FILENAME:
            armed["done"] = True
            read_fd = os.open(dst, os.O_RDONLY, dir_fd=dst_dir_fd)
            try:
                payload = os.read(read_fd, 65536)
            finally:
                os.close(read_fd)
            os.unlink(dst, dir_fd=dst_dir_fd)
            clone_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=dst_dir_fd)
            try:
                os.write(clone_fd, payload)
                os.fsync(clone_fd)
            finally:
                os.close(clone_fd)

    monkeypatch.setattr(os, "replace", replace_then_clone)
    with pytest.raises(StoreCustodyError, match="inode"):
        store.rebuild_projection(now_epoch=T0 + 1)


def test_projection_parent_rename_and_substitution_is_refused(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(scratch)
    view = J.replay(store.dir, run_id=store.identity.run_id)
    projection = J.build_projection(store.identity.run_id, view, written_at_epoch=T0 + 1)
    real_replace = os.replace
    held = store.dir.with_name(store.dir.name + ".held")
    armed = {"done": False}

    def replace_then_substitute(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        if not armed["done"] and Path(os.fsdecode(dst)).name == J.PROJECTION_FILENAME:
            armed["done"] = True
            store.dir.rename(held)
            store.dir.mkdir()

    monkeypatch.setattr(os, "replace", replace_then_substitute)
    with pytest.raises(StoreCustodyError, match="directory identity"):
        J.write_projection(store.dir, projection)


def test_journal_name_clone_swap_during_append_is_refused(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returned success requires the authority name to remain the flocked inode."""
    store = _store(scratch)
    journal = store.dir / J.JOURNAL_FILENAME
    before = journal.read_bytes()
    held = store.dir / f"{J.JOURNAL_FILENAME}.held"
    real_write = os.write
    armed = {"done": False}

    def write_after_cloning_name(fd: int, payload: bytes | memoryview) -> int:
        if not armed["done"]:
            armed["done"] = True
            journal.rename(held)
            journal.write_bytes(before)
        return real_write(fd, payload)

    monkeypatch.setattr(os, "write", write_after_cloning_name)
    with pytest.raises(StoreCustodyError, match="inode changed"):
        store.transition(
            RunState.CAPTURING,
            reason="clone race must reconcile",
            now_epoch=T0 + 1,
            actor_pid=123,
            actor_boot_id=BOOT,
        )

    assert journal.read_bytes() == before
    assert b'"CAPTURING"' in held.read_bytes()


def test_in_place_run_identity_rewrite_is_refused_on_rebind(scratch: Path) -> None:
    """Process-incarnation fields are not run-id inputs, but run.json is immutable."""
    store = _store(scratch)
    path = store.dir / "run.json"
    rewritten = json.loads(path.read_text())
    rewritten["pid"] = rewritten["pid"] + 1
    path.write_text(json.dumps(rewritten, indent=2, sort_keys=True) + "\n")

    with pytest.raises(StoreCustodyError, match=r"immutable run\.json content changed"):
        store.refresh()


def test_lease_lock_deletion_recreation_refuses_before_owner_publish(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock name must still denote the flocked inode before owner mutation."""
    store = _store(scratch)
    real_flock = L.fcntl.flock
    armed = {"done": False}

    def flock_then_swap(fd: int, operation: int) -> None:
        real_flock(fd, operation)
        if operation == L.fcntl.LOCK_EX and not armed["done"]:
            armed["done"] = True
            lease_dir = store.dir / L.LEASE_DIRNAME
            lock = lease_dir / "adopt.lock"
            held = lease_dir / "adopt.lock.held"
            lock.rename(held)
            lock.write_bytes(b"replacement lock inode\n")

    monkeypatch.setattr(L.fcntl, "flock", flock_then_swap)
    with pytest.raises(StoreCustodyError, match="inode changed"):
        L.acquire(store.dir, _owner(), boot_id_now=BOOT)

    assert not (store.dir / L.LEASE_DIRNAME / L.OWNER_FILENAME).exists()


# ---- round-11 (finding 3): the journal's durable name→inode binding closes the
# SUCCESSOR window --
#
# Round-11 review fix (2026-08-25): journal.append_record's post-fsync
# name check (round-8, finding 4) covers only the in-process window. A swap
# landing AFTER that check but BEFORE the call returns (the LOCK_UN, the fd
# close, the store-dir fsync) was invisible: the append acknowledged, the
# byte-copy clone installed at journal.jsonl was a fully valid shorter
# chain, and a successor process replayed and APPENDED to the clone — two
# authority tails for one run. journal.jsonl now carries a durable
# name→inode binding (a companion identity record custody-written beside it
# at creation; every open verifies the name still maps to the bound inode
# and refuses as corruption/reconciliation, never success), so the clone is
# refused at the next open and can never gain authority.


def test_journal_clone_swap_after_the_name_check_is_refused_at_the_next_open(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The racing point is the append's FINAL store-dir fsync, which runs
    AFTER the round-8 name check has already passed. The racing transition
    may acknowledge or refuse at its own re-replay (both name the incident);
    the CONTRACT is the successor — a fresh replay of the clone must REFUSE
    on the durable binding, and the clone must never gain a record."""
    store = _store(scratch)
    journal = store.dir / J.JOURNAL_FILENAME
    before = journal.read_bytes()  # the GENESIS line only
    held = store.dir / f"{J.JOURNAL_FILENAME}.held"
    real_fsync = os.fsync
    calls = {"n": 0}

    def fsync_swapping_after_the_name_check(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # fsync #1 is the journal fd; #2 is the store dir
            journal.rename(held)  # the locked fd keeps the inode
            journal.write_bytes(before)  # a byte-copy CLONE at the name
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync_swapping_after_the_name_check)
    try:
        # The racing append already passed the name check, so the transition
        # may still acknowledge; post-fix its own closing re-replay usually
        # names the incident first. Either way it must not crash differently.
        try:
            store.transition(
                RunState.CAPTURING,
                reason="clone race successor must reconcile",
                now_epoch=T0 + 1,
                actor_pid=123,
                actor_boot_id=BOOT,
            )
        except JournalCorruptError:
            pass  # post-fix: the transition's re-replay refused the clone
        # the SUCCESSOR process must refuse the clone at its open
        with pytest.raises(JournalCorruptError, match="durable binding"):
            J.replay(store.dir, run_id=store.identity.run_id)
        assert journal.read_bytes() == before, (
            "the clone at the authority name must never gain a record"
        )
    finally:
        with contextlib.suppress(OSError):
            held.unlink()
