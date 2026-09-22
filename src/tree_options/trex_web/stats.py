"""Paper-money stats derivation for the Performance page (pure, read-only).

Two separately-labeled bases, never conflated (Codex-6): the EQUITY curve
is net-liquidation over time — account truth that may include non-trading
activity; the P&L days table is book-derived TRADING P&L (realized from
fill events + end-of-day unrealized from marks history). Missing marks
are gaps, never zeros; unknown inputs are skipped, never fabricated.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from tree_options.trex.history import read_tail
from tree_options.trex.series import decimate_pairs, y_extent
from tree_options.trex_web.reader import list_plans, read_marks_history


def equity_series(
    records: list[dict[str, Any]], max_points: int = 600
) -> dict[str, Any] | None:
    """Net-liquidation over time; same series shape the SPA charts take."""
    pts: list[tuple[int, float]] = []
    for r in records:
        try:
            ts = datetime.fromisoformat(str(r["ts"]))
            val = float(r["net_liquidation"])
        except (KeyError, ValueError, TypeError):
            continue
        pts.append((int(ts.timestamp() * 1000), val))
    if len(pts) < 2 or pts[-1][0] - pts[0][0] < 1000:
        return None
    pts = decimate_pairs(pts, max_points)
    y_lo, y_hi = y_extent(pts)
    last_v = pts[-1][1]
    return {
        "points": [[t, v] for t, v in pts],
        "y_lo": y_lo,
        "y_hi": y_hi,
        "last": {"ts_ms": pts[-1][0], "value": last_v, "pos": last_v >= 0},
    }


def _day(ts: object) -> str | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).date().isoformat()
    except ValueError:
        return None


def realized_by_day(
    events: list[Any], entry_fills: dict[str, float]
) -> dict[str, float]:
    """Realized P&L per ISO date, diffed from cumulative exit_fill events.

    ``filled`` counters are cumulative per structure; each event's
    increment realizes ``(avg - entry) * increment * 100`` on its date.
    Blended-entry is the final book entry (an approximation only when an
    entry_fill postdates the first exit_fill - disclosed by the caller).
    Unknown entry -> the event is skipped, never fabricated.
    """
    prev_filled: dict[str, int] = {}
    out: dict[str, float] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("event") != "exit_fill":
            continue
        sid = ev.get("structure")
        day = _day(ev.get("ts"))
        if not isinstance(sid, str) or day is None or sid not in entry_fills:
            continue
        try:
            filled = int(ev["filled"])
            avg = float(ev["avg"])
        except (KeyError, TypeError, ValueError):
            continue
        increment = filled - prev_filled.get(sid, 0)
        prev_filled[sid] = filled
        if increment <= 0:
            continue
        out[day] = out.get(day, 0.0) + (avg - entry_fills[sid]) * increment * 100
    return out


def unrealized_eod_by_day(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Last total_unrealized sample per ISO date (one plan's history)."""
    last: dict[str, float] = {}
    for row in rows:
        day = _day(row.get("ts"))
        if day is None:
            continue
        try:
            val = float(row["total_unrealized"])
        except (KeyError, TypeError, ValueError):
            continue
        last[day] = val  # rows are chronological; later samples win
    return last


def stats_payload(
    state_root: Path,
    plans_root: Path,
    discovery_root: Path,
    now: datetime,
) -> dict[str, Any]:
    """GET /api/stats body. Plain numbers at the boundary."""
    from tree_options.trex_web.reader import read_account_history

    views = list_plans(state_root, plans_root)
    equity_rows = read_account_history(discovery_root)

    realized_days: dict[str, float] = {}
    unrealized_days: dict[str, float] = {}
    per_plan: list[dict[str, Any]] = []
    per_structure: list[dict[str, Any]] = []
    totals_realized = 0.0
    totals_unrealized_last = 0.0
    structures_closed = 0
    wins = 0
    losses = 0
    tracking_since: str | None = None

    for view in views:
        plan_realized = 0.0
        plan_unrealized_last: float | None = None
        marks_rows = read_marks_history(state_root, view.plan.id)
        events = read_tail(state_root / view.plan.id / "events.jsonl")
        entry_fills = {
            sid: float(st.entry_fill)
            for sid, st in view.structures.items()
            if st is not None and st.entry_fill is not None
        }
        for day, val in realized_by_day(events, entry_fills).items():
            realized_days[day] = realized_days.get(day, 0.0) + val
            plan_realized += val
        for day, val in unrealized_eod_by_day(marks_rows).items():
            unrealized_days[day] = unrealized_days.get(day, 0.0) + val
        if marks_rows:
            try:
                plan_unrealized_last = float(str(marks_rows[-1]["total_unrealized"]))
            except (KeyError, TypeError, ValueError):
                plan_unrealized_last = None
        first_ts: str | None = None
        last_ts: str | None = None
        for spec in view.plan.structures:
            sid = spec.id
            st = view.structures.get(sid)
            realized = None
            if st is not None:
                realized = (
                    float(st.realized_pnl)
                    if st.realized_pnl is not None
                    else None
                )
                if st.state.value == "closed":
                    structures_closed += 1
                    if realized is not None:
                        wins += realized > 0
                        losses += realized < 0
            per_structure.append(
                {
                    "plan_id": view.plan.id,
                    "structure_id": sid,
                    "underlying": spec.underlying,
                    "status": st.state.value if st is not None else "planned",
                    "entry_fill": (
                        float(st.entry_fill)
                        if st is not None and st.entry_fill is not None
                        else None
                    ),
                    "filled_qty": st.filled_qty if st is not None else 0,
                    "realized": realized,
                }
            )
            if st is not None and st.updated_at is not None:
                iso = st.updated_at.isoformat()
                last_ts = iso if last_ts is None else max(last_ts, iso)
                first_ts = iso if first_ts is None else min(first_ts, iso)
        if marks_rows and marks_rows[0].get("ts"):
            ts0 = str(marks_rows[0]["ts"])
            first_ts = ts0 if first_ts is None else min(first_ts, ts0)
        totals_realized += plan_realized
        if plan_unrealized_last is not None:
            totals_unrealized_last += plan_unrealized_last
        per_plan.append(
            {
                "plan_id": view.plan.id,
                "realized": plan_realized,
                "unrealized_last": plan_unrealized_last,
                "structures_closed": sum(
                    1
                    for st in view.structures.values()
                    if st is not None and st.state.value == "closed"
                ),
                "first_ts": first_ts,
                "last_ts": last_ts,
            }
        )

    for row in equity_rows:
        ts = row.get("ts")
        if isinstance(ts, str):
            tracking_since = ts if tracking_since is None else min(tracking_since, ts)
    for view in views:
        rows = read_marks_history(state_root, view.plan.id)
        if rows and isinstance(rows[0].get("ts"), str):
            ts0 = str(rows[0]["ts"])
            tracking_since = ts0 if tracking_since is None else min(tracking_since, ts0)

    days: list[dict[str, Any]] = []
    for day in sorted(set(realized_days) | set(unrealized_days)):
        realized = realized_days.get(day, 0.0)
        unrealized = unrealized_days.get(day)
        days.append(
            {
                "date": day,
                "realized": realized,
                "unrealized_eod": unrealized,  # None = gap, never zero
                "total": realized + unrealized if unrealized is not None else None,
            }
        )
    best_day = max((d["total"] for d in days if d["total"] is not None), default=None)
    worst_day = min((d["total"] for d in days if d["total"] is not None), default=None)
    closed_with_pnl = wins + losses

    return {
        "now": now.isoformat(),
        "tracking_since": tracking_since,
        "equity": equity_series(equity_rows),
        "days": days,
        "totals": {
            "realized": totals_realized,
            "unrealized_last": totals_unrealized_last if views else None,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / closed_with_pnl) if closed_with_pnl else None,
            "best_day": best_day,
            "worst_day": worst_day,
            "plans_tracked": len(views),
            "structures_closed": structures_closed,
        },
        "per_plan": per_plan,
        "per_structure": per_structure,
    }
