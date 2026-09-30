#!/usr/bin/env python3
"""Freeze an ATM contract sample from pre-window Massive reference captures."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tree_options.desk.intraday_action_graph import parse_contract  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--masters", type=Path, required=True)
    parser.add_argument("--spot-proxy", type=Path, required=True)
    parser.add_argument("--selected-as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--window-start", type=date.fromisoformat, required=True)
    parser.add_argument("--window-end", type=date.fromisoformat, required=True)
    parser.add_argument("--underlyings", default="SPY,QQQ,IWM")
    parser.add_argument("--strikes-per-side", type=int, default=3)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if (
        args.selected_as_of > args.window_start
        or args.window_start >= args.window_end
        or args.strikes_per_side < 2
        or args.out.exists()
    ):
        parser.error("invalid selection/window/output")
    spot_raw = args.spot_proxy.read_bytes()
    spots = json.loads(spot_raw)
    tickers = []
    source_hashes = {"spot_proxy": hashlib.sha256(spot_raw).hexdigest()}
    counts = {}
    for name in [s.strip().upper() for s in args.underlyings.split(",") if s.strip()]:
        source = args.masters / f"{name}_{args.selected_as_of}.json"
        master_raw = source.read_bytes()
        master = json.loads(master_raw)
        if (
            master.get("as_of") != str(args.selected_as_of)
            or master.get("underlying_ticker") != name
        ):
            raise ValueError(f"master identity mismatch: {source.name}")
        spot = Decimal(str(spots[name][str(args.selected_as_of)]))
        source_hashes[source.name] = hashlib.sha256(master_raw).hexdigest()
        grouped = {}
        for page in master["pages"]:
            for row in page.get("results", []):
                ticker = row.get("ticker", "")
                try:
                    contract = parse_contract(ticker)
                except ValueError:
                    continue
                if (
                    contract.underlying != name
                    or contract.expiry <= args.window_start
                    or contract.expiry > args.window_end + timedelta(days=60)
                    or contract.expiry.weekday() != 4
                ):
                    continue
                # The monthly reference sample uses third Fridays only.
                if not 15 <= contract.expiry.day <= 21:
                    continue
                grouped.setdefault((contract.expiry, contract.right), []).append(contract)
        picked = 0
        for key in sorted(grouped):
            chain = sorted(grouped[key], key=lambda c: (abs(c.strike - spot), c.strike))
            nearest = sorted(chain[: args.strikes_per_side], key=lambda c: c.strike)
            tickers.extend(c.ticker for c in nearest)
            picked += len(nearest)
        counts[name] = {
            "spot_at_selection": str(spot),
            "selected_contracts": picked,
            "expiry_right_groups": len(grouped),
        }
    source_sha = hashlib.sha256(json.dumps(source_hashes, sort_keys=True).encode()).hexdigest()
    report = {
        "schema": "desk-intraday-contract-selection/1",
        "selected_as_of": str(args.selected_as_of),
        "window_start": str(args.window_start),
        "window_end": str(args.window_end),
        "source_sha256": source_sha,
        "source_files": source_hashes,
        "counts": counts,
        "tickers": sorted(set(tickers)),
        "execution_authorized": False,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {"out": str(args.out), "contracts": len(report["tickers"]), "counts": counts},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
