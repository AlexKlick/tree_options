"""Shared time-series helpers for the trex lanes.

``decimate_pairs`` is the stride decimation used by every over-time
series the cockpit serves (P&L history, equity curve, valuation
scenarios): first and last points preserved exactly, interior points
stride-picked, output never exceeds ``max_points``.
"""

from __future__ import annotations


def decimate_pairs(
    pts: list[tuple[int, float]], max_points: int = 600
) -> list[tuple[int, float]]:
    """Stride-decimate (ts_ms, value) pairs; first/last kept exactly."""
    if len(pts) <= max_points:
        return pts
    stride = len(pts) / max_points
    keep = sorted(
        {0, len(pts) - 1} | {int(i * stride) for i in range(max_points - 1)}
    )
    return [pts[i] for i in keep]


LEVEL_PAD = 0.08  # of the data span, each side


def level_extent(pts: list[tuple[int, float]]) -> tuple[float, float]:
    """Y bounds for LEVEL series (account equity, stock prices): fit the
    data plus LEVEL_PAD, never anchored at zero. A flat or near-flat series
    gets a band of max(1.0, |level| * 1e-5) so a cent wiggle on a $1M
    account does not fill the chart."""
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    min_span = max(1.0, abs((lo + hi) / 2) * 1e-5)
    span = hi - lo
    if span < min_span:
        mid = (lo + hi) / 2
        return mid - min_span / 2, mid + min_span / 2
    pad = span * LEVEL_PAD
    return lo - pad, hi + pad


def y_extent(pts: list[tuple[int, float]]) -> tuple[float, float]:
    """Y bounds including zero, with a minimum 1.0 span (flat lines still
    render a visible band around the zero line). For P&L series; level
    series (equity, prices) use ``level_extent``."""
    y_lo = min(0.0, *(v for _, v in pts))
    y_hi = max(0.0, *(v for _, v in pts))
    if y_hi - y_lo < 1.0:
        y_hi = y_lo + 1.0
    return y_lo, y_hi
