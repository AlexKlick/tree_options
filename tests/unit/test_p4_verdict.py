"""(P4, owner rulings 2026-09-03) the window-A verdict machinery's owners.

Two surfaces under test:

- the DATE-SCOPE rule — which sealed sessions are label-complete on a
  world whose grid ends at a given last session (the boundary is
  arithmetic on grid steps, and the excluded dates are a REFUSAL to
  pad, not a silent drop);
- the RETURN-CHANNEL DUAL FALSIFIER — F1 (>=2 of 3 nulls negative) and
  F2 (both mom arms strictly above the null max), evaluated from
  stamped payload bodies only, with named refusals for anything that
  is not exactly the six-artifact evaluation set.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tree_options.protocol.holdout import FINAL_HOLDOUT_DATES
from tree_options.trials.p4_verdict import (
    P4VerdictError,
    evaluate_window_a,
    label_complete_permitted_sessions,
)

_H = 5  # the ratified D4 label horizon in grid sessions


def _grid(first: date, last: date) -> tuple[date, ...]:
    """Every Friday on [first, last] — the lane-2 decision grid's shape."""
    out = []
    cursor = first
    while cursor.weekday() != 4:  # roll to the first Friday
        cursor += timedelta(days=1)
    while cursor <= last:
        out.append(cursor)
        cursor += timedelta(weeks=1)
    return tuple(out)


# the real world's shape: grid 2024-08-09..2026-08-14 (105 Fridays), the
# 13 sealed dates among them, world end = grid end
_REAL_GRID = _grid(date(2024, 8, 9), date(2026, 8, 14))
_REAL_SEALED = [s for s in _REAL_GRID if s.isoformat() in set(FINAL_HOLDOUT_DATES)]


def test_real_world_shape_permits_exactly_eight_dates() -> None:
    """H=5 steps of headroom: 2026-05-08..2026-07-10 are label-complete
    (the 07-10 decision's entry fills 07-17 and its exit-4 exit fills
    08-14, the world's last session); 07-17..08-14 (5 dates) are excluded
    — disclosed, never padded."""
    permitted = label_complete_permitted_sessions(_REAL_GRID, date(2026, 8, 14), _H)
    assert permitted == tuple(_REAL_SEALED[:8])
    assert permitted[-1] == date(2026, 7, 10)
    excluded = set(_REAL_SEALED) - set(permitted)
    assert excluded == {
        date(2026, 7, 17),
        date(2026, 7, 24),
        date(2026, 7, 31),
        date(2026, 8, 7),
        date(2026, 8, 14),
    }


def test_a_longer_world_admits_the_earlier_boundary_dates() -> None:
    """The rule measures headroom, not the enumeration: a world whose
    grid runs past the seal admits every sealed date with 5 steps of
    room (a HYPOTHETICAL future-era shape — this world's grid ends
    2026-08-14; the test pins that the rule is world-arithmetic, not a
    hardcoded date list)."""
    longer = _grid(date(2024, 8, 9), date(2026, 9, 25))
    permitted = label_complete_permitted_sessions(longer, date(2026, 9, 25), _H)
    assert date(2026, 7, 17) in permitted
    assert date(2026, 8, 14) in permitted
    assert len(permitted) == 13


def test_a_world_through_2026_09_18_admits_all_thirteen_at_the_exact_boundary() -> None:
    """(window-A extension, owner ruling 2026-09-04: the two-cycle capture)
    THE seal-math pin for the continuation: with the grid carried through
    Friday 2026-09-18, the sealed 08-14 decision has EXACTLY five grid
    steps of headroom — 08-21, 08-28, 09-04, 09-11, 09-18 — so every sealed
    date is label-complete and the extension scope (all five excluded
    dates) completes. 08-21 counts as a grid step although the vendor
    carried no close that day: the grid is calendar-Fridays-within-span
    (the 08-21 MASTER exists), not the set of closes — pinned here by
    membership, pinned at the grid constructor in the extension tests."""
    grown = _grid(date(2024, 8, 9), date(2026, 9, 18))
    assert date(2026, 8, 21) in grown  # the vendor-gap Friday is a grid step
    steps_after_0814 = [s for s in grown if s > date(2026, 8, 14)]
    assert steps_after_0814 == [
        date(2026, 8, 21),
        date(2026, 8, 28),
        date(2026, 9, 4),
        date(2026, 9, 11),
        date(2026, 9, 18),
    ]
    permitted = label_complete_permitted_sessions(grown, date(2026, 9, 18), _H)
    assert len(permitted) == 13
    assert permitted[-1] == date(2026, 8, 14)  # exactly-5 counts: >= H holds


def test_a_world_one_friday_short_leaves_08_14_immature() -> None:
    """The other side of the boundary: through 09-11 only, 08-14 has four
    steps of headroom — the all-or-nothing extension scope cannot complete
    on that world (the capture MUST reach 09-18, or 09-25 if the vendor
    gaps another scoped Friday)."""
    short = _grid(date(2024, 8, 9), date(2026, 9, 11))
    permitted = label_complete_permitted_sessions(short, date(2026, 9, 11), _H)
    assert permitted[-1] == date(2026, 8, 7)
    assert date(2026, 8, 14) not in permitted


def test_world_last_between_grid_sessions_resolves_to_the_prior_grid_session() -> None:
    """The world's last BAR session need not be a grid session: the rule
    measures from the last grid session at or before it (a Wednesday
    world end resolves to the preceding Friday)."""
    permitted = label_complete_permitted_sessions(_REAL_GRID, date(2026, 8, 12), _H)
    # last grid session 2026-08-07: the 07-10 decision has only 4 steps of
    # headroom, so the permitted set closes at 06-26
    assert permitted == tuple(_REAL_SEALED[:7])
    assert permitted[-1] == date(2026, 6, 26)


def test_no_label_complete_date_refuses() -> None:
    """A world that ends inside the seal with no complete date refuses —
    evaluating a padded set is never an option."""
    short_grid = _grid(date(2024, 8, 9), date(2026, 5, 29))
    with pytest.raises(P4VerdictError, match="nothing to consume"):
        label_complete_permitted_sessions(short_grid, date(2026, 5, 29), _H)


def test_bad_horizon_and_empty_grid_refuse() -> None:
    with pytest.raises(P4VerdictError, match="label_horizon_sessions"):
        label_complete_permitted_sessions(_REAL_GRID, date(2026, 8, 14), 0)
    with pytest.raises(P4VerdictError, match="grid is empty"):
        label_complete_permitted_sessions((), date(2026, 8, 14), _H)


def _body(total_return: float, ic_mean: float | None = 0.1) -> dict:
    payload: dict = {"backtest": {"total_return": total_return}}
    pooled = {} if ic_mean is None else {"cohort_ic_mean": ic_mean}
    payload["pooled"] = pooled
    return {"payload": payload}


def _six(nulls: tuple[float, float, float], mom: tuple[float, float], hold: float):
    return {
        "p4-null-1": _body(nulls[0]),
        "p4-null-2": _body(nulls[1]),
        "p4-null-3": _body(nulls[2]),
        "p4-mom-a": _body(mom[0]),
        "p4-mom-b": _body(mom[1]),
        "p4-hold-exit2": _body(hold),
    }


_PRIOR = 0.5118361912977869  # the recorded wave-0 calibration prior


def test_f1_passes_at_two_of_three_negative() -> None:
    verdict = evaluate_window_a(_six((-0.1, 0.05, -0.2), (0.3, 0.4), -0.05), _PRIOR)
    assert verdict["f1_bleed_persisted"] is True
    assert verdict["f1_null_negative_count"] == 2


def test_f1_fails_at_one_of_three_negative() -> None:
    verdict = evaluate_window_a(_six((0.05, 0.1, -0.2), (0.3, 0.4), -0.05), _PRIOR)
    assert verdict["f1_bleed_persisted"] is False
    assert verdict["f1_null_negative_count"] == 1


def test_f2_requires_both_arms_strictly_above_the_null_max() -> None:
    both = evaluate_window_a(_six((-0.1, -0.2, -0.05), (0.01, 0.02), -0.04), _PRIOR)
    assert both["f2_anomaly_persisted"] is True
    one = evaluate_window_a(_six((-0.1, -0.2, -0.05), (0.01, -0.06), -0.04), _PRIOR)
    assert one["f2_anomaly_persisted"] is False
    # a TIE against the null max is a fail — the bar is strictly above
    tie = evaluate_window_a(_six((-0.1, -0.2, -0.05), (0.0, -0.05), -0.04), _PRIOR)
    assert tie["f2_anomaly_persisted"] is False


def test_secondary_disclosures_carry_the_ic_bar_and_ladder_reading() -> None:
    verdict = evaluate_window_a(_six((-0.1, -0.2, -0.05), (0.01, 0.02), 0.03), _PRIOR)
    secondary = verdict["secondary_disclosures"]
    assert secondary["cohort_ic_two_se_bar"] == pytest.approx(2 * _PRIOR / 3**0.5)
    assert secondary["hold_exit2_return"] == 0.03
    assert secondary["hold_exit2_above_null_max"] is True
    assert set(secondary["cohort_ic_mean"]) == {
        "p4-null-1",
        "p4-null-2",
        "p4-null-3",
        "p4-mom-a",
        "p4-mom-b",
        "p4-hold-exit2",
    }


@pytest.mark.parametrize(
    ("label", "bodies", "match"),
    [
        (
            "missing slot",
            {k: v for k, v in _six((-0.1, -0.2, -0.3), (0.1, 0.2), 0.0).items() if k != "p4-mom-b"},
            "missing artifacts",
        ),
        (
            "extra slot",
            {**_six((-0.1, -0.2, -0.3), (0.1, 0.2), 0.0), "p4-band": _body(0.1)},
            "unexpected artifacts",
        ),
        (
            "non-numeric return",
            {**_six((-0.1, -0.2, -0.3), (0.1, 0.2), 0.0), "p4-hold-exit2": _body("nan")},
            "total_return is not numeric",
        ),
    ],
)
def test_malformed_evaluation_sets_refuse_by_name(label, bodies, match) -> None:
    with pytest.raises(P4VerdictError, match=match):
        evaluate_window_a(bodies, _PRIOR)


def test_malformed_prior_refuses() -> None:
    with pytest.raises(P4VerdictError, match="prior_stride4_cohort_ic_sd"):
        evaluate_window_a(_six((-0.1, -0.2, -0.3), (0.1, 0.2), 0.0), 0.0)
