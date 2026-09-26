from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal

from tree_options.research.contracts import ResearchRegistration

"""Drawdown + peak-to-trough + recovery duration.

Handoff §3 (Default charts): drawdown and recovery on the same
calendar, loss relative to prior peak, with unresolved recovery
durations visibly open-ended.

Handoff §11 (Selection integrity): "Exploration cannot edit a
registered result." For ``retrospective_backfill`` candidates the
drawdown is computed but the recovery duration is left as ``None`` —
we cannot claim a recovery on a back-filled window because the original
selection froze before the window completed.
"""


date = _dt.date  # type alias used inside function bodies; field annotations use _dt.date


@dataclass(frozen=True)
class DrawdownCell:
    date: _dt.date
    drawdown_dollar: Decimal      # signed negative; >= ending_value - peak
    drawdown_pct: Decimal          # signed negative; pct off peak
    recovery_end_date: _dt.date | None  # None when still underwater OR when retrospective_only


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
    peak gets a negative drawdown in both dollars and pct.

    Recovery is the first subsequent date the series returns to the
    peak level (>=) — unless the registration is retrospective, in
    which case recovery is left as None (the audit window isn't
    complete in the original selection's reference frame).
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
    recovery_underway: dict[Decimal, date | None] = {}
    for d in sorted_dates:
        v = ending_value_by_session[d]
        peak, _pdate = peak_after_observation[d]
        if v < peak:
            dd_dollar = v - peak  # negative
            dd_pct = dd_dollar / peak if peak > 0 else Decimal("0")
            if peak in recovery_underway:
                rec_end = recovery_underway[peak]
            else:
                rec_end = None
            cells.append(DrawdownCell(
                date=d,
                drawdown_dollar=dd_dollar,
                drawdown_pct=dd_pct,
                recovery_end_date=rec_end,
            ))
        else:
            # at/above the peak — close any open recovery for this peak
            if peak in recovery_underway and recovery_underway[peak] is None:
                recovery_underway[peak] = d

    # Annotate retroactively: for each under-peak cell, set recovery_end
    # to the first date the series came back to that peak. For
    # retrospective registrations, leave None for any cell whose
    # recovery end is None or whose peak never recovered by the last
    # observed date.
    last_obs = sorted_dates[-1]
    for c in list(cells):
        if c.recovery_end_date is not None:
            continue
        # walk forward from c.date looking for the first time v >= peak
        peak_now, _ = peak_after_observation[c.date]
        if registration is ResearchRegistration.RETROSPECTIVE_BACKFILL:
            # Back-filled window: cannot claim a recovery on incomplete
            # data. Leave None.
            continue
        # Forward walk
        forward_rec: date | None = None
        for d2 in sorted_dates:
            if d2 <= c.date:
                continue
            if ending_value_by_session[d2] >= peak_now:
                forward_rec = d2
                break
        if rec_end is None:
            # Still under at last observation — leave None (open-ended)
            if c.date == last_obs:
                rec_end = None
        cells[cells.index(c)] = DrawdownCell(
            date=c.date,
            drawdown_dollar=c.drawdown_dollar,
            drawdown_pct=c.drawdown_pct,
            recovery_end_date=forward_rec,
        )

    return DrawdownSeries(
        candidate_id=candidate_id,
        registration=registration,
        cells=tuple(cells),
        peak_value=peak_value,
        peak_date=peak_date,
    )


__all__ = ["DrawdownCell", "DrawdownSeries", "compute_drawdown"]
