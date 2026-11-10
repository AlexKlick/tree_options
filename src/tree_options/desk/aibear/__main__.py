"""``python -m tree_options.desk.aibear <command>``.

* ``history-fetch``  free daily history into ``<DESK_STORE>/history`` (exit 0 when
                     every source recorded, 3 when any transport gap, 1 otherwise)
* ``backtest``       the sealed AI-BEAR-001 scorer; writes results.json + trades.jsonl
* ``monitor``        the daily read-only regime / shadow-card / LLM read
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


def _history(args: argparse.Namespace) -> int:
    from tree_options.desk.aibear.history import fetch_history

    lines = fetch_history(only=args.only or ())
    for line in lines:
        print(
            json.dumps(
                {k: line.get(k) for k in ("source", "status", "rows", "last_date", "detail")}
            )
        )
    statuses = {line["status"] for line in lines}
    if statuses <= {"recorded"}:
        return 0
    return 3 if statuses <= {"recorded", "error"} else 1


def _backtest(args: argparse.Namespace) -> int:
    from tree_options.desk.aibear.backtest import score, write_outputs

    result, trades = score(null_seeds=args.null_seeds)
    digests = write_outputs(Path(args.out), result, trades)
    for name, row in result["sleeves"].items():
        s = row["summary"]
        print(
            f"{name:7} trades={s['trades']:4d} total_R={s['total_r']:+9.2f} "
            f"ci95={s['ci95_total_r']} p_null_holm={row['null']['p_null_holm']} "
            f"verdict={row['verdict']}"
        )
    print(json.dumps(digests, sort_keys=True))
    return 0


def _monitor(args: argparse.Namespace) -> int:
    from tree_options.desk.aibear.monitor import run_monitor

    doc = run_monitor(
        session=date.fromisoformat(args.session) if args.session else None,
        use_llm=args.llm,
        dry_run=args.dry_run,
    )
    print(json.dumps(doc["summary"], sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tree_options.desk.aibear")
    sub = ap.add_subparsers(dest="command", required=True)
    hf = sub.add_parser("history-fetch", help="FRED + Yahoo daily history into the desk store")
    hf.add_argument("--only", nargs="*", help="source names (default: all)")
    bt = sub.add_parser("backtest", help="the sealed AI-BEAR-001 scorer")
    bt.add_argument("--out", required=True, help="output directory")
    bt.add_argument("--null-seeds", type=int, default=None, help=argparse.SUPPRESS)
    mo = sub.add_parser("monitor", help="daily read-only regime + shadow cards + LLM read")
    mo.add_argument("--session", help="ISO date (default: latest panel session)")
    mo.add_argument("--llm", action="store_true", help="add the advisory LLM bubble-stress read")
    mo.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = ap.parse_args(argv)
    handler = {"history-fetch": _history, "backtest": _backtest, "monitor": _monitor}[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
