"""Pure chart math + SVG geometry for the cockpit (broker-free).

Three surfaces:

- At-expiry payoff per structure (hockey stick): S >= long strike loses
  the debit; short < S < long is linear; S <= short strike is maximum.
- Book summary tiles: committed debit, max gain at/below the short
  strikes, max loss at/above the long strikes.
- Unrealized-P&L-over-time line from the monitor's marks history.

All functions consume plan numbers / persisted marks and emit
ready-to-render coordinates; the trade's discipline never holds to
expiry (touch-exit at the long strike, hard time stops) — the payoff
chart shows the terminal shape only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

MULT = 100
CHART_W = 640
CHART_H = 240
PAD_L = 66
PAD_R = 18
PAD_T = 16
PAD_B = 30


def expiry_pnl(
    long_strike: float, short_strike: float, entry: float, qty: int, s: float
) -> float:
    """Put-debit-spread P&L at expiry with the underlying at ``s``."""
    intrinsic = max(0.0, long_strike - s) - max(0.0, short_strike - s)
    return (intrinsic - entry) * qty * MULT


def _usd(v: float) -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}${abs(v):,.0f}"


def build_payoff_chart(
    long_strike: float,
    short_strike: float,
    entry: float,
    qty: int,
    spot: float | None = None,
) -> dict[str, Any] | None:
    """Geometry + levels for one structure's at-expiry payoff chart.

    Returns None when there is no position to chart. All coordinates are
    pixel-space against a fixed viewBox, so the template stays arithmetic-
    free and the output is trivially unit-testable.
    """
    if qty <= 0 or entry < 0 or long_strike <= short_strike:
        return None

    width = long_strike - short_strike
    breakeven = long_strike - entry
    max_gain = (width - entry) * qty * MULT
    max_loss = -entry * qty * MULT

    x_lo = short_strike - 30.0
    x_hi = max(long_strike + 25.0, (spot + 15.0) if spot is not None else 0.0)
    y_lo = min(max_loss, 0.0)
    y_hi = max(max_gain, 0.0)
    if y_hi - y_lo < 1.0:  # degenerate flat book; keep a drawable band
        y_hi = y_lo + 1.0

    def sx(v: float) -> float:
        return PAD_L + (v - x_lo) / (x_hi - x_lo) * (CHART_W - PAD_L - PAD_R)

    def sy(v: float) -> float:
        return PAD_T + (y_hi - v) / (y_hi - y_lo) * (CHART_H - PAD_T - PAD_B)

    # Sample the price axis: kinks + endpoints + a light linear grid.
    step = 5.0
    xs: set[float] = {x_lo, x_hi, short_strike, long_strike, breakeven}
    xs.update(x_lo + i * step for i in range(int((x_hi - x_lo) / step) + 1))
    ordered = sorted(xs)

    pts = [(sx(x), sy(expiry_pnl(long_strike, short_strike, entry, qty, x))) for x in ordered]
    polyline = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)

    zero_y = sy(0.0)

    # Profit region: everything at/below breakeven (pnl >= 0 there);
    # loss region: at/above breakeven. Closed along the zero line.
    pos_area = neg_area = None
    if max_gain > 0:
        pos_pts = [
            (px, py)
            for (px, py), x in zip(pts, ordered, strict=True)
            if x <= breakeven
        ]
        if pos_pts:
            pos_area = (
                " ".join(f"{px:.1f},{py:.1f}" for px, py in pos_pts)
                + f" {sx(breakeven):.1f},{zero_y:.1f} {pos_pts[0][0]:.1f},{zero_y:.1f}"
            )
    if max_loss < 0:
        neg_pts = [
            (px, py)
            for (px, py), x in zip(pts, ordered, strict=True)
            if x >= breakeven
        ]
        if neg_pts:
            neg_area = (
                f"{sx(breakeven):.1f},{zero_y:.1f} "
                + " ".join(f"{px:.1f},{py:.1f}" for px, py in neg_pts)
                + f" {neg_pts[-1][0]:.1f},{zero_y:.1f}"
            )

    x_ticks = [
        {"x": round(sx(short_strike), 1), "label": f"{short_strike:g} short"},
        {"x": round(sx(breakeven), 1), "label": f"BE {breakeven:g}"},
        {"x": round(sx(long_strike), 1), "label": f"{long_strike:g} long"},
    ]
    y_ticks = [
        {"y": round(sy(max_gain), 1), "label": _usd(max_gain)},
        {"y": round(zero_y, 1), "label": "$0"},
        {"y": round(sy(max_loss), 1), "label": _usd(max_loss)},
    ]

    spot_mark = None
    if spot is not None and x_lo <= spot <= x_hi:
        spot_mark = {"x": round(sx(spot), 1), "label": f"spot {spot:g}"}

    return {
        "w": CHART_W,
        "h": CHART_H,
        "polyline": polyline,
        "pos_area": pos_area,
        "neg_area": neg_area,
        "zero_y": round(zero_y, 1),
        "x_ticks": x_ticks,
        "y_ticks": y_ticks,
        "spot": spot_mark,
        "max_gain_label": {"x": round(sx(x_lo) + 6, 1), "y": round(sy(max_gain) - 6, 1), "text": _usd(max_gain)},
        "levels": {
            "long_strike": f"{long_strike:g}",
            "short_strike": f"{short_strike:g}",
            "entry": f"{entry:.2f}",
            "qty": qty,
            "width": f"{width:g}",
            "breakeven": f"{breakeven:g}",
            "max_gain_usd": _usd(max_gain),
            "max_loss_usd": _usd(max_loss),
            "spot": f"{spot:g}" if spot is not None else None,
        },
    }


def summarize_book(legs: list[tuple[float, float, float, int]]) -> dict[str, Any]:
    """Book-level tiles: committed debit, max gain, max loss.

    Each leg is (long_strike, short_strike, entry, qty). Max gain is the
    sum of the wings at/below the short strikes; max loss is the sum of
    the debits (at/above the long strikes).
    """
    committed = sum(entry * qty * MULT for _, _, entry, qty in legs)
    max_gain = sum((long_s - short_s - entry) * qty * MULT for long_s, short_s, entry, qty in legs)
    short_floor = min((short_s for _, short_s, _, _ in legs), default=None)
    return {
        "committed_usd": f"${committed:,.0f}",
        "max_gain_usd": _usd(max_gain),
        "max_loss_usd": _usd(-committed),
        "short_floor": f"{short_floor:g}" if short_floor is not None else None,
    }


HIST_W = 640
HIST_H = 180
HIST_PAD_L = 66
HIST_PAD_R = 52
HIST_PAD_T = 14
HIST_PAD_B = 26


def build_pnl_history_chart(samples: list[dict[str, str]]) -> dict[str, Any] | None:
    """Unrealized-P&L-over-time line from the monitor's marks history.

    X is proportional to wall-clock time (gaps when the monitor was down
    compress honestly); y spans the observed range plus zero.
    """
    pts_raw: list[tuple[datetime, float]] = []
    for s in samples:
        try:
            ts = datetime.fromisoformat(str(s["ts"]))
            val = float(s["total"])
        except (KeyError, ValueError):
            continue
        pts_raw.append((ts, val))
    if len(pts_raw) < 2:
        return None

    t0 = pts_raw[0][0].timestamp()
    t1 = pts_raw[-1][0].timestamp()
    if t1 - t0 < 1.0:
        return None
    y_lo = min(0.0, *(v for _, v in pts_raw))
    y_hi = max(0.0, *(v for _, v in pts_raw))
    if y_hi - y_lo < 1.0:
        y_hi = y_lo + 1.0

    def hx(ts: datetime) -> float:
        return HIST_PAD_L + (ts.timestamp() - t0) / (t1 - t0) * (HIST_W - HIST_PAD_L - HIST_PAD_R)

    def hy(v: float) -> float:
        return HIST_PAD_T + (y_hi - v) / (y_hi - y_lo) * (HIST_H - HIST_PAD_T - HIST_PAD_B)

    polyline = " ".join(f"{hx(ts):.1f},{hy(v):.1f}" for ts, v in pts_raw)
    last_ts, last_v = pts_raw[-1]

    return {
        "w": HIST_W,
        "h": HIST_H,
        "polyline": polyline,
        "zero_y": round(hy(0.0), 1),
        "y_ticks": [
            {"y": round(hy(y_hi), 1), "label": _usd(y_hi)},
            {"y": round(hy(0.0), 1), "label": "$0"},
            {"y": round(hy(y_lo), 1), "label": _usd(y_lo)},
        ],
        "x_ticks": [
            {"x": round(hx(pts_raw[0][0]), 1), "label": pts_raw[0][0].strftime("%H:%M"), "anchor": "start"},
            {"x": round(hx(last_ts), 1), "label": last_ts.strftime("%H:%M"), "anchor": "end"},
        ],
        "last": {
            "x": round(hx(last_ts), 1),
            "y": round(hy(last_v), 1),
            "label": _usd(last_v),
            "pos": last_v >= 0,
        },
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

    ~n uniform samples over the same x-range rule as ``build_payoff_chart``
    with the kinks and the breakeven forced in EXACTLY (replacing the
    nearest grid point), so client-side linear interpolation between
    adjacent points is exact everywhere. All pnl values come from
    ``expiry_pnl`` — Python stays the single source of the math.
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

    Same parsing/validity rules as ``build_pnl_history_chart``; emits
    epoch-milliseconds + floats, and stride-decimates to ``max_points``
    (first and last preserved exactly) so a multi-week marks history
    cannot blow the 15s-poll budget through the no-compression portal.
    """
    pts: list[tuple[int, float]] = []
    for s in samples:
        try:
            ts = datetime.fromisoformat(str(s["ts"]))
            val = float(s["total"])
        except (KeyError, ValueError):
            continue
        pts.append((int(ts.timestamp() * 1000), val))
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < 1000:
        return None

    if len(pts) > max_points:
        stride = len(pts) / max_points
        keep = sorted(
            {0, len(pts) - 1} | {int(i * stride) for i in range(max_points - 1)}
        )
        pts = [pts[i] for i in keep]

    y_lo = min(0.0, *(v for _, v in pts))
    y_hi = max(0.0, *(v for _, v in pts))
    if y_hi - y_lo < 1.0:
        y_hi = y_lo + 1.0
    return {
        "points": [[t, v] for t, v in pts],
        "y_lo": y_lo,
        "y_hi": y_hi,
        "last": {"ts_ms": pts[-1][0], "pnl": pts[-1][1], "pos": pts[-1][1] >= 0},
    }
