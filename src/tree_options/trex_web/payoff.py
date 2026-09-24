"""Pure chart math for the cockpit (broker-free).

Three surfaces:

- At-expiry payoff per structure (hockey stick): S >= long strike loses
  the debit; short < S < long is linear; S <= short strike is maximum.
- Book summary numbers: committed debit, max gain at/below the short
  strikes, max loss at/above the long strikes.
- Unrealized-P&L-over-time series from the monitor's marks history.

All functions consume plan numbers / persisted marks and emit plain
data — the SPA owns render-space. The trade's discipline never holds to
expiry (touch-exit at the long strike, hard time stops) — the payoff
series shows the terminal shape only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

MULT = 100


def expiry_pnl(
    long_strike: float, short_strike: float, entry: float, qty: int, s: float
) -> float:
    """Put-debit-spread P&L at expiry with the underlying at ``s``."""
    intrinsic = max(0.0, long_strike - s) - max(0.0, short_strike - s)
    return (intrinsic - entry) * qty * MULT


def _usd(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.0f}"


def summarize_book(legs: list[tuple[float, float, float, int]]) -> dict[str, Any]:
    """Book-level tiles as numbers (the client formats): committed
    debit, max gain, max loss.

    Each leg is (long_strike, short_strike, entry, qty). Max gain is the
    sum of the wings at/below the short strikes; max loss is the sum of
    the debits (at/above the long strikes).
    """
    committed = sum(entry * qty * MULT for _, _, entry, qty in legs)
    max_gain = sum((long_s - short_s - entry) * qty * MULT for long_s, short_s, entry, qty in legs)
    short_floor = min((short_s for _, short_s, _, _ in legs), default=None)
    return {
        "committed": committed,
        "max_gain": max_gain,
        "max_loss": -committed,
        "short_floor": short_floor,
    }


def payoff_series(
    long_strike: float,
    short_strike: float,
    entry: float,
    qty: int,
    spot: float | None = None,
    n: int = 120,
) -> dict[str, Any] | None:
    """Data-space payoff series for the SPA (no pixel geometry).

    ~n uniform samples over the x-range (short-30 .. max(long+25, spot+15))
    with the kinks and the breakeven forced in EXACTLY, so client-side
    linear interpolation between adjacent points is exact everywhere.
    All pnl values come from ``expiry_pnl`` — Python stays the single
    source of the math.
    """
    if qty <= 0 or entry < 0 or long_strike <= short_strike or n < 4:
        return None

    width = long_strike - short_strike
    breakeven = long_strike - entry
    max_gain = (width - entry) * qty * MULT
    max_loss = -entry * qty * MULT

    x_lo = short_strike - 30.0
    x_hi = max(long_strike + 25.0, (spot + 15.0) if spot is not None else 0.0)
    # Uniform grid sized to leave room for the kinks, UNIONED with the
    # kinks (replacing the nearest grid point breaks when two kinks —
    # long strike and breakeven — share a grid slot).
    xs = [x_lo + i * (x_hi - x_lo) / (n - 4) for i in range(n - 3)]
    xs.extend(k for k in (short_strike, long_strike, breakeven) if x_lo <= k <= x_hi)
    ordered = sorted({round(x, 6) for x in xs})

    points = [
        [x, round(expiry_pnl(long_strike, short_strike, entry, qty, x), 4)]
        for x in ordered
    ]
    return {
        "view": {"x_lo": x_lo, "x_hi": x_hi},
        "points": points,
        "levels": {
            "long_strike": long_strike,
            "short_strike": short_strike,
            "entry": entry,
            "qty": qty,
            "width": width,
            "breakeven": round(breakeven, 6),
            "max_gain": max_gain,
            "max_loss": max_loss,
            "spot": spot,
        },
        "labels": {"max_gain": _usd(max_gain), "max_loss": _usd(max_loss)},
    }


def pnl_history_series(
    samples: list[dict[str, str]], max_points: int = 600
) -> dict[str, Any] | None:
    """Data-space P&L-over-time series for the SPA.

    Emits epoch-milliseconds + floats, and stride-decimates to
    ``max_points`` (first and last preserved exactly) so a multi-week
    marks history cannot blow the 15s-poll budget through the
    no-compression portal.
    """
    pts: list[tuple[int, float]] = []
    for s in samples:
        try:
            ts = datetime.fromisoformat(str(s["ts"]))
            if s["total"] is None:
                # a tick where nothing quoted (empty books at the open, a
                # quote-less terminal line): no observation, never a $0 one
                continue
            val = float(s["total"])
        except (KeyError, TypeError, ValueError):
            continue
        pts.append((int(ts.timestamp() * 1000), val))
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < 1000:
        return None

    from tree_options.trex.series import decimate_pairs, y_extent

    pts = decimate_pairs(pts, max_points)
    y_lo, y_hi = y_extent(pts)
    return {
        "points": [[t, v] for t, v in pts],
        "y_lo": y_lo,
        "y_hi": y_hi,
        "last": {"ts_ms": pts[-1][0], "pnl": pts[-1][1], "pos": pts[-1][1] >= 0},
    }
