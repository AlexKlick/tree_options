"""``python -m tree_options.desk <command>``: the desk's timer entry points.

    record-chains [--session D] [--symbols A,B] [--dry-run] [--recheck]
        CBOE delayed chains for session D (default: the latest session
        whose 16:15 ET cutoff has passed) into the chain store; each
        symbol is held to its own options close (store.validate).
        Exit 0 if >= 90% recorded, 3 if the rest is not published yet
        (the timer retries), 1 otherwise, 2 on bad arguments.

    eod-equity [--session D] [--dry-run]
        Panel refresh (fetch_ohlc.py), XSMOM/PEAD signals, draft cards and
        an ntfy push. Exit 0 done/no-op, 3 vendor lag or too early, 1 failure.

    record-indices [--sources VIX,DTB3] [--dry-run] [--force]
        CBOE index histories + FRED DTB3 into <store>/indices/. Sources
        already stored through the latest session are skipped (no request)
        unless --force. Exit 0 all current, 3 a vendor lags or a transport
        error left a soft gap, 1 a vendor file gone/bad, 2 bad arguments.

    record-dividends [--session D] [--symbols A,B] [--dry-run]
        Polygon /v3/reference/dividends histories into
        DESK_STORE/dividends/<D>/<SYM>.json (default D: the latest
        completed session; default symbols: the 35-name chain universe).
        A symbol already stored for D is skipped without a request. Exit 0
        all stored, 3 a retryable gap, 1 an entitlement/key refusal or
        nothing stored, 2 bad arguments. The Polygon key is read from its
        key file inside the client and never printed.

    update-events [--horizon N] [--dry-run]
        Earnings timing (Nasdaq estimates; EDGAR 8-K 2.02 only when
        DESK_SEC_UA is set) and the macro seal/Fed-page check. Exit 0,
        3 partial vendor failure, 1 drift/broken seal/total failure.

    seal-macro --from D --to D [--fomc-html FILE --fetched-on D]
               [--gap-note TEXT] [--basis T]
        Operator/agent tool (never a timer): rebuild and reseal
        macro-<Y1>-<Y2>.json in the events dir, carrying hand-entered
        CPI/NFP items; --gap-note words the todo for years without them.
        Exit 0 sealed, 1 source unreadable/incomplete, 2 bad arguments.

    features --session D
        Vol-surface features (ATM term, constant maturity, 25d skew, term
        slope, implied earnings move, liquidity, IV rank, VRP) from the
        recorded chains of D into DESK_STORE/features/<D>.json. Exit 0
        written, 1 no chains for D, 2 bad arguments.

    ivhist-build [--massive-cache P] [--start D] [--end D] [--names A,B]
                 [--raw-snapshots]
        IVHIST-001's VWAP 30-day ATM IV history from the on-disk Polygon
        bars into DESK_STORE/iv-history/vwap_atm.json (read-only cache).
        DTB3 is read from <store>/indices/DTB3.csv in the stored format
        (record-indices); --raw-snapshots reads the raw FRED file instead
        (the sealed IVHIST-001 run's input).

    ivhist-001 [--indices-dir P] [--raw-snapshots]
        The pre-registered benchmark of that history against the CBOE vol
        indices into IVHIST-001-verdict.json: the stored <X>.csv files, or
        with --raw-snapshots the raw CBOE <X>_History.csv snapshots.

    forecast-001 [--out P] [--coverage-sensitivity]
        The pre-registered FORECAST-001 scoring of the pooled log-HAR into
        DESK_STORE/evaluations/FORECAST-001.{json,md}. With
        --coverage-sensitivity: the disclosed earnings-coverage sensitivity
        figure instead (FORECAST-001-coverage-sensitivity.json).

    mine [--session D] [--dry-run] [--names A,B] [--out PATH]
         [--desk-specs DIR] [--desk-book FILE]
        The deal miner (plan D6, desk.miner): session D's entry queue
        (default D: the latest completed session) into
        <TREX_DESK_QUEUE>/<D>.json, schema trex.deal/1, written once and
        marked done in stages/<D>/mine.done.json; features/<D>.json is
        built first when missing. --dry-run writes nothing under the store
        or the state (the payload goes to --out when given); --names (a
        subset of the chain universe) needs --dry-run or --out. Exit 0
        written or already done, 3 inputs not ready (no chains; D's
        signals file missing, unreadable, another session's or on a panel
        short of D; no features) or the lock held: no queue, no marker,
        the next slot retries; 1 a conflict with the written queue or a
        failure, 2 bad arguments. Places no orders.

Each command holds a per-command lock (``<state>/locks/<command>.lock``)
while it writes; a second concurrent run exits 3. No secrets are printed
(the chain, index and calendar feeds are keyless; fetch_ohlc.py reads its
own key file and redacts it; the SEC contact User-Agent is read from
DESK_SEC_UA and never echoed).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
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

from tree_options.data.massive_client import (  # noqa: E402
    MassiveClient,
    MassiveError,
    RateGovernor,
    default_cache_dir,
    load_api_key,
)
from tree_options.desk import (  # noqa: E402
    dividends,
    econ_jobs,
    eod_equity,
    events,
    http,
    indices,
    ivhist,
    miner,
    paths,
    store,
)
from tree_options.desk.chains import urllib_transport  # noqa: E402
from tree_options.desk.sessions import (  # noqa: E402
    Calendar,
    ClosingCalendar,
    cutoff_instant,
    latest_completed_session,
)
from tree_options.desk.universe import CHAIN_UNIVERSE  # noqa: E402
from tree_options.trex.clock import ET, now_et, session_calendar  # noqa: E402
from tree_options.trex.discovery.market import Transport  # noqa: E402

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m tree_options.desk")
    sub = ap.add_subparsers(dest="command", required=True)
    rc = sub.add_parser("record-chains", help="record the CBOE delayed option chains")
    rc.add_argument("--session", type=date.fromisoformat)
    rc.add_argument(
        "--symbols",
        help=f"comma-separated (default: the {len(CHAIN_UNIVERSE)}-name chain universe)",
    )
    rc.add_argument("--dry-run", action="store_true", help="fetch and validate; write nothing")
    rc.add_argument(
        "--recheck",
        action="store_true",
        help="refetch recorded symbols to detect a changed payload (conflict)",
    )
    eq = sub.add_parser("eod-equity", help="panel refresh + XSMOM/PEAD signals + draft cards")
    eq.add_argument("--session", type=date.fromisoformat)
    eq.add_argument("--dry-run", action="store_true", help="plan only: no fetch, no files, no push")
    ri = sub.add_parser("record-indices", help="CBOE index histories + FRED DTB3")
    ri.add_argument("--sources", help="comma-separated names (default: all 15)")
    ri.add_argument("--dry-run", action="store_true", help="fetch and compare; write nothing")
    ri.add_argument(
        "--force",
        action="store_true",
        help="fetch even sources already stored through the latest session",
    )
    rd = sub.add_parser("record-dividends", help="Polygon dividend histories (ex-dividend rail)")
    rd.add_argument("--session", type=date.fromisoformat)
    rd.add_argument("--symbols", help="comma-separated (default: the 35-name chain universe)")
    rd.add_argument("--dry-run", action="store_true", help="fetch; write nothing")
    ue = sub.add_parser("update-events", help="earnings timing + macro seal check (weekly)")
    ue.add_argument("--horizon", type=int, default=events.HORIZON, help="sessions of estimates")
    ue.add_argument("--dry-run", action="store_true", help="fetch and merge; write nothing")
    sm = sub.add_parser("seal-macro", help="rebuild + reseal the macro calendar (operator)")
    sm.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    sm.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    sm.add_argument("--fomc-html", help="a saved copy of the Fed calendar page (else fetched)")
    sm.add_argument("--fetched-on", type=date.fromisoformat, help="when --fomc-html was fetched")
    sm.add_argument("--basis", default="", help="extra provenance for the seal row")
    sm.add_argument(
        "--gap-note",
        default=events.DEFAULT_GAP_NOTE,
        help="todo wording for a CPI/NFP year without items (e.g. 'not yet published by BLS')",
    )
    fe = sub.add_parser("features", help="vol-surface features of a recorded session")
    fe.add_argument("--session", type=date.fromisoformat, required=True)
    ib = sub.add_parser("ivhist-build", help="IVHIST-001 VWAP IV history from the Polygon cache")
    ib.add_argument("--massive-cache", type=Path)
    ib.add_argument("--start", type=date.fromisoformat, default=ivhist.WINDOW[0])
    ib.add_argument("--end", type=date.fromisoformat, default=ivhist.WINDOW[1])
    ib.add_argument(
        "--names",
        help=f"comma-separated (default: the {len(CHAIN_UNIVERSE)}-name chain universe)",
    )
    raw_help = "read the raw vendor snapshots of the sealed run (adapter), not the stored format"
    ib.add_argument("--raw-snapshots", action="store_true", help=raw_help)
    ie = sub.add_parser("ivhist-001", help="IVHIST-001 benchmark vs the CBOE vol indices")
    ie.add_argument("--indices-dir", type=Path)
    ie.add_argument("--raw-snapshots", action="store_true", help=raw_help)
    fc = sub.add_parser("forecast-001", help="FORECAST-001 out-of-sample HAR scoring")
    fc.add_argument("--out", type=Path, help="output directory (default DESK_STORE/evaluations)")
    fc.add_argument(
        "--coverage-sensitivity",
        action="store_true",
        help="write the disclosed earnings-coverage sensitivity figure (not a re-score)",
    )
    mn = sub.add_parser("mine", help="the deal miner: session D's entry queue (trex.deal/1)")
    mn.add_argument("--session", type=date.fromisoformat)
    mn.add_argument(
        "--dry-run", action="store_true", help="write nothing under the store or the state"
    )
    mn.add_argument("--names", help="comma-separated chain-universe subset (--dry-run/--out)")
    mn.add_argument("--out", type=Path, help="write the payload here, not to the queue dir")
    mn.add_argument("--desk-specs", type=Path, help="the desk runtime's spec dir (Wave 3)")
    mn.add_argument("--desk-book", type=Path, help="the desk runtime's book.json (Wave 3)")
    from tree_options.desk import production

    production.register(sub)
    return ap


def _update_events(
    args: argparse.Namespace,
    *,
    get: http.Get,
    sleep: store.Sleep,
    clock: store.Clock,
    cal: ClosingCalendar,
) -> int:
    if args.horizon < 1:
        print("update-events: --horizon must be >= 1", file=sys.stderr)
        return 2
    ua = os.environ.get(events.SEC_UA_ENV, "").strip() or None
    res = events.update_events(
        get=get,
        clock=clock,
        sleep=sleep,
        cal=cal,
        paper=paths.paper_dir(),
        events_dir=paths.events_dir(),
        state=paths.state_root(),
        sec_ua=ua,
        horizon=args.horizon,
        dry_run=args.dry_run,
    )
    print(res.line() + (" (dry run)" if args.dry_run else ""))
    return res.exit_code


def _seal_macro(
    args: argparse.Namespace, *, get: http.Get, clock: store.Clock, cal: Calendar
) -> int:
    if args.end < args.start:
        print("seal-macro: --to is before --from", file=sys.stderr)
        return 2
    if args.fomc_html:
        if args.fetched_on is None:
            print("seal-macro: --fomc-html needs --fetched-on (provenance)", file=sys.stderr)
            return 2
        html = Path(args.fomc_html).read_text(encoding="utf-8", errors="replace")
        fetched_on, how = args.fetched_on, f"saved page {Path(args.fomc_html).name}"
    else:
        status, body = get(events.FOMC_URL, headers=events.FED_HEADERS, timeout=events.TIMEOUT_S)
        if status != 200:
            print(f"seal-macro: Fed calendar page HTTP {status}", file=sys.stderr)
            return 1
        html = body.decode("utf-8", "replace")
        fetched_on, how = clock().astimezone(ET).date(), "live GET"
    out = paths.events_dir() / f"macro-{args.start.year}-{args.end.year}.json"
    carried: dict[str, list[dict[str, str]]] = {"cpi": [], "nfp": []}
    try:
        if out.exists():  # hand-entered items survive a reseal (even a hand-edited file)
            prior = json.loads(out.read_text())
            carried = {k: list(prior.get(k) or []) for k in events.HAND_KINDS}
        doc = events.build_macro(
            events.parse_fomc(html),
            cal,
            args.start,
            args.end,
            fetched_on=fetched_on,
            cpi=carried["cpi"],
            nfp=carried["nfp"],
            gap_note=args.gap_note,
        )
    except (events.EventsError, TypeError, ValueError, AttributeError) as exc:
        print(f"seal-macro: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    basis = (
        f"FOMC parsed by events.parse_fomc from {events.FOMC_URL} ({how}, fetched {fetched_on}); "
        f"cpi {len(doc['cpi'])} / nfp {len(doc['nfp'])} items carried as hand-entered "
        "(entered_by per item); opex + vix_expiry computed on the NYSE session calendar"
        + (f"; {args.basis}" if args.basis else "")
    )
    sha = events.seal_macro(doc, out, basis=basis, sealed_at=clock())
    counts = " ".join(f"{k}={len(doc[k])}" for k in ("fomc", "cpi", "nfp", "opex", "vix_expiry"))
    print(f"seal-macro {out.name} sha256={sha} {counts}")
    for todo in doc["todo"]:
        print(f"  todo: {todo}")
    return 0


def _record_indices(
    args: argparse.Namespace,
    *,
    get: http.Get,
    sleep: store.Sleep,
    clock: store.Clock,
    cal: Calendar,
) -> int:
    by_name = {s.name: s for s in indices.SOURCES}
    if args.sources is not None:
        names = [s.strip() for s in args.sources.split(",") if s.strip()]
        bad = [n for n in names if n not in by_name]
        if bad or not names:
            print(f"record-indices: bad --sources {args.sources!r}", file=sys.stderr)
            return 2
        sources = [by_name[n] for n in names]
    else:
        sources = list(indices.SOURCES)
    summary = indices.record_indices(
        sources,
        root=paths.store_root(),
        get=get,
        clock=clock,
        sleep=sleep,
        cal=cal,
        dry_run=args.dry_run,
        skip_current=not args.force,
    )
    rc = indices.exit_code(summary)
    for name, r in summary.results.items():
        if r.status not in ("new", "updated", "unchanged", "current") or r.lagging:
            lag = " (lagging)" if r.lagging else ""
            print(f"  {name}: {r.status}{lag} last={r.last_date} {r.detail}")
    print(summary.line(rc) + (" (dry run)" if args.dry_run else ""))
    return rc


def _record_dividends(
    args: argparse.Namespace,
    *,
    client: MassiveClient | None,
    clock: store.Clock,
    cal: Calendar,
) -> int:
    now = clock()
    if args.session is None:
        session = latest_completed_session(now, cal)
    elif not cal.is_session(args.session) or args.session > now.astimezone(ET).date():
        print(f"record-dividends: {args.session} is not a past NYSE session", file=sys.stderr)
        return 2
    else:
        session = args.session
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        if not symbols or any(not _SYMBOL.match(s) for s in symbols):
            print(f"record-dividends: bad --symbols {args.symbols!r}", file=sys.stderr)
            return 2
    else:
        symbols = list(CHAIN_UNIVERSE)
    if client is None:
        try:  # the paid plan: no request spacing; the key stays in the client
            client = MassiveClient(
                api_key=load_api_key(), cache_dir=None, governor=RateGovernor(None), timeout=20.0
            )
        except MassiveError as exc:  # fixed text: no key material, no path
            print(
                f"record-dividends: no usable Polygon key ({type(exc).__name__})", file=sys.stderr
            )
            return 1
    run = dividends.record_dividends(
        session, symbols, client=client, clock=clock, dry_run=args.dry_run
    )
    for sym, r in run.results.items():
        if r.status == "failed":
            print(f"  {sym}: failed {r.detail}")
    print(run.line() + (" (dry run)" if args.dry_run else ""))
    return run.exit_code


def _record_chains(
    args: argparse.Namespace,
    *,
    transport: Transport,
    sleep: store.Sleep,
    clock: store.Clock,
    cal: ClosingCalendar,
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


def _mine(args: argparse.Namespace, *, clock: store.Clock, cal: Calendar) -> int:
    names = None
    if args.names is not None:
        names = [s.strip() for s in args.names.split(",") if s.strip()]
        if not names or any(not _SYMBOL.match(n) for n in names):
            print(f"mine: bad --names {args.names!r}", file=sys.stderr)
            return 2
    res = miner.run_mine(
        session=args.session,
        now=clock(),
        cal=cal,
        dry_run=args.dry_run,
        names=names,
        out=args.out,
        desk_specs=args.desk_specs,
        desk_book=args.desk_book,
        build_features=lambda d: econ_jobs.run_features(d, cal),
    )
    for line in res.summary():
        print(line)
    print(res.line() + (" (dry run)" if args.dry_run else ""))
    if res.exit_code == 2:
        print(f"mine: {res.detail}", file=sys.stderr)
    return res.exit_code


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
    cal: ClosingCalendar | None = None,
    fetch: eod_equity.FetchRunner | None = None,
    notify: eod_equity.Notify | None = None,
    get: http.Get | None = None,
    dividend_client: MassiveClient | None = None,
) -> int:
    """The CLI with injectable I/O (tests); :func:`main` wires the real ones."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    fixed = now
    clock: store.Clock = (lambda: fixed) if fixed is not None else now_et
    cal = cal or session_calendar()
    if getattr(args, "production_command", False):
        from tree_options.desk import production

        # SQLite serializes all evidence writes across command names. Read-only
        # commands and dry runs must not create the old per-command lock files.
        return production.dispatch(args, now=clock(), cal=cal)
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
            return econ_jobs.run_ivhist_build(
                cache, args.start, args.end, names, cal, raw_snapshots=args.raw_snapshots
            )
        if args.command == "ivhist-001":
            return econ_jobs.run_ivhist_001(
                args.indices_dir or paths.store_root() / "indices", raw_cboe=args.raw_snapshots
            )
        if args.command == "forecast-001":
            return econ_jobs.run_forecast_001(
                args.out, cal, coverage_sensitivity=args.coverage_sensitivity
            )
        if args.command == "record-indices":
            return _record_indices(
                args,
                get=get or http.urllib_get,
                sleep=sleep or time.sleep,
                clock=clock,
                cal=cal,
            )
        if args.command == "record-dividends":
            return _record_dividends(args, client=dividend_client, clock=clock, cal=cal)
        if args.command == "update-events":
            return _update_events(
                args,
                get=get or http.urllib_get,
                sleep=sleep or time.sleep,
                clock=clock,
                cal=cal,
            )
        if args.command == "seal-macro":
            return _seal_macro(args, get=get or http.urllib_get, clock=clock, cal=cal)
        if args.command == "mine":
            return _mine(args, clock=clock, cal=cal)
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
