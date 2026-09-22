"""Cross-plan portfolio rollup (pure; broker-free).

Unrealized is computed on TWO bases and both are served, distinctly
named (codex-review trap #6/#18):

- ``unrealized_open``   remaining contracts: (mark - entry) * open_qty * 100
- ``unrealized_filled`` the monitor's marks.json total (filled basis)

A partially exited structure values only what is still held in the open
basis — the filled basis would keep counting contracts already sold.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

MULT = 100
DEFAULT_MARKS_STALE_AFTER = 120  # seconds


def _f(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw))
    except ValueError:
        return None


def plan_unrealized(
    view: Any, marks: dict[str, Any] | None
) -> tuple[float | None, float | None]:
    """(open-basis, filled-basis) unrealized for one plan."""
    if marks is None:
        return None, None
    filled_total = _f(marks.get("total_unrealized"))
    rows = marks.get("structures")
    open_total = 0.0
    any_row = False
    if isinstance(rows, dict):
        for sid, row in rows.items():
            st = view.structures.get(sid)
            if st is None:
                continue
            open_qty = st.open_qty
            if open_qty <= 0:
                continue
            mark = _f(row.get("mark"))
            entry = _f(row.get("entry"))
            if mark is None or entry is None:
                continue
            open_total += (mark - entry) * open_qty * MULT
            any_row = True
    return (open_total if any_row else None), filled_total


def plan_realized(view: Any) -> tuple[float | None, int]:
    """Summed realized P&L + how many structures exited partially."""
    total = 0.0
    any_realized = False
    partial = 0
    for st in view.structures.values():
        if st.realized_pnl is not None:
            total += float(st.realized_pnl)
            any_realized = True
            if st.open_qty > 0:
                partial += 1
    return (total if any_realized else None), partial


def portfolio_rollup(
    entries: list[tuple[Any, dict[str, Any] | None]],
    *,
    marks_stale_after: int = DEFAULT_MARKS_STALE_AFTER,
    now: datetime | None = None,
) -> dict[str, Any]:
    """entries: (PlanView, raw marks payload | None); dedupes plan ids.

    Returns the top-level ``portfolio`` payload for /api/plans.
    """
    seen: set[str] = set()
    open_qty = 0
    committed_caps = 0.0
    committed_filled = 0.0
    unrealized_open = 0.0
    any_open = False
    unrealized_filled = 0.0
    any_filled = False
    realized = 0.0
    any_realized = False
    partial_count = 0
    plans_with_state = 0
    worst_rank = -1
    worst = None
    marks_age: int | None = None
    modes: dict[str, dict[str, Any]] = {}

    for view, marks in entries:
        if view.plan.id in seen:
            continue  # duplicate plan TOMLs must not double-count
        seen.add(view.plan.id)
        mode = str(view.plan.account_mode)
        bucket = modes.setdefault(
            mode,
            {
                "open_qty": 0,
                "committed_filled": 0.0,
                "unrealized_open": 0.0,
                "unrealized_filled": 0.0,
                "realized": 0.0,
            },
        )
        if view.state_present:
            plans_with_state += 1
        u_open, u_filled = plan_unrealized(view, marks)
        realized_sum, partial = plan_realized(view)
        partial_count += partial
        plan_open = 0
        for st in view.structures.values():
            plan_open += st.open_qty
            if st.entry_fill is not None:
                committed_filled += float(st.entry_fill) * st.filled_qty * MULT
                bucket["committed_filled"] += float(st.entry_fill) * st.filled_qty * MULT
        open_qty += plan_open
        bucket["open_qty"] += plan_open
        committed_caps += float(view.plan.committed_at_caps)
        if u_open is not None:
            unrealized_open += u_open
            bucket["unrealized_open"] += u_open
            any_open = True
        if u_filled is not None:
            unrealized_filled += u_filled
            bucket["unrealized_filled"] += u_filled
            any_filled = True
        if realized_sum is not None:
            realized += realized_sum
            bucket["realized"] += realized_sum
            any_realized = True
        if view.worst_state is not None:
            rank = {"open": 4, "enter_working": 3, "exit_working": 2, "closed": 1}.get(
                view.worst_state.value, 0
            )
            if rank > worst_rank:
                worst_rank, worst = rank, view.worst_state.value
        if marks is not None:
            from tree_options.trex_web.reader import marks_age_seconds

            age = marks_age_seconds(marks)
            if age is not None and (marks_age is None or age < marks_age):
                marks_age = age

    return {
        "plans_count": len(seen),
        "plans_with_state": plans_with_state,
        "open_qty": open_qty,
        "committed_at_caps": committed_caps,
        "committed_filled": committed_filled,
        "unrealized_open": unrealized_open if any_open else None,
        "unrealized_filled": unrealized_filled if any_filled else None,
        "realized": realized if any_realized else None,
        "realized_partial_count": partial_count,
        "marks_age_seconds": marks_age,
        "marks_stale": marks_age is None or marks_age > marks_stale_after,
        "worst_state": worst,
        "by_account_mode": modes,
    }
