"""Long-term per-symbol OHLCV history for the cockpit symbol pages.

The viewer's own bars envelope is a 365-day window; this module serves the
desk's durable store instead — ``artifacts/paper-trades/ohlc-panel.json``
(split-adjusted daily bars since 2021, extended nightly by desk-eod-equity)
— under the panel's SHARED flock with a web-sized timeout: a writer holding
the lock degrades to an honest busy payload, never a 500 and never a wait.

Rows are decimated by shared INDEX (first and last sessions exact, interior
stride-picked — the same semantics as ``trex.series.decimate_pairs``), so a
point is always one real session's O/H/L/C/V, never a per-field mix. The
returned payload is split into a stable half (hashed into the ETag) and the
route attaches the volatile stamps (``now``, ``history_age_seconds``) only
on 200s — a 60 s re-poll of a nightly-updating series is a 304.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from tree_options.desk.panel import PanelLocked, read_panel_with_sha256
from tree_options.time import calendar_days
from tree_options.trex.clock import ET
from tree_options.trex.series import level_extent

# calendar-day windows cut BACK from the panel's last session; None = all
RANGES: dict[str, int | None] = {"1y": 365, "3y": 1095, "5y": 1825, "max": None}
DEFAULT_RANGE = "3y"
DEFAULT_MAX_POINTS = 600
MIN_MAX_POINTS, MAX_MAX_POINTS = 120, 1200
LOCK_TIMEOUT_S = 3.0  # web-sized, not the desk's 300 s default


def clamp_request(range_key: str, max_points: int) -> tuple[str, int]:
    """:(range, max_points) with unknown/odd inputs coerced to sane values."""
    r = range_key if range_key in RANGES else DEFAULT_RANGE
    m = max(MIN_MAX_POINTS, min(MAX_MAX_POINTS, int(max_points)))
    return r, m


def _ts_ms(day: str) -> int:
    """Panel session date -> ET-midnight epoch ms (ET day formatting works)."""
    return int(
        datetime.fromisoformat(day).replace(tzinfo=ET).timestamp() * 1000
    )


def _keep_indices(n: int, max_points: int) -> list[int]:
    """Stride indexes, first/last exact — decimate_pairs semantics on rows."""
    if n <= max_points:
        return list(range(n))
    stride = n / max_points
    return sorted({0, n - 1} | {int(i * stride) for i in range(max_points - 1)})


def _row(day: str, bar: dict[str, Any]) -> list[Any] | None:
    """One panel bar -> [ts, o, h, l, c, v]; incomplete bars are skipped."""
    try:
        return [
            _ts_ms(day),
            float(bar["open"]),
            float(bar["high"]),
            float(bar["low"]),
            float(bar["close"]),
            int(bar["volume"]),
        ]
    except (KeyError, TypeError, ValueError):
        return None


def history_payload(
    panel: Path,
    sym: str,
    range_key: str,
    max_points: int,
) -> tuple[dict[str, Any], str]:
    """(stable payload, etag). Degrades: busy/unavailable/not-in-panel all
    return 200-shaped bodies with an honest ``error``/``in_panel`` state."""
    base: dict[str, Any] = {
        "symbol": sym,
        "source": "ohlc-panel",
        "range": range_key,
        "in_panel": False,
        "panel_last_session": None,
        "panel_sha256_12": None,
        "points": [],
        "y_lo": None,
        "y_hi": None,
        "vol_max": None,
        "last": None,
        "note": "split-adjusted daily OHLCV · ohlc-panel.json · extended nightly by desk-eod-equity",
        "error": None,
    }
    try:
        doc, sha = read_panel_with_sha256(panel, timeout_s=LOCK_TIMEOUT_S)
    except PanelLocked:
        base["error"] = "panel busy: writer holds the lock"
        base["points"] = None
        return base, _etag(base)
    except (OSError, ValueError):
        base["error"] = "panel unavailable"
        base["points"] = None
        return base, _etag(base)

    base["panel_sha256_12"] = sha[:12]
    series = doc.get(sym)
    if not isinstance(series, dict) or not series:
        return base, _etag(base)  # in_panel stays False, points []
    days = sorted(series)
    base["in_panel"] = True
    base["panel_last_session"] = days[-1]

    window = RANGES[range_key]
    if window is not None:
        cut = (
            datetime.fromisoformat(days[-1]) - calendar_days(window)
        ).date().isoformat()
        days = [d for d in days if d >= cut]
    base["range_start"] = days[0] if days else None
    base["range_sessions"] = len(days)

    rows = [r for d in days if (r := _row(d, series[d])) is not None]
    rows = [rows[i] for i in _keep_indices(len(rows), max_points)]
    if not rows:
        base["error"] = "no complete bars in range"
        base["points"] = None
        return base, _etag(base)
    base["points"] = rows
    y_lo, y_hi = level_extent([(int(r[0]), float(r[4])) for r in rows])
    base["y_lo"], base["y_hi"] = y_lo, y_hi
    base["vol_max"] = max(int(r[5]) for r in rows)
    base["last"] = {
        "date": days[-1],
        "ts_ms": rows[-1][0],
        "close": rows[-1][4],
    }
    return base, _etag(base)


def _etag(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f'W/"{digest[:16]}"'


def history_age_seconds(last_session: str | None, now: datetime) -> float | None:
    """Age of the newest bar (ET midnight of the panel's last session)."""
    if not last_session:
        return None
    return max(0.0, (now - datetime.fromisoformat(last_session).replace(tzinfo=ET)).total_seconds())
