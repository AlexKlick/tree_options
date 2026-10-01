#!/usr/bin/env python3
"""Capture historical option minute aggregates with a hard wire budget.

The input contract list must identify a pre-window reference snapshot. This
command refuses wire access while the desk's structural capture is running.
Cache-only mode is safe to run during that capture and reports missing series.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tree_options.data.massive_client import (  # noqa: E402
    ResponseCache,
    cache_key_for,
    client_from_environment,
    loads_exact,
)
from tree_options.desk.intraday_action_graph import parse_contract  # noqa: E402


def _structural_capture_active() -> bool:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except (OSError, PermissionError):
            continue
        if (
            args
            and b"python" in Path(args[0].decode(errors="ignore")).name.encode()
            and any(arg.endswith(b"/scripts/capture_massive_structural.py") for arg in args[1:])
        ):
            try:
                state = (entry / "stat").read_text().rsplit(") ", 1)[1][0]
            except (OSError, IndexError):
                return True
            if state != "T":  # a deliberately paused capture cannot spend wire quota
                return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contracts",
        type=Path,
        required=True,
        help="JSON {selected_as_of, source_sha256, tickers:[O:...]}",
    )
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--wire-budget", type=int, default=0)
    args = parser.parse_args()
    if args.start >= args.end or args.out.exists() or args.wire_budget < 0:
        parser.error("invalid range, wire budget, or output already exists")
    spec = json.loads(args.contracts.read_text())
    selected = date.fromisoformat(spec["selected_as_of"])
    tickers = spec["tickers"]
    if (
        selected > args.start
        or not isinstance(spec.get("source_sha256"), str)
        or len(spec["source_sha256"]) != 64
        or not isinstance(tickers, list)
        or len(tickers) != len(set(tickers))
    ):
        parser.error("contract provenance must predate window and have unique tickers")
    if (
        hashlib.sha256(json.dumps(spec.get("source_files"), sort_keys=True).encode()).hexdigest()
        != spec["source_sha256"]
    ):
        parser.error("contract source hash mismatch")
    for ticker in tickers:
        parse_contract(ticker)
    if args.wire_budget and _structural_capture_active():
        parser.error("structural Massive capture active; refusing concurrent wire requests")
    cache = ResponseCache(args.cache)
    client = None
    used = 0
    found: dict[str, dict] = {}
    missing = []
    for ticker in tickers:
        path = f"/v2/aggs/ticker/{ticker}/range/1/minute/{args.start}/{args.end}"
        params = {"adjusted": "true", "sort": "asc", "limit": 50000}
        cached = cache.get(cache_key_for(path, params))
        if cached is not None:
            body = loads_exact(cached)
        elif used < args.wire_budget:
            if _structural_capture_active():
                raise RuntimeError(
                    "structural Massive capture resumed; stopping before another wire request"
                )
            if client is None:
                client = client_from_environment(cache_dir=args.cache)
            if used + client.backoff.max_attempts > args.wire_budget:
                missing.append(ticker)
                continue
            before = client.stats.requests
            body = client.get_json(path, params)
            used += client.stats.requests - before
        else:
            missing.append(ticker)
            continue
        if body.get("status") not in ("OK", "DELAYED") or body.get("ticker") != ticker:
            raise ValueError(f"unusable minute response for {ticker}")
        bars = body.get("results", [])
        if (
            body.get("next_url")
            or len(bars) >= 50000
            or body.get("resultsCount", len(bars)) != len(bars)
        ):
            raise ValueError(f"truncated minute response for {ticker}")
        if any(not isinstance(bar.get("t"), int) or bar.get("v", 0) <= 0 for bar in bars):
            raise ValueError(f"invalid bars for {ticker}")
        found[ticker] = {"ticker": ticker, "timespan": "minute", "results": bars}
    report = {
        "schema": "desk-option-minute-bars/1",
        "start": str(args.start),
        "end": str(args.end),
        "selected_as_of": str(selected),
        "contract_source_sha256": spec["source_sha256"],
        "captured_at": datetime.now(UTC).isoformat(),
        "requested": len(tickers),
        "found": len(found),
        "missing_tickers": missing,
        "wire_requests": used,
        "contracts": found,
        "execution_authorized": False,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "requested": len(tickers),
                "found": len(found),
                "missing": len(missing),
                "wire_requests": used,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
