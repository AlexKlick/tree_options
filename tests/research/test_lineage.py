"""Lineage oracle (oracle 4 of the RL-2 acceptance matrix).

The lineage table records ``parent_run_id → child_run_id`` and the
parent's effective identity (engine_sha / input_snapshot_sha /
calendar_sha). These oracles pin:

    1. attach_child is idempotent on identical content, refuses on
       conflicting content, never silently changes.
    2. parent_changed detects drift in any of the three integrity
       shas.
    3. parent_missing refuses both "no envelope" and "non-terminal
       envelope" forks.
    4. list_children walks the lineage table in insertion order.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tree_options.research.runstate.store import (
    RunstateStoreError,
    open_runstate_store,
)
from tree_options.research.scenarios.lineage import (
    PARENT_KIND,
    ChildRef,
    ParentRef,
    attach_child,
    list_children,
    load_parent_ref,
    parent_changed,
    parent_missing,
    store_parent_ref,
)
from tree_options.research.scenarios.refusal_codes import (
    SCENARIO_PARENT_CHANGED,
    SCENARIO_PARENT_MISSING,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "rs"
    ws.mkdir()
    return ws


def _ref(*, parent_run_id: str = "p1",
         engine: str = "a" * 64,
         input_sha: str = "b" * 64,
         calendar_sha: str = "c" * 64,
         spec_hash: str = "x" * 64) -> ParentRef:
    return ParentRef(
        parent_run_id=parent_run_id,
        parent_spec_hash=spec_hash,
        parent_engine_sha256=engine,
        parent_input_snapshot_sha256=input_sha,
        parent_calendar_sha256=calendar_sha,
    )


# -- attach_child ---------------------------------------------------------


def test_attach_child_is_idempotent_on_identical_content(workspace):
    with open_runstate_store(workspace) as store:
        ref = ChildRef(
            child_run_id="child1",
            parent_run_id="parent1",
            scenario_kind="contribution_planning",
            scenario_diff_sha256="d" * 64,
        )
        sha1 = attach_child(store, ref, at=datetime.now())
        sha2 = attach_child(store, ref, at=datetime.now())
        assert sha1 == sha2


def test_attach_child_refuses_conflicting_content(workspace):
    with open_runstate_store(workspace) as store:
        ref1 = ChildRef(
            child_run_id="child1",
            parent_run_id="parent1",
            scenario_kind="contribution_planning",
            scenario_diff_sha256="d1" * 32,
        )
        attach_child(store, ref1, at=datetime.now())
        ref2 = ChildRef(
            child_run_id="child1",  # same child
            parent_run_id="parent_OTHER",  # different parent
            scenario_kind="contribution_planning",
            scenario_diff_sha256="d2" * 32,
        )
        with pytest.raises(RunstateStoreError):
            attach_child(store, ref2, at=datetime.now())


def test_list_children_walks_lineage_in_insertion_order(workspace):
    with open_runstate_store(workspace) as store:
        for cid in ("c1", "c2", "c3"):
            attach_child(store, ChildRef(
                child_run_id=cid,
                parent_run_id="parent_X",
                scenario_kind="contribution_planning",
                scenario_diff_sha256="d" * 64,
            ), at=datetime.now())
        # And one to a different parent, which must be filtered out.
        attach_child(store, ChildRef(
            child_run_id="c4",
            parent_run_id="parent_Y",
            scenario_kind="contribution_planning",
            scenario_diff_sha256="d" * 64,
        ), at=datetime.now())
        kids = list_children(store, "parent_X")
    assert kids == ("c1", "c2", "c3")


# -- parent_ref ------------------------------------------------------


def test_store_and_load_parent_ref_roundtrip(workspace):
    ref = _ref(parent_run_id="p1")
    with open_runstate_store(workspace) as store:
        store_parent_ref(store, ref, at=datetime.now())
        loaded = load_parent_ref(store, "p1")
    assert loaded == ref


def test_load_parent_ref_returns_none_for_unknown_parent(workspace):
    with open_runstate_store(workspace) as store:
        loaded = load_parent_ref(store, "missing")
    assert loaded is None


def test_store_parent_ref_is_idempotent_on_identical_content(workspace):
    ref = _ref(parent_run_id="p1")
    with open_runstate_store(workspace) as store:
        store_parent_ref(store, ref, at=datetime.now())
        store_parent_ref(store, ref, at=datetime.now())
        all_refs = list(store.all_at(PARENT_KIND))
    assert len(all_refs) == 1


def test_store_parent_ref_refuses_conflicting_content(workspace):
    """A parent's effective identity is immutable once attached —
    reforging it with conflicting shas raises (this is the
    'lineage is honest' invariant)."""
    with open_runstate_store(workspace) as store:
        store_parent_ref(store, _ref(engine="a" * 64), at=datetime.now())
        with pytest.raises(RunstateStoreError):
            store_parent_ref(store, _ref(engine="z" * 64),
                             at=datetime.now())


# -- parent_changed + parent_missing oracle surfaces -----------------


def test_parent_changed_flags_calendar_drift():
    ref = _ref()
    current = {
        "engine_sha256": ref.parent_engine_sha256,
        "input_snapshot_sha256": ref.parent_input_snapshot_sha256,
        # calendar drifts (different pinned calendar)
        "calendar_sha256": "Z" * 64,
    }
    refusal = parent_changed(current, ref)
    assert refusal is not None
    assert refusal.code == SCENARIO_PARENT_CHANGED


def test_parent_changed_returns_none_when_all_shas_match():
    ref = _ref()
    current = {
        "engine_sha256": ref.parent_engine_sha256,
        "input_snapshot_sha256": ref.parent_input_snapshot_sha256,
        "calendar_sha256": ref.parent_calendar_sha256,
    }
    assert parent_changed(current, ref) is None


def test_parent_missing_when_envelope_is_none():
    refusal = parent_missing(None)
    assert refusal is not None
    assert refusal.code == SCENARIO_PARENT_MISSING


def test_parent_missing_when_envelope_status_not_completed():
    refusal = parent_missing({"status": "running"})
    assert refusal is not None
    assert refusal.code == SCENARIO_PARENT_MISSING


def test_parent_missing_returns_none_for_completed_envelope():
    assert parent_missing({"status": "completed"}) is None
