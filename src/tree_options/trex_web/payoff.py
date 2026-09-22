"""Pure at-expiry payoff math + SVG geometry for the cockpit.

Broker-free by construction: consumes plan numbers (strikes, entry, qty)
and an optional current spot, and emits ready-to-render coordinates and
labels. The payoff of a put debit spread is a hockey stick:

    S >= long strike   ->  lose the debit
    short < S < long   ->  (long - S - entry) per spread, linear
    S <= short strike  ->  maximum (width - entry) per spread

The trade's discipline never holds to expiry (touch-exit at the long
strike, hard time stops); this chart shows the terminal shape only.
"""

from __future__ import annotations

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
