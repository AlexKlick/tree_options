"""M5 pipeline negative controls: exact corruption contracts + algebraic canaries."""

from __future__ import annotations

import datetime as dt

import pytest

from tree_options.evaluation.pipeline_controls import (
    future_shifted_series,
    invert_chronology,
    perfect_foresight_feature,
    same_close_fills,
)
from tree_options.evaluation.stats import spearman_rank_ic

D1, D2, D3, D4 = (
    dt.date(2026, 1, 5),
    dt.date(2026, 1, 6),
    dt.date(2026, 1, 7),
    dt.date(2026, 1, 8),
)


def test_future_shifted_series_is_the_lookahead_tail() -> None:
    values = (0.5, -0.25, 0.125, -0.0625, 0.75)
    # lookahead 1: value at t becomes value at t+1 — the LAST value has no
    # successor and is dropped, the first drops out of use; nothing fabricated
    assert future_shifted_series(values, shift=1) == (-0.25, 0.125, -0.0625, 0.75)
    assert future_shifted_series(values, shift=2) == (0.125, -0.0625, 0.75)
    # the corrupted series is NOT the safe lagged form values[:-shift]
    assert future_shifted_series(values, shift=1) != values[:-1]
    with pytest.raises(ValueError, match="shift"):
        future_shifted_series(values, shift=0)
    with pytest.raises(ValueError, match="shift"):
        future_shifted_series(values, shift=5)  # == len: empty series refused
    with pytest.raises(ValueError, match="shift"):
        future_shifted_series(values, shift=1.5)  # non-int shift refused
    with pytest.raises(ValueError, match="shift"):
        future_shifted_series((1.0,), shift=1)
    with pytest.raises(ValueError, match="finite"):
        future_shifted_series((1.0, float("inf")), shift=1)


def test_shift_rejects_non_int() -> None:
    with pytest.raises(ValueError, match="shift"):
        future_shifted_series((1.0, 2.0), shift=True)  # bools are not shifts


def test_invert_chronology_reverses_sessions_not_values() -> None:
    sessions = (D1, D2, D3, D4)
    values = (0.5, -0.25, 0.125, 0.75)
    corrupted_sessions, corrupted_values = invert_chronology(sessions, values)
    assert corrupted_sessions == (D4, D3, D2, D1)  # the axis reversed
    assert corrupted_values == values  # the data STAYED — pairs misaligned
    with pytest.raises(ValueError, match="same length"):
        invert_chronology((D1, D2), (1.0,))
    with pytest.raises(ValueError, match="non-empty"):
        invert_chronology((), ())
    with pytest.raises(ValueError, match="finite"):
        invert_chronology((D1, D2), (1.0, float("nan")))


def test_two_point_ic_sign_flip_under_label_reversal() -> None:
    """The corruption's effect on discovery, pinned exactly at n=2 (no ties).

    For two points Spearman is +-1, so reversing the label order flips the
    sign EXACTLY — the canonical demonstration that a reversed chronology
    inverts the measured relationship. (The general-n identity does NOT
    hold and is deliberately not claimed.)
    """
    scores = (0.5, -0.25)
    labels = (0.75, -0.125)
    ic = spearman_rank_ic(scores, labels)
    ic_reversed = spearman_rank_ic(scores, tuple(reversed(labels)))
    assert ic == 1.0
    assert ic_reversed == -1.0


def test_same_close_fills_collapses_execution_onto_decision() -> None:
    decisions = (D1, D2, D3)
    fills = same_close_fills(decisions)
    assert fills == (D1, D2, D3)  # every execution == its decision session
    assert all(decision == fill for decision, fill in zip(decisions, fills, strict=True))
    with pytest.raises(ValueError, match="non-empty"):
        same_close_fills(())


def test_perfect_foresight_feature_scores_exactly_one() -> None:
    """The canary: IC(feature, labels) == 1.0 exactly, ties included."""
    labels = (0.5, -0.25, 0.125, 0.5, -0.25)  # ties on purpose
    feature = perfect_foresight_feature(labels)
    assert feature == labels
    assert spearman_rank_ic(feature, labels) == 1.0
    with pytest.raises(ValueError, match="two"):
        perfect_foresight_feature((1.0,))
    # constant labels are REFUSED: their IC is None (no rank information),
    # so the canary cannot fire — accepting them would let a harness pass
    # a corruption that proves nothing
    with pytest.raises(ValueError, match="DISTINCT"):
        perfect_foresight_feature((0.5, 0.5, 0.5))
    with pytest.raises(ValueError, match="finite"):
        perfect_foresight_feature((1.0, float("-inf")))
