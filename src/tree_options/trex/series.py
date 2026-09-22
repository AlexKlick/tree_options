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


def y_extent(pts: list[tuple[int, float]]) -> tuple[float, float]:
    """Y bounds including zero, with a minimum 1.0 span (flat lines still
    render a visible band around the zero line)."""
    y_lo = min(0.0, *(v for _, v in pts))
    y_hi = max(0.0, *(v for _, v in pts))
    if y_hi - y_lo < 1.0:
        y_hi = y_lo + 1.0
    return y_lo, y_hi
