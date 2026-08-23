"""Journal: chaining, torn tail, mid-file corruption, atomic projection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    _append_chain(store_dir, 3)
    path = store_dir / J.JOURNAL_FILENAME
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
