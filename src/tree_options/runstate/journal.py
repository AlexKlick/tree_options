"""Append-only, hash-chained transition journal + atomic state projection.

The journal is the authority (handoff constraint 9: never `/tmp`). Design:

* One JSON line per record, `fsync` before the append returns, so a crash
  can lose at most the line being written — never an earlier one.
* Each record carries `record_sha256 = sha256(domain ‖ canonical-json of the
  record without its hash)` and `prev_record_sha256` chaining to its
  predecessor (genesis chains to 64 zeros). Replay verifies BOTH, so a
  single tampered or reordered record anywhere is detected.
* A torn FINAL line (crash mid-append: undecodable JSON, bad hash, or a
  broken chain) is *tolerated and reported* (`tail_damaged=True`) — it was
  never acknowledged. The same damage in a non-final record is
  `JOURNAL_CORRUPT`: something rewrote history, which is an incident, and
  the store refuses rather than guess.
* `current.json` is a convenience projection rebuilt from the journal after
  every append via a pid-qualified temp file + `os.replace` (the
  `ResponseCache.put` atomicity precedent). A torn projection is never
  trusted: `load_projection` refuses it and the caller either rebuilds (a
  writer) or reports (the read-only status command).
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path

from tree_options.data.digest import sha256_hex
from tree_options.runstate.errors import (
    JournalConcurrentWriteError,
    JournalCorruptError,
    ProjectionTornError,
)
from tree_options.runstate.states import RunState
from tree_options.schemas.common import StrictModel

RUNSTATE_JOURNAL_DOMAIN = b"tree-options-runstate-journal-v1"
GENESIS_PREV = "0" * 64

JOURNAL_FILENAME = "journal.jsonl"
PROJECTION_FILENAME = "current.json"


class JournalRecord(StrictModel):
    """One journal line. `kind` discriminates the payload semantics."""

    seq: int  # 1-based; GENESIS is seq 1
    kind: str  # GENESIS | TRANSITION | MANIFEST_PINNED | NOTE
    from_state: RunState | None = None
    to_state: RunState | None = None
    reason: str = ""
    actor_pid: int
    actor_boot_id: str
    at_epoch: int
    manifest_sha256: str | None = None
    prev_record_sha256: str
    record_sha256: str = ""  # filled by the writer; "" is invalid on disk


@dataclass(frozen=True)
class JournalView:
    records: tuple[JournalRecord, ...]
    tail_hash: str  # GENESIS_PREV when empty
    tail_damaged: bool


def _record_hash(record: JournalRecord) -> str:
    body = json.dumps(
        {k: v for k, v in record.model_dump().items() if k != "record_sha256"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_hex(RUNSTATE_JOURNAL_DOMAIN + body)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_store_dir_nofollow(store_dir: Path, *, run_id: str) -> int:
    """Round-8 review fix (2026-08-24, finding 4): the run directory held as
    a REAL directory fd, opened once with ``O_NOFOLLOW``.

    store.py validates the run DIRECTORY (root resolution, one-component run
    id, descendant check), but the journal used to then follow
    ``journal.jsonl`` BY NAME on both the read and the append path — so a
    journal NAME symlinked to a copied chain under /tmp was read and
    appended through. Every journal open below rides this one dir fd, so the
    journal name can never redirect authority outside the validated store
    directory. (The intermediate ancestors were already resolved by the
    store's root validation; this is the sized-down mirror of the seal/bars
    ledger custody rule.)"""
    try:
        return os.open(store_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise JournalCorruptError(
                run_id,
                f"the run directory {store_dir} is a SYMLINK — run-state "
                "authority is never read or written through a symlinked "
                "store directory",
            ) from None
        raise


def _open_journal_nofollow(dir_fd: int, flags: int, *, run_id: str, store_dir: Path) -> int:
    """Round-8 review fix (2026-08-24, finding 4): open the journal NAME
    without ever following a symlink at it (the runstate mirror of the
    seal/bars ledger-name rule).

    A symlink at ``journal.jsonl`` — dangling or not — is ``ELOOP`` under
    ``O_NOFOLLOW`` regardless of its target; that is corruption of the
    authority surface, refused by name (``O_CREAT|O_NOFOLLOW`` on a dangling
    link fails the same way, which is exactly the refusal wanted: authority
    is never CREATED through a link either). The open rides the store-dir
    custody fd, so the store directory pathname is never re-resolved."""
    try:
        return os.open(JOURNAL_FILENAME, flags | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise JournalCorruptError(
                run_id,
                f"{store_dir / JOURNAL_FILENAME} is a SYMLINK — run-state "
                "authority (journal.jsonl) is never created, read, or "
                "appended through a symlink: a journal name that redirects "
                "outside the validated store directory (e.g. a copied chain "
                "under /tmp) is corruption of the authority surface, and a "
                "reboot must never erase a returned-success transition",
            ) from None
        raise


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while True:
        chunk = os.pread(fd, 65536, offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def append_record(store_dir: Path, record: JournalRecord) -> str:
    """Append one hash-chained record; returns its `record_sha256`.

    Round-1 review fix (2026-08-23, probe /tmp/pr-a-runstate-write-probes.log
    STALE_APPEND_RETURNED_WITH_DAMAGED_TAIL): the advisory flock is taken,
    the journal tail is re-read FROM DISK under the lock, and the caller's
    `prev_record_sha256` is verified against it before the new record is
    written. A mismatch raises `JournalConcurrentWriteError` — the caller
    must re-replay and rebuild the prev hash. A torn final line is also
    refused outright: appending past a torn tail converts a damaged record
    into mid-file corruption, which the store can never safely repair.

    Round-8 review fix (2026-08-24, finding 4): the append opens the journal
    NAME ``O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW`` against the store dir held
    as a REAL directory fd — a symlink at journal.jsonl (dangling or not) is
    ``ELOOP`` → ``JournalCorruptError``, so authority is never appended
    through a link (the pre-fix plain ``open(..., "a")`` followed it and a
    copied chain under /tmp gained the record).
    """
    record = record.model_copy(update={"record_sha256": _record_hash(record)})
    run_id = store_dir.name
    line = json.dumps(
        json.loads(record.model_dump_json()),  # enum -> plain str, sorted not needed
        sort_keys=True,
        separators=(",", ":"),
    )
    store_dir.mkdir(parents=True, exist_ok=True)
    dir_fd = _open_store_dir_nofollow(store_dir, run_id=run_id)
    try:
        fd = _open_journal_nofollow(
            dir_fd, os.O_WRONLY | os.O_APPEND | os.O_CREAT, run_id=run_id, store_dir=store_dir
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                # Round-1 review fix: re-read the locked tail and verify.
                tail_view = _locked_tail_view(dir_fd, run_id=run_id)
                if tail_view.torn_tail:
                    raise JournalConcurrentWriteError(
                        run_id,
                        "journal has a torn final line; refusing to append "
                        "past it (append-after-torn converts damage into "
                        "mid-file corruption). Repair is an explicit owner act",
                    )
                if tail_view.tail_hash != record.prev_record_sha256:
                    raise JournalConcurrentWriteError(
                        run_id,
                        f"caller's prev_record_sha256 {record.prev_record_sha256[:12]}… "
                        f"does not match the locked tail hash {tail_view.tail_hash[:12]}…",
                    )
                os.write(fd, (line + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        # directory durability on the CUSTODY fd: the store dir pathname is
        # never re-resolved (round-8, finding 4).
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return record.record_sha256


@dataclass(frozen=True)
class _LockedTail:
    """What the journal tail looks like right now, under the flock."""

    tail_hash: str  # hash of the LAST valid record; GENESIS_PREV if empty
    torn_tail: bool  # True iff the final line on disk failed decode/verify


def _locked_tail_view(dir_fd: int, *, run_id: str) -> _LockedTail:
    """Read the journal from disk (under the held flock) and classify its tail.

    A torn FINAL line (decode or chain-verify fail on the last line only) is
    reported as `torn_tail=True` — it was never acknowledged and must NOT
    be followed by an append (probe COMPLETE_BAD_TAIL_TOLERATED).
    Mid-file damage is NOT possible here because the replay rule would
    have raised JournalCorruptError before reaching this function — but if
    we see it on the locked read, surface it as torn to keep the caller
    safe (never silently skip non-tail damage).

    Round-8 review fix (finding 4): the read re-opens the journal NAME
    ``O_RDONLY|O_NOFOLLOW`` against the store-dir custody fd — the advisory
    lock is on the file inode, not the file handle, so the second open from
    the same thread sees the same locked state, and a symlink at the name
    refuses with ``ELOOP`` instead of being read through.
    """
    try:
        fd = os.open(JOURNAL_FILENAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        return _LockedTail(tail_hash=GENESIS_PREV, torn_tail=False)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise JournalCorruptError(
                run_id,
                "journal.jsonl is a SYMLINK under the held lock — run-state "
                "authority is never read or appended through a symlink",
            ) from None
        raise
    try:
        raw = _read_all(fd).decode("utf-8", errors="replace")
    finally:
        os.close(fd)
    lines = raw.splitlines()
    prev_hash = GENESIS_PREV
    last_good_hash = GENESIS_PREV
    for _index, line in enumerate(lines):
        record = _decode_record(line)
        if record is None or not _verify_chain(record, prev_hash):
            return _LockedTail(tail_hash=last_good_hash, torn_tail=True)
        last_good_hash = record.record_sha256
        prev_hash = record.record_sha256
    return _LockedTail(tail_hash=last_good_hash, torn_tail=False)


def _decode_record(line: str) -> JournalRecord | None:
    try:
        raw = json.loads(line)
        return JournalRecord.model_validate(raw)
    except Exception:
        return None


def _verify_chain(record: JournalRecord, prev_hash: str) -> bool:
    if record.prev_record_sha256 != prev_hash:
        return False
    return record.record_sha256 == _record_hash(record)


def replay(store_dir: Path, *, run_id: str = "?") -> JournalView:
    """Verify + decode the journal. See module docstring for the torn-tail
    rule: the final line may be damaged (crash mid-append); anything earlier
    must verify or the store is corrupt evidence.

    Round-8 review fix (2026-08-24, finding 4): the journal NAME is opened
    ``O_RDONLY|O_NOFOLLOW`` against the store dir held as a REAL directory
    fd — a symlink at journal.jsonl (dangling or not; ``Path.exists()`` is
    False for the dangling case and used to classify it as absent) is
    ``ELOOP`` → ``JournalCorruptError``, so the chain is never READ through
    a link either."""
    try:
        dir_fd = _open_store_dir_nofollow(store_dir, run_id=run_id)
    except FileNotFoundError:
        # Missing store dir: an absent journal is not corruption (the caller
        # classifies: pre-journal legacy -> UNKNOWN, never FAILED).
        return JournalView(records=(), tail_hash=GENESIS_PREV, tail_damaged=False)
    try:
        try:
            fd = _open_journal_nofollow(dir_fd, os.O_RDONLY, run_id=run_id, store_dir=store_dir)
        except FileNotFoundError:
            return JournalView(records=(), tail_hash=GENESIS_PREV, tail_damaged=False)
        try:
            raw = _read_all(fd).decode("utf-8", errors="replace")
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
    lines = raw.splitlines()
    records: list[JournalRecord] = []
    prev_hash = GENESIS_PREV
    damaged_tail = False
    for _index, line in enumerate(lines):
        record = _decode_record(line)
        is_final = _index == len(lines) - 1
        if record is None or not _verify_chain(record, prev_hash):
            if is_final:
                damaged_tail = True
                continue  # a torn tail was never acknowledged; exclude it
            raise JournalCorruptError(
                run_id,
                f"journal line {_index + 1} failed decode/hash/chain verification",
            )
        records.append(record)
        prev_hash = record.record_sha256
    return JournalView(records=tuple(records), tail_hash=prev_hash, tail_damaged=damaged_tail)


class Projection(StrictModel):
    run_id: str
    state: RunState | None  # None only before the GENESIS record exists
    seq: int
    tail_hash: str
    tail_damaged: bool
    written_at_epoch: int


def build_projection(run_id: str, view: JournalView, *, written_at_epoch: int) -> Projection:
    state: RunState | None = None
    seq = 0
    for record in view.records:
        if record.to_state is not None:
            state = record.to_state
        seq = record.seq
    return Projection(
        run_id=run_id,
        state=state,
        seq=seq,
        tail_hash=view.tail_hash,
        tail_damaged=view.tail_damaged,
        written_at_epoch=written_at_epoch,
    )


def write_projection(store_dir: Path, projection: Projection) -> None:
    """Atomic projection write: pid-qualified temp + `os.replace`, so a
    reader never observes a half-written `current.json`."""
    tmp = store_dir / f".{PROJECTION_FILENAME}.{os.getpid()}.tmp"
    tmp.write_text(
        json.dumps(json.loads(projection.model_dump_json()), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, store_dir / PROJECTION_FILENAME)
    _fsync_dir(store_dir)


def load_projection(store_dir: Path, *, run_id: str = "?") -> Projection:
    path = store_dir / PROJECTION_FILENAME
    try:
        return Projection.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise ProjectionTornError(run_id, "current.json absent") from None
    except Exception as exc:
        raise ProjectionTornError(run_id, f"current.json unreadable ({exc})") from exc
