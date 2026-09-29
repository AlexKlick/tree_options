#!/usr/bin/env python3
"""Build a board-universe bundle vintage from a selection.

Series come, in order, from ``--seed`` bundles (earlier captures, reused
verbatim: the v1 bundle for the v1 contracts), the Massive response cache
(the request window ``reference..end``, then each ``--try-window``
covering the contract's listing through its expiry), and last the wire
under a hard ``--wire-budget`` (free tier: 5 requests/minute; one request
per contract). A NOT_AUTHORIZED body stops the run. Every fetched body is
cached, so an interrupted run resumes for free. The bundle is written only
when every selected series is present (``--allow-missing`` overrides and
records the gaps). ``--plan`` reports the split without any wire request.

The output is a NEW file; this script never touches an existing vintage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from capture_desk_option_minutes import _structural_capture_active  # noqa: E402
from tree_options.data.massive_client import (  # noqa: E402
    MassiveNotEntitledError,
    ResponseCache,
    cache_key_for,
    client_from_environment,
    loads_exact,
)
from tree_options.desk import board_universe as bu  # noqa: E402
from tree_options.desk import indices  # noqa: E402
from tree_options.desk import intraday_action_graph as iag  # noqa: E402

PARAMS = {"adjusted": "true", "sort": "asc", "limit": 50000}


def _path(ticker: str, start: date, end: date) -> str:
    return f"/v2/aggs/ticker/{ticker}/range/1/minute/{start}/{end}"


def _window(text: str) -> tuple[date, date]:
    start, _, end = text.partition(":")
    return date.fromisoformat(start), date.fromisoformat(end)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--seed", type=Path, action="append", default=[])
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--try-window", action="append", default=[], help="START:END")
    parser.add_argument("--indices", type=Path, required=True)
    parser.add_argument("--wire-budget", type=int, default=0)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--allow-missing", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists() or args.wire_budget < 0:
        parser.error("output exists or negative wire budget")
    selection_raw = args.selection.read_bytes()
    selection = json.loads(selection_raw)
    if selection.get("schema") != bu.SELECTION_SCHEMA:
        parser.error("board-universe selection required")
    if args.wire_budget and _structural_capture_active():
        parser.error("structural Massive capture active; refusing concurrent wire requests")

    seeds: dict[str, tuple[list[dict[str, Any]], str]] = {}
    seed_files: dict[str, str] = {}
    for seed in args.seed:
        raw_bytes = seed.read_bytes()
        seed_files[seed.name] = hashlib.sha256(raw_bytes).hexdigest()
        raw = json.loads(raw_bytes)
        if raw.get("schema") != iag.BARS_V1:  # a plain capture, never a v3 vintage
            parser.error(f"seed {seed} is not a {iag.BARS_V1} capture")
        for ticker, body in raw["contracts"].items():
            if body.get("ticker") != ticker or body.get("timespan") != "minute":
                raise ValueError(f"seed identity mismatch: {ticker}")
            seeds.setdefault(ticker, (body["results"], f"seed:{seed.name}"))
        del raw
    tries = [_window(text) for text in args.try_window]
    cache = ResponseCache(args.cache)
    series: dict[str, list[dict[str, Any]]] = {}
    sources: dict[str, str] = {}
    wire: list[tuple[str, date]] = []
    for ticker, spec in sorted(selection["contracts"].items()):
        if ticker in seeds:
            series[ticker], sources[ticker] = seeds[ticker]
            continue
        listed = date.fromisoformat(spec["listed_from"])
        reference = date.fromisoformat(spec["reference"])
        need_end = min(iag.parse_contract(ticker).expiry, args.end)
        for start, end in [(reference, args.end), *tries]:
            if start > listed or end < need_end:
                continue
            cached = cache.get(cache_key_for(_path(ticker, start, end), PARAMS))
            if cached is not None:
                series[ticker] = bu.verify_body(ticker, loads_exact(cached))
                sources[ticker] = f"cache:{start}:{end}"
                break
        else:
            wire.append((ticker, reference))
    plan = {"selected": len(selection["contracts"]), "seeded": sum(
        s.startswith("seed:") for s in sources.values()),
        "cached": sum(s.startswith("cache:") for s in sources.values()),
        "wire_needed": len(wire), "wire_minutes_at_5_per_min": round(len(wire) / 5, 1)}
    print(json.dumps({"plan": plan}), flush=True)
    if args.plan:
        return 0

    client = None
    used = 0
    missing: list[str] = []
    for position, (ticker, reference) in enumerate(wire, 1):
        if client is None:
            client = client_from_environment(cache_dir=args.cache)
        if used + client.backoff.max_attempts > args.wire_budget:
            missing.append(ticker)
            continue
        if _structural_capture_active():
            raise RuntimeError("structural Massive capture resumed; stopping before another request")
        before = client.stats.requests
        try:
            body = client.get_json(_path(ticker, reference, args.end), PARAMS)
        except MassiveNotEntitledError as error:
            print(json.dumps({"refused": "not entitled", "ticker": ticker,
                              "detail": str(error)[:200]}), flush=True)
            return 4
        used += client.stats.requests - before
        series[ticker] = bu.verify_body(ticker, body)
        sources[ticker] = f"wire:{reference}:{args.end}"
        if position % 20 == 0 or position == len(wire):
            print(json.dumps({"fetched": position, "of": len(wire), "wire_requests": used,
                              "at": datetime.now(UTC).isoformat()}), flush=True)
    if missing and not args.allow_missing:
        print(json.dumps({"incomplete": len(missing), "wire_requests": used,
                          "note": "fetched bodies are cached; re-run with a budget to resume"}))
        return 3

    start = date.fromisoformat(selection["window_start"])
    closes: dict[str, dict[str, str]] = {}
    files: dict[str, str] = {}
    underlyings = sorted({iag.parse_contract(t).underlying for t in selection["contracts"]})
    for name in underlyings:
        path = args.indices / f"{bu.IV_INDEX[name]}.csv"
        files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        closes[name] = bu.iv_closes(indices.read_store(path), start - timedelta(days=45), args.end)
    iv_context = {"source": "CBOE 30-day implied-vol index closes (desk-store indices/<X>.csv)",
                  "index_for": {n: bu.IV_INDEX[n] for n in underlyings}, "files": files,
                  "exposure": "a board sees the close of the latest date strictly before "
                              "its session", "closes": closes}
    bundle = bu.assemble_bundle(
        selection, series, iv_context,
        selection_sha256=hashlib.sha256(selection_raw).hexdigest(),
        captured_at=datetime.now(UTC).isoformat(), wire_requests=used, sources=sources)
    bundle["seed_files"] = seed_files
    bundle["missing_tickers"] = sorted(set(bundle["missing_tickers"]) | set(missing))
    data = json.dumps(bundle, sort_keys=True, separators=(",", ":"), default=str).encode()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    partial = args.out.with_name(args.out.name + ".partial")
    partial.write_bytes(data)
    partial.replace(args.out)
    summary = {"out": str(args.out), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
               "requested": bundle["requested"], "found": bundle["found"],
               "missing": len(bundle["missing_tickers"]), "empty_after_listing":
               len(bundle["empty_after_listing"]), "bars": sum(
                   len(c["results"]) for c in bundle["contracts"].values()),
               "bars_dropped_before_listing": bundle["bars_dropped_before_listing"],
               "wire_requests": used, "plan": plan}
    Path(f"{args.out}.summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
