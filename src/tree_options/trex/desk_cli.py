"""Agent-facing CLI for the supervised desk: status, requests, kill files.

The desk process owns the broker session; this CLI never connects. It
composes and validates entry requests locally (the live screening stays
with the desk), reads the desk's on-disk state, and flips the kill files.
Every command is safe to run from any agent session: nothing here can
reach the broker.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from tree_options.trex.desk_runtime import DeskPaths
from tree_options.trex.plan import ExitRules, Leg, LegStructure, TakeProfit, validate_package_order
from tree_options.trex.supervised import SupervisedPaths
from tree_options.trex.supervised import status as supervised_status
from tree_options.trex.supervised_desk import EntryRequest
from tree_options.trex.supervised_ibkr import SupervisedEffect, supervised_order_ref

DEFAULT_ACCOUNT = "DUT143714"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# ------------------------------------------------------------------ status


def collect_status(paths: DeskPaths, supervised: SupervisedPaths,
                   *, now: datetime, events: int = 10) -> dict[str, Any]:
    """One read-only dump of the desk's on-disk state (no broker contact)."""
    report: dict[str, Any] = {"schema": "desk-cli-status/1", "at": now.isoformat(),
                              "run_dir": str(paths.root)}
    report["kill_files"] = sorted(f.name for f in (paths.halt(), paths.flatten())
                                  if f.exists())
    owner = paths.root / "owner.json"
    report["owner"] = json.loads(owner.read_bytes()) if owner.exists() else None
    book = paths.book()
    if book.exists():
        raw = json.loads(book.read_bytes())
        structures = {sid: {"status": st.get("status"),
                            "open_qty": (int(st.get("filled_qty", 0))
                                         - int(st.get("exit_filled_qty", 0)))}
                      for sid, st in raw.get("structures", {}).items()}
        report["book"] = structures
    else:
        report["book"] = None
    inbox = paths.root / "requests"
    if inbox.is_dir():
        report["inbox"] = sorted(p.name for p in inbox.glob("*.json")
                                 if not p.name.endswith((".result.json", ".claimed.json")))
        results = sorted(inbox.glob("*.result.json"))
        report["last_results"] = [json.loads(p.read_bytes()) for p in results[-3:]]
    else:
        report["inbox"] = []
        report["last_results"] = []
    report["supervised"] = supervised_status(supervised, now=now)
    log = paths.events()
    if log.exists() and events > 0:
        report["events"] = [json.loads(line) for line in
                            log.read_text().splitlines()[-events:]]
    return report


# ------------------------------------------------------------------ request


def compose_request(*, intent_id: str, account_id: str, underlying: str,
                    buy_strike: Decimal, sell_strike: Decimal, expiry: date,
                    debit: Decimal, cap: Decimal, entry_date: date,
                    exit_deadline: date, tp_frac: Decimal | None,
                    send_deadline: datetime, requested_by: str,
                    tp_basis: Literal["width_frac", "gain_frac",
                                      "credit_frac"] = "gain_frac") -> EntryRequest:
    """Build a validated put debit-vertical entry request (never sends).

    The take-profit basis is explicit (operator ruling 2026-09-29): the
    default ``gain_frac`` reads the fraction as a share of the DEBIT PAID
    (0.5 = take profit at +50% of entry); the former implicit
    ``width_frac`` read it as a share of the structure WIDTH, which on a
    cheap, wide vertical targets an enormous multiple of entry (the
    744/742 at $0.44 with width 2.00 put TP at $1.00, +127%). An unknown
    basis or a value out of the basis's range refuses (pydantic
    ValidationError is a ValueError)."""
    legs = (Leg(right="P", action="BUY", strike=buy_strike, expiry=expiry),
            Leg(right="P", action="SELL", strike=sell_strike, expiry=expiry))
    exits = ExitRules(touch=False, breach=False, take_profit=(
        TakeProfit(basis=tp_basis, value=tp_frac) if tp_frac is not None else None))
    structure = LegStructure(
        id=intent_id, underlying=underlying, kind="debit_vertical", legs=legs,
        quantity=1, entry_date=entry_date, exit_deadline=exit_deadline,
        limit=cap, exits=exits)
    effect = SupervisedEffect(
        intent_id=intent_id, account_id=account_id, structure=structure,
        side=structure.open_side, quantity=1, limit=debit,
        order_ref=supervised_order_ref(intent_id))
    # the same bounds the broker adapter will enforce at the send boundary
    validate_package_order(structure, effect.side, effect.quantity, debit)
    return EntryRequest(strategy_version="operational-canary/1",
                        send_deadline=send_deadline, requested_by=requested_by,
                        effect=effect)


# --------------------------------------------------------------------- CLI


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tree_options.trex.desk_cli",
        description="Operate the supervised desk from files (no broker contact).")
    parser.add_argument("--dir", type=Path, default=None,
                        help="desk run dir (default TREX_DESK_RUN_DIR)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="one read-only dump of the desk state")
    events_p = sub.add_parser("events", help="tail the desk event log")
    events_p.add_argument("-n", type=int, default=20)
    sub.add_parser("halt", help="place HALT (no new orders)")
    sub.add_parser("flatten", help="place FLATTEN (close everything)")
    sub.add_parser("resume", help="remove HALT and FLATTEN")
    req = sub.add_parser("request", help="compose+validate an entry request file")
    req.add_argument("--underlying", default="SPY")
    req.add_argument("--buy-strike", required=True, type=Decimal)
    req.add_argument("--sell-strike", required=True, type=Decimal)
    req.add_argument("--expiry", required=True, type=date.fromisoformat)
    req.add_argument("--debit", required=True, type=Decimal,
                     help="the order limit (debit orientation)")
    req.add_argument("--cap", type=Decimal, default=Decimal("1.20"),
                     help="the structure cap (default 1.20)")
    req.add_argument("--entry-date", type=date.fromisoformat, default=None)
    req.add_argument("--exit-deadline", required=True, type=date.fromisoformat)
    req.add_argument("--tp-frac", type=Decimal, default=Decimal("0.5"),
                     help="take-profit threshold in the basis's units (default 0.5)")
    req.add_argument("--tp-basis", default="gain_frac",
                     choices=("width_frac", "gain_frac", "credit_frac"),
                     help="take-profit basis (default gain_frac: TP at entry x "
                          "(1 + tp_frac), i.e. +50%% of the debit paid at the "
                          "default 0.5; width_frac: TP at tp_frac x the structure "
                          "width - on a cheap wide vertical that targets a large "
                          "multiple of entry; credit_frac: that fraction of the "
                          "entry credit captured)")
    req.add_argument("--intent-id", default=None,
                     help="also the structure id and file name (default canary-<date>-<n>)")
    req.add_argument("--account", default=DEFAULT_ACCOUNT)
    req.add_argument("--requested-by", default="operator-terminal")
    args = parser.parse_args(argv)

    from tree_options.trex.clock import ET
    now = datetime.now(ET)
    root = args.dir or DeskPaths.default().root
    paths = DeskPaths(root)
    supervised = SupervisedPaths(Path(os.environ.get(
        "TREX_SUPERVISED_DIR", "~/.local/state/trex/supervised")).expanduser())

    if args.command == "status":
        print(json.dumps(collect_status(paths, supervised, now=now), indent=2,
                         default=str))
        return 0
    if args.command == "events":
        log = paths.events()
        if not log.exists():
            return 0
        for line in log.read_text().splitlines()[-args.n:]:
            print(line)
        return 0
    if args.command == "halt":
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.halt().touch()
        print("HALT placed")
        return 0
    if args.command == "flatten":
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.flatten().touch()
        print("FLATTEN placed")
        if paths.halt().exists():  # the same kill-file read `status` reports
            print("WARNING: HALT is also present. The desk runtime gates EXIT "
                  "orders on HALT too, so this FLATTEN will not close anything "
                  "until the HALT file is gone. To flatten now: `resume` "
                  "(removes HALT and FLATTEN), then `flatten` again.",
                  file=sys.stderr)
        return 0
    if args.command == "resume":
        for flag in (paths.halt(), paths.flatten()):
            flag.unlink(missing_ok=True)
        print("HALT/FLATTEN removed")
        return 0

    # request
    entry_date = args.entry_date or now.date()
    inbox = paths.requests_dir() if hasattr(paths, "requests_dir") else paths.root / "requests"
    inbox.mkdir(parents=True, exist_ok=True)
    n = 1
    intent_id = args.intent_id
    while intent_id is None:
        candidate = f"canary-{entry_date.isoformat()}-{chr(96 + n)}"
        if not (inbox / f"{candidate}.json").exists() and \
                not (inbox / f"{candidate}.result.json").exists():
            intent_id = candidate
        n += 1
        if n > 26:
            print("refused: no free intent id", file=sys.stderr)
            return 2
    try:
        request = compose_request(
            intent_id=intent_id, account_id=args.account, underlying=args.underlying,
            buy_strike=args.buy_strike, sell_strike=args.sell_strike,
            expiry=args.expiry, debit=args.debit, cap=args.cap,
            entry_date=entry_date, exit_deadline=args.exit_deadline,
            tp_frac=args.tp_frac, tp_basis=args.tp_basis,
            send_deadline=now.replace(
                hour=11, minute=15, second=0) if now.hour < 11 else now,
            requested_by=args.requested_by)
    except ValueError as error:
        print(f"refused: invalid_request {error}", file=sys.stderr)
        return 2
    out = inbox / f"{intent_id}.json"
    _write_atomic(out, json.dumps(request.model_dump(mode="json", by_alias=True),
                                  indent=2))
    print(json.dumps({"written": str(out), "intent_id": intent_id,
                      "debit": str(args.debit), "cap": str(args.cap)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
