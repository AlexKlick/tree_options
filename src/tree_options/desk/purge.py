"""Purged walk-forward with an embargo for the long run's finalist
selection (Lopez de Prado 2018, *Advances in Financial Machine Learning*,
ch. 7: purging and embargoing).

The leak it closes: a train (tune) decision entered on or before the cutoff
session with a multi-day horizon (hold:N, expiry) exits AFTER the cutoff,
so ranking finalists on its P&L uses post-cutoff prices.

- PURGE: in finalist selection a train decision counts only when its exit
  instant (the outcome engine's exit for that horizon, ``exit_at``) falls
  on or before the close of the cutoff session (ET). A purged decision
  scores 0 in selection and is COUNTED per arm, never dropped silently. An
  outcome plug-in that reports no exit gets a horizon estimate over the
  window's sessions (intraday/eod: the entry session; hold:N: N sessions
  later; any other horizon: purged), counted as estimated.
- The random null gets the IDENTICAL purge: a (row, horizon) option of a
  train board whose exit crosses the cutoff scores 0, so tune diffs stay
  paired. Rule arms are arms: they get the same purge.
- EMBARGO: the confirmatory test split counts only decisions entered at
  least ``embargo_sessions`` sessions after the cutoff session (default 1:
  the first session after it, i.e. the pre-purge test split).

Only the walk-forward is affected; the whole-window standings are not.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

ET = ZoneInfo("America/New_York")
RULE = (
    "purged walk-forward (Lopez de Prado 2018, AFML ch. 7): a train decision counts in "
    "finalist selection only if its exit is on/before the close of the cutoff session "
    "(ET); purged decisions score 0 in selection and are counted per arm; the random "
    "null's (row, horizon) options get the identical purge; the test split counts only "
    "decisions entered >= embargo_sessions sessions after the cutoff session"
)

#: (snapshot, row id, horizon) -> the outcome's exit instant (ISO) or None
ExitFn = Callable[[str, str, str | None], str | None]
#: (snapshot, row id, horizon) -> (gross, net) or None (no fill)
ValueFn = Callable[[str, str, str | None], tuple[float, float] | None]


def exit_date(exit_at: Any) -> date | None:
    """The ET session date of an aware ISO exit instant (None when absent)."""
    if not exit_at:
        return None
    try:
        instant = datetime.fromisoformat(str(exit_at))
    except ValueError:
        return None
    return None if instant.tzinfo is None else instant.astimezone(ET).date()


def crosses(
    exit_at: Any, horizon: str | None, session: str, cutoff: str, sessions: Sequence[str]
) -> tuple[bool, bool]:
    """(the exit falls after the cutoff session, the exit was estimated)."""
    when = exit_date(exit_at)
    if when is not None:
        return when > date.fromisoformat(cutoff), False
    if horizon is None or horizon in ("intraday", "eod"):
        offset: int | None = 0
    elif horizon.startswith("hold:"):
        offset = int(horizon.split(":", 1)[1])
    else:
        offset = None  # expiry or unknown: cannot place the exit, so purge
    if offset is None:
        return True, True
    at = list(sessions).index(session) + offset
    return (at >= len(sessions) or sessions[at] > cutoff), True


def purge_decisions(
    boards: Sequence[Any],
    decisions: Sequence[tuple[str | None, str | None]],
    nets: Sequence[float],
    value: ValueFn,
    exit_of: ExitFn,
    cutoff: str,
    sessions: Sequence[str],
) -> tuple[list[float], dict[str, int]]:
    """Selection values (a purged train decision -> 0) and the per-arm counts."""
    values = list(nets)
    counts = {"train_entered": 0, "purged": 0, "estimated_exits": 0}
    for i, (board, (choice, horizon)) in enumerate(zip(boards, decisions, strict=True)):
        if choice is None or board.session > cutoff:
            continue
        counts["train_entered"] += 1
        if value(board.snapshot, choice, horizon) is None:
            continue  # no fill: scores 0, nothing priced after the cutoff
        cross, estimated = crosses(
            exit_of(board.snapshot, choice, horizon), horizon, board.session, cutoff, sessions
        )
        counts["estimated_exits"] += estimated
        if cross:
            values[i] = 0.0
            counts["purged"] += 1
    return values, counts


def purged_null(
    boards: Sequence[Any],
    sessions: Sequence[str],
    horizons: Sequence[str | None],
    value: ValueFn,
    exit_of: ExitFn,
    cutoff: str,
    p_enter: float,
    window: Sequence[str],
) -> tuple[np.ndarray, dict[str, int]]:
    """The random null's per-session expectation for selection: a train
    board's (row, horizon) option whose exit crosses the cutoff scores 0."""
    index = {s: i for i, s in enumerate(sessions)}
    out = np.zeros(len(sessions))
    counts = {"train_options": 0, "purged_options": 0}
    for board in boards:
        vals: list[float] = []
        for rid in board.ids:
            for horizon in horizons:
                got = value(board.snapshot, rid, horizon)
                net = 0.0 if got is None else got[1]
                if got is not None and board.session <= cutoff:
                    counts["train_options"] += 1
                    if crosses(
                        exit_of(board.snapshot, rid, horizon),
                        horizon,
                        board.session,
                        cutoff,
                        window,
                    )[0]:
                        net = 0.0
                        counts["purged_options"] += 1
                vals.append(net)
        if vals:
            out[index[board.session]] += p_enter * float(np.mean(vals))
    return out, counts


def own_coverage(
    boards: Sequence[Any],
    receipts: Mapping[str, Mapping[str, Any]],
    value: ValueFn,
    exit_of: ExitFn,
    cutoff: str,
    window: Sequence[str],
) -> dict[str, int]:
    """Purge counts over an arm's own ok receipts (meaningful on partial runs)."""
    mine = [b for b in boards if receipts.get(b.snapshot, {}).get("ok")]
    decisions = [
        (receipts[b.snapshot].get("choice"), receipts[b.snapshot].get("horizon")) for b in mine
    ]
    return purge_decisions(mine, decisions, [0.0] * len(mine), value, exit_of, cutoff, window)[1]
