"""Exclusive run lease with stale-owner detection (duplicate-launch refusal).

A lease is a directory entry (`lease/owner.json`) created with `O_CREAT |
O_EXCL`, so two launchers cannot both win. Liveness of the CURRENT owner is
decided from three independent facts, all injectable for tests:

* **pid alive** — `/proc/<pid>` exists;
* **pid identity** — field 22 (`starttime` in clock ticks) of
  `/proc/<pid>/stat` equals the recorded value, so a REUSED pid (the old
  owner died, the kernel handed its number to an unrelated process) is never
  mistaken for the owner;
* **boot id** — `/proc/sys/kernel/random/boot_id`; a different boot means
  the recorded owner died with the previous boot, no matter what pid now
  exists.

A stale lease may be ADOPTED (`allow_stale_adopt=True` after the classifier
says so) — adoption replaces the owner file and is itself recorded by the
caller in the journal. A `HELD` lease is refused outright: a live owner is
presumed working, and "no log output" is not evidence of death (the live
coverage era writes nothing to its log for hours by design).

Lock protocol (round-2 review fix, 2026-08-23): EVERY mutation of
`lease/owner.json` — the fresh `O_EXCL` create in acquire(), the adoption
replace, and the unlink in release() — happens while holding `flock(LOCK_EX)`
on `lease/adopt.lock`. The round-1 fix covered only the adoption branch;
without the lock on the other two mutators, an adopter classifying STALE
under the lock could interleave with an unlocked release() + fresh acquire()
and stomp the fresh owner file, leaving two live owners.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from enum import StrEnum
from pathlib import Path

from tree_options.runstate import custody
from tree_options.runstate.errors import LeaseHeldError
from tree_options.schemas.common import StrictModel

LEASE_DIRNAME = "lease"
OWNER_FILENAME = "owner.json"


class LeaseOwner(StrictModel):
    pid: int
    pid_start_ticks: int
    boot_id: str
    started_epoch: int
    argv_hash: str


class LeaseClassification(StrEnum):
    HELD = "HELD"
    STALE_DEAD_PID = "STALE_DEAD_PID"
    STALE_BOOT_CHANGED = "STALE_BOOT_CHANGED"
    STALE_PID_REUSED = "STALE_PID_REUSED"
    TORN = "TORN"


def read_boot_id(proc_root: Path | None = None) -> str:
    root = proc_root or Path("/proc")
    return (root / "sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()


def proc_pid_alive(pid: int, proc_root: Path | None = None) -> bool:
    root = proc_root or Path("/proc")
    return (root / str(pid)).exists()


def proc_start_ticks(pid: int, proc_root: Path | None = None) -> int | None:
    """Field 22 of /proc/<pid>/stat (starttime). None when unreadable.

    Field 2 (`comm`) can contain spaces and parentheses, so the safe parse
    takes the text AFTER the last ')' — every field after that is
    space-separated with no quoting.
    """
    root = proc_root or Path("/proc")
    try:
        stat_text = (root / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    tail = stat_text[stat_text.rfind(")") + 1 :].split()
    try:
        return int(tail[19])  # tail[0] is field 3 (state); +19 -> field 22
    except (IndexError, ValueError):
        return None


def current_owner(*, now_epoch: int, proc_root: Path | None = None) -> LeaseOwner:
    """The identity of THIS process, for acquiring a lease."""
    from tree_options.data.digest import sha256_hex

    argv = " ".join(sys.argv)
    return LeaseOwner(
        pid=os.getpid(),
        pid_start_ticks=proc_start_ticks(os.getpid(), proc_root) or 0,
        boot_id=read_boot_id(proc_root),
        started_epoch=now_epoch,
        argv_hash=sha256_hex(argv.encode("utf-8")),
    )


def _open_store_fd(store_dir: Path, supplied: int | None) -> tuple[int, bool]:
    if supplied is not None:
        return supplied, False
    run_id = store_dir.name
    fd = custody.open_directory(
        store_dir,
        create=False,
        run_id=run_id,
        purpose="run-state store",
    )
    if fd is None:
        raise FileNotFoundError(store_dir)
    return fd, True


def _lease_fd(
    store_dir: Path,
    store_fd: int,
    *,
    create: bool,
) -> int | None:
    run_id = store_dir.name
    fd = custody.open_child_directory(
        store_fd,
        LEASE_DIRNAME,
        create=create,
        run_id=run_id,
        purpose="lease directory",
    )
    custody.verify_directory_identity(store_dir, store_fd, run_id=run_id)
    if fd is not None and create:
        os.fsync(store_fd)
    return fd


def _decode_owner(raw: bytes | None) -> LeaseOwner | None:
    if raw is None:
        return None
    try:
        return LeaseOwner.model_validate(json.loads(raw))
    except Exception:
        return None


def _read_owner(store_dir: Path, *, _store_fd: int | None = None) -> LeaseOwner | None:
    """Read owner bytes under custody (None only for absent/torn content)."""
    store_fd, owned = _open_store_fd(store_dir, _store_fd)
    lease_fd: int | None = None
    try:
        lease_fd = _lease_fd(store_dir, store_fd, create=False)
        if lease_fd is None:
            return None
        raw = custody.read_named_bytes(
            store_dir / LEASE_DIRNAME,
            lease_fd,
            OWNER_FILENAME,
            run_id=store_dir.name,
            purpose="lease owner.json",
            allow_missing=True,
        )
        return _decode_owner(raw)
    finally:
        if lease_fd is not None:
            os.close(lease_fd)
        if owned:
            os.close(store_fd)


def _classify_owner(
    owner: LeaseOwner | None,
    *,
    boot_id_now: str,
    proc_root: Path | None,
) -> LeaseClassification:
    if owner is None:
        return LeaseClassification.TORN
    if owner.boot_id != boot_id_now:
        return LeaseClassification.STALE_BOOT_CHANGED
    if not proc_pid_alive(owner.pid, proc_root):
        return LeaseClassification.STALE_DEAD_PID
    live_ticks = proc_start_ticks(owner.pid, proc_root)
    if live_ticks is None or live_ticks != owner.pid_start_ticks:
        return LeaseClassification.STALE_PID_REUSED
    return LeaseClassification.HELD


def classify_existing(
    store_dir: Path,
    *,
    boot_id_now: str,
    proc_root: Path | None = None,
    _store_fd: int | None = None,
) -> LeaseClassification:
    """Classify an existing lease owner file without mutating anything."""
    owner = _read_owner(store_dir, _store_fd=_store_fd)
    return _classify_owner(owner, boot_id_now=boot_id_now, proc_root=proc_root)


def owner_exists(store_dir: Path, *, _store_fd: int | None = None) -> bool:
    """Return whether a safe owner name exists; unsafe names refuse."""
    store_fd, owned = _open_store_fd(store_dir, _store_fd)
    lease_fd: int | None = None
    try:
        lease_fd = _lease_fd(store_dir, store_fd, create=False)
        if lease_fd is None:
            return False
        return custody.name_exists(
            store_dir / LEASE_DIRNAME,
            lease_fd,
            OWNER_FILENAME,
            run_id=store_dir.name,
            purpose="lease owner.json",
        )
    finally:
        if lease_fd is not None:
            os.close(lease_fd)
        if owned:
            os.close(store_fd)


def acquire(
    store_dir: Path,
    owner: LeaseOwner,
    *,
    boot_id_now: str,
    proc_root: Path | None = None,
    allow_stale_adopt: bool = False,
) -> LeaseClassification:
    """Create the lease; returns HELD on success (this owner now holds it).

    With `allow_stale_adopt`, a stale/torn lease is atomically replaced.
    Without it, ANY pre-existing lease raises — the caller decides adoption
    only after looking at the classification.

    Round-1 review fix (2026-08-23, probe /tmp/pr-a-runstate-race-probe.log
    TWO_STALE_ADOPTERS_SUCCEEDED): the adoption path classifies and
    replaces under `flock(LOCK_EX)` on `lease/adopt.lock`, refusing if the
    classification became HELD (another adopter won the race).

    Round-2 review fix (2026-08-23, probe
    /tmp/pr-a-f2-lease-interleaving.log): the lock is now taken BEFORE the
    fresh `O_EXCL` create, not only on the adoption branch — EVERY
    owner.json mutation in this module happens under the same lock (see
    the module docstring's lock-protocol invariant). The earlier
    unlocked fresh create let a locked adopter's `os.replace` stomp a
    fresh acquirer's file, leaving two live owners.
    """
    run_id = store_dir.name
    lease_path = store_dir / LEASE_DIRNAME
    payload = (json.dumps(json.loads(owner.model_dump_json()), sort_keys=True) + "\n").encode(
        "utf-8"
    )
    store_fd, owned_store = _open_store_fd(store_dir, None)
    lease_fd: int | None = None
    lock_fd: int | None = None
    try:
        lease_fd = _lease_fd(store_dir, store_fd, create=True)
        assert lease_fd is not None
        lock_fd = custody.open_regular(
            lease_fd,
            "adopt.lock",
            os.O_RDWR | os.O_CREAT,
            run_id=run_id,
            purpose="lease adopt.lock",
            mode=0o600,
        )
        assert lock_fd is not None
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            custody.verify_name_identity(
                lease_fd,
                "adopt.lock",
                lock_fd,
                run_id=run_id,
                purpose="lease adopt.lock",
            )
            raw = custody.read_named_bytes(
                lease_path,
                lease_fd,
                OWNER_FILENAME,
                run_id=run_id,
                purpose="lease owner.json",
                allow_missing=True,
            )
            if raw is None:
                custody.atomic_write(
                    lease_path,
                    lease_fd,
                    OWNER_FILENAME,
                    payload,
                    run_id=run_id,
                    purpose="lease owner.json",
                    mode=0o600,
                    exclusive=True,
                )
                result = LeaseClassification.HELD
            else:
                classification = _classify_owner(
                    _decode_owner(raw),
                    boot_id_now=boot_id_now,
                    proc_root=proc_root,
                )
                if classification is LeaseClassification.HELD or not allow_stale_adopt:
                    raise LeaseHeldError(
                        run_id,
                        f"lease classification {classification.value}",
                    ) from None
                # Round-11 review fix (finding 6): the adoption replace is
                # IDENTITY-CONDITIONAL. atomic_write used to require only "a
                # safe regular file" at the name, so a LIVE owner B published
                # between the read and the replace was overwritten by the
                # adoption of stale owner A — two live launchers. The replace
                # may now swap out only the exact inode (and bytes) that were
                # classified; anything else is a custody refusal.
                expected = custody.capture_replacement_expectation(
                    lease_fd,
                    OWNER_FILENAME,
                    raw,
                    run_id=run_id,
                    purpose="lease owner.json",
                )
                custody.atomic_write(
                    lease_path,
                    lease_fd,
                    OWNER_FILENAME,
                    payload,
                    run_id=run_id,
                    purpose="lease owner.json",
                    mode=0o600,
                    exclusive=False,
                    expected=expected,
                )
                result = classification
            custody.verify_name_identity(
                lease_fd,
                "adopt.lock",
                lock_fd,
                run_id=run_id,
                purpose="lease adopt.lock",
            )
            custody.verify_directory_identity(store_dir, store_fd, run_id=run_id)
            return result
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if lease_fd is not None:
            os.close(lease_fd)
        if owned_store:
            os.close(store_fd)


def release(store_dir: Path, owner: LeaseOwner) -> bool:
    """Release ONLY if the lease still names this owner. Returns whether the
    release happened; releasing someone else's lease is silently refused.

    Round-2 review fix (2026-08-23): the read-compare-unlink happens under
    `flock(LOCK_EX)` on `lease/adopt.lock`, like every other owner.json
    mutation. The earlier unlocked unlink raced a locked adoption (the
    adopter could classify STALE, then have the file vanish and reappear
    under a fresh acquirer before its replace landed).
    """
    run_id = store_dir.name
    lease_path = store_dir / LEASE_DIRNAME
    store_fd, owned_store = _open_store_fd(store_dir, None)
    lease_fd: int | None = None
    lock_fd: int | None = None
    try:
        lease_fd = _lease_fd(store_dir, store_fd, create=False)
        if lease_fd is None:
            return False
        lock_fd = custody.open_regular(
            lease_fd,
            "adopt.lock",
            os.O_RDWR | os.O_CREAT,
            run_id=run_id,
            purpose="lease adopt.lock",
            mode=0o600,
        )
        assert lock_fd is not None
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            custody.verify_name_identity(
                lease_fd,
                "adopt.lock",
                lock_fd,
                run_id=run_id,
                purpose="lease adopt.lock",
            )
            owner_fd = custody.open_regular(
                lease_fd,
                OWNER_FILENAME,
                os.O_RDONLY,
                run_id=run_id,
                purpose="lease owner.json",
                allow_missing=True,
            )
            if owner_fd is None:
                return False
            try:
                current = _decode_owner(custody.read_all(owner_fd))
                custody.verify_name_identity(
                    lease_fd,
                    OWNER_FILENAME,
                    owner_fd,
                    run_id=run_id,
                    purpose="lease owner.json",
                )
                if current != owner:
                    return False
                custody.unlink_held_name(
                    lease_path,
                    lease_fd,
                    OWNER_FILENAME,
                    owner_fd,
                    run_id=run_id,
                    purpose="lease owner.json",
                )
            finally:
                os.close(owner_fd)
            custody.verify_name_identity(
                lease_fd,
                "adopt.lock",
                lock_fd,
                run_id=run_id,
                purpose="lease adopt.lock",
            )
            custody.verify_directory_identity(store_dir, store_fd, run_id=run_id)
            return True
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if lease_fd is not None:
            os.close(lease_fd)
        if owned_store:
            os.close(store_fd)
