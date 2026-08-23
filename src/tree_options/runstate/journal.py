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
from tree_options.runstate.errors import JournalCorruptError, ProjectionTornError
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

    The advisory `flock` serializes concurrent appenders (the lease normally
    makes them impossible; this is the belt to that braces).
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
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    _fsync_dir(store_dir)
    return record.record_sha256


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
    for index, line in enumerate(lines):
        record = _decode_record(line)
        is_final = index == len(lines) - 1
        if record is None or not _verify_chain(record, prev_hash):
            if is_final:
                damaged_tail = True
                continue  # a torn tail was never acknowledged; exclude it
            raise JournalCorruptError(
                run_id,
                f"journal line {index + 1} failed decode/hash/chain verification",
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
