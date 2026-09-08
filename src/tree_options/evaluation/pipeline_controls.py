"""M5 pipeline-level negative controls: leakage corruption generators.

Pure, stdlib-only, deterministic — the idiom of ``evaluation.controls``
(``block_shuffle``, ``random_scores``): generators that produce CORRUPTED
inputs a downstream harness must refuse or expose.  Each generator's
docstring declares the expected verdict when the corruption is fed
through the real pipeline; the exact contracts here pin the corruption
itself, so a harness proven against these inputs is proven against the
canonical shape of the leak.

The three canonical pipeline leaks this module materializes:

1. FUTURE-FEATURE injection — a feature whose value at session t is the
   series' value at t+shift (the classic lookahead).  The corrupted
   series is ``values[shift:]``: the last ``shift`` values have no
   legitimate successor and are honestly DROPPED, never fabricated.  Any
   scorer that does not flag a future-shifted feature is broken.
2. TIMESTAMP INVERSION — the session axis reversed while the values stay
   in their original order, misaligning every (timestamp, value) pair.
   Every join/merge against real chronology now pairs the wrong rows.
3. SAME-CLOSE fills — execution sessions collapsed onto their decision
   sessions.  The fill door's rule 6 (SAME_SESSION_EXECUTION,
   ``guards.fills`` INV-10) must refuse every pair.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date


def _finite_series(values: Sequence[float], *, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in result):
        raise ValueError(f"{name} values must be finite")
    return result


def future_shifted_series(values: Sequence[float], *, shift: int = 1) -> tuple[float, ...]:
    """Feature corrupted by lookahead: value at t becomes value at t+shift.

    Returns exactly ``values[shift:]`` — the first ``shift`` values (the
    ones whose future is not in the sample) drop out and the tail shrinks
    by ``shift``; nothing is fabricated to preserve length.  Expected
    verdict downstream: the scorer/evaluator must treat this series as
    leaked evidence, never as a legitimate feature.
    """
    series = _finite_series(values, name="feature")
    if not isinstance(shift, int) or isinstance(shift, bool):
        raise ValueError("shift must be an int")
    if shift < 1 or shift >= len(series):
        raise ValueError("shift must be >= 1 and < len(values)")
    return series[shift:]


def invert_chronology(
    sessions: Sequence[date], values: Sequence[float]
) -> tuple[tuple[date, ...], tuple[float, ...]]:
    """Timestamp axis reversed; values stay in their original order.

    Every (timestamp, value) pair is misaligned — a join against real
    chronology now pairs value[t] with the timestamp of its mirror
    session.  Expected verdict downstream: any availability join or
    as-of merge built on the corrupted axis must mismatch, never silently
    produce a panel.
    """
    if not sessions:
        raise ValueError("sessions must be non-empty")
    if len(sessions) != len(values):
        raise ValueError("sessions and values must have the same length")
    _finite_series(values, name="value")
    return tuple(reversed(tuple(sessions))), tuple(values)


def same_close_fills(decision_sessions: Sequence[date]) -> tuple[date, ...]:
    """Execution sessions collapsed onto their decision sessions.

    Pair i is (decision_sessions[i], decision_sessions[i]) — the
    same-session execution the fill door refuses (INV-10 door 6,
    SAME_SESSION_EXECUTION: execution must be strictly after decision).
    Expected verdict downstream: EVERY pair is refused; a fill engine
    that fills even one of them has a lookahead door.
    """
    if not decision_sessions:
        raise ValueError("decision_sessions must be non-empty")
    return tuple(decision_sessions)


def perfect_foresight_feature(labels: Sequence[float]) -> tuple[float, ...]:
    """Feature corrupted to BE the outcome it should not know.

    The corrupted feature equals the label vector exactly.  Expected
    verdict downstream: rank IC against the labels is EXACTLY 1.0 — any
    evaluation harness that lets a perfect-foresight feature through its
    gates without flagging it is not measuring discovery at all.

    Constant label vectors are REFUSED: with no rank information the IC
    is undefined (``None``), so the canary cannot fire and the corrupted
    input would silently prove nothing.
    """
    series = _finite_series(labels, name="label")
    if len(series) < 2:
        raise ValueError("labels must have at least two values")
    if len(set(series)) < 2:
        raise ValueError(
            "labels must have at least two DISTINCT values "
            "(a constant vector has no rank information)"
        )
    return series
