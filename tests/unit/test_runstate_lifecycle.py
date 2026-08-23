"""Legal/illegal transition matrix over the full 16x16 grid."""

from __future__ import annotations

import pytest

from tree_options.runstate import LEGAL_EDGES, RunState, is_legal


def test_every_legal_edge_is_whitelisted_and_forward_or_retry():
    for source, targets in LEGAL_EDGES.items():
        for target in targets:
            assert is_legal(source, target), (source, target)


def test_unknown_is_never_a_target():
    for source in RunState:
        assert not is_legal(source, RunState.UNKNOWN)


def test_unknown_is_never_a_source():
    for target in RunState:
        assert not is_legal(RunState.UNKNOWN, target)


def test_full_matrix_refuses_everything_else():
    legal_pairs = {
        (source, target) for source, targets in LEGAL_EDGES.items() for target in targets
    }
    for source in RunState:
        for target in RunState:
            if (source, target) in legal_pairs:
                assert is_legal(source, target)
            else:
                assert not is_legal(source, target), (source, target)


def test_planned_to_capturing_legal():
    assert is_legal(RunState.PLANNED, RunState.CAPTURING)


def test_regression_refused():
    assert not is_legal(RunState.CAPTURING, RunState.PLANNED)
    assert not is_legal(RunState.INSPECTED, RunState.CAPTURE_COMPLETE)


def test_skip_refused():
    assert not is_legal(RunState.PLANNED, RunState.INSPECTED)
    assert not is_legal(RunState.CAPTURING, RunState.BARS_CAPTURING)


def test_failed_retry_edges():
    assert is_legal(RunState.FAILED, RunState.CAPTURING)
    assert is_legal(RunState.FAILED, RunState.INSPECTION_RUNNING)
    assert is_legal(RunState.FAILED, RunState.BARS_CAPTURING)


def test_failed_cannot_reach_sealed_lane():
    # The sealed event is one-shot: a crash after authority consumption is
    # UNKNOWN/RECONCILIATION_REQUIRED, never a retry from FAILED.
    for target in (
        RunState.SEALED_PREFLIGHT_READY,
        RunState.SEALED_RUNNING,
        RunState.SEALED_COMPLETE,
    ):
        assert not is_legal(RunState.FAILED, target)


def test_sealed_complete_is_terminal():
    assert not any(is_legal(RunState.SEALED_COMPLETE, target) for target in RunState)


def test_inspection_failed_may_rerun_inspection_only():
    assert is_legal(RunState.INSPECTION_FAILED, RunState.INSPECTION_RUNNING)
    assert not is_legal(RunState.INSPECTION_FAILED, RunState.CAPTURING)
    assert not is_legal(RunState.INSPECTION_FAILED, RunState.INSPECTED)


@pytest.mark.parametrize(
    ("source", "target"),
    sorted((s, t) for s in RunState for t in RunState),
)
def test_is_legal_never_crashes(source: RunState, target: RunState):
    assert isinstance(is_legal(source, target), bool)
