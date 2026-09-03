"""Unit coverage for the mutation harness's own orchestration (successor
packet 2026-09-03): contiguous sharding, the --changed-since selection, the
per-shard baseline cache and its HARNESS_ERROR invalidation. The per-mutant
subprocess work is faked (monkeypatched ``_run`` / ``_prepare_worktree``) —
no nested campaigns run here; the full sharded campaign is proven by the
m0 gate itself.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "mutate_harness_under_test", REPO / "scripts" / "mutate.py"
)
mutate = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mutate)

S1 = "tests/unit/test_fake_one.py"
S2 = "tests/unit/test_fake_two.py"
TARGET = "src/fake_target.py"


def _mut(mid: str, selectors: list[str]) -> dict:
    return dict(
        id=mid,
        owner=f"owner_{mid}",
        file=TARGET,
        anchor="x = 1",
        replacement="x = 2",
        selectors=list(selectors),
        invariant="harness unit invariant",
    )


@pytest.fixture()
def fake_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A disposable-worktree stand-in whose target file carries every test
    anchor, with _prepare_worktree swapped for it (no copy, no uv sync)."""
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "fake_target.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(mutate, "_prepare_worktree", lambda parent: (tmp_path / "root", wt))
    return wt


def _fake_run_factory(calls: list[list[str]], *, timeout_selectors: set[str] | None = None):
    """Record every _run invocation; baselines are pytest invocations
    without --tb=no. Selectors in timeout_selectors make the BASELINE time
    out (the cheapest HARNESS_ERROR path — no sleeps, no tree damage)."""

    def fake_run(worktree, args, timeout):
        calls.append(list(args))
        if args[0] == "pytest" and "--tb=no" not in args:
            if timeout_selectors and any(s in args for s in timeout_selectors):
                raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    return fake_run


def _baseline_calls(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[0] == "pytest" and "--tb=no" not in c]


def test_split_contiguous_is_order_preserving_and_bounded() -> None:
    items = list(range(10))
    chunks = mutate._split_contiguous(items, 3)
    assert [i for chunk in chunks for i in chunk] == items
    assert len(chunks) == 3
    # more parts than items: at most one item per chunk, none empty
    chunks = mutate._split_contiguous([1, 2], 5)
    assert [i for chunk in chunks for i in chunk] == [1, 2]
    assert all(chunk for chunk in chunks)
    # degenerate shapes
    assert mutate._split_contiguous([], 3) == [[]]
    assert mutate._split_contiguous([1], 1) == [[1]]


def test_select_changed_since_matches_target_and_selector_files() -> None:
    mutants = [
        _mut("M-a", [S1]),
        _mut("M-b", [S2]),
        _mut("M-c", [S1, S2]),
    ]
    # target-file hit
    assert [m["id"] for m in mutate._select_changed_since(mutants, {TARGET})] == [
        "M-a",
        "M-b",
        "M-c",
    ]
    # selector-file hit only
    assert [m["id"] for m in mutate._select_changed_since(mutants, {S2})] == [
        "M-b",
        "M-c",
    ]
    # unrelated change selects nothing
    assert mutate._select_changed_since(mutants, {"README.md"}) == []


def test_changed_files_refuses_a_bad_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = subprocess.CompletedProcess(
        args=[], returncode=128, stdout="", stderr="fatal: bad revision 'nope'"
    )
    monkeypatch.setattr(mutate.subprocess, "run", lambda *a, **k: bad)
    with pytest.raises(SystemExit, match="bad ref"):
        mutate._changed_files("nope")


def test_the_baseline_cache_hits_within_a_shard(
    fake_worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two mutants sharing a selector set pay ONE baseline; a different
    selector set pays its own. (Restore is byte-verified per mutant, so a
    cached pass still describes the tree the next baseline would see.)"""
    calls: list[list[str]] = []
    monkeypatch.setattr(mutate, "_run", _fake_run_factory(calls))
    shard = [_mut("M-a1", [S1]), _mut("M-b1", [S2]), _mut("M-a2", [S1])]
    results, _worktree_root, _wt = mutate._run_shard(shard, fake_worktree.parent)
    baselines = _baseline_calls(calls)
    s1_baselines = [c for c in baselines if S1 in c]
    s2_baselines = [c for c in baselines if S2 in c]
    assert len(s1_baselines) == 1, calls
    assert len(s2_baselines) == 1, calls
    # every mutant still pays its own mutated run
    mutated = [c for c in calls if "--tb=no" in c]
    assert len(mutated) == 3, calls
    assert [r["id"] for r in results] == ["M-a1", "M-b1", "M-a2"]
    # pristine tree: all verdicts are SURVIVED under the always-pass fake
    assert all(r["verdict"] == "SURVIVED" for r in results), results


def test_a_harness_error_clears_the_baseline_cache(
    fake_worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard: any HARNESS_ERROR verdict clears the shard's baseline
    cache — a damaged tree can no longer be assumed pristine, so cached
    baseline passes stop being statements about this tree. A later mutant
    on the previously-cached selectors must re-run its baseline."""
    calls: list[list[str]] = []
    monkeypatch.setattr(mutate, "_run", _fake_run_factory(calls, timeout_selectors={S2}))
    shard = [_mut("M-a1", [S1]), _mut("M-bad", [S2]), _mut("M-a2", [S1])]
    results, _worktree_root, _wt = mutate._run_shard(shard, fake_worktree.parent)
    assert results[1]["verdict"] == "HARNESS_ERROR", results
    assert results[1]["detail"] == "baseline timeout"
    baselines = [c for c in _baseline_calls(calls) if S1 in c]
    assert len(baselines) == 2, calls  # cached, then re-run after the clear
