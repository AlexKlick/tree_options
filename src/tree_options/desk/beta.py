"""Point-in-time beta vs SPY from the research panel (for the delta rail).

Input: ``<paper>/ohlc-panel.json`` (split-adjusted closes, strings), read
under its writers' shared lock (:mod:`tree_options.desk.panel`), never
written. Beta as of D is the least-squares slope of the name's daily log
returns on SPY's:

* THE WINDOW is fixed by the NYSE calendar, not by the data: the
  :data:`WINDOW` sessions ending at the last session on or before D
  (``session``). The return at session s is ln(close_s / close_prev(s)),
  prev(s) the calendar session before s; the window's first return
  (``window_first``) is anchored on the close one session earlier.
* GAP-AWARE, NO STRETCHING: a return needs all four closes (the name's
  and SPY's at s and prev(s)); a missing, non-positive or unparseable
  close drops every return that needs it (two per interior hole). The
  window is never extended backwards to make up for dropped returns, and
  a return is never taken across a hole (no multi-session returns).
* Fewer than :data:`MIN_RETURNS` usable returns: no beta (``beta`` None,
  ``reason`` says why), so the delta rail fails closed.
* POINT IN TIME: only closes dated on sessions inside the window (all on
  or before D) are read, so no data after D can move the estimate. The
  panel is split-adjusted: a later split rescales every earlier close by
  one factor, which leaves log returns unchanged.

Beta is reported as a Decimal rounded to 6 places (a statistic, computed
in float; it multiplies money only inside the rail).
"""

from __future__ import annotations

import bisect
import itertools
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from tree_options.desk import paths
from tree_options.desk.panel import read_panel

WINDOW = 252
MIN_RETURNS = 200
BENCHMARK = "SPY"
PANEL_FILE = "ohlc-panel.json"


class SessionCalendar(Protocol):
    def sessions(self) -> tuple[date, ...]: ...


@dataclass(frozen=True)
class BetaEstimate:
    symbol: str
    as_of: date  # the requested date
    session: date | None  # the window's last session (<= as_of)
    beta: Decimal | None  # None: unavailable (see reason)
    n_returns: int
    window_first: date | None  # the session of the window's first return
    reason: str = ""


def default_panel_path() -> Path:
    return paths.paper_dir() / PANEL_FILE


def _close(rows: Mapping[str, Any] | None, day: date) -> float | None:
    if rows is None:
        return None
    row = rows.get(day.isoformat())
    if not isinstance(row, Mapping):
        return None
    try:
        px = float(str(row.get("close")))
    except ValueError:
        return None
    return px if math.isfinite(px) and px > 0 else None


def beta_from_panel(
    panel: Mapping[str, Any],
    symbol: str,
    as_of: date,
    cal: SessionCalendar,
    *,
    window: int = WINDOW,
    min_returns: int = MIN_RETURNS,
    benchmark: str = BENCHMARK,
) -> BetaEstimate:
    """Beta of ``symbol`` on ``benchmark`` as of ``as_of`` (pure; see the
    module docstring for the window and gap rules)."""
    if window < 2 or min_returns < 2:
        raise ValueError(f"window ({window}) and min_returns ({min_returns}) must be >= 2")
    sessions = cal.sessions()
    end = bisect.bisect_right(sessions, as_of) - 1
    if end - window < 0:
        return BetaEstimate(symbol, as_of, None, None, 0, None, "calendar too short for the window")
    days = sessions[end - window : end + 1]  # window + 1 closes -> window returns
    name_rows = panel.get(symbol)
    bench_rows = panel.get(benchmark)
    xs: list[float] = []
    ys: list[float] = []
    for prev, cur in itertools.pairwise(days):
        b0, b1 = _close(bench_rows, prev), _close(bench_rows, cur)
        n0, n1 = _close(name_rows, prev), _close(name_rows, cur)
        if b0 is None or b1 is None or n0 is None or n1 is None:
            continue
        xs.append(math.log(b1 / b0))
        ys.append(math.log(n1 / n0))
    n = len(xs)
    base = BetaEstimate(symbol, as_of, days[-1], None, n, days[1])
    if n < min_returns:
        return _with_reason(
            base, f"{n} usable returns < {min_returns} in the {window}-session window"
        )
    mx = math.fsum(xs) / n
    my = math.fsum(ys) / n
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    sxy = math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    if sxx <= 0:
        return _with_reason(base, f"{benchmark} returns have no variance in the window")
    return BetaEstimate(symbol, as_of, days[-1], Decimal(f"{sxy / sxx:.6f}"), n, days[1])


def _with_reason(est: BetaEstimate, reason: str) -> BetaEstimate:
    return BetaEstimate(
        est.symbol, est.as_of, est.session, None, est.n_returns, est.window_first, reason
    )


def load_betas(
    symbols: Iterable[str],
    as_of: date,
    cal: SessionCalendar,
    *,
    panel_path: Path | None = None,
) -> dict[str, BetaEstimate]:
    """:func:`beta_from_panel` for each symbol from one locked panel read."""
    panel = read_panel(panel_path or default_panel_path())
    return {s: beta_from_panel(panel, s, as_of, cal) for s in symbols}
