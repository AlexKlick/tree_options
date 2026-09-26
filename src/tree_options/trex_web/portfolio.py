"""Cross-plan portfolio rollup (pure; broker-free).

Unrealized is computed on TWO bases and both are served, distinctly
named (codex-review trap #6/#18):

- ``unrealized_open``   remaining contracts: (mid - entry) * open_qty * 100,
                        mid = the unrounded bid/ask mid (``open_unrealized``)
- ``unrealized_filled`` the monitor's marks.json total (filled basis)

A partially exited structure values only what is still held in the open
basis — the filled basis would keep counting contracts already sold.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from tree_options.trex_web.positions import open_unrealized

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
    """(open-basis, filled-basis) unrealized for one plan.

    Both are None while any structure's entry price coverage is incomplete
    (R3-02): a priced-subset average must not be rescaled to the whole open
    quantity, and a stale complete mark proves nothing about the current
    book's newly added unpriced fills."""
    if marks is None:
        return None, None
    filled_total = _f(marks.get("total_unrealized"))
    rows = marks.get("structures")
    open_total = 0.0
    any_row = False
    coverage_incomplete = False
    if isinstance(rows, dict):
        for sid, row in rows.items():
            st = view.structures.get(sid)
            if st is None:
                continue
            if getattr(st, "entry_unpriced_qty", 0):
                coverage_incomplete = True
                continue
            entry = _f(row.get("entry"))
            if entry is None:
                continue
            value = open_unrealized(row, entry, st.open_qty, int(st.filled_qty or 0))
            if value is None:
                continue
            open_total += value
            any_row = True
    if coverage_incomplete:
        return None, None
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
    # R3-02: whole-book cost is unknown while any filled structure's entry
    # price coverage is incomplete; the priced subset stays available as a
    # separately labelled subtotal, never silently substituted.
    committed_known = 0.0
    cost_unknown = False
    unpriced_qty = 0
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
                "committed_known": 0.0,
                "cost_unknown": False,
                "unpriced_qty": 0,
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
            unpriced = getattr(st, "entry_unpriced_qty", 0)
            if st.entry_fill is not None and not unpriced:
                committed_filled += float(st.entry_fill) * st.filled_qty * MULT
                bucket["committed_filled"] += float(st.entry_fill) * st.filled_qty * MULT
                committed_known += float(st.entry_fill) * st.filled_qty * MULT
                bucket["committed_known"] += float(st.entry_fill) * st.filled_qty * MULT
            elif st.filled_qty > 0:
                # a priced-subset average times the whole fill would fabricate
                # cost: the whole-book number is unknown (R3-02)
                cost_unknown = True
                bucket["cost_unknown"] = True
                unpriced_qty += unpriced
                bucket["unpriced_qty"] += unpriced
                if st.entry_fill is not None:
                    known = float(st.entry_fill) * (st.filled_qty - unpriced) * MULT
                    committed_known += known
                    bucket["committed_known"] += known
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

    for bucket in modes.values():
        if bucket["cost_unknown"]:
            bucket["committed_filled"] = None  # not a whole-bucket number

    return {
        "plans_count": len(seen),
        "plans_with_state": plans_with_state,
        "open_qty": open_qty,
        "committed_at_caps": committed_caps,
        "committed_filled": None if cost_unknown else committed_filled,
        "committed_known": committed_known,
        "cost_unknown": cost_unknown,
        "unpriced_qty": unpriced_qty,
        "unrealized_open": unrealized_open if any_open else None,
        "unrealized_filled": unrealized_filled if any_filled else None,
        "realized": realized if any_realized else None,
        "realized_partial_count": partial_count,
        "marks_age_seconds": marks_age,
        "marks_stale": marks_age is None or marks_age > marks_stale_after,
        "worst_state": worst,
        "by_account_mode": modes,
    }
