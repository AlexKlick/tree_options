"""Phase 0 owner custody: consistent runtime backups and quarantined restores.

The supervised desk process (``trex-desk.service``, clientId 83) holds the
run-dir lock (``DeskPaths.lock``) and the account fences for its lifetime,
so a copy taken from OUTSIDE it is only an observation: files can change
between reads. A consistent multi-file backup is therefore taken INSIDE the
owner, from ``DeskRuntime.beat``: the beat runs single-threaded under the
runtime lock, and the supervised root is copied under its own advisory lock
(``SupervisedPaths.lock``, the lock every grant/revoke/permit write takes).

- **When.** On the first beat after 16:30 ET on each NYSE session day, and
  on any beat outside 09:30-16:15 ET that finds ``BACKUP-REQUEST`` in the
  run dir (consumed atomically by a rename). Never inside the session.
- **Shape.** ``<dest>/<UTC stamp>-<owner epoch>/`` with ``run/`` and
  ``supervised/`` trees and ``manifest.json`` (``trex-desk-backup/1``:
  every file's path, sha256 and size, the owner epoch, the instant and the
  source roots). Written into a hidden partial directory, fsynced, then
  renamed: a backup either exists complete or not at all. The newest
  ``KEEP`` are kept; directories this module did not name are never touched.
- **Failure isolation.** The hook can never raise into the owner loop: a
  failure is the ``backup_failed`` event with the error class only.
- **Restore** goes into a scratch directory, never into or around a live
  root, after the manifest verifies. Authority and consumed-command state
  (``owner.json``, every request, FLATTEN/RESOLVE-FLAT commands, the mandate
  and expired mandates, every permit) is MOVED into ``<scratch>/archive/``:
  an old mandate, permit, request or owner epoch can never become active
  authority again. Restrictions (HALT files, the mandate revocation
  tombstone) are restored as they were: fail-safe, reviewed by hand.

CLI (no broker contact): ``python -m tree_options.trex.desk_backup
{snapshot,verify,restore}``. ``snapshot`` refuses while an owner holds the
run dir; touch ``BACKUP-REQUEST`` instead. Runbook: docs/product/RESTORE.md.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import time as wall
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, time
from pathlib import Path, PurePosixPath
from typing import Any

from tree_options.trex.clock import ET, is_session
from tree_options.trex.supervised import SupervisedPaths

BACKUP_SCHEMA = "trex-desk-backup/1"
RESTORE_SCHEMA = "trex-desk-restore/1"
MANIFEST = "manifest.json"
RECEIPT = "RESTORE.json"
ARCHIVE = "archive"
#: the operator's on-demand trigger, dropped in the run dir
BACKUP_REQUEST = "BACKUP-REQUEST"
#: backups kept in the destination root (newest by name, i.e. by UTC stamp)
KEEP = 14
#: the scheduled backup: first beat at or after this ET time on a session day
SCHEDULED_AT = time(16, 30)
#: never back up inside the session (supervised_desk's tick window)
SESSION_OPEN = time(9, 30)
SESSION_END = time(16, 15)
#: how long a backup waits for the supervised state lock (a grant mid-write)
LOCK_WAIT_S = 2.0

_RUN, _SUP = "run", "supervised"
_PARTIAL = ".partial-"
_NAME = re.compile(r"^\d{8}T\d{6}Z-")
_STAMP_LEN = len("20261021T203000Z")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1 << 20


class BackupError(RuntimeError):
    """Fail-closed refusal with a machine-readable reason."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class RestoreRefused(BackupError):
    """The restore destination is unsafe; nothing was written."""


def default_backup_root(run_dir: Path) -> Path:
    """``~/.local/state/trex/desk-backups`` for the default run dir: beside
    the run dir, never inside it (``exit_watch.scan_books`` reads only
    ``<state>/*/book.json``; a backup's book sits two levels deeper)."""
    return run_dir.parent / "desk-backups"


# ------------------------------------------------------------------ helpers


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _excluded(key: str, rel: PurePosixPath) -> bool:
    """Never copied: locks, in-progress tmp writes, the backup trigger."""
    name = rel.name
    if name.endswith(".tmp"):
        return True
    top = len(rel.parts) == 1
    if key == _RUN and top and (name == "runtime.lock" or name.startswith(BACKUP_REQUEST)):
        return True
    return key == _SUP and top and name == ".lock"


def _walk(key: str, root: Path) -> Iterator[tuple[Path, PurePosixPath]]:
    """Every regular file under ``root`` in sorted order; a symlink or any
    other non-regular entry refuses (a backup must not follow one out)."""
    if not root.exists():
        return
    for current, dirs, files in os.walk(root, followlinks=False):
        here = Path(current)
        dirs.sort()
        for name in dirs:
            if (here / name).is_symlink():
                raise BackupError("not_a_regular_file", f"{key}/{name}")
        for name in sorted(files):
            path = here / name
            rel = PurePosixPath(path.relative_to(root).as_posix())
            if _excluded(key, rel):
                continue
            if path.is_symlink() or not path.is_file():
                raise BackupError("not_a_regular_file", f"{key}/{rel}")
            yield path, rel


def _copy(source: Path, target: Path) -> tuple[str, int]:
    """Copy one file, fsynced; (sha256, bytes) of exactly what was written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with open(source, "rb") as src, open(target, "wb") as dst:
        while chunk := src.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    return digest.hexdigest(), size


def _file_sha(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


@contextmanager
def _flock(path: Path, *, wait_s: float, reason: str) -> Iterator[None]:
    """Hold ``path``'s advisory lock, waiting at most ``wait_s``."""
    with open(path, "a+b") as handle:
        deadline = wall.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if wall.monotonic() >= deadline:
                    raise BackupError(reason, str(path)) from None
                wall.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def owner_epoch_of(run_dir: Path) -> str | None:
    """The owner epoch ``owner.json`` names; None when absent/unreadable."""
    try:
        doc = json.loads((run_dir / "owner.json").read_bytes())
    except (OSError, ValueError):
        return None
    epoch = doc.get("owner_epoch") if isinstance(doc, dict) else None
    return epoch if isinstance(epoch, str) else None


def _stamp(instant: datetime) -> str:
    return instant.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _backups(dest_root: Path) -> list[Path]:
    """Complete backups this module named, oldest first."""
    if not dest_root.is_dir():
        return []
    return sorted(
        p
        for p in dest_root.iterdir()
        if p.is_dir() and _NAME.match(p.name) and (p / MANIFEST).is_file()
    )


# ----------------------------------------------------------------- snapshot


def snapshot(
    run_dir: Path,
    supervised: SupervisedPaths,
    dest_root: Path,
    *,
    now: datetime | None = None,
    owner_epoch: str | None = None,
    keep: int = KEEP,
    lock_wait_s: float = LOCK_WAIT_S,
) -> Path:
    """Copy the run dir and the supervised root into a new backup directory.

    The CALLER guarantees the run dir does not change meanwhile (the owner's
    beat, or the CLI holding the runtime lock); the supervised root is
    copied under its own lock. Returns the completed backup directory."""
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    epoch = owner_epoch if owner_epoch is not None else owner_epoch_of(run_dir)
    dest = dest_root.resolve()
    for live in (run_dir.resolve(), supervised.root.resolve()):
        if dest == live or live in dest.parents:  # it would copy itself
            raise BackupError("destination_inside_live_root", str(live))
    dest_root.mkdir(parents=True, exist_ok=True)
    for stale in dest_root.glob(_PARTIAL + "*"):  # a crashed earlier attempt
        shutil.rmtree(stale, ignore_errors=True)
    tag = re.sub(r"[^A-Za-z0-9_.-]", "_", epoch)[:64] if epoch else "no-epoch"
    base = f"{_stamp(instant)}-{tag}"
    name, n = base, 1
    while (dest_root / name).exists():
        n += 1
        name = f"{base}.{n}"
    partial = dest_root / f"{_PARTIAL}{name}-{os.getpid()}"
    partial.mkdir()
    try:
        files: list[dict[str, Any]] = []
        for path, rel in _walk(_RUN, run_dir):
            sha, size = _copy(path, partial / _RUN / rel)
            files.append({"path": f"{_RUN}/{rel}", "sha256": sha, "bytes": size})
        if supervised.root.exists():
            with _flock(supervised.lock(), wait_s=lock_wait_s, reason="supervised_state_busy"):
                for path, rel in _walk(_SUP, supervised.root):
                    sha, size = _copy(path, partial / _SUP / rel)
                    files.append({"path": f"{_SUP}/{rel}", "sha256": sha, "bytes": size})
        manifest = {
            "schema": BACKUP_SCHEMA,
            "owner_epoch": epoch,
            "taken_at": instant.astimezone(UTC).isoformat(),
            "source_roots": {_RUN: str(run_dir), _SUP: str(supervised.root)},
            "files": files,
        }
        with open(partial / MANIFEST, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        for directory in sorted({partial / PurePosixPath(f["path"]).parent for f in files}):
            _fsync_dir(directory)
        _fsync_dir(partial)
        final = dest_root / name
        os.rename(partial, final)
        _fsync_dir(dest_root)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    prune(dest_root, keep=keep, protect=final)
    return final


def prune(dest_root: Path, *, keep: int = KEEP, protect: Path | None = None) -> list[str]:
    """Delete all but the newest ``keep`` backups (never ``protect``)."""
    found = _backups(dest_root)
    doomed = found[: max(0, len(found) - keep)]
    removed: list[str] = []
    for old in doomed:
        if protect is not None and old == protect:
            continue
        shutil.rmtree(old)
        removed.append(old.name)
    return removed


# ------------------------------------------------------------------- verify


def _manifest_path(raw: Any) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise BackupError("manifest_invalid", f"bad path {raw!r}")
    rel = PurePosixPath(raw)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        raise BackupError("manifest_invalid", f"unsafe path {raw!r}")
    if len(rel.parts) < 2 or rel.parts[0] not in (_RUN, _SUP):
        raise BackupError("manifest_invalid", f"path outside run/ and supervised/: {raw!r}")
    return rel


def verify(backup: Path) -> dict[str, Any]:
    """The manifest when every listed file matches and nothing else exists;
    BackupError otherwise."""
    try:
        manifest = json.loads((backup / MANIFEST).read_bytes())
    except (OSError, ValueError) as error:
        raise BackupError("manifest_unreadable", type(error).__name__) from None
    if not isinstance(manifest, dict) or manifest.get("schema") != BACKUP_SCHEMA:
        raise BackupError("manifest_invalid", "not a trex-desk-backup/1 manifest")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise BackupError("manifest_invalid", "files is not a list")
    listed: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise BackupError("manifest_invalid", "file entry is not an object")
        rel = _manifest_path(entry.get("path"))
        sha, size = entry.get("sha256"), entry.get("bytes")
        if not isinstance(sha, str) or not _SHA.match(sha):
            raise BackupError("manifest_invalid", f"bad sha256 for {rel}")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise BackupError("manifest_invalid", f"bad size for {rel}")
        if str(rel) in listed:
            raise BackupError("manifest_invalid", f"duplicate {rel}")
        listed.add(str(rel))
        path = backup / rel
        if path.is_symlink() or not path.is_file():
            raise BackupError("file_missing", str(rel))
        if _file_sha(path) != (sha, size):
            raise BackupError("file_mismatch", str(rel))
    present = {
        Path(current, name).relative_to(backup).as_posix()
        for current, _, names in os.walk(backup, followlinks=False)
        for name in names
    }
    extra = sorted(present - listed - {MANIFEST})
    if extra:
        raise BackupError("unlisted_file", extra[0])
    return manifest


# ------------------------------------------------------------------ restore


def quarantined(rel: str) -> bool:
    """Authority and consumed-command state: archived on restore, never
    active. Restrictions (HALT, ``mandate.revoked.json``) stay active."""
    parts = PurePosixPath(rel).parts
    key, tail = parts[0], parts[1:]
    if key == _RUN:
        if tail[0] == "requests":
            return True  # claimed, results AND unclaimed requests: never replayed
        return len(tail) == 1 and (
            tail[0] in ("owner.json", "FLATTEN") or tail[0].startswith("RESOLVE-FLAT-")
        )
    if tail[0] == "permits":
        return True  # issued and consumed alike
    return (
        len(tail) == 1
        and tail[0].startswith("mandate")
        and tail[0].endswith(".json")
        and tail[0] != "mandate.revoked.json"
    )


def _check_destination(into: Path, live_roots: Sequence[Path]) -> Path:
    """The resolved destination (symlinks followed), or RestoreRefused."""
    target = into.expanduser().resolve()
    for root in live_roots:
        live = root.expanduser().resolve()
        if target == live or live in target.parents:
            raise RestoreRefused("destination_inside_live_root", str(live))
        if target in live.parents:
            raise RestoreRefused("destination_contains_live_root", str(live))
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise RestoreRefused("destination_not_empty", str(target))
    return target


def restore(
    backup: Path,
    into: Path,
    *,
    live_roots: Sequence[Path] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify ``backup``, restore it under ``into`` (``run/``, ``supervised/``)
    and move authority/consumed-command state into ``into/archive/``.

    Refuses a destination that is, is inside, or contains a live root (the
    given ones, the manifest's source roots, the backup root) or that is not
    empty. Returns the receipt also written to ``into/RESTORE.json``."""
    manifest = verify(backup)
    roots = [*live_roots, backup.resolve().parent]
    sources = manifest.get("source_roots")
    if isinstance(sources, dict):
        roots.extend(Path(str(v)) for v in sources.values())
    target = _check_destination(into, roots)
    target.mkdir(parents=True, exist_ok=True)
    restored: list[str] = []
    archived: list[str] = []
    for entry in manifest["files"]:
        rel = str(entry["path"])
        sha, _ = _copy(backup / rel, target / rel)
        if sha != entry["sha256"]:
            raise BackupError("file_mismatch", rel)  # changed after verify
    for entry in manifest["files"]:
        rel = str(entry["path"])
        if quarantined(rel):
            moved = target / ARCHIVE / rel
            moved.parent.mkdir(parents=True, exist_ok=True)
            os.rename(target / rel, moved)
            archived.append(rel)
        else:
            restored.append(rel)
    for key in (_RUN, _SUP):
        (target / key).mkdir(exist_ok=True)
    at = now or datetime.now(UTC)
    receipt = {
        "schema": RESTORE_SCHEMA,
        "backup": str(backup),
        "backup_owner_epoch": manifest.get("owner_epoch"),
        "backup_taken_at": manifest.get("taken_at"),
        "restored_at": at.astimezone(UTC).isoformat(),
        "restored": sorted(restored),
        "quarantined": sorted(archived),
        "before_any_entry": [
            "reconcile the broker's orders and positions with run/book.json",
            "start a NEW owner (a fresh owner epoch); never reuse the archived one",
            "grant a NEW mandate for that epoch; never copy an archived mandate or permit back",
        ],
    }
    with open(target / RECEIPT, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return receipt


# ------------------------------------------------------- the owner's hook


def in_session_hours(now: datetime) -> bool:
    local = now.astimezone(ET)
    return is_session(local) and SESSION_OPEN <= local.time() <= SESSION_END


class OwnerBackups:
    """The owner's custody hook: called from every ``DeskRuntime.beat``.

    ``on_beat`` returns the event to record (``backup_taken`` or
    ``backup_failed``) or None, and never raises. One scheduled attempt per
    session day per process (a failure is not retried every beat; the
    operator retries with BACKUP-REQUEST); a restarted owner finds the day's
    backup on disk by its stamp."""

    def __init__(
        self,
        run_dir: Path,
        supervised: SupervisedPaths,
        dest_root: Path,
        *,
        keep: int = KEEP,
    ) -> None:
        self.run_dir = run_dir
        self.supervised = supervised
        self.dest_root = dest_root
        self.keep = keep
        self._scheduled_day: date | None = None
        self._quiet_day: date | None = None  # an undecidable beat reports once a day

    def _requested(self) -> bool:
        """Consume BACKUP-REQUEST atomically: exactly one beat wins it."""
        request = self.run_dir / BACKUP_REQUEST
        claimed = request.with_name(BACKUP_REQUEST + ".consumed")
        try:
            os.rename(request, claimed)
        except FileNotFoundError:
            return False
        claimed.unlink(missing_ok=True)
        return True

    def _scheduled(self, local: datetime) -> bool:
        day = local.date()
        if self._scheduled_day == day or local.time() < SCHEDULED_AT:
            return False
        self._scheduled_day = day  # decided once per day, success or not
        if not is_session(local):
            return False
        cutoff = datetime.combine(day, SCHEDULED_AT, tzinfo=ET)
        since = _stamp(cutoff)
        return not any(p.name[:_STAMP_LEN] >= since for p in _backups(self.dest_root))

    def on_beat(self, now: datetime, owner_epoch: str | None) -> dict[str, Any] | None:
        local = now.astimezone(ET)
        trigger: str | None = None
        try:
            if in_session_hours(local):
                return None  # a request waits for the session to end
            requested = self._requested()
            if requested:
                trigger = "request"  # consumed: a failure from here is reported
            scheduled = self._scheduled(local)
            if not (requested or scheduled):
                return None
            trigger = trigger or "scheduled"
            backup = snapshot(
                self.run_dir,
                self.supervised,
                self.dest_root,
                now=now,
                owner_epoch=owner_epoch,
                keep=self.keep,
            )
            files = json.loads((backup / MANIFEST).read_bytes())["files"]
            return {
                "event": "backup_taken",
                "trigger": trigger,
                "backup": backup.name,
                "files": len(files),
                "bytes": sum(int(f["bytes"]) for f in files),
                "owner_epoch": owner_epoch,
            }
        except Exception as error:  # custody can never break the owner loop
            if trigger is None:  # failed before deciding: not every beat
                if self._quiet_day == local.date():
                    return None
                self._quiet_day = local.date()
            failed: dict[str, Any] = {
                "event": "backup_failed",
                "trigger": trigger or "undecided",
                "error": type(error).__name__,
            }
            if isinstance(error, BackupError):
                failed["reason"] = error.reason
            return failed


# ---------------------------------------------------------------------- CLI


@contextmanager
def _runtime_lock(run_dir: Path) -> Iterator[None]:
    """Hold the owner's run-dir lock for an out-of-process snapshot."""
    from tree_options.trex.desk_runtime import DeskPaths

    if not run_dir.is_dir():
        raise BackupError("run_dir_absent", str(run_dir))
    handle = open(DeskPaths(run_dir).lock(), "a+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError(
                "owner_running",
                f"the desk owner holds {run_dir}: touch {run_dir / BACKUP_REQUEST} and the "
                "owner snapshots on its next beat outside 09:30-16:15 ET",
            ) from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def main(argv: list[str] | None = None) -> int:
    from tree_options.trex.desk_runtime import DeskPaths

    ap = argparse.ArgumentParser(
        prog="python -m tree_options.trex.desk_backup",
        description="Desk owner custody: backup, verify, restore (no broker contact).",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    def roots(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--run-dir", type=Path, default=None, help="default: the desk run dir")
        parser.add_argument(
            "--supervised-dir", type=Path, default=None, help="default: TREX_SUPERVISED_DIR"
        )

    snap = sub.add_parser("snapshot", help="back up while NO owner runs (else BACKUP-REQUEST)")
    roots(snap)
    snap.add_argument("--dest", type=Path, default=None, help="default: <run dir>/../desk-backups")
    snap.add_argument("--keep", type=int, default=KEEP)
    check = sub.add_parser("verify", help="check a backup against its manifest")
    check.add_argument("backup", type=Path)
    back = sub.add_parser("restore", help="restore into an EMPTY scratch dir, authority archived")
    back.add_argument("backup", type=Path)
    back.add_argument("--into", type=Path, required=True)
    roots(back)
    back.add_argument(
        "--live-root", type=Path, action="append", default=[], help="another root to protect"
    )
    args = ap.parse_args(argv)
    try:
        if args.command == "verify":
            manifest = verify(args.backup)
            report = {
                "backup": str(args.backup),
                "owner_epoch": manifest.get("owner_epoch"),
                "taken_at": manifest.get("taken_at"),
                "files": len(manifest["files"]),
            }
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        run_dir = (args.run_dir or DeskPaths.default().root).expanduser()
        sup = SupervisedPaths(
            args.supervised_dir.expanduser()
            if args.supervised_dir
            else SupervisedPaths.default().root
        )
        if args.command == "snapshot":
            dest = (args.dest or default_backup_root(run_dir)).expanduser()
            with _runtime_lock(run_dir):
                backup = snapshot(run_dir, sup, dest, keep=args.keep)
            print(json.dumps({"backup": str(backup)}, indent=2))
            return 0
        live = [
            run_dir,
            sup.root,
            DeskPaths.default().root,
            SupervisedPaths.default().root,
            *args.live_root,
        ]
        receipt = restore(args.backup, args.into, live_roots=live)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    except BackupError as refused:
        print(f"refused: {refused.reason} {refused.detail}".rstrip(), file=sys.stderr)
        return 2
    except OSError as error:
        print(f"refused: os_error {type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
