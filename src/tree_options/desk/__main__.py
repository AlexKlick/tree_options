"""``python -m tree_options.desk <command>``: the desk's timer entry points.

    record-chains [--session D] [--symbols A,B] [--dry-run] [--recheck]
        CBOE delayed chains for session D (default: the latest session
        whose 16:15 ET cutoff has passed) into the chain store.
        Exit 0 if >= 90% recorded, 3 if the rest is not published yet
        (the timer retries), 1 otherwise, 2 on bad arguments.

    eod-equity [--session D] [--dry-run]
        Panel refresh (fetch_ohlc.py), XSMOM/PEAD signals, draft cards and
        an ntfy push. Exit 0 done/no-op, 3 vendor lag or too early, 1 failure.

    features --session D
        Vol-surface features (ATM term, constant maturity, 25d skew, term
        slope, implied earnings move, liquidity, IV rank, VRP) from the
        recorded chains of D into DESK_STORE/features/<D>.json. Exit 0
        written, 1 no chains for D, 2 bad arguments.

    ivhist-build [--massive-cache P] [--start D] [--end D] [--names A,B]
        IVHIST-001's VWAP 30-day ATM IV history from the on-disk Polygon
        bars into DESK_STORE/iv-history/vwap_atm.json (read-only cache).

    ivhist-001 [--indices-dir P]
        The pre-registered benchmark of that history against the CBOE vol
        indices (<X>_History.csv) into IVHIST-001-verdict.json.

    forecast-001 [--out P]
        The pre-registered FORECAST-001 scoring of the pooled log-HAR into
        DESK_STORE/evaluations/FORECAST-001.{json,md}.

Each command holds a per-command lock (``<state>/locks/<command>.lock``)
while it writes; a second concurrent run exits 3. No secrets are needed or
printed (the chain feed is keyless; fetch_ohlc.py reads its own key file
and redacts it).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import re
import sys
import time
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

# BLAS thread pins BEFORE the first numpy import (the econometrics commands
# fit with lstsq; byte-identical re-runs need a fixed reduction order)
from tree_options.models.determinism import force_single_threaded_blas

force_single_threaded_blas()

from tree_options.data.massive_client import default_cache_dir  # noqa: E402
from tree_options.desk import econ_jobs, eod_equity, ivhist, paths, store  # noqa: E402
from tree_options.desk.chains import urllib_transport  # noqa: E402
from tree_options.desk.sessions import (  # noqa: E402
    Calendar,
    cutoff_instant,
    latest_completed_session,
)
from tree_options.desk.universe import CHAIN_UNIVERSE  # noqa: E402
from tree_options.trex.clock import now_et, session_calendar  # noqa: E402
from tree_options.trex.discovery.market import Transport  # noqa: E402

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m tree_options.desk")
    sub = ap.add_subparsers(dest="command", required=True)
    rc = sub.add_parser("record-chains", help="record the CBOE delayed option chains")
    rc.add_argument("--session", type=date.fromisoformat)
    rc.add_argument("--symbols", help="comma-separated (default: the 35-name chain universe)")
    rc.add_argument("--dry-run", action="store_true", help="fetch and validate; write nothing")
    rc.add_argument(
        "--recheck",
        action="store_true",
        help="refetch recorded symbols to detect a changed payload (conflict)",
    )
    eq = sub.add_parser("eod-equity", help="panel refresh + XSMOM/PEAD signals + draft cards")
    eq.add_argument("--session", type=date.fromisoformat)
    eq.add_argument("--dry-run", action="store_true", help="plan only: no fetch, no files, no push")
    fe = sub.add_parser("features", help="vol-surface features of a recorded session")
    fe.add_argument("--session", type=date.fromisoformat, required=True)
    ib = sub.add_parser("ivhist-build", help="IVHIST-001 VWAP IV history from the Polygon cache")
    ib.add_argument("--massive-cache", type=Path)
    ib.add_argument("--start", type=date.fromisoformat, default=ivhist.WINDOW[0])
    ib.add_argument("--end", type=date.fromisoformat, default=ivhist.WINDOW[1])
    ib.add_argument("--names", help="comma-separated (default: the 35-name chain universe)")
    ie = sub.add_parser("ivhist-001", help="IVHIST-001 benchmark vs the CBOE vol indices")
    ie.add_argument("--indices-dir", type=Path)
    fc = sub.add_parser("forecast-001", help="FORECAST-001 out-of-sample HAR scoring")
    fc.add_argument("--out", type=Path, help="output directory (default DESK_STORE/evaluations)")
    return ap


def _record_chains(
    args: argparse.Namespace,
    *,
    transport: Transport,
    sleep: store.Sleep,
    clock: store.Clock,
    cal: Calendar,
) -> int:
    now = clock()
    if args.session is None:
        session = latest_completed_session(now, cal)
    elif not cal.is_session(args.session):
        print(f"record-chains: {args.session} is not an NYSE session", file=sys.stderr)
        return 2
    elif now < cutoff_instant(args.session):
        print(f"record-chains: {args.session} has not closed yet (16:15 ET)", file=sys.stderr)
        return 2
    else:
        session = args.session
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        bad = [s for s in symbols if not _SYMBOL.match(s)]
        if bad or not symbols:
            print(f"record-chains: bad --symbols {args.symbols!r}", file=sys.stderr)
            return 2
    else:
        symbols = list(CHAIN_UNIVERSE)
    summary = store.record_session(
        session,
        symbols,
        store=store.ChainStore(paths.store_root()),
        transport=transport,
        clock=clock,
        sleep=sleep,
        cal=cal,
        dry_run=args.dry_run,
        recheck=args.recheck,
    )
    rc = store.exit_code(summary)
    for sym, r in summary.results.items():
        if r.status not in ("ok", "exists"):
            print(f"  {sym}: {r.status} {r.detail}")
    print(summary.line(rc) + (" (dry run)" if args.dry_run else ""))
    return rc


def _eod_equity(
    args: argparse.Namespace,
    *,
    clock: store.Clock,
    cal: Calendar,
    fetch: eod_equity.FetchRunner | None,
    notify: eod_equity.Notify | None,
) -> int:
    now = clock()
    from tree_options.trex.alert_policy import load_quiet_hours
    from tree_options.trex.notify import load_config, read_env, send

    env_path = paths.notify_env_path()
    quiet = load_quiet_hours(read_env(env_path))
    cfg = load_config(env_path) if notify is None else None
    if cfg is not None:
        push_cfg = cfg

        def push(title: str, message: str, priority: str) -> bool:
            return send(push_cfg, title, message, priority)

        notify = push
    state = paths.state_root()
    paper = paths.paper_dir()
    if fetch is None:
        tag = args.session.isoformat() if args.session else now.date().isoformat()
        fetch = eod_equity.subprocess_fetch_runner(
            paper, paths.repo_root(), state / "logs" / f"eod-equity-{tag}.log"
        )
    res = eod_equity.run_eod_equity(
        session=args.session,
        now=now,
        cal=cal,
        state=state,
        paper=paper,
        fetch=fetch,
        notify=notify,
        quiet=quiet,
        dry_run=args.dry_run,
        clock=clock,
    )
    print(res.line())
    return res.exit_code


def run_cli(
    argv: list[str] | None = None,
    *,
    transport: Transport | None = None,
    sleep: store.Sleep | None = None,
    now: datetime | None = None,
    cal: Calendar | None = None,
    fetch: eod_equity.FetchRunner | None = None,
    notify: eod_equity.Notify | None = None,
) -> int:
    """The CLI with injectable I/O (tests); :func:`main` wires the real ones."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    fixed = now
    clock: store.Clock = (lambda: fixed) if fixed is not None else now_et
    cal = cal or session_calendar()
    with _single_run(args.command, enabled=not getattr(args, "dry_run", False)) as owned:
        if not owned:
            print(f"{args.command}: another run holds the lock; retry later", file=sys.stderr)
            return 3
        if args.command == "record-chains":
            return _record_chains(
                args,
                transport=transport or urllib_transport,
                sleep=sleep or time.sleep,
                clock=clock,
                cal=cal,
            )
        if args.command == "features":
            return econ_jobs.run_features(args.session, cal)
        if args.command == "ivhist-build":
            names = (
                [s.strip() for s in args.names.split(",") if s.strip()]
                if args.names
                else list(CHAIN_UNIVERSE)
            )
            if not names or any(not _SYMBOL.match(n) for n in names):
                print(f"ivhist-build: bad --names {args.names!r}", file=sys.stderr)
                return 2
            cache = args.massive_cache or default_cache_dir()
            return econ_jobs.run_ivhist_build(cache, args.start, args.end, names, cal)
        if args.command == "ivhist-001":
            return econ_jobs.run_ivhist_001(args.indices_dir or paths.store_root() / "indices")
        if args.command == "forecast-001":
            return econ_jobs.run_forecast_001(args.out, cal)
        return _eod_equity(args, clock=clock, cal=cal, fetch=fetch, notify=notify)


@contextlib.contextmanager
def _single_run(command: str, *, enabled: bool) -> Iterator[bool]:
    """One writer per command (a manual run racing the timer would merge the
    panel or the manifest twice); a busy lock means exit 3, retry later."""
    if not enabled:
        yield True
        return
    lock = paths.state_root() / "locks" / f"{command}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def main(argv: list[str] | None = None) -> int:
    return run_cli(argv)


if __name__ == "__main__":
    sys.exit(main())
