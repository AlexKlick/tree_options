"""Net-exposure rollup for the cockpit (broker-free, pure).

Groups filled structures by underlying so the operator sees the book the
way risk actually aggregates: NVDA oct + nov are two structures but one
underlying exposure. All exposure metrics are computed on ``open_qty``
(filled minus exited) — a fully-exited structure drops out, a partial
exit shrinks its row. Unfilled structures never appear.
"""

from __future__ import annotations

from typing import Any

MULT = 100


def _num(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw))
    except ValueError:
        return None


def open_unrealized(
    row: dict[str, Any] | None, entry: float, open_qty: int, filled_qty: int
) -> float | None:
    """Unrealized on the contracts still OPEN, from the exact quote mid.

    The one calculation behind the portfolio tile, the plan card and the
    net-positions table (they disagreed live on 2026-09-22: +$1 vs -$3).
    Order: the unrounded bid/ask mid; else the stored ``mark`` (rounded to
    the cent by the monitor); else the monitor's FILLED-basis
    ``unrealized`` rescaled to open_qty. None when the row has no quote.
    """
    if not row or open_qty <= 0:
        return None
    bid, ask = _num(row.get("bid")), _num(row.get("ask"))
    mid = (bid + ask) / 2 if bid is not None and ask is not None else _num(row.get("mark"))
    if mid is not None:
        return (mid - entry) * open_qty * MULT
    filled_basis = _num(row.get("unrealized"))
    if filled_basis is None or filled_qty <= 0:
        return None
    return filled_basis * open_qty / filled_qty


def net_positions(
    specs: list[dict[str, Any]],
    states: dict[str, dict[str, Any]],
    marks: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """One row per underlying with open exposure.

    ``specs`` are the plan's structure dicts (id, underlying, strikes,
    expiry), ``states`` the per-structure state views, ``marks`` the
    per-structure mark rows (valued by ``open_unrealized``).
    """
    groups: dict[str, dict[str, Any]] = {}
    for s in specs:
        st = states.get(str(s["id"]))
        if not st:
            continue
        open_qty = int(st.get("open_qty") or 0)
        if open_qty <= 0:
            continue
        entry = st.get("entry_fill")
        unpriced = int(st.get("entry_unpriced_qty") or 0)
        entry_f = float(entry) if entry is not None else None
        long_f = float(s["long_strike"])
        short_f = float(s["short_strike"])
        row = groups.setdefault(
            str(s["underlying"]),
            {
                "underlying": str(s["underlying"]),
                "structure_count": 0,
                "open_qty": 0,
                "committed": 0.0,  # complete-coverage cost only (None else)
                "committed_known": 0.0,  # labelled priced subtotal (R3-02)
                "unpriced_qty": 0,
                "cost_unknown": False,
                "unrealized_vals": [],
                "short_floor": short_f,
                "long_ceiling": long_f,
                "max_gain": 0.0,
                "legs": [],
            },
        )
        row["structure_count"] += 1
        row["open_qty"] += open_qty
        row["unpriced_qty"] += unpriced
        # An unknown entry cost is NOT an absent position (R3-02): the row
        # and its legs stay visible; entry-derived aggregates become unknown
        # and the priced subset is kept as a labelled subtotal.
        if entry_f is not None and not unpriced:
            row["committed"] += entry_f * open_qty * MULT
            row["committed_known"] += entry_f * open_qty * MULT
            row["max_gain"] += (long_f - short_f - entry_f) * open_qty * MULT
        else:
            row["cost_unknown"] = True
            if entry_f is not None:
                row["committed_known"] += entry_f * (open_qty - unpriced) * MULT
        row["short_floor"] = min(row["short_floor"], short_f)
        row["long_ceiling"] = max(row["long_ceiling"], long_f)
        row["legs"].append(
            {
                "structure_id": s["id"],
                "expiry": str(s["expiry"]),
                "long_strike": long_f,
                "short_strike": short_f,
                "open_qty": open_qty,
                "entry": entry_f,
                "entry_unpriced_qty": unpriced,
            }
        )
        if entry_f is not None and not unpriced:
            unrealized = open_unrealized(
                (marks or {}).get(str(s["id"])),
                entry_f,
                open_qty,
                int(st.get("filled_qty") or 0),
            )
            if unrealized is not None:
                row["unrealized_vals"].append(unrealized)

    rows: list[dict[str, Any]] = []
    for row in groups.values():
        vals: list[float] = row.pop("unrealized_vals")
        open_qty = int(row["open_qty"])
        unknown = bool(row["cost_unknown"])
        row["committed"] = None if unknown else row["committed"]
        row["max_gain"] = None if unknown else row["max_gain"]
        row["avg_entry"] = (
            row["committed"] / open_qty / MULT
            if open_qty and not unknown and row["committed"] is not None
            else None
        )
        row["max_loss"] = None if unknown or row["committed"] is None else -float(row["committed"])
        row["unrealized"] = sum(vals) if vals else None
        rows.append(row)
    return rows


def merge_net_positions(
    rows_per_plan: list[tuple[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Portfolio-level net positions: merge per-plan rows by underlying.

    Feeds the plans index, where the operator expects to see current
    positions without clicking into a plan. ``rows_per_plan`` carries the
    owning plan id per row list so a structure id colliding across plans
    (nvda-oct exists in every weekly book) is disambiguated instead of
    producing duplicate React keys downstream.
    """
    merged: dict[str, dict[str, Any]] = {}
    for plan_id, plan_rows in rows_per_plan:
        for row in plan_rows:
            key = str(row["underlying"])
            target = merged.setdefault(
                key,
                {
                    "underlying": key,
                    "structure_count": 0,
                    "open_qty": 0,
                    "committed": 0.0,
                    "committed_known": 0.0,
                    "unpriced_qty": 0,
                    "cost_unknown": False,
                    "max_gain": 0.0,
                    "short_floor": row["short_floor"],
                    "long_ceiling": row["long_ceiling"],
                    "legs": [],
                    "unrealized_vals": [],
                },
            )
            target["structure_count"] += int(row["structure_count"])
            target["open_qty"] += int(row["open_qty"])
            target["unpriced_qty"] += int(row.get("unpriced_qty") or 0)
            if row.get("committed") is not None:
                target["committed"] += float(row["committed"])
            else:
                # one unknown leg makes the whole merged cost unknown (R3-02)
                target["cost_unknown"] = True
            target["committed_known"] += float(row.get("committed_known") or 0.0)
            if row.get("max_gain") is not None:
                target["max_gain"] += float(row["max_gain"])
            target["short_floor"] = min(target["short_floor"], row["short_floor"])
            target["long_ceiling"] = max(target["long_ceiling"], row["long_ceiling"])
            for leg in row["legs"]:
                leg = dict(leg)
                sid = str(leg["structure_id"])
                if any(str(other["structure_id"]) == sid for other in target["legs"]):
                    leg["structure_id"] = f"{plan_id}/{sid}"
                target["legs"].append(leg)
            if row["unrealized"] is not None:
                target["unrealized_vals"].append(float(row["unrealized"]))

    rows: list[dict[str, Any]] = []
    for row in merged.values():
        vals: list[float] = row.pop("unrealized_vals")
        open_qty = int(row["open_qty"])
        unknown = bool(row["cost_unknown"])
        row["committed"] = None if unknown else row["committed"]
        row["max_gain"] = None if unknown else row["max_gain"]
        row["avg_entry"] = (
            row["committed"] / open_qty / MULT
            if open_qty and not unknown and row["committed"] is not None
            else None
        )
        row["max_loss"] = None if unknown or row["committed"] is None else -float(row["committed"])
        row["unrealized"] = sum(vals) if vals else None
        rows.append(row)
    rows.sort(key=lambda r: str(r["underlying"]))
    return rows
