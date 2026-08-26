"""Append-only, hash-chained transition journal + atomic state projection.

The journal is the authority (handoff constraint 9: never `/tmp`). Design:

* One JSON line per record, `fsync` before the append returns, so a crash
  can lose at most the line being written — never an earlier one.
* Each record carries `record_sha256 = sha256(domain ‖ canonical-json of the
  record without its hash)` and `prev_record_sha256` chaining to its
  predecessor (genesis chains to 64 zeros). Replay verifies BOTH, so a
  single tampered or reordered record anywhere is detected.
* R15 (finding 5, 2026-08-25): the companion identity record beside the
  journal carries its COMMITTED EXTENT (`extent_size` +
  `committed_tail_sha256`, the seal convention) — written at creation over
  the empty extent, advanced after each append's data fsync + final-name
  check, and verified at every replay/append by the three-branch class
  extent check (`custody.check_committed_extent`): a same-inode prefix
  rollback, an in-place rewrite of the pinned bytes, or an unproven larger
  rewrite refuses, while the benign next-append-after-crash window (a torn
  tail beyond the pinned extent) is accepted only through the prefix proof.
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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from tree_options.data.digest import sha256_hex
from tree_options.runstate import custody
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


def _store_fd(
    store_dir: Path,
    *,
    run_id: str,
    create: bool,
    supplied: int | None,
) -> tuple[int | None, bool]:
    """Return (fd, owned): supplied FDs stay owned by their caller.

    R15 (finding 2, 2026-08-25): the journal's store-dir walk is a DURABLE
    TRAVERSAL — every traversed component's entry is committed in its parent
    on BOTH branches (created and existing-open), so a reboot can no longer
    drop a store component a prior invocation left between its mkdir and
    its parent fsync (restart closure for the journal's authority tree)."""
    if supplied is not None:
        return supplied, False
    return (
        custody.open_directory(
            store_dir,
            create=create,
            run_id=run_id,
            purpose="run-state store",
            durable=True,
        ),
        True,
    )


def _load_or_bind_journal_name(
    store_dir: Path, dir_fd: int, journal_fd: int, *, run_id: str
) -> custody.NameBinding:
    """Round-11 review fix (2026-08-25, finding 3): the journal's durable
    name→inode binding — the successor-window closer.

    The round-8 post-fsync name check can only see swaps that land BEFORE
    it; a swap landing after it but before the return left a byte-copy clone
    at ``journal.jsonl`` that a successor process replayed and APPENDED to —
    two authority tails for one run. At journal creation (an empty journal,
    under the flock, BEFORE the first append lands) the file's
    ``(st_dev, st_ino)`` is pinned in a companion identity record
    (``journal.jsonl.identity.json``, custody-written beside the journal);
    every open after that verifies the name still maps to the bound inode
    and refuses — corruption/reconciliation, never success. A clone has the
    wrong inode, so it is refused at the next open and can never gain
    authority. An unbound NON-EMPTY journal is reconciliation, never an
    append and never a silent re-bind.

    R15 (finding 5): returns the binding so the caller runs the
    committed-extent check against it and advances it after each append —
    the companion is this journal's ONLY durable extent record.
    """
    purpose = "journal.jsonl authority"
    binding = custody.load_name_binding(
        store_dir, dir_fd, JOURNAL_FILENAME, run_id=run_id, purpose=purpose
    )
    if binding is None:
        held = os.fstat(journal_fd)
        if held.st_size != 0:
            raise JournalCorruptError(
                run_id,
                f"journal.jsonl holds {held.st_size} bytes with no durable name "
                "binding — an unbound journal is never appended or re-bound in "
                "place; reconcile with the owner",
            )
        return custody.bind_name_identity(
            store_dir, dir_fd, JOURNAL_FILENAME, journal_fd, run_id=run_id, purpose=purpose
        )
    custody.verify_name_binding(
        dir_fd,
        JOURNAL_FILENAME,
        journal_fd,
        binding,
        run_id=run_id,
        purpose=purpose,
    )
    return binding


def _read_bound_journal_bytes(
    store_dir: Path, dir_fd: int, journal_fd: int, *, run_id: str
) -> tuple[bytes, custody.NameBinding | None]:
    """The replay-side read: the journal name must still map to its BOUND
    inode while its bytes are read. An EMPTY unbound journal carries no
    authority yet (the creation crash window — bound at the next append); a
    NON-EMPTY unbound journal is reconciliation, never authority. Returns
    the payload with the binding (None when nothing was ever bound) for the
    caller's committed-extent check (R15, finding 5)."""
    purpose = "journal.jsonl authority"
    binding = custody.load_name_binding(
        store_dir, dir_fd, JOURNAL_FILENAME, run_id=run_id, purpose=purpose
    )
    if binding is None:
        if os.fstat(journal_fd).st_size != 0:
            raise JournalCorruptError(
                run_id,
                f"journal.jsonl holds {os.fstat(journal_fd).st_size} bytes with "
                "no durable name binding — an unbound journal is never read as "
                "authority; reconcile with the owner",
            )
        custody.verify_directory_identity(store_dir, dir_fd, run_id=run_id)
        return b"", None
    custody.verify_name_binding(
        dir_fd,
        JOURNAL_FILENAME,
        journal_fd,
        binding,
        run_id=run_id,
        purpose=purpose,
    )
    payload = custody.read_all(journal_fd)
    custody.verify_name_identity(
        dir_fd,
        JOURNAL_FILENAME,
        journal_fd,
        run_id=run_id,
        purpose=purpose,
    )
    custody.verify_directory_identity(store_dir, dir_fd, run_id=run_id)
    return payload, binding


def _binding_refusal(run_id: str) -> Callable[[str], NoReturn]:
    """The committed-extent refusal raises in the journal's corruption family."""

    def refuse(detail: str) -> NoReturn:
        raise JournalCorruptError(run_id, detail) from None

    return refuse


def _check_journal_committed_extent(
    store_dir: Path,
    binding: custody.NameBinding,
    *,
    ledger_bytes: int,
    view: JournalView,
    raw_ledger_bytes: bytes,
    run_id: str,
) -> None:
    """R15 (finding 5): the journal's committed-extent rule — the ONE class
    mechanism (``custody.check_committed_extent``, R15 finding 1) applied to
    the journal's companion identity record, its only durable extent record.
    A valid chain prefix is not committed authority: a same-inode
    truncation/prefix rollback refuses, an in-place rewrite of the pinned
    bytes refuses, and a journal LARGER than the pinned extent is accepted
    only through the prefix proof (the benign next-append-after-crash window
    — a torn tail beyond the extent)."""
    custody.check_committed_extent(
        extent_size=binding.extent_size,
        committed_tail_sha256=binding.committed_tail_sha256,
        ledger_bytes=ledger_bytes,
        view_tail_sha256=view.tail_hash,
        raw_ledger_bytes=raw_ledger_bytes,
        replay_prefix=lambda text: _replay_journal_text(text, run_id=run_id),
        subject=str(store_dir / JOURNAL_FILENAME),
        origin="the companion identity record",
        refuse=_binding_refusal(run_id),
    )


def append_record(
    store_dir: Path,
    record: JournalRecord,
    *,
    _dir_fd: int | None = None,
) -> str:
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
    dir_fd, owned = _store_fd(
        store_dir,
        run_id=run_id,
        create=True,
        supplied=_dir_fd,
    )
    assert dir_fd is not None
    try:
        fd = custody.open_regular(
            dir_fd,
            JOURNAL_FILENAME,
            os.O_RDWR | os.O_APPEND | os.O_CREAT,
            run_id=run_id,
            purpose="journal.jsonl authority",
        )
        assert fd is not None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                # Round-11 (finding 3): bind at creation (empty journal,
                # before the first append lands) or verify the name still
                # maps to the bound inode — under the flock either way.
                # R15 (finding 5): the binding is also the journal's durable
                # COMMITTED-EXTENT record, checked below and advanced after
                # the append.
                binding = _load_or_bind_journal_name(store_dir, dir_fd, fd, run_id=run_id)
                # Round-1 review fix: re-read the locked tail and verify.
                custody.verify_name_identity(
                    dir_fd,
                    JOURNAL_FILENAME,
                    fd,
                    run_id=run_id,
                    purpose="journal.jsonl authority",
                )
                tail_view = _locked_tail_view(fd)
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
                # R15 (finding 5): never append onto a journal that no longer
                # holds its committed extent in full as a PROVEN prefix — a
                # truncated or unproven-larger rewrite must not be re-spent
                # by this append.
                locked_raw = custody.read_all(fd)
                _check_journal_committed_extent(
                    store_dir,
                    binding,
                    ledger_bytes=len(locked_raw),
                    view=_replay_journal_text(
                        locked_raw.decode("utf-8", errors="replace"), run_id=run_id
                    ),
                    raw_ledger_bytes=locked_raw,
                    run_id=run_id,
                )
                os.lseek(fd, 0, os.SEEK_END)
                payload = (line + "\n").encode("utf-8")
                # Round-11 (finding 5): the journal's looped write is now the
                # shared custody.write_all — the only write path for authority
                # records (a short write is completed or raises, never
                # acknowledged torn).
                custody.write_all(fd, payload)
                os.fsync(fd)
                custody.verify_name_identity(
                    dir_fd,
                    JOURNAL_FILENAME,
                    fd,
                    run_id=run_id,
                    purpose="journal.jsonl authority",
                )
                post = _locked_tail_view(fd)
                if post.torn_tail or post.tail_hash != record.record_sha256:
                    raise JournalCorruptError(
                        run_id,
                        "journal bytes no longer contain the just-fsynced record "
                        "at the verified tail",
                    )
                custody.verify_directory_identity(store_dir, dir_fd, run_id=run_id)
                # R15 (finding 5): the append is durable and the name still
                # maps to the locked inode, so the companion's COMMITTED
                # EXTENT advances to it now — the last act under the flock.
                # A refusal here leaves the record durable and the next open
                # accepting it only through the prefix proof (the crash
                # window), re-anchoring at the next append.
                custody.advance_name_binding_extent(
                    store_dir,
                    dir_fd,
                    JOURNAL_FILENAME,
                    fd,
                    new_extent_size=len(locked_raw) + len(payload),
                    new_committed_tail_sha256=record.record_sha256,
                    run_id=run_id,
                    purpose="journal.jsonl authority",
                )
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        # directory durability on the CUSTODY fd: the store dir pathname is
        # never re-resolved (round-8, finding 4).
        os.fsync(dir_fd)
    finally:
        if owned:
            os.close(dir_fd)
    return record.record_sha256


@dataclass(frozen=True)
class _LockedTail:
    """What the journal tail looks like right now, under the flock."""

    tail_hash: str  # hash of the LAST valid record; GENESIS_PREV if empty
    torn_tail: bool  # True iff the final line on disk failed decode/verify


def _locked_tail_view(fd: int) -> _LockedTail:
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
    raw = custody.read_all(fd).decode("utf-8", errors="replace")
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


def _replay_journal_text(text: str, *, run_id: str = "?") -> JournalView:
    """The journal's own replay verifier over decoded text (the local replay
    function every committed-extent prefix proof reuses — the exact shape
    ``_replay_bars_text`` gave the bars ledger). The torn-tail rule is
    unchanged: a damaged FINAL line is reported, never followed; the same
    damage in a non-final record is corruption."""
    lines = text.splitlines()
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


def replay(
    store_dir: Path,
    *,
    run_id: str = "?",
    _dir_fd: int | None = None,
) -> JournalView:
    """Verify + decode the journal. See module docstring for the torn-tail
    rule: the final line may be damaged (crash mid-append); anything earlier
    must verify or the store is corrupt evidence.

    Round-8 review fix (2026-08-24, finding 4): the journal NAME is opened
    ``O_RDONLY|O_NOFOLLOW`` against the store dir held as a REAL directory
    fd — a symlink at journal.jsonl (dangling or not; ``Path.exists()`` is
    False for the dangling case and used to classify it as absent) is
    ``ELOOP`` → ``JournalCorruptError``, so the chain is never READ through
    a link either.

    Round-11 review fix (2026-08-25, finding 3): the read verifies the
    journal name's durable name→inode binding (see
    ``_load_or_bind_journal_name``) — a clone installed at the name during a
    prior append's success window has the wrong inode and is refused here,
    so it can never be replayed as authority. A bound name that vanished is
    refused too; only a never-bound absent name is an empty view.

    R15 review fix (2026-08-25, finding 5): the replayed view is verified
    against the companion's COMMITTED EXTENT (the three-branch class extent
    check) — a valid complete prefix is not committed authority, so a
    same-inode truncation/rollback, an in-place rewrite of the pinned bytes,
    or an unproven larger rewrite refuses here and the store never consumes
    it. A torn tail BEYOND the pinned extent keeps its damaged_tail
    semantics (the extent check proves the committed prefix)."""
    dir_fd, owned = _store_fd(
        store_dir,
        run_id=run_id,
        create=False,
        supplied=_dir_fd,
    )
    if dir_fd is None:
        return JournalView(records=(), tail_hash=GENESIS_PREV, tail_damaged=False)
    try:
        journal_fd = custody.open_regular(
            dir_fd,
            JOURNAL_FILENAME,
            os.O_RDONLY,
            run_id=run_id,
            purpose="journal.jsonl authority",
            allow_missing=True,
        )
        if journal_fd is None:
            if (
                custody.load_name_binding(
                    store_dir,
                    dir_fd,
                    JOURNAL_FILENAME,
                    run_id=run_id,
                    purpose="journal.jsonl authority",
                )
                is not None
            ):
                raise JournalCorruptError(
                    run_id,
                    "journal.jsonl is absent while its durable name binding "
                    "exists — bound authority may not silently vanish",
                )
            custody.verify_directory_identity(store_dir, dir_fd, run_id=run_id)
            return JournalView(records=(), tail_hash=GENESIS_PREV, tail_damaged=False)
        try:
            raw_bytes, binding = _read_bound_journal_bytes(
                store_dir, dir_fd, journal_fd, run_id=run_id
            )
        finally:
            os.close(journal_fd)
    finally:
        if owned:
            os.close(dir_fd)
    view = _replay_journal_text(raw_bytes.decode("utf-8", errors="replace"), run_id=run_id)
    # R15 (finding 5): the companion's COMMITTED EXTENT — a valid prefix is
    # not committed authority, and a journal larger than the pinned extent
    # must PROVE the pinned prefix (the class extent check, all three
    # branches). RunStore.open/transition consume only this verified view.
    if binding is not None:
        _check_journal_committed_extent(
            store_dir,
            binding,
            ledger_bytes=len(raw_bytes),
            view=view,
            raw_ledger_bytes=raw_bytes,
            run_id=run_id,
        )
    return view


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


def write_projection(
    store_dir: Path,
    projection: Projection,
    *,
    _dir_fd: int | None = None,
) -> None:
    """Atomic projection write: pid-qualified temp + `os.replace`, so a
    reader never observes a half-written `current.json`."""
    run_id = projection.run_id
    dir_fd, owned = _store_fd(
        store_dir,
        run_id=run_id,
        create=False,
        supplied=_dir_fd,
    )
    if dir_fd is None:
        raise FileNotFoundError(store_dir)
    try:
        payload = (
            json.dumps(json.loads(projection.model_dump_json()), indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        custody.atomic_write(
            store_dir,
            dir_fd,
            PROJECTION_FILENAME,
            payload,
            run_id=run_id,
            purpose="current.json projection",
            mode=0o644,
            exclusive=False,
        )
    finally:
        if owned:
            os.close(dir_fd)


def load_projection(
    store_dir: Path,
    *,
    run_id: str = "?",
    _dir_fd: int | None = None,
) -> Projection:
    dir_fd, owned = _store_fd(
        store_dir,
        run_id=run_id,
        create=False,
        supplied=_dir_fd,
    )
    if dir_fd is None:
        raise ProjectionTornError(run_id, "current.json absent")
    try:
        raw = custody.read_named_bytes(
            store_dir,
            dir_fd,
            PROJECTION_FILENAME,
            run_id=run_id,
            purpose="current.json projection",
            allow_missing=True,
        )
    finally:
        if owned:
            os.close(dir_fd)
    if raw is None:
        raise ProjectionTornError(run_id, "current.json absent")
    try:
        return Projection.model_validate(json.loads(raw))
    except Exception as exc:
        raise ProjectionTornError(run_id, f"current.json unreadable ({exc})") from exc
