"""(P4, owner rulings 2026-09-03) the window-A sealed-evaluation verdict.

Two things live here, both pure functions of committed inputs:

- ``label_complete_permitted_sessions`` — the owner-ratified DATE-SCOPE
  rule: a sealed grid session is label-complete iff at least
  ``label_horizon_sessions`` grid steps remain between it and the world's
  last in-world grid session. The bound is the deepest consumption the
  pre-registered schedule can ask of a decision date — the entry executes
  the NEXT grid session and the exit-4 ladder fills four sessions after
  that, i.e. exactly H=5 forward grid sessions — and the fold's
  label-horizon purge wants the same forward window. A date that close to
  the world's end can never complete on THIS world; it is disclosed as
  excluded, never silently dropped.

- ``evaluate_window_a`` — the RETURN-CHANNEL DUAL FALSIFIER the owner
  ratified, evaluated from the six stamped payload bodies only (the
  same evidence discipline as the sealed gate: no in-memory truth, no
  recomputation of returns):

  * F1 (bleed persistence): at least 2 of the 3 window-A null seeds
    post negative pooled total_return.
  * F2 (anomaly persistence): BOTH momentum arms exceed the maximum of
    the three window-A null seeds — the same both-arms-above-the-spread
    bar the composition anomaly cleared in-sample (P3, 2026-09-02).

  Secondary DISCLOSURES (never verdicts): each slot's cohort_ic_mean
  against the 2-SE bar ``2*prior/sqrt(3)`` from the wave-0 calibration,
  and the exit-2 ladder point against the null spread (the lose-least
  replication reading).

Nonclaims: machinery discipline only; no investment claim. The verdict
is a falsifier record — F2 "persisted" is a statement about THIS
window's spread, not an edge claim.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from tree_options.protocol.holdout import FINAL_HOLDOUT_DATES

P4_NULL_SLOTS = ("p4-null-1", "p4-null-2", "p4-null-3")
P4_MOM_SLOTS = ("p4-mom-a", "p4-mom-b")
P4_HOLD_SLOTS = ("p4-hold-exit2",)
P4_SLOT_ORDER = P4_NULL_SLOTS + P4_MOM_SLOTS + P4_HOLD_SLOTS

_VERDICT_RULE = (
    "owner ruling 2026-09-03 (return-channel dual falsifier): F1 = at"
    " least 2 of 3 window-A null seeds negative; F2 = BOTH momentum arms"
    " strictly above the max of the 3 window-A null seeds"
)


@dataclass(frozen=True)
class P4VerdictError(ValueError):
    """A malformed evaluation input — the verdict refuses, never guesses."""

    detail: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"P4 verdict refused: {self.detail}"


def label_complete_permitted_sessions(
    grid_sessions: Sequence[date],
    world_last_session: date,
    label_horizon_sessions: int,
) -> tuple[date, ...]:
    """The owner-ratified date-scope rule (ruling 3, 2026-09-03).

    ``grid_sessions`` is the FULL decision grid (research sessions and
    sealed dates alike); ``world_last_session`` is the world's last bar
    session, which need not itself be a grid session — the rule measures
    from the last grid session AT OR BEFORE it. Returns the sealed grid
    sessions with at least ``label_horizon_sessions`` grid steps of
    forward headroom, strictly increasing."""
    if label_horizon_sessions < 1:
        raise P4VerdictError(f"label_horizon_sessions must be >= 1, got {label_horizon_sessions}")
    sessions = tuple(sorted(set(grid_sessions)))
    if not sessions:
        raise P4VerdictError("the grid is empty")
    in_world = [s for s in sessions if s <= world_last_session]
    if not in_world:
        raise P4VerdictError(
            f"no grid session is at or before the world's last session {world_last_session}"
        )
    last_index = sessions.index(in_world[-1])
    sealed = frozenset(FINAL_HOLDOUT_DATES)
    permitted = tuple(
        session
        for index, session in enumerate(sessions[: last_index + 1])
        if session.isoformat() in sealed and (last_index - index) >= label_horizon_sessions
    )
    if not permitted:
        raise P4VerdictError(
            "no sealed session is label-complete on this world — the window-A"
            " evaluation has nothing to consume; refusing rather than"
            " evaluating a padded set"
        )
    return permitted


def _total_return(slot: str, bodies: Mapping[str, Mapping[str, Any]]) -> float:
    if slot not in bodies:
        raise P4VerdictError(f"the {slot} artifact is missing from the evaluation set")
    payload = bodies[slot].get("payload")
    if not isinstance(payload, Mapping):
        raise P4VerdictError(f"the {slot} artifact carries no payload")
    value = payload.get("backtest", {}).get("total_return")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise P4VerdictError(f"the {slot} artifact's total_return is not numeric: {value!r}")
    return float(value)


def _cohort_ic_mean(slot: str, bodies: Mapping[str, Mapping[str, Any]]) -> float | None:
    pooled = bodies[slot].get("payload", {}).get("pooled", {})
    value = pooled.get("cohort_ic_mean")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise P4VerdictError(f"the {slot} artifact's cohort_ic_mean is not numeric: {value!r}")
    return float(value)


def evaluate_window_a(
    bodies: Mapping[str, Mapping[str, Any]],
    prior_stride4_cohort_ic_sd: float,
    *,
    rule: str = _VERDICT_RULE,
) -> dict[str, Any]:
    """The return-channel dual falsifier over the six stamped artifacts.

    ``bodies`` maps the six P4 slot ids to their stamped artifact BODIES
    (the parsed JSON, payload included) — exactly what the artifacts on
    disk are. ``prior_stride4_cohort_ic_sd`` is the wave-0 calibration
    prior (0.5118…, recorded in the wave-0 state); it feeds ONLY the
    secondary IC disclosure, never F1/F2.

    ``rule`` is the verdict's self-describing rule TEXT (the default is
    the window-A owner ruling; the extension passes its own rule so the
    extension evidence never carries the base window's ruling text)."""
    expected = set(P4_SLOT_ORDER)
    extra = sorted(set(bodies) - expected)
    if extra:
        raise P4VerdictError(f"unexpected artifacts in the evaluation set: {extra}")
    missing = sorted(expected - set(bodies))
    if missing:
        raise P4VerdictError(f"missing artifacts from the evaluation set: {missing}")
    if prior_stride4_cohort_ic_sd <= 0 or not math.isfinite(prior_stride4_cohort_ic_sd):
        raise P4VerdictError(
            f"prior_stride4_cohort_ic_sd must be finite and positive, got {prior_stride4_cohort_ic_sd}"
        )
    null_returns = {slot: _total_return(slot, bodies) for slot in P4_NULL_SLOTS}
    mom_returns = {slot: _total_return(slot, bodies) for slot in P4_MOM_SLOTS}
    hold_return = _total_return(P4_HOLD_SLOTS[0], bodies)
    negatives = sum(1 for value in null_returns.values() if value < 0.0)
    null_max = max(null_returns.values())
    f1_bleed_persisted = negatives >= 2
    f2_anomaly_persisted = min(mom_returns.values()) > null_max
    ic_bar = 2.0 * prior_stride4_cohort_ic_sd / math.sqrt(3.0)
    return {
        "rule": rule,
        "f1_bleed_persisted": f1_bleed_persisted,
        "f1_null_negative_count": negatives,
        "f1_null_returns": null_returns,
        "f2_anomaly_persisted": f2_anomaly_persisted,
        "f2_mom_returns": mom_returns,
        "f2_null_max": null_max,
        "secondary_disclosures": {
            "prior_stride4_cohort_ic_sd": prior_stride4_cohort_ic_sd,
            "cohort_ic_two_se_bar": ic_bar,
            "cohort_ic_mean": {slot: _cohort_ic_mean(slot, bodies) for slot in P4_SLOT_ORDER},
            "hold_exit2_return": hold_return,
            "hold_exit2_above_null_max": hold_return > null_max,
        },
    }


__all__ = [
    "P4_HOLD_SLOTS",
    "P4_MOM_SLOTS",
    "P4_NULL_SLOTS",
    "P4_SLOT_ORDER",
    "P4VerdictError",
    "evaluate_window_a",
    "label_complete_permitted_sessions",
]
