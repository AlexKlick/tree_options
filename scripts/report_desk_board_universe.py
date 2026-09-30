#!/usr/bin/env python3
"""Describe a bundle vintage's boards and outcome table (board universe).

Per candidate: structure, width, short-strike moneyness against the as-of
parity spot (itm / atm within +-0.5% / otm), credit as % of width, and a
short-leg delta proxy (Black-Scholes, r = 0, the bundle's prior-session
implied-vol index; absent without ``iv_context``). Joined to the outcome
table: no-fill share and mean gross/net per class and exit mode, plus how
many rows of the stratified v2 board (what a model sees) are OTM credits.
Descriptive only: no claim of edge. No network.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from statistics import NormalDist
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tree_options.desk import board_universe as bu  # noqa: E402
from tree_options.desk import intraday_action_graph as iag  # noqa: E402
from tree_options.desk import lab, outcomes  # noqa: E402

CREDIT_BUCKETS = (10, 15, 25, 35, 50)
DELTA_BUCKETS = (10, 15, 30, 50)


def _bucket(value: float, edges: tuple[int, ...]) -> str:
    low = 0
    for edge in edges:
        if value < edge:
            return f"{low}-{edge}"
        low = edge
    return f">={low}"


def _moneyness(right: str, strike: Decimal, spot: Decimal | None) -> tuple[str, float | None]:
    if spot is None:
        return "unknown", None
    pct = float((strike / spot - 1) * 100)
    otm = -pct if right == "P" else pct  # positive = out of the money
    return ("otm" if otm > 0.5 else "itm" if otm < -0.5 else "atm"), otm


def _delta(right: str, strike: Decimal, spot: Decimal, sigma: float, years: float) -> float:
    d1 = (math.log(float(spot / strike)) + sigma * sigma * years / 2) / (sigma * math.sqrt(years))
    call = NormalDist().cdf(d1)
    return abs(call if right == "C" else call - 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--table", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    raw = json.loads(args.bundle.read_bytes())
    index = outcomes.prepare_index(raw)
    classes: dict[tuple[str, str], dict[str, Any]] = {}
    mix: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    credit_pct: dict[str, list[float]] = defaultdict(list)
    delta_hist: dict[str, Counter[str]] = defaultdict(Counter)
    coverage: dict[str, int] = Counter()
    v2_rows = Counter()
    boards = 0
    for day, clock, at in index.timeline:
        candidates = outcomes.board_candidates(index, day, clock)
        if not candidates:
            continue
        boards += 1
        snapshot = f"s:{day.isoformat()}T{clock}"
        spot = outcomes.spot_asof(index, at)
        for underlying in {c["underlying"] for c in candidates}:
            coverage[underlying] += 1
        for c in candidates:
            short = iag.parse_contract(c["short"])
            bucket, _ = _moneyness(short.right, short.strike, spot.get(c["underlying"]))
            width = str(Decimal(c["width"]).normalize())
            structure = c["structure"]
            credit = structure.endswith("credit")
            info = {
                "structure": structure,
                "width": width,
                "moneyness": bucket,
                "otm_credit": credit and bucket == "otm",
            }
            mix[structure][width][bucket] += 1
            if credit:
                pct = float(Decimal(c["observed_premium"]) / Decimal(c["width"]) * 100)
                credit_pct[structure].append(pct)
                if bucket == "otm":
                    credit_pct[f"{structure}:otm"].append(pct)
                ivs = index.iv.get(c["underlying"], {})
                iv = bu.iv_prev_close(ivs, day) if ivs else None
                dte = (short.expiry - day).days
                if iv is not None and spot.get(c["underlying"]) is not None and dte > 0:
                    d = (
                        _delta(
                            short.right,
                            short.strike,
                            spot[c["underlying"]],
                            float(iv) / 100,
                            dte / 365,
                        )
                        * 100
                    )
                    delta_hist[structure][_bucket(d, DELTA_BUCKETS)] += 1
            classes[(snapshot, c["id"])] = info
        context = lab.board_context(index, day, clock)
        for row in lab.board_rows_v2({"as_of": at.isoformat(), "candidates": candidates}, context):
            v2_rows["rows"] += 1
            if classes[(snapshot, row["id"])]["otm_credit"]:
                v2_rows["otm_credit_rows"] += 1
    agg: dict[str, dict[str, dict[str, Any]]] = defaultdict(
        lambda: defaultdict(lambda: {"rows": 0, "no_fill": 0, "gross": [], "net": []})
    )
    reasons: dict[str, Counter[str]] = defaultdict(Counter)
    with args.table.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            info = classes[(row["snapshot"], row["candidate_id"])]
            keys = ["all", f"{info['structure']}:{info['moneyness']}"]
            if info["otm_credit"]:
                keys.append("otm_credit")
            for key in keys:
                cell = agg[row["exit_mode"]][key]
                cell["rows"] += 1
                if row["status"] == "no_fill":
                    cell["no_fill"] += 1
                    if row["exit_mode"] == "intraday":
                        reasons[key][row["exit_reason"]] += 1
                else:
                    cell["gross"].append(float(row["gross"]))
                    cell["net"].append(float(row["net"]))

    def summary(cell: dict[str, Any]) -> dict[str, Any]:
        filled = len(cell["net"])
        return {
            "rows": cell["rows"],
            "no_fill_share": round(cell["no_fill"] / cell["rows"], 4),
            "filled": filled,
            "mean_gross": round(statistics.fmean(cell["gross"]), 3) if filled else None,
            "mean_net": round(statistics.fmean(cell["net"]), 3) if filled else None,
        }

    def quantiles(values: list[float]) -> dict[str, Any]:
        if not values:
            return {"n": 0}
        cuts = statistics.quantiles(values, n=10) if len(values) > 1 else [values[0]] * 9
        return {
            "n": len(values),
            "p10": round(cuts[0], 1),
            "p50": round(statistics.median(values), 1),
            "p90": round(cuts[-1], 1),
            "hist": dict(sorted(Counter(_bucket(v, CREDIT_BUCKETS) for v in values).items())),
        }

    total = sum(sum(sum(b.values()) for b in widths.values()) for widths in mix.values())
    otm_share = {}
    for structure in ("put_credit", "call_credit", "put_debit", "call_debit"):
        counts = Counter[str]()
        for width in mix.get(structure, {}).values():
            counts.update(width)
        n = sum(counts.values())
        otm_share[structure] = {
            "n": n,
            "otm_gt_0.5pct": round(counts["otm"] / n, 4) if n else None,
            "atm_within_0.5pct": round(counts["atm"] / n, 4) if n else None,
        }
    document = {
        "bundle": str(args.bundle),
        "table": str(args.table),
        "sessions": [
            index.sessions[0].isoformat(),
            index.sessions[-1].isoformat(),
            len(index.sessions),
        ],
        "boards_with_candidates": boards,
        "scheduled_boards": len(index.timeline),
        "candidates": total,
        "board_coverage_by_underlying": dict(sorted(coverage.items())),
        "candidates_by_structure_width_moneyness": {
            s: {w: dict(sorted(b.items())) for w, b in sorted(widths.items())}
            for s, widths in sorted(mix.items())
        },
        "short_strike_moneyness_share": otm_share,
        "credit_pct_of_width": {k: quantiles(v) for k, v in sorted(credit_pct.items())},
        "credit_short_delta_proxy_pct": {
            k: dict(sorted(v.items())) for k, v in sorted(delta_hist.items())
        },
        "v2_board_rows": {
            "rows": v2_rows["rows"],
            "otm_credit_rows": v2_rows["otm_credit_rows"],
            "otm_credit_share": round(v2_rows["otm_credit_rows"] / v2_rows["rows"], 4)
            if v2_rows["rows"]
            else None,
        },
        "outcomes": {
            mode: {key: summary(cell) for key, cell in sorted(cells.items())}
            for mode, cells in agg.items()
        },
        "intraday_no_fill_reasons": {
            k: dict(sorted(v.items())) for k, v in sorted(reasons.items())
        },
        "note": "descriptive; trade-bar proxies; net = gross - CostModel round trip; not an edge claim",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: document[k]
                for k in (
                    "sessions",
                    "boards_with_candidates",
                    "candidates",
                    "short_strike_moneyness_share",
                    "v2_board_rows",
                )
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
