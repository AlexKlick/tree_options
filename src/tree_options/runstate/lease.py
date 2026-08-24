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


def _read_owner(store_dir: Path) -> LeaseOwner | None:
    """Read the current lease owner payload (None if the file is torn)."""
    owner_path = store_dir / LEASE_DIRNAME / OWNER_FILENAME
    try:
        raw = owner_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        return LeaseOwner.model_validate(json.loads(raw))
    except Exception:
        return None


def classify_existing(
    store_dir: Path,
    *,
    boot_id_now: str,
    proc_root: Path | None = None,
) -> LeaseClassification:
    """Classify an existing lease owner file without mutating anything."""
    owner_path = store_dir / LEASE_DIRNAME / OWNER_FILENAME
    try:
        owner = LeaseOwner.model_validate(json.loads(owner_path.read_text(encoding="utf-8")))
    except Exception:
        return LeaseClassification.TORN
    if owner.boot_id != boot_id_now:
        # Boot identity dominates: a pid number from a previous boot is
        # meaningless on this one, alive-looking or not.
        return LeaseClassification.STALE_BOOT_CHANGED
    if not proc_pid_alive(owner.pid, proc_root):
        return LeaseClassification.STALE_DEAD_PID
    live_ticks = proc_start_ticks(owner.pid, proc_root)
    if live_ticks is None or live_ticks != owner.pid_start_ticks:
        return LeaseClassification.STALE_PID_REUSED
    return LeaseClassification.HELD


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
    lease_dir = store_dir / LEASE_DIRNAME
    lease_dir.mkdir(parents=True, exist_ok=True)
    owner_path = lease_dir / OWNER_FILENAME
    adopt_lock_path = lease_dir / "adopt.lock"
    payload = json.dumps(json.loads(owner.model_dump_json()), sort_keys=True) + "\n"
    # Round-2 review: open the lock file FIRST and hold LOCK_EX across the
    # entire mutation — the fresh create and the adoption replace are both
    # owner.json mutations and serialize against each other and release().
    with open(adopt_lock_path, "w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            try:
                fd = os.open(owner_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                return LeaseClassification.HELD
            except FileExistsError:
                # Round-1 review: ALL classification and adoption work
                # happens under the lock; two concurrent or sequential
                # adopters cannot both win.
                classification = classify_existing(
                    store_dir, boot_id_now=boot_id_now, proc_root=proc_root
                )
                if classification is LeaseClassification.HELD or not allow_stale_adopt:
                    raise LeaseHeldError(
                        store_dir.name,
                        f"lease classification {classification.value}",
                    ) from None
                # The classification came from THIS file content — adopt.
                tmp = lease_dir / f".{OWNER_FILENAME}.{os.getpid()}.tmp"
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, owner_path)
                return classification
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def release(store_dir: Path, owner: LeaseOwner) -> bool:
    """Release ONLY if the lease still names this owner. Returns whether the
    release happened; releasing someone else's lease is silently refused.

    Round-2 review fix (2026-08-23): the read-compare-unlink happens under
    `flock(LOCK_EX)` on `lease/adopt.lock`, like every other owner.json
    mutation. The earlier unlocked unlink raced a locked adoption (the
    adopter could classify STALE, then have the file vanish and reappear
    under a fresh acquirer before its replace landed).
    """
    lease_dir = store_dir / LEASE_DIRNAME
    owner_path = lease_dir / OWNER_FILENAME
    if not lease_dir.is_dir():
        # No lease directory means no lease to release; do not materialize
        # one (or its lock file) as a side effect of a failed release.
        return False
    with open(lease_dir / "adopt.lock", "w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            try:
                current = LeaseOwner.model_validate(
                    json.loads(owner_path.read_text(encoding="utf-8"))
                )
            except Exception:
                return False
            if current != owner:
                return False
            owner_path.unlink()
            return True
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
