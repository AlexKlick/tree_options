"""The desk's direction signals: pure functions over the research panel.

Only the two backtest survivors may point a trade (plan D5):

* ``xsmom_top3``: XSMOM-TOP3 monthly. On the first session of a month,
  rank the 36 tradables (panel minus SPY) by trailing return and take the
  top 3; hold 20 sessions. Baseline PROTOCOL-XSMOM.md.
* ``pead_beat``: PEAD-BIGSURPRISE, beats only. On the first session after
  a report date in earnings-calendar.json, fire if
  close(session)/close(prior)-1 >= +1.5% (PEAD-SIGN.md: the drift lives in
  beats, +97 USD/card vs +2 on misses); hold 20. Baseline PROTOCOL-PEAD.md.

RANKING CONVENTION (found 2026-09-23 while pinning goldens): the rule text
(CRON-paper-engine.md, iter004.py's docstring) says
close(t-21)/close(t-273)-1, a 12-1 momentum. The code that produced every
protocol row (iter004.py, and iter003/xu_xsmom/xsmom_exitgrid before it)
computes ``closes[p] / closes[p - skip - lookback] - 1``, i.e.
close(t)/close(t-273)-1: the skip never skips. PROTOCOL-XSMOM.md
reproduces only under that reading (46/46 months; the skip-21 reading
picks a different set in 19/46). ``top3`` follows the evidence; the prose
reading is reported beside it (``top3_skip21``) until the operator rules.

Conventions copied from iter004.py: each name is indexed on its own panel
sessions; a name is dropped if two consecutive panel dates inside the
look-back are more than 10 calendar days apart (the vendor-hole guard);
fewer than 30 ranked names means no signal; ties keep name order. Nothing
here imports the research scripts (a test enforces it).
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from tree_options.desk.sessions import (
    Calendar,
    calendar_days_between,
    first_session_after,
    is_first_session_of_month,
    previous_session,
)
from tree_options.desk.universe import NO_OPTIONS_EXPRESSION, XSMOM_TRADABLES

ALLOWED_DIRECTION: frozenset[str] = frozenset({"xsmom_top3", "pead_beat"})
# conditions that may shape a structure but never pick a direction (plan D5)
CONTEXT_ONLY: frozenset[str] = frozenset({"trend", "breadth", "vix_term"})
# refuted by the research program (RESEARCH-LEDGER.md DEAD/DEFLATED,
# CRON-paper-engine.md dead rules, PEAD-SIGN.md, the gated short-vol lane);
# banned by construction as direction signals
BANNED: frozenset[str] = frozenset(
    {
        "volspike",
        "mom_top_tercile",
        "mr_h1",
        "r3f",
        "r3f_up",
        "r1",
        "cont",
        "ts_momentum",
        "mom60_h60",
        "squeeze",
        "gap_fade",
        "gap_cont",
        "exec_open_gap",
        "breadth_gate",
        "sector_rotation",
        "xsmom_60skip5",
        "sweep_divergence_swing",
        "short_any",
        "xsmom_short_leg",
        "pead_miss",
        "short_vol_gated",
        "semi_reversion",
    }
)

XSMOM_LOOKBACK = 273  # 252 + 21 sessions
XSMOM_SKIP_PROSE = 21
XSMOM_TOPK = 3
XSMOM_MIN_RANKED = 30
XSMOM_CONVENTION = "close(t)/close(t-273)-1 (the computation behind PROTOCOL-XSMOM.md)"
HOLE_DAYS = 10
PEAD_THRESHOLD = Decimal("0.015")
PEAD_CLEAN_BACK = 5
HOLD_SESSIONS = 20
_Q = Decimal("0.000001")

Panel = Mapping[str, Mapping[str, Mapping[str, Any]]]


class BannedSignalError(ValueError):
    pass


def require_direction_signal(name: str) -> None:
    """Refuse anything but the two allowed direction signals."""
    if name in BANNED:
        raise BannedSignalError(f"{name}: refuted by the research program; banned")
    if name in CONTEXT_ONLY:
        raise BannedSignalError(f"{name}: context only, never a direction signal")
    if name not in ALLOWED_DIRECTION:
        raise BannedSignalError(f"{name}: not an allowed direction signal")


def fmt(x: Decimal) -> str:
    return str(x.quantize(_Q))


def _close(bars: Mapping[str, Mapping[str, Any]], d: str) -> Decimal:
    return Decimal(str(bars[d]["close"]))


def _clean(ds: list[str], lo: int, hi: int) -> bool:
    lo = max(0, lo)
    return all(
        calendar_days_between(a, b) <= HOLE_DAYS
        for a, b in zip(ds[lo:hi], ds[lo + 1 : hi + 1], strict=True)
    )


def _index(ds: list[str], d: str) -> int:
    i = bisect.bisect_left(ds, d)
    if i == len(ds) or ds[i] != d:
        raise KeyError(d)
    return i


# ------------------------------------------------------------------- XSMOM


@dataclass(frozen=True)
class XsmomResult:
    session: date
    is_rebalance_day: bool
    fires: bool
    top3: tuple[str, ...]
    ranked: tuple[tuple[str, Decimal], ...]
    scores: dict[str, Decimal]
    top3_skip21: tuple[str, ...]
    scores_skip21: dict[str, Decimal]
    conventions_agree: bool
    excluded: dict[str, str]
    n_ranked: int
    no_options_expression: tuple[str, ...]


def xsmom_rank(
    panel: Panel, session: date, *, skip: int, names: Iterable[str] = XSMOM_TRADABLES
) -> tuple[list[tuple[str, Decimal]], dict[str, str]]:
    """Names ranked by close(t-skip)/close(t-273)-1, best first, plus the
    excluded names with a reason."""
    d = session.isoformat()
    scored: list[tuple[str, Decimal]] = []
    excluded: dict[str, str] = {}
    for name in sorted(names):
        bars = panel.get(name)
        if not bars:
            excluded[name] = "not_in_panel"
            continue
        if d not in bars:
            excluded[name] = "no_session_bar"
            continue
        ds = sorted(bars)
        p = _index(ds, d)
        if p < XSMOM_LOOKBACK:
            excluded[name] = "insufficient_history"
            continue
        if not _clean(ds, p - XSMOM_LOOKBACK, p):
            excluded[name] = "hole"
            continue
        scored.append((name, _close(bars, ds[p - skip]) / _close(bars, ds[p - XSMOM_LOOKBACK]) - 1))
    scored.sort(key=lambda x: -x[1])  # stable: ties keep name order
    return scored, excluded


def xsmom_top3(
    panel: Panel, session: date, cal: Calendar, *, names: Iterable[str] = XSMOM_TRADABLES
) -> XsmomResult:
    names = list(names)
    ranked, excluded = xsmom_rank(panel, session, skip=0, names=names)
    prose, _ = xsmom_rank(panel, session, skip=XSMOM_SKIP_PROSE, names=names)
    rebalance = is_first_session_of_month(session, cal)
    top3 = tuple(n for n, _ in ranked[:XSMOM_TOPK])
    top21 = tuple(n for n, _ in prose[:XSMOM_TOPK])
    return XsmomResult(
        session=session,
        is_rebalance_day=rebalance,
        fires=rebalance and len(ranked) >= XSMOM_MIN_RANKED,
        top3=top3,
        ranked=tuple(ranked),
        scores=dict(ranked),
        top3_skip21=top21,
        scores_skip21=dict(prose),
        conventions_agree=set(top3) == set(top21),
        excluded=excluded,
        n_ranked=len(ranked),
        no_options_expression=tuple(sorted(set(top3) & NO_OPTIONS_EXPRESSION)),
    )


def xsmom_doc(res: XsmomResult) -> dict[str, Any]:
    return {
        "is_rebalance_day": res.is_rebalance_day,
        "fires": res.fires,
        "top3": list(res.top3),
        "scores": {n: fmt(s) for n, s in res.ranked},
        "convention": XSMOM_CONVENTION,
        "top3_skip21": list(res.top3_skip21),
        "conventions_agree": res.conventions_agree,
        "excluded": dict(sorted(res.excluded.items())),
        "n_ranked": res.n_ranked,
        "no_options_expression": list(res.no_options_expression),
    }


# -------------------------------------------------------------------- PEAD


@dataclass(frozen=True)
class PeadEvent:
    name: str
    report_date: str
    session: str
    prior_session: str | None
    move: Decimal | None
    fires: bool
    # beat | below_threshold | miss_proxy | no_session_bar | no_prior_bar | hole | prior_gap
    reason: str


@dataclass(frozen=True)
class PeadResult:
    session: date
    evaluated: list[PeadEvent]
    beats: list[PeadEvent]


def pead_beats(
    panel: Panel, earnings: Mapping[str, Iterable[str]], session: date, cal: Calendar
) -> PeadResult:
    """Every calendar report whose first post-report session is ``session``,
    evaluated; the beats (move >= +1.5%) fire."""
    d = session.isoformat()
    evaluated: list[PeadEvent] = []
    for name in sorted(earnings):
        bars = panel.get(name)
        if not bars:
            continue
        for rep in sorted(set(earnings[name])):
            try:
                report = date.fromisoformat(rep)
            except ValueError:
                continue
            if first_session_after(report, cal) != session:
                continue
            if d not in bars:
                evaluated.append(PeadEvent(name, rep, d, None, None, False, "no_session_bar"))
                continue
            ds = sorted(bars)
            i = _index(ds, d)
            if i < 1:
                evaluated.append(PeadEvent(name, rep, d, None, None, False, "no_prior_bar"))
                continue
            if not _clean(ds, i - PEAD_CLEAN_BACK, i):
                evaluated.append(PeadEvent(name, rep, d, ds[i - 1], None, False, "hole"))
                continue
            prior = previous_session(session, cal)
            if prior is None or ds[i - 1] != prior.isoformat():
                # a missing prior bar would stretch the report-session move
                # over two sessions (stricter than iter004, which had a
                # gapless panel): never fire on it
                evaluated.append(PeadEvent(name, rep, d, ds[i - 1], None, False, "prior_gap"))
                continue
            move = _close(bars, d) / _close(bars, ds[i - 1]) - 1
            fires = move >= PEAD_THRESHOLD
            reason = (
                "beat" if fires else "miss_proxy" if move <= -PEAD_THRESHOLD else "below_threshold"
            )
            evaluated.append(PeadEvent(name, rep, d, ds[i - 1], move, fires, reason))
    return PeadResult(session, evaluated, [e for e in evaluated if e.fires])


def pead_doc(res: PeadResult) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(beats in the signals-file shape, every evaluated event)."""
    beats = [
        {"name": e.name, "report_date": e.report_date, "move": fmt(e.move)}
        for e in res.beats
        if e.move is not None
    ]
    evaluated = [
        {
            "name": e.name,
            "report_date": e.report_date,
            "prior_session": e.prior_session,
            "move": fmt(e.move) if e.move is not None else None,
            "fires": e.fires,
            "reason": e.reason,
        }
        for e in res.evaluated
    ]
    return beats, evaluated
