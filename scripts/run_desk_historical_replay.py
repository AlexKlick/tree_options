#!/usr/bin/env python3
"""Run an exploratory options VWAP replay over one or more cached data sets.

Read-only on vendor caches and panel. Results go to --out-dir, with one
immutable JSON per invocation. Never writes a desk verdict or trading queue.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tree_options.desk import econ_jobs, historical_replay, ivhist  # noqa: E402
from tree_options.desk.paths import paper_dir, store_root  # noqa: E402
from tree_options.desk.universe import CHAIN_UNIVERSE  # noqa: E402
from tree_options.trex.clock import session_calendar  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, action="append", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--names", default=",".join(CHAIN_UNIVERSE))
    parser.add_argument("--signals", default="xsmom_top3,pead_beat")
    parser.add_argument("--structures", default=",".join(historical_replay.STRUCTURES))
    parser.add_argument("--min-dte", type=int, default=30)
    parser.add_argument("--max-dte", type=int, default=60)
    parser.add_argument("--hold-sessions", type=int, default=20)
    parser.add_argument("--haircut", type=float, default=0.01)
    parser.add_argument("--max-loss", type=float, default=300.0)
    parser.add_argument("--all-expiries", action="store_true")
    parser.add_argument(
        "--out-dir", type=Path, default=store_root() / "evaluations" / "historical-replay"
    )
    args = parser.parse_args(argv)
    spec = historical_replay.ReplaySpec(
        start=args.start,
        end=args.end,
        names=tuple(s.strip().upper() for s in args.names.split(",") if s.strip()),
        signals=tuple(s.strip() for s in args.signals.split(",") if s.strip()),
        structures=tuple(s.strip() for s in args.structures.split(",") if s.strip()),
        min_dte=args.min_dte,
        max_dte=args.max_dte,
        hold_sessions=args.hold_sessions,
        haircut=args.haircut,
        max_loss=args.max_loss,
    )
    if any(name not in CHAIN_UNIVERSE for name in spec.names):
        parser.error("--names must be members of the desk chain universe")
    if any(not path.is_dir() for path in args.cache):
        parser.error("every --cache must be an existing directory")
    warnings: list[str] = []
    panel, panel_sha = econ_jobs.load_panel(warnings)
    earnings, earnings_sha = econ_jobs.load_earnings(warnings)
    if panel is None or earnings_sha is None:
        parser.error("; ".join(warnings))
    cal = session_calendar()
    sessions = cal.sessions()
    end_index = next((i for i, d in enumerate(sessions) if d > spec.end), len(sessions))
    scan_end = sessions[min(len(sessions) - 1, end_index + spec.hold_sessions + 1)]
    scans = [
        ivhist.scan_cache(
            cache,
            spec.names,
            spec.start,
            scan_end,
            cal,
            min_dte=0,
            max_dte=spec.max_dte,
            monthly_only=not args.all_expiries,
        )
        for cache in args.cache
    ]
    merged = historical_replay.merge_scans(scans)
    result = historical_replay.replay(merged, panel, earnings, cal, spec)
    result["provenance"] = {
        "code": "tree_options.desk.historical_replay",
        "panel_sha256": panel_sha,
        "earnings_sha256": earnings_sha,
        "sources": [
            {
                "path": str(s.source),
                "input_sha256": s.input_digest,
                "input_files": s.input_files,
                "scan_stats": dict(sorted(s.stats.items())),
            }
            for s in scans
        ],
        "all_expiries": args.all_expiries,
        "scan_end": scan_end.isoformat(),
        "paper_dir": str(paper_dir()),
    }
    raw = json.dumps(result, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n"
    run_time = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    stem = f"replay-{run_time}-{hashlib.sha256(raw).hexdigest()[:12]}"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    target = args.out_dir / f"{stem}.json"
    with target.open("xb") as fh:
        fh.write(raw)
    print(f"historical-replay {result['counts']} -> {target}")
    print(f"modeled rows={len(result['rows'])} sources={len(scans)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
