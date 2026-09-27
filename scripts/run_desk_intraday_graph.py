#!/usr/bin/env python3
"""Replay overlapping three-month windows from verified option minute bars.

The default decision policy is no trade. A supplied decision JSON maps
snapshot IDs to candidate IDs and must be generated from outcome-free packets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tree_options.desk.intraday_action_graph import replay, windows  # noqa: E402
from tree_options.trex.clock import session_calendar  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--stride-sessions", type=int, default=21)
    parser.add_argument("--decisions", type=Path)
    parser.add_argument("--policy", choices=("no_trade", "put_credit", "call_credit",
                                             "put_debit", "call_debit"), default="no_trade")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    summary_path = args.out.with_suffix(".summary.json")
    if (args.start > args.end or args.out.exists() or summary_path.exists()
            or (args.decisions and args.policy != "no_trade")):
        parser.error("invalid date range or output already exists")
    source = args.bundle.read_bytes()
    bundle = json.loads(source)
    choices = json.loads(args.decisions.read_text()) if args.decisions else None
    if choices is not None and (not isinstance(choices, dict) or any(
            not isinstance(key, str) or not key.startswith("s:")
            or not isinstance(value, (str, type(None))) for key, value in choices.items())):
        parser.error("decisions must map snapshot IDs to candidate IDs or null")
    sessions = [d for d in session_calendar().sessions() if args.start <= d <= args.end]
    spans = windows(sessions, stride_sessions=args.stride_sessions)
    if not spans:
        parser.error("range does not contain a complete three-month window")
    if choices is not None and any(not any(
            start.isoformat() <= key[2:12] <= end.isoformat() for start, end in spans)
            for key in choices):
        parser.error("decision outside all replay windows")
    results = []
    for start, end in spans:
        selected = [day for day in sessions if start <= day <= end]
        choices_in_span = ({key: value for key, value in choices.items()
                            if start.isoformat() <= key[2:12] <= end.isoformat()}
                           if choices is not None else None)
        graph = replay(bundle, selected, choices_in_span, policy=args.policy)
        results.append({"start": start.isoformat(), "end": end.isoformat(), "graph": graph})
    report = {"schema": "desk-intraday-rolling-replay/1", "source_sha256": hashlib.sha256(source).hexdigest(),
              "engine_sha256": hashlib.sha256((ROOT / "src/tree_options/desk/intraday_action_graph.py").read_bytes()).hexdigest(),
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "decisions_sha256": hashlib.sha256(args.decisions.read_bytes()).hexdigest()
              if args.decisions else None, "policy": args.policy if not args.decisions else "external_decisions",
              "windows": results, "execution_authorized": False}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    summary = {"schema": "desk-intraday-graph-summary/1", "policy": report["policy"],
               "source_sha256": report["source_sha256"], "engine_sha256": report["engine_sha256"],
               "requested_contracts": bundle.get("requested"), "captured_contracts": bundle.get("found"),
               "traded_minute_bars": sum(len(body.get("results", [])) for body in bundle.get("contracts", {}).values()),
               "windows": [{"start": row["start"], "end": row["end"],
                            **{key: row["graph"][key] for key in (
                                "sessions", "scheduled_snapshots", "potential_trades", "entered",
                                "modeled_wins", "modeled_losses", "open_at_end",
                                "closed_capital_proxy", "minimum_closed_capital_proxy",
                                "peak_open_loss_reserved")}}
                           for row in results],
               "limitations": results[0]["graph"]["limitations"],
               "execution_authorized": False}
    with summary_path.open("x") as stream:
        json.dump(summary, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"out": str(args.out), "windows": len(results),
                      "summary": str(summary_path),
                      "snapshots": sum(w["graph"]["scheduled_snapshots"] for w in results),
                      "potential_trades": sum(w["graph"]["potential_trades"] for w in results),
                      "entered": sum(w["graph"]["entered"] for w in results)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
