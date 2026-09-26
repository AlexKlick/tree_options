from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal

from tree_options.research.contracts import ResearchRegistration

"""Drawdown + peak-to-trough + recovery duration.

Handoff §3 (Default charts): drawdown and recovery on the same
calendar, loss relative to prior peak, with unresolved recovery
durations visibly open-ended.

RL1-06 correction (2026-09-25 audit): an OBSERVED recovery is data,
not a scientific claim — a fully supplied series 100 -> 80 -> 100 has a
recovery date no matter what the registration label says. Describing
it neither edits a registration nor asserts predictive validation; the
registration stays visible on the summary as a qualification. Recovery
is ``None`` only when the series is genuinely still underwater at the
last observation (rendered open-ended).
"""


date = _dt.date  # type alias used inside function bodies; field annotations use _dt.date


@dataclass(frozen=True)
class DrawdownCell:
    date: _dt.date
    drawdown_dollar: Decimal      # signed negative; >= ending_value - peak
    drawdown_pct: Decimal          # signed negative; pct off peak
    recovery_end_date: _dt.date | None  # None = still underwater at last observation


@dataclass(frozen=True)
class DrawdownSeries:
    candidate_id: str
    registration: ResearchRegistration
    cells: tuple[DrawdownCell, ...]
    peak_value: Decimal
    peak_date: _dt.date


def compute_drawdown(
    candidate_id: str,
    registration: ResearchRegistration,
    ending_value_by_session: dict[date, Decimal],
) -> DrawdownSeries:
    """Standard peak-to-trough drawdown over the supplied series.

    Sort the observed dates; track the running peak. Each cell below
    peak gets a negative drawdown in both dollars and pct, and its
    recovery is the first later date the series returns to that peak
    (>=) — or None when it never does within the observation.
    """
    if not ending_value_by_session:
        return DrawdownSeries(candidate_id, registration, (), Decimal("0"),
                             date(1970, 1, 1))

    sorted_dates = sorted(ending_value_by_session)
    peak_value = ending_value_by_session[sorted_dates[0]]
    peak_date = sorted_dates[0]
    peak_after_observation: dict[date, tuple[Decimal, date]] = {}

    for d in sorted_dates:
        v = ending_value_by_session[d]
        if v >= peak_value:
            peak_value = v
            peak_date = d
        peak_after_observation[d] = (peak_value, peak_date)

    cells: list[DrawdownCell] = []
    for d in sorted_dates:
        v = ending_value_by_session[d]
        peak, _pdate = peak_after_observation[d]
        if v >= peak:
            continue
        dd_dollar = v - peak  # negative
        dd_pct = dd_dollar / peak if peak > 0 else Decimal("0")
        forward_rec: date | None = None
        for d2 in sorted_dates:
            if d2 <= d:
                continue
            if ending_value_by_session[d2] >= peak:
                forward_rec = d2
                break
        cells.append(DrawdownCell(
            date=d,
            drawdown_dollar=dd_dollar,
            drawdown_pct=dd_pct,
            recovery_end_date=forward_rec,
        ))

    return DrawdownSeries(
        candidate_id=candidate_id,
        registration=registration,
        cells=tuple(cells),
        peak_value=peak_value,
        peak_date=peak_date,
    )


__all__ = ["DrawdownCell", "DrawdownSeries", "compute_drawdown"]
