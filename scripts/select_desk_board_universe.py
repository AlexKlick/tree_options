#!/usr/bin/env python3
"""Select the board-universe v3 contracts (OTM short strikes + wings).

Rolling, lookahead-free selection over the desk long-dated capture's
contract masters (see ``tree_options.desk.board_universe``): each master
names contracts from its spot-proxy close and implied-vol index close, and
a contract is listed from the first session its master serves.

``--block PATH:FROM[:UNTIL]`` adds a v1-rule selection
(``select_desk_intraday_contracts.py``) whose contracts are listed on the
boards of FROM..UNTIL only (UNTIL omitted = through expiry); FROM must be
after the block's ``selected_as_of``. The v1 selection as a block keeps
every v1 candidate on the new boards. ``--deltas none`` selects no OTM
contracts (a longer window of the existing rule only). No network.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tree_options.desk import board_universe as bu  # noqa: E402
from tree_options.desk import indices  # noqa: E402
from tree_options.desk import intraday_action_graph as iag  # noqa: E402
from tree_options.trex.clock import session_calendar  # noqa: E402


def _decimals(text: str) -> tuple[Decimal, ...]:
    return tuple(Decimal(part.strip()) for part in text.split(",") if part.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--masters", type=Path, required=True)
    parser.add_argument("--spot-proxy", type=Path, required=True)
    parser.add_argument("--indices", type=Path, required=True, help="desk-store indices/ dir")
    parser.add_argument(
        "--block",
        action="append",
        default=[],
        help="PATH:FROM[:UNTIL] a v1-rule selection listed FROM..UNTIL",
    )
    parser.add_argument("--window-start", type=date.fromisoformat, required=True)
    parser.add_argument("--window-end", type=date.fromisoformat, required=True)
    parser.add_argument("--underlyings", default="SPY,QQQ,IWM")
    parser.add_argument(
        "--deltas",
        default=",".join(str(d) for d in bu.DEFAULT_DELTAS),
        help="target short deltas, or 'none' (no OTM selection)",
    )
    parser.add_argument(
        "--widths",
        default=",".join(str(w) for w in bu.DEFAULT_WIDTHS),
        help="wing widths and the candidate pairing widths, or 'adjacent' "
        "(v1 pairing; only with --deltas none)",
    )
    parser.add_argument("--vintage", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.window_start >= args.window_end or args.out.exists():
        parser.error("invalid window or output already exists")
    deltas = () if args.deltas == "none" else _decimals(args.deltas)
    widths = () if args.widths == "adjacent" else _decimals(args.widths)
    names = [s.strip().upper() for s in args.underlyings.split(",") if s.strip()]
    if (
        any(w <= 0 for w in widths)
        or any(n not in bu.IV_INDEX for n in names)
        or (deltas and not widths)
        or not (deltas or args.block)
    ):
        parser.error("OTM deltas need widths; underlyings need an iv index; select something")

    calendar_sessions = session_calendar().sessions()
    window = [d for d in calendar_sessions if args.window_start <= d <= args.window_end]
    spot_raw = args.spot_proxy.read_bytes()
    spots = json.loads(spot_raw)
    sources: dict[str, str] = {"spot_proxy": bu.sha256_bytes(spot_raw)}
    contracts: dict[str, dict[str, object]] = {}
    blocks = []
    for text in args.block:
        path_text, _, span = text.partition(":")
        start_text, _, until_text = span.partition(":")
        block_raw = Path(path_text).read_bytes()
        block = json.loads(block_raw)
        start = date.fromisoformat(start_text)
        until = date.fromisoformat(until_text) if until_text else None
        if (
            block.get("schema") != "desk-intraday-contract-selection/1"
            or date.fromisoformat(block["selected_as_of"]) >= start
            or start not in window
            or (until is not None and until < start)
        ):
            parser.error(
                f"block {path_text}: listing must start on a window session "
                "after its selected_as_of"
            )
        sources[f"block:{Path(path_text).name}"] = bu.sha256_bytes(block_raw)
        blocks.append(
            {
                "file": Path(path_text).name,
                "selected_as_of": block["selected_as_of"],
                "from": start.isoformat(),
                "until": None if until is None else until.isoformat(),
                "contracts": len(block["tickers"]),
            }
        )
        for ticker in block["tickers"]:
            iag.parse_contract(ticker)
            if ticker in contracts:
                parser.error(f"{ticker} is in two blocks")
            contracts[ticker] = {
                "listed_from": start.isoformat(),
                "listed_until": None if until is None else until.isoformat(),
                "reference": block["selected_as_of"],
                "role": "block",
            }

    # references: the last master before the first session, then every later
    # master inside the window (the same dates for every underlying)
    dates = {
        n: {date.fromisoformat(p.stem.split("_", 1)[1]) for p in args.masters.glob(f"{n}_*.json")}
        for n in names
    }
    common = sorted(set.intersection(*dates.values()))
    first = [d for d in common if d < window[0]]
    if deltas and not first:
        parser.error("no master predates the window")
    references = first[-1:] + [d for d in common if window[0] <= d < args.window_end]
    served = bu.served_sessions(references, window, args.window_end) if deltas else {}

    iv: dict[str, dict[date, Decimal]] = {}
    for name in names:
        index_path = args.indices / f"{bu.IV_INDEX[name]}.csv"
        sources[index_path.name] = bu.sha256_bytes(index_path.read_bytes())
        closes = bu.iv_closes(indices.read_store(index_path), date(2000, 1, 1), args.window_end)
        iv[name] = {date.fromisoformat(k): Decimal(v) for k, v in closes.items()}

    report = []
    for ref, days in served.items():
        row: dict[str, object] = {
            "as_of": ref.isoformat(),
            "serves": [days[0].isoformat(), days[-1].isoformat(), len(days)],
        }
        for name in names:
            source = args.masters / f"{name}_{ref}.json"
            raw = source.read_bytes()
            master = json.loads(raw)
            if master.get("as_of") != ref.isoformat():
                raise ValueError(f"master date mismatch: {source.name}")
            sources[source.name] = bu.sha256_bytes(raw)
            spot = Decimal(str(spots[name][ref.isoformat()]))
            sigma = iv[name].get(ref)
            if sigma is None:
                raise ValueError(f"no {bu.IV_INDEX[name]} close on {ref}")
            picked = bu.select_reference(
                master, name, spot, sigma / 100, days, calendar_sessions, deltas, widths
            )
            added = 0
            for spec in picked:
                ticker = str(spec.pop("ticker"))
                if ticker not in contracts:
                    added += 1
                    contracts[ticker] = {
                        "listed_from": days[0].isoformat(),
                        "listed_until": None,
                        "reference": ref.isoformat(),
                        **spec,
                    }
            row[name] = {
                "spot": str(spot),
                "iv_index": str(sigma),
                "picked": len(picked),
                "new": added,
            }
        report.append(row)

    ordered = dict(sorted(contracts.items()))
    roles = Counter(str(spec["role"]) for spec in ordered.values())
    document = {
        "schema": bu.SELECTION_SCHEMA,
        "vintage": args.vintage,
        "window_start": args.window_start.isoformat(),
        "window_end": args.window_end.isoformat(),
        "sessions": [window[0].isoformat(), window[-1].isoformat(), len(window)],
        "rule": {
            "name": bu.RULE,
            "deltas": [str(d) for d in deltas],
            "widths": [str(w) for w in widths] or "adjacent",
            "dte_range": [bu.DTE_MIN, bu.DTE_MAX],
            "iv_index": {n: bu.IV_INDEX[n] for n in names},
            "rate": "0",
            "expiries": "standard monthly (third Friday; prior session on a holiday)",
            "listing": "first session served by the reference master (blocks: their "
            "FROM..UNTIL); earlier bars dropped",
        },
        "blocks": blocks,
        "references": report,
        "source_files": dict(sorted(sources.items())),
        "source_sha256": bu.canonical_sha256(dict(sorted(sources.items()))),
        "counts": {
            "contracts": len(ordered),
            "roles": dict(sorted(roles.items())),
            "by_underlying": dict(
                sorted(Counter(iag.parse_contract(t).underlying for t in ordered).items())
            ),
        },
        "contracts": ordered,
        "execution_authorized": False,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "counts": document["counts"],
                "references": [r["as_of"] for r in report],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
