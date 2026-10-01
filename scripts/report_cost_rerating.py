#!/usr/bin/env python
"""Re-rate a run's arms at the MEASURED shaped cost, beside the flat model.

The input is one JSON object per ENTERED entry, carrying the entry's gross,
its dte, and each leg's real ticker/strike plus an ``|delta|`` recovered by
inverting Black-Scholes on that leg's own last-traded-minute close. Deltas
are NOT synthesised from a volatility assumption: this file prices what the
run actually traded.

This script deliberately calls the SHIPPED ``desk.cost`` model rather than
re-implementing the surface, so the table it prints is evidence about the
module that ships, not about a second copy of the arithmetic.

Refusals are FAIL-CLOSED: an entry with a leg outside the measured universe
(|delta| > 0.70, or an unmeasured symbol/dte) is DROPPED and counted, never
priced at the boundary cell. Both the flat and the shaped column are computed
over the same priced subset, so the two columns differ only in the cost.

Usage:
    report_cost_rerating.py <leg-deltas.jsonl> [--json OUT]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tree_options.desk.cost import SpreadCostModel, UnpricedCostError  # noqa: E402

#: outcomes.CostModel().round_trip() -- 4 fills x $0.03 x 100 + 4 x $0.65.
FLAT = Decimal("14.60")
EOD = "cboe-delayed-eod"


def symbol_of(ticker: str) -> str:
    """``O:SPY251121P00661000`` -> ``SPY``. The three tradeable roots are all
    three characters, so this is unambiguous."""
    body = ticker.split(":")[-1]
    return body[:3].upper()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("legs_jsonl", type=Path)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    model = SpreadCostModel.measured()
    per_arm: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "dropped": 0, "gross": 0.0, "flat_net": 0.0,
                 "shaped_net": 0.0, "cost": 0.0, "reasons": defaultdict(int),
                 "n_flat_entries": 0, "arm_cost": 0.0})
    seen = set()

    with args.legs_jsonl.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            arm = str(entry["arm"])
            key = (arm, str(entry["id"]), str(entry.get("status")))
            if key in seen:                       # the same entry, re-listed
                continue
            seen.add(key)
            row = per_arm[arm]
            legs = entry.get("legs")
            dte = int(entry.get("days") or entry.get("dte") or 0)
            if not isinstance(legs, list) or not legs:
                # The input could not recover this entry's legs at all. That
                # is a DATA gap, not a cost-model refusal, and conflating the
                # two would understate how much the model actually refused.
                row["dropped"] += 1
                row["reasons"]["legs_unrecoverable"] += 1
                continue
            if any(not isinstance(leg, dict) or leg.get("ad") is None for leg in legs):
                row["dropped"] += 1
                row["reasons"]["delta_unrecoverable"] += 1
                continue
            try:
                cost = model.round_trip([
                    _leg(symbol_of(str(leg["ticker"])), leg["ad"], dte)
                    for leg in legs])
            except UnpricedCostError as refused:
                row["dropped"] += 1
                row["reasons"][refused.reason] += 1
                continue
            gross = float(entry.get("gross") or 0.0)
            row["n"] += 1
            row["n_flat_entries"] += 1
            row["gross"] += gross
            row["cost"] += float(cost)
            row["arm_cost"] += float(cost)
            row["shaped_net"] += gross - float(cost)
            row["flat_net"] += gross - float(FLAT)

    total_priced = sum(r["n"] for r in per_arm.values())
    total_dropped = sum(r["dropped"] for r in per_arm.values())
    total_cost = sum(r["cost"] for r in per_arm.values())
    mean_cost = total_cost / total_priced if total_priced else 0.0

    arms = []
    for arm, row in sorted(per_arm.items(), key=lambda kv: -kv[1]["flat_net"]):
        # The LEVEL control: the same run-wide mean cost charged as a FLAT
        # SCALAR, i.e. the shaped model with all of its shape removed. If
        # this column reproduces the shaped column's conclusions, the shape
        # buys nothing that a one-line constant change would not.
        level_net = row["gross"] - mean_cost * row["n"]
        arms.append({
            "arm": arm, "priced": row["n"], "dropped": row["dropped"],
            "gross": round(row["gross"], 2),
            "flat_net": round(row["flat_net"], 2),
            "level_net": round(level_net, 2),
            "shaped_net": round(row["shaped_net"], 2),
            "delta": round(row["shaped_net"] - row["flat_net"], 2),
            "mean_shaped_cost": round(row["cost"] / row["n"], 2) if row["n"] else None,
            "reasons": dict(sorted(row["reasons"].items())),
        })

    total_priced = sum(a["priced"] for a in arms)
    total_dropped = sum(a["dropped"] for a in arms)
    summary = {
        "arms": len(arms),
        "priced_entries": total_priced,
        "dropped_entries": total_dropped,
        "mean_shaped_cost": round(mean_cost, 4),
        "flat_cost": str(FLAT),
        "level_multiple": round(mean_cost / float(FLAT), 4) if mean_cost else None,
        "profitable_flat": [a["arm"] for a in arms if a["flat_net"] > 0],
        "profitable_level": [a["arm"] for a in arms if a["level_net"] > 0],
        "profitable_shaped": [a["arm"] for a in arms if a["shaped_net"] > 0],
        "sign_flips": [a["arm"] for a in arms
                       if (a["flat_net"] > 0) != (a["shaped_net"] > 0)],
        "shape_adds_nothing": sorted(a["arm"] for a in arms
                                     if a["level_net"] > 0) == sorted(
                                         a["arm"] for a in arms if a["shaped_net"] > 0),
        "provenance": {
            "cost_source": EOD,
            "deltas": "inverted from each leg's own last-traded-minute close "
                      "(Black-Scholes, rate=0, divy=0, T=dte/365); not synthesised",
            "refusal_policy": "fail-closed: out-of-universe legs are DROPPED and "
                              "counted, never priced at the boundary cell",
            "caveat": "EOD snapshots cannot describe a 10:00-15:15 ET fill",
        },
    }
    doc = {"summary": summary, "arms": arms}
    if args.json is not None:
        args.json.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    print(f"arms={summary['arms']}  priced={summary['priced_entries']}  "
          f"dropped={summary['dropped_entries']}  "
          f"mean shaped cost=${summary['mean_shaped_cost']}  "
          f"({summary['level_multiple']}x the flat ${FLAT})")
    print(f"profitable flat   ({len(summary['profitable_flat'])}): "
          f"{summary['profitable_flat']}")
    print(f"profitable LEVEL  ({len(summary['profitable_level'])}): "
          f"{summary['profitable_level']}   <- shape removed, scalar only")
    print(f"profitable shaped ({len(summary['profitable_shaped'])}): "
          f"{summary['profitable_shaped']}")
    print(f"sign flips ({len(summary['sign_flips'])}): {summary['sign_flips']}")
    print(f"SHAPE ADDS NOTHING OVER A SCALAR: {summary['shape_adds_nothing']}")
    print()
    header = (f"{'arm':<42} {'n':>5} {'drop':>5} {'flat net':>10} "
              f"{'level net':>10} {'shaped net':>11} {'mean cost':>10}")
    print(header)
    print("-" * len(header))
    for a in arms:
        print(f"{a['arm'][:42]:<42} {a['priced']:>5} {a['dropped']:>5} "
              f"{a['flat_net']:>10,.0f} {a['level_net']:>10,.0f} "
              f"{a['shaped_net']:>11,.0f} {a['mean_shaped_cost']:>10,.2f}")
    return 0


def _leg(symbol: str, abs_delta: Any, dte: int) -> Any:
    from tree_options.desk.cost import Leg
    return Leg(symbol=symbol, abs_delta=Decimal(str(abs_delta)), dte=dte,
               source_session=EOD, source_timestamp_et="2026-09-30T18:00:00-04:00",
               is_eod_snapshot=True)


if __name__ == "__main__":
    raise SystemExit(main())
