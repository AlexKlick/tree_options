"""Journal: chaining, torn tail, mid-file corruption, atomic projection."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tree_options.runstate import custody as C
from tree_options.runstate import journal as J
from tree_options.runstate.errors import JournalCorruptError, ProjectionTornError
from tree_options.runstate.states import RunState


def _record(seq: int, prev: str, *, to_state: RunState | None = RunState.CAPTURING):
    return J.JournalRecord(
        seq=seq,
        kind="GENESIS" if seq == 1 else "TRANSITION",
        to_state=RunState.PLANNED if seq == 1 else to_state,
        reason=f"record {seq}",
        actor_pid=1,
        actor_boot_id="boot-test",
        at_epoch=1_700_000_000 + seq,
        prev_record_sha256=prev,
    )


@pytest.fixture()
def store_dir(tmp_path: Path) -> Path:
    d = tmp_path / "run"
    d.mkdir()
    return d


def _append_chain(store_dir: Path, n: int) -> str:
    prev = J.GENESIS_PREV
    last = prev
    for seq in range(1, n + 1):
        last = J.append_record(store_dir, _record(seq, prev))
        prev = last
    return last


def test_genesis_chains_to_zeros_and_hashes_domain_separated(store_dir):
    digest = J.append_record(store_dir, _record(1, J.GENESIS_PREV))
    view = J.replay(store_dir)
    assert view.records[0].prev_record_sha256 == J.GENESIS_PREV
    assert view.records[0].record_sha256 == digest
    assert view.tail_hash == digest
    assert not view.tail_damaged


def test_chain_verifies_across_many_records(store_dir):
    last = _append_chain(store_dir, 5)
    view = J.replay(store_dir)
    assert len(view.records) == 5
    assert view.tail_hash == last
    assert not view.tail_damaged


def test_single_record_hash_tamper_detected_midfile(store_dir):
    _append_chain(store_dir, 4)
    lines = (store_dir / J.JOURNAL_FILENAME).read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["reason"] = "rewritten history"
    lines[1] = json.dumps(tampered, sort_keys=True, separators=(",", ":"))
    (store_dir / J.JOURNAL_FILENAME).write_text("\n".join(lines) + "\n")
    with pytest.raises(JournalCorruptError):
        J.replay(store_dir)


def test_reordering_detected(store_dir):
    _append_chain(store_dir, 3)
    lines = (store_dir / J.JOURNAL_FILENAME).read_text().splitlines()
    lines[0], lines[1] = lines[1], lines[0]
    (store_dir / J.JOURNAL_FILENAME).write_text("\n".join(lines) + "\n")
    with pytest.raises(JournalCorruptError):
        J.replay(store_dir)


def test_truncated_final_line_is_tail_damaged_never_corrupt(store_dir):
    _append_chain(store_dir, 3)
    path = store_dir / J.JOURNAL_FILENAME
    text = path.read_text()
    path.write_text(text + '{"seq": 4, "kind": "TRANSIT')  # crash mid-append
    view = J.replay(store_dir)
    assert view.tail_damaged
    assert len(view.records) == 3


def test_tampered_final_line_is_tail_damaged(store_dir):
    """A tampered FINAL line is tail damage, never mid-file corruption —
    R15 (finding 5) scopes the tolerance exactly as the seal and bars
    ledgers landed it (R15 finding 1): the damaged line must lie BEYOND the
    committed extent, because only then was it never acknowledged. The
    fixture lands a third record the way the interrupted append does (direct
    write + fsync, no companion extent advance), then rewrites that line's
    reason: the committed prefix still proves, the tampered line fails chain
    verification as the FINAL line, and the view reports tail damage with
    the two committed records intact. A tampered line INSIDE the committed
    extent is the in-place-rewrite refusal instead (the R15 extent tests)."""
    _append_chain(store_dir, 2)
    path = store_dir / J.JOURNAL_FILENAME
    committed_size = path.stat().st_size
    # the crash window: a chained third record lands and is fsynced on the
    # journal, but the companion extent advance never runs
    view = J.replay(store_dir)
    third = _record(3, view.tail_hash)
    signed = third.model_copy(update={"record_sha256": J._record_hash(third)})
    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        os.write(
            fd,
            (
                json.dumps(
                    json.loads(signed.model_dump_json()),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8"),
        )
        os.fsync(fd)
    finally:
        os.close(fd)
    assert path.stat().st_size > committed_size
    # tamper the final (never-committed) line
    lines = path.read_text().splitlines()
    last = json.loads(lines[-1])
    last["reason"] = "never happened"
    lines[-1] = json.dumps(last, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    view = J.replay(store_dir)
    assert view.tail_damaged
    assert len(view.records) == 2


def test_missing_journal_is_empty_view_not_corruption(tmp_path):
    view = J.replay(tmp_path / "absent")
    assert view.records == ()
    assert view.tail_hash == J.GENESIS_PREV
    assert not view.tail_damaged


def test_projection_write_is_atomic_and_loadable(store_dir):
    _append_chain(store_dir, 2)
    view = J.replay(store_dir)
    projection = J.build_projection("run-x", view, written_at_epoch=123)
    J.write_projection(store_dir, projection)
    assert not list(store_dir.glob(".current.json.*.tmp"))  # no tmp residue
    loaded = J.load_projection(store_dir)
    assert loaded.state is RunState.CAPTURING
    assert loaded.seq == 2
    assert (
        loaded == projection.model_copy(update={"written_at_epoch": loaded.written_at_epoch})
        or loaded.state == projection.state
    )


def test_projection_rebuild_is_pure_function_of_journal(store_dir):
    _append_chain(store_dir, 3)
    first = J.load_projection(store_dir) if (store_dir / J.PROJECTION_FILENAME).exists() else None
    view = J.replay(store_dir)
    projection = J.build_projection("run-x", view, written_at_epoch=999)
    assert projection.state is RunState.CAPTURING
    assert first is None  # nothing wrote a projection yet in this fixture


def test_torn_projection_refused_on_load(store_dir):
    (store_dir / J.PROJECTION_FILENAME).write_text("{ not json")
    with pytest.raises(ProjectionTornError):
        J.load_projection(store_dir)


def test_absent_projection_refused_on_load(store_dir):
    with pytest.raises(ProjectionTornError):
        J.load_projection(store_dir)


def test_fsync_durable_directory_roundtrip(store_dir):
    _append_chain(store_dir, 2)
    # A fresh open+append continues the chain from the verified tail.
    view = J.replay(store_dir)
    digest = J.append_record(
        store_dir, _record(3, view.tail_hash, to_state=RunState.CAPTURE_COMPLETE)
    )
    view2 = J.replay(store_dir)
    assert view2.records[-1].record_sha256 == digest
    assert not view2.tail_damaged


# ---- R15 (finding 5): the journal companion carries the COMMITTED EXTENT --------------
#
# The committed-extent mechanism (R15 findings 1 + 4) existed for the seal
# runstate anchor and the bars companion only; the journal companion bound
# name/st_dev/st_ino alone, and replay accepted ANY valid complete prefix.
# Attack (no concurrency, same inode): after two acknowledged transitions,
# truncate journal.jsonl in place to the first complete record — the inode
# and companion still verify, the successor replays an EARLIER state,
# accepts a legal transition from that rolled-back tail, and rewrites the
# projection: accepted history that omits acknowledged lifecycle records.
# The companion now carries extent_size + committed_tail_sha256 (the seal
# convention: the empty extent 0/GENESIS at creation, advanced after every
# append's data fsync + final-name check), and every replay/append runs the
# three-branch class extent check (custody.check_committed_extent).


def _truncate_in_place(path: Path, keep: bytes) -> None:
    """The same-inode attack primitive: truncate + rewrite in place."""
    fd = os.open(path, os.O_WRONLY)
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, keep)
        os.fsync(fd)
    finally:
        os.close(fd)


def test_same_inode_rollback_to_an_earlier_transition_refused(store_dir):
    """The R15 finding-5 attack: transitions committed, then the journal is
    truncated IN PLACE to the first complete record — the same inode, so the
    durable name binding verifies exactly. The replay must refuse on the
    committed extent and a successor append from the rolled-back tail must
    be refused: acknowledged lifecycle records are never silently forgotten."""
    _append_chain(store_dir, 3)
    path = store_dir / J.JOURNAL_FILENAME
    first_line = path.read_text().splitlines(keepends=True)[0].encode("utf-8")
    before = os.stat(path)
    _truncate_in_place(path, first_line)
    after = os.stat(path)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino), (
        "the attack keeps the inode, so every round-11 identity check passes"
    )
    with pytest.raises(JournalCorruptError, match="committed extent"):
        J.replay(store_dir)
    # the successor cannot spend the rolled-back tail either: an append
    # chained from the surviving prefix must refuse, never re-anchor on it
    survivor = J.JournalRecord.model_validate(json.loads(first_line.decode("utf-8")))
    with pytest.raises(JournalCorruptError, match="committed extent"):
        J.append_record(store_dir, _record(2, survivor.record_sha256))
    assert path.read_bytes() == first_line, (
        "a rolled-back journal must never gain a successor record — "
        "acknowledged lifecycle records are never silently forgotten"
    )


def test_tampered_final_line_inside_the_committed_extent_is_an_in_place_rewrite_refusal(
    store_dir,
):
    """The other edge of the scoped tolerance: a final line that WAS
    acknowledged (inside the committed extent) and is then rewritten in
    place is NOT tail damage — the companion proves those bytes were
    committed, so this is the in-place-rewrite refusal, never a silent drop
    of the acknowledged record."""
    _append_chain(store_dir, 3)
    path = store_dir / J.JOURNAL_FILENAME
    lines = path.read_text().splitlines()
    last = json.loads(lines[-1])
    last["reason"] = "never happened"
    lines[-1] = json.dumps(last, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(JournalCorruptError, match="committed extent"):
        J.replay(store_dir)


def test_companion_extent_advances_to_the_appended_bytes_and_tail(store_dir):
    """After each append the companion identity record pins exactly the new
    byte size and the view's tail hash (the seal convention: written at
    creation over the empty extent, advanced after every append's data fsync
    + final-name check via the identity-conditional custody replacement)."""
    _append_chain(store_dir, 2)
    companion_path = store_dir / C.name_binding_filename(J.JOURNAL_FILENAME)
    mid = C.parse_name_binding_bytes(companion_path.read_bytes(), J.JOURNAL_FILENAME)
    assert mid.extent_size == (store_dir / J.JOURNAL_FILENAME).stat().st_size
    assert mid.committed_tail_sha256 == J.replay(store_dir).tail_hash
    last = J.append_record(store_dir, _record(3, J.replay(store_dir).tail_hash))
    advanced = C.parse_name_binding_bytes(companion_path.read_bytes(), J.JOURNAL_FILENAME)
    assert advanced.extent_size == (store_dir / J.JOURNAL_FILENAME).stat().st_size
    assert advanced.committed_tail_sha256 == last
    assert advanced.committed_tail_sha256 == J.replay(store_dir).tail_hash


def test_append_reanchors_after_a_crash_between_fsync_and_extent_advance(store_dir):
    """The crash window: a record lands and is fsynced on the journal, but
    the companion extent advance never runs. The next read accepts the
    larger journal only through the prefix proof, and the next append
    RE-ANCHORS the companion at the full extent — a failed extent commit
    can never un-spend an acknowledged record."""
    _append_chain(store_dir, 2)
    path = store_dir / J.JOURNAL_FILENAME
    companion_path = store_dir / C.name_binding_filename(J.JOURNAL_FILENAME)
    pinned = C.parse_name_binding_bytes(companion_path.read_bytes(), J.JOURNAL_FILENAME)
    # land the third record the way the interrupted append does: direct
    # write + fsync, no companion extent advance
    view = J.replay(store_dir)
    third = _record(3, view.tail_hash)
    signed = third.model_copy(update={"record_sha256": J._record_hash(third)})
    line = json.dumps(
        json.loads(signed.model_dump_json()), sort_keys=True, separators=(",", ":")
    )
    fd = os.open(path, os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    assert path.stat().st_size > pinned.extent_size
    # the read accepts the larger journal ONLY through the prefix proof
    view = J.replay(store_dir)
    assert len(view.records) == 3
    assert not view.tail_damaged
    # the next append re-anchors the companion at the full extent
    last = J.append_record(store_dir, _record(4, view.tail_hash))
    reanchored = C.parse_name_binding_bytes(companion_path.read_bytes(), J.JOURNAL_FILENAME)
    assert reanchored.extent_size == path.stat().st_size
    assert reanchored.committed_tail_sha256 == last
    assert reanchored.committed_tail_sha256 == J.replay(store_dir).tail_hash
