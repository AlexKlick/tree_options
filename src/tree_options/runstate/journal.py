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
    """
    record = record.model_copy(update={"record_sha256": _record_hash(record)})
    journal_path = store_dir / JOURNAL_FILENAME
    line = json.dumps(
        json.loads(record.model_dump_json()),  # enum -> plain str, sorted not needed
        sort_keys=True,
        separators=(",", ":"),
    )
    store_dir.mkdir(parents=True, exist_ok=True)
    with open(journal_path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            # Round-1 review fix: re-read the locked tail and verify.
            tail_view = _locked_tail_view(fh)
            if tail_view.torn_tail:
                raise JournalConcurrentWriteError(
                    store_dir.name,
                    "journal has a torn final line; refusing to append "
                    "past it (append-after-torn converts damage into "
                    "mid-file corruption). Repair is an explicit owner act",
                )
            if tail_view.tail_hash != record.prev_record_sha256:
                raise JournalConcurrentWriteError(
                    store_dir.name,
                    f"caller's prev_record_sha256 {record.prev_record_sha256[:12]}… "
                    f"does not match the locked tail hash {tail_view.tail_hash[:12]}…",
                )
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    _fsync_dir(store_dir)
    return record.record_sha256


@dataclass(frozen=True)
class _LockedTail:
    """What the journal tail looks like right now, under the flock."""

    tail_hash: str  # hash of the LAST valid record; GENESIS_PREV if empty
    torn_tail: bool  # True iff the final line on disk failed decode/verify


def _locked_tail_view(fh: object) -> _LockedTail:
    """Read the journal from disk (under the held flock) and classify its tail.

    A torn FINAL line (decode or chain-verify fail on the last line only) is
    reported as `torn_tail=True` — it was never acknowledged and must NOT
    be followed by an append (probe COMPLETE_BAD_TAIL_TOLERATED).
    Mid-file damage is NOT possible here because the replay rule would
    have raised JournalCorruptError before reaching this function — but if
    we see it on the locked read, surface it as torn to keep the caller
    safe (never silently skip non-tail damage).

    NB: `fh` is opened in append mode and seek+read returns nothing
    meaningful past EOF; we re-open the path in read mode under the
    OUTER caller's flock — the advisory lock is on the file inode, not
    the file handle, so a second open from the same thread sees the
    same locked state.
    """
    path = Path(getattr(fh, "name", "")) if not isinstance(getattr(fh, "name", ""), int) else None
    if path is None or not str(path):
        # Defensive fallback: no path known — refuse rather than guess.
        raise JournalConcurrentWriteError(
            "<unknown>",
            "append_record's locked-tail view could not determine the "
            "journal path from the file handle; refusing to append",
        )
    raw = path.read_text(encoding="utf-8", errors="replace")
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
    must verify or the store is corrupt evidence."""
    journal_path = store_dir / JOURNAL_FILENAME
    if not journal_path.exists():
        # Missing log: the caller classifies (pre-journal legacy -> UNKNOWN,
        # never FAILED — constraint 10). An absent journal is not corruption.
        return JournalView(records=(), tail_hash=GENESIS_PREV, tail_damaged=False)
    lines = journal_path.read_text(encoding="utf-8").splitlines()
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
