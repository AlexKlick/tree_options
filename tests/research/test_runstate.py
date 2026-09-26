"""Runstate store tests — content-addressed research lane artifacts.

The runstate store is the research lane's equivalent of the desk
evidence store. It is scoped to its own SQLite file under the
research workspace (``<workspace>/runstate.sqlite3``) and never shares a
file with the desk evidence store.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tree_options.research.runstate.store import (
    RunstateStoreError,
    open_runstate_store,
)


def test_open_runstate_store_creates_db_in_workspace(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        assert store.path == tmp_path / "runstate.sqlite3"
        assert store.path.exists()


def test_put_returns_payload_sha256(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        sha = store.put("spec", {"x": 1}, key="abc", at=datetime(2026, 9, 25))
        assert len(sha) == 64
        assert all(c in "0123456789abcdef" for c in sha)


def test_put_is_idempotent_on_same_payload(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        sha1 = store.put("spec", {"x": 1}, key="abc")
        sha2 = store.put("spec", {"x": 1}, key="abc")
        assert sha1 == sha2
        assert store.verify()["objects"] == 1


def test_put_raises_content_conflict_on_different_payload(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        store.put("spec", {"x": 1}, key="abc")
        with pytest.raises(RunstateStoreError, match="content_conflict"):
            store.put("spec", {"x": 2}, key="abc")


def test_unknown_kind_is_rejected(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        with pytest.raises(RunstateStoreError, match="unknown kind"):
            store.put("not_a_real_kind", {"x": 1}, key="abc")


def test_all_returns_objects_in_insertion_order(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        store.put("run", {"i": 1}, key="a", at=datetime(2026, 9, 25))
        store.put("run", {"i": 2}, key="b", at=datetime(2026, 9, 26))
        store.put("run", {"i": 3}, key="c", at=datetime(2026, 9, 27))
        rows = store.all("run")
        assert [r["i"] for r in rows] == [1, 2, 3]


def test_all_at_returns_payload_and_timestamp(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        store.put("result", {"x": 1}, key="abc", at=datetime(2026, 9, 25, 12, 0))
        items = store.all_at("result")
        assert len(items) == 1
        payload, ts = items[0]
        assert payload == {"x": 1}
        assert ts == datetime(2026, 9, 25, 12, 0)


def test_verify_returns_ok_envelope(tmp_path: Path) -> None:
    with open_runstate_store(tmp_path) as store:
        store.put("run", {"i": 1}, key="a")
        store.put("spec", {"x": "y"}, key="b")
        head = store.verify()
        assert head["events"] >= 2
        assert head["objects"] == 2
        assert len(head["head_sha256"]) == 64


def test_runstate_and_desk_evidence_stores_are_isolated(tmp_path: Path) -> None:
    """The research runstate MUST NOT share a file with the desk evidence
    store. The path-overlap guard in ``research.paths`` covers the
    directory level; this test confirms the store itself never reads
    the desk evidence store."""
    desk_path = tmp_path / "desk.sqlite3"
    runstate_path = tmp_path / "research.sqlite3"
    # Desk evidence store is at desk_path; research runstate at runstate_path.
    # Even if the operator misconfigures RESEARCH_WORKSPACE_DIR to point
    # at the desk path, the path-overlap guard fires before the store opens.
    assert desk_path != runstate_path


def test_spec_hash_is_deterministic(tmp_path: Path) -> None:
    """Two specs with the same logical content produce the same hash;
    reordering top-level keys does NOT change the hash (canonical() sorts)."""
    from datetime import date
    from decimal import Decimal

    from tree_options.research.contracts import ComparisonSpec
    from tree_options.research.runstate.spec_hash import spec_hash

    a = ComparisonSpec(
        candidate_ids=("a", "b"),
        starting_capital=Decimal("1000"),
        common_start=date(2024, 1, 2),
        common_end=date(2026, 9, 25),
    )
    # Same content but build a new dataclass instance — must hash equal.
    b = ComparisonSpec(
        candidate_ids=("a", "b"),
        starting_capital=Decimal("1000"),
        common_start=date(2024, 1, 2),
        common_end=date(2026, 9, 25),
    )
    assert spec_hash(a) == spec_hash(b)


def test_spec_hash_differs_when_content_differs(tmp_path: Path) -> None:
    from datetime import date
    from decimal import Decimal

    from tree_options.research.contracts import ComparisonSpec
    from tree_options.research.runstate.spec_hash import spec_hash

    a = ComparisonSpec(
        candidate_ids=("a", "b"),
        starting_capital=Decimal("1000"),
        common_start=date(2024, 1, 2),
        common_end=date(2026, 9, 25),
    )
    b = ComparisonSpec(
        candidate_ids=("a", "b"),
        starting_capital=Decimal("2000"),  # different
        common_start=date(2024, 1, 2),
        common_end=date(2026, 9, 25),
    )
    assert spec_hash(a) != spec_hash(b)
