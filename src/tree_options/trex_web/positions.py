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


def net_positions(
    specs: list[dict[str, Any]],
    states: dict[str, dict[str, Any]],
    marks: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """One row per underlying with open exposure.

    ``specs`` are the plan's structure dicts (id, underlying, strikes,
    expiry), ``states`` the per-structure state views, ``marks`` the
    per-structure mark rows (only ``unrealized`` is read here).
    """
    groups: dict[str, dict[str, Any]] = {}
    for s in specs:
        st = states.get(str(s["id"]))
        if not st:
            continue
        open_qty = int(st.get("open_qty") or 0)
        entry = st.get("entry_fill")
        if open_qty <= 0 or entry is None:
            continue
        entry_f = float(entry)
        long_f = float(s["long_strike"])
        short_f = float(s["short_strike"])
        row = groups.setdefault(
            str(s["underlying"]),
            {
                "underlying": str(s["underlying"]),
                "structure_count": 0,
                "open_qty": 0,
                "committed": 0.0,
                "unrealized_vals": [],
                "short_floor": short_f,
                "long_ceiling": long_f,
                "max_gain": 0.0,
                "legs": [],
            },
        )
        row["structure_count"] += 1
        row["open_qty"] += open_qty
        row["committed"] += entry_f * open_qty * MULT
        row["max_gain"] += (long_f - short_f - entry_f) * open_qty * MULT
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
            }
        )
        unrealized = (marks or {}).get(str(s["id"]), {}).get("unrealized")
        if unrealized is not None:
            row["unrealized_vals"].append(float(unrealized))

    rows: list[dict[str, Any]] = []
    for row in groups.values():
        vals: list[float] = row.pop("unrealized_vals")
        open_qty = int(row["open_qty"])
        row["avg_entry"] = row["committed"] / open_qty / MULT if open_qty else None
        row["max_loss"] = -float(row["committed"])
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
                    "max_gain": 0.0,
                    "short_floor": row["short_floor"],
                    "long_ceiling": row["long_ceiling"],
                    "legs": [],
                    "unrealized_vals": [],
                },
            )
            target["structure_count"] += int(row["structure_count"])
            target["open_qty"] += int(row["open_qty"])
            target["committed"] += float(row["committed"])
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
        row["avg_entry"] = row["committed"] / open_qty / MULT if open_qty else None
        row["max_loss"] = -float(row["committed"])
        row["unrealized"] = sum(vals) if vals else None
        rows.append(row)
    rows.sort(key=lambda r: str(r["underlying"]))
    return rows
