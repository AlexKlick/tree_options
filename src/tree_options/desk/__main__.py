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

    forward-select [--selected-on D] [--strikes-per-group N] [--max-contracts N]
        Re-select the forward minute-bar corpus's contracts from the latest
        recorded CBOE chain at or before D (default: today ET): the measured
        universe (IWM/QQQ/SPY, 7 <= dte <= 60, |delta| <= 0.70, volume > 0,
        oi > 0, monthly third-Friday expiries), N nearest-the-money strikes
        per (underlying, expiry, right), capped. No wire requests; write-once
        per D into DESK_FORWARD_DIR/selection/<D>.json. Exit 0, 1 no chain
        session records all three underlyings. Weekly slot: Sat 09:00 ET.

    forward-minutes [--session D] [--selection PATH] [--budget N] [--massive-cache P]
                    [--dry-run]
        Capture D's Massive per-contract minute aggregates for the selection's
        contracts into DESK_FORWARD_DIR/bars/<D>.json (default D: the latest
        session whose 16:15 ET cutoff has passed; one request per contract,
        cache hits free). A daily wire guard stops on exceed and logs every
        refusal to DESK_FORWARD_DIR/budget/<date>.json. Write-once: a captured
        session is never refetched. --dry-run is cache-only (no wire, writes
        nothing). Exit 0, 3 retryable, 4 selection stale or
        missing (run forward-select), 5 budget stopped the capture before
        anything landed, 6 a decision clock (10:00/10:15/15:15 ET) is
        uncovered. Slots: Mon-Fri 17:25 ET + Mon-Sat 06:50 catch-up.

    forward-verify [--session D]
        Decision-clock coverage verdict for D's captured bars into
        DESK_FORWARD_DIR/verify/<D>.json (no wire; idempotent). Exit codes as
        forward-minutes' 0/6.

    forward-coverage [--session D]
        Eight-clock coverage EVIDENCE for D's captured bars (the
        RESTART-THRESHOLD C1 schedule 10:00..15:15 ET, not forward-verify's
        three outcome clocks) into DESK_FORWARD_DIR/coverage/<D>.json, no
        wire. For every clock: contracts with a bar in [C, C+15min) ET,
        the missing named with their reason. Prints raw numbers and
        boolean facts only -- the qualifying threshold stays the
        operator's sealed call. Exit 0 table written, 3 no bars document
        for D (run forward-minutes), 4 no selection in the forward store.

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

    features [--session D] [--idempotent]
        Vol-surface features (ATM term, constant maturity, 25d skew, term
        slope, implied earnings move, liquidity, IV rank, VRP) from the
        recorded chains of D into DESK_STORE/features/<D>.json (default D:
        the latest completed session). Exit 0 written, 1 no chains for D,
        2 bad arguments. --idempotent is the timer mode
        (deploy/desk/desk-features.service): 0 when features/<D>.json is
        already there (nothing to do) or when D has no chains yet, 3 while
        D's manifest still has symbols outside ok|exists|conflict (the
        chain recorder has not closed D; the next slot retries, and the
        document is never frozen from a half-recorded session).

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

    challenge run --bundles-from DIR [--windows FILE] [--lab-root DIR]
                  [--rounds N] [--dry-run]
        The end-to-end challenge game: every policy (the archive pareto
        front plus the no_trade control) scored on every frozen bundle in
        DESK_STORE/evaluations/intraday-graph through the same mechanical
        replay accounting, with hindsight gap samples and one digest under
        evaluations/challenge/<UTCts>/. Hard caps: <= 200 board calls and
        <= 8 reflection calls per bundle, <= 1000 board calls per
        challenge. A FRESH (< 6 h) quota snapshot with no under-using
        window skips the model policies (reason quota_dry, exit 0); rules
        policies always run. --dry-run computes the plan and writes
        nothing. Exit 0 done/gated, 2 bad arguments or a refused plan.

    outcome-table --bundle FILE --out FILE.jsonl [--sync 2|off]
                  [--half-spread 0.03] [--commission 0.65]
                  [--cost-model flat|measured]
        Environment v2 (desk.outcomes): one row per (board, candidate,
        exit mode) of a frozen minute-bar bundle — gross and net of the
        round-trip cost, leg-synced fills and marks (default 2 min),
        status closed / marked_at_end / no_fill / no_price — plus the summary
        FILE.jsonl.summary.json (printed too). --cost-model is an EXPLICIT
        choice: flat (default) is the frozen $14.60 baseline every historical
        digest is calibrated on; measured prices per moneyness from the CBOE
        EOD corpus and, because a board candidate carries no |delta, refuses
        and counts every candidate it cannot price. Pure mechanics: no model,
        no network, no store writes; it takes no state lock. Exit 0 written,
        2 bad arguments or an unreadable bundle.

    longrun run --config FILE.json [--run-dir DIR] [--limit N] [--score-only]
    longrun status [--dir DIR]
        The desk lab long run (desk.longrun): every policy on every board,
        paired, resumable, quota-aware; one digest under
        evaluations/longrun/<UTCts>/. Never promotes. Exit 0 finished, 3
        stopped (resumable) or the run dir is locked, 2 a refused config.
    longrun redigest --run-dir DIR [--table FILE.jsonl] [--out DIR]
        Re-score a run dir from its receipts + the outcome table with the
        exact counterfactual skill section (desk.skill): ZERO model calls,
        no bundle parse. In place only when the run's lock is free (exit 3
        while it is live); --out writes elsewhere and never touches it.

Each command holds a per-command lock (``<state>/locks/<command>.lock``)
while it writes (outcome-table excepted: it writes only --out); a second
concurrent run exits 3. No secrets are printed
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
    forward_coverage,
    forward_minutes,
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
    cc = sub.add_parser(
        "clock-coverage",
        help="per-clock capture coverage of a session's A2 tier (ok/stale + the persistent stale set)",
    )
    cc.add_argument("--session", type=date.fromisoformat)
    cc.add_argument("--min-ok", type=int, default=30, help="alert when a clock records fewer (default 30)")
    rc = sub.add_parser("record-chains", help="record the CBOE delayed option chains")
    rc.add_argument("--session", type=date.fromisoformat)
    rc.add_argument(
        "--clock",
        help=(
            "record the intraday A2 clock tier: a decision clock (HH:MM ET), "
            "'auto' (the open clock from the 8-clock schedule), or 'evening' "
            "(the post-close observation tier; docs/desk/EVENING-TIER.md)"
        ),
    )
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
    fe.add_argument(
        "--session",
        type=date.fromisoformat,
        help="default: the latest session whose 16:15 ET cutoff has passed",
    )
    fe.add_argument(
        "--idempotent",
        action="store_true",
        help="timer mode: exit 0 when features/<D>.json is already there, exit 3 while D "
        "is still being recorded (never rewrite a document from a partial chain set)",
    )
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
    fw = sub.add_parser(
        "forward-select",
        help="refresh the forward minute-bar contract selection (weekly; no wire)",
    )
    fw.add_argument("--selected-on", type=date.fromisoformat,
                    help="default: today ET (the Saturday 09:00 slot)")
    fw.add_argument("--strikes-per-group", type=int,
                    help=f"default {forward_minutes.STRIKES_PER_GROUP}")
    fw.add_argument("--max-contracts", type=int,
                    help=f"default {forward_minutes.MAX_CONTRACTS}")
    fwd = sub.add_parser(
        "forward-minutes",
        help="capture the session's decision-clock minute bars (wire-budgeted)",
    )
    fwd.add_argument("--session", type=date.fromisoformat,
                     help="default: the latest session whose 16:15 ET cutoff has passed")
    fwd.add_argument("--selection", type=Path,
                     help="default: the newest selection in the forward store")
    fwd.add_argument("--budget", type=int,
                     help=f"max wire requests for today (default {forward_minutes.DAILY_BUDGET_DEFAULT})")
    fwd.add_argument("--massive-cache", type=Path,
                     help="default: the desk captures' artifacts/massive-cache-desk")
    fwd.add_argument("--dry-run", action="store_true",
                     help="cache-only: no wire requests, nothing written")
    fv = sub.add_parser(
        "forward-verify",
        help="decision-clock coverage verdict for a captured session (no wire)",
    )
    fv.add_argument("--session", type=date.fromisoformat,
                    help="default: the latest session whose 16:15 ET cutoff has passed")
    fco = sub.add_parser(
        "forward-coverage",
        help="eight-clock coverage evidence for a captured session (no wire; decides nothing)",
    )
    fco.add_argument("--session", type=date.fromisoformat,
                     help="default: the latest session whose 16:15 ET cutoff has passed")
    from tree_options.desk import production

    production.register(sub)
    lab = sub.add_parser("lab-run", help="the historical theory lab: one quota-gated policy run")
    lab.add_argument("--bundle", required=True, type=Path)
    lab.add_argument("--policy", required=True)
    lab.add_argument("--sessions", type=int, default=3)
    lab.add_argument("--boards-cap", type=int, default=24)
    lab.add_argument("--windows", type=Path)
    lab.add_argument("--lab-root", type=Path)
    sub.add_parser("lab-scoreboard", help="aggregate lab runs into a per-policy scoreboard")
    ovn = sub.add_parser(
        "lab-overnight",
        help="the overnight lab: hindsight gaps + GEPA policy evolution + digest")
    ovn.add_argument("--bundle", required=True, type=Path)
    ovn.add_argument("--windows", type=Path)
    ovn.add_argument("--lab-root", type=Path)
    ch = sub.add_parser("challenge",
                        help="the end-to-end challenge game over the frozen bundles")
    ch_sub = ch.add_subparsers(dest="challenge_command", required=True)
    ch_run = ch_sub.add_parser(
        "run", help="score every policy on every frozen bundle, one digest")
    ch_run.add_argument("--bundles-from", type=Path,
                        help="the desk store holding evaluations/intraday-graph "
                             "(default DESK_STORE)")
    ch_run.add_argument("--windows", type=Path,
                        help="quota snapshot (a FRESH snapshot gates model policies)")
    ch_run.add_argument("--lab-root", type=Path)
    ch_run.add_argument("--rounds", type=int, help="play only the newest N bundles")
    ch_run.add_argument("--dry-run", action="store_true",
                        help="compute and print the plan; write nothing")
    ch_standings = ch_sub.add_parser(
        "standings",
        help="rebuild the cross-digest standings (post-seal digests only) "
             "and write evaluations/challenge/standings.json")
    ch_standings.add_argument("--bundles-from", type=Path,
                              help="the desk store (default DESK_STORE)")
    ch_standings.add_argument("--dry-run", action="store_true",
                              help="print the standings; write nothing")
    ch_rule = ch_sub.add_parser(
        "rule-check",
        help="evaluate the REGISTERED promotion rule's clauses against the "
             "standings (informational; promotes nothing)")
    ch_rule.add_argument("--bundles-from", type=Path,
                         help="the desk store (default DESK_STORE)")
    ot = sub.add_parser("outcome-table",
                        help="environment v2: per-candidate outcomes x exit modes, gross/net")
    ot.add_argument("--bundle", required=True, type=Path)
    ot.add_argument("--out", required=True, type=Path)
    ot.add_argument("--sync", default="2")
    ot.add_argument("--half-spread", default="0.03")
    ot.add_argument("--commission", default="0.65")
    ot.add_argument("--cost-model", default="flat", metavar="{flat,measured}",
                    help="flat: the frozen $14.60 baseline every digest is calibrated on. "
                         "measured: per-moneyness, from the CBOE EOD corpus; every "
                         "candidate without a |delta| is refused and counted, never "
                         "back-filled flat. Explicit, never defaulted silently.")
    sup = sub.add_parser("supervised-previews",
                         help="E6 shadow: request previews from the deal queue (never the inbox)")
    sup.add_argument("--session", type=date.fromisoformat)
    sup.add_argument("--queue-dir", type=Path)
    sup.add_argument("--run-dir", type=Path, help="the desk run dir (HALT/AUTO_OFF, previews)")
    sup.add_argument("--database", type=Path)
    sup.add_argument("--account", default="DUT143714")
    sup.add_argument("--dry-run", action="store_true")
    from tree_options.desk import longrun  # `longrun run|status` (desk.longrun owns it)

    longrun.register_cli(sub)
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


def _forward_select(args: argparse.Namespace, *, clock: store.Clock) -> int:
    selected_on = args.selected_on or clock().astimezone(ET).date()
    try:
        report = forward_minutes.refresh_selection(
            selected_on=selected_on,
            strikes_per_group=args.strikes_per_group or forward_minutes.STRIKES_PER_GROUP,
            max_contracts=args.max_contracts or forward_minutes.MAX_CONTRACTS,
        )
    except forward_minutes.SelectionError as exc:
        print(f"forward-select: {exc}", file=sys.stderr)
        return 1
    print(f"forward-select: {selected_on} {report['status']} "
          f"chain={report['chain_session']} contracts={len(report['contracts'])} "
          f"skipped_groups={len(report['skipped_groups'])}")
    return 0


def _forward_minutes(
    args: argparse.Namespace,
    *,
    client: MassiveClient | None,
    clock: store.Clock,
    cal: ClosingCalendar,
) -> int:
    now = clock()
    if args.session is None:
        session = latest_completed_session(now, cal)
    elif not cal.is_session(args.session) or args.session > now.astimezone(ET).date():
        print(f"forward-minutes: {args.session} is not a past NYSE session", file=sys.stderr)
        return 2
    else:
        session = args.session
    selection = (
        json.loads(args.selection.read_text()) if args.selection
        else forward_minutes.latest_selection()
    )
    if selection is None:
        print("forward-minutes: no selection in the forward store; run forward-select",
              file=sys.stderr)
        return forward_minutes.STALE_SELECTION_EXIT
    budget = forward_minutes.WireBudget.load(
        forward_minutes.budget_path(now.astimezone(ET).date()),
        cap=args.budget or forward_minutes.DAILY_BUDGET_DEFAULT,
    )
    if client is None:
        try:
            client = forward_minutes.build_client(
                args.massive_cache or forward_minutes.default_massive_cache()
            )
        except MassiveError as exc:  # fixed text: no key material, no path
            print(f"forward-minutes: no usable Massive key ({type(exc).__name__})",
                  file=sys.stderr)
            return 1
    result = forward_minutes.capture_session(
        session, selection=selection, client=client, budget=budget, now=now,
        dry_run=args.dry_run,
    )
    print(result.line())
    if result.status in ("written", "exists"):
        verdict = forward_minutes.verify_session(session, now=now)
        print(verdict.line())
        return result.exit_code or verdict.exit_code
    return result.exit_code


def _forward_verify(args: argparse.Namespace, *, clock: store.Clock,
                    cal: ClosingCalendar) -> int:
    now = clock()
    session = args.session or latest_completed_session(now, cal)
    verdict = forward_minutes.verify_session(session, now=now)
    print(verdict.line())
    return verdict.exit_code


def _forward_coverage(args: argparse.Namespace, *, clock: store.Clock,
                      cal: ClosingCalendar) -> int:
    now = clock()
    session = args.session or latest_completed_session(now, cal)
    try:
        doc = forward_coverage.score_session(session, now=now)
    except forward_coverage.CoverageInputError as exc:
        print(f"forward-coverage: {exc}", file=sys.stderr)
        return exc.exit_code
    except ValueError as exc:  # a bars document that is not one (fixed text)
        print(f"forward-coverage: {exc}", file=sys.stderr)
        return 1
    for line in forward_coverage.render_lines(doc):
        print(line)
    return 0


def _probe_clock_tier(
    target_clock: str,
    session: date,
    *,
    transport: Transport,
    sleep: store.Sleep,
    clock: store.Clock,
    cal: ClosingCalendar,
    attempts: int = 30,
    interval_s: float = 20.0,
) -> tuple[bool, str]:
    """The delayed feed publishes on a ~1-min cadence with ~2-4 min lag
    (A0 probe 2026-10-01). Captures fire at clock+3; this probe waits, with
    ONE symbol, until the publication has rolled to the clock's content
    BEFORE sweeping the universe (a sweep that starts early records every
    name as stale and burns the window)."""
    from tree_options.desk import store as store_mod

    inst = store_mod.clock_instant(session, target_clock)
    deadline = store_mod.shift_instant(inst, store_mod.CLOCK_WINDOW_S)
    for attempt in range(attempts):
        try:
            raw = store_mod.fetch_raw("SPY", transport)
            parsed = store_mod.parse_chain(raw, "SPY")
        except Exception as exc:  # probe failures retry; the sweep reports its own
            sleep(interval_s)
            continue
        if inst <= parsed.source_as_of < deadline:
            return True, ""
        if clock() >= deadline:
            return False, (
                f"probe never saw the {target_clock} clock content before the "
                f"+{store_mod.CLOCK_WINDOW_S // 60} min window closed"
            )
        sleep(interval_s)
    return False, f"probe gave up after {attempts} attempts waiting for {target_clock}"


def _record_chains(
    args: argparse.Namespace,
    *,
    transport: Transport,
    sleep: store.Sleep,
    clock: store.Clock,
    cal: ClosingCalendar,
) -> int:
    now = clock()
    if args.clock is not None:
        from tree_options.desk import store as store_mod
        from tree_options.desk.intraday_action_graph import SCHEDULE

        now_et = now.astimezone(store_mod.ET)
        session = now_et.date()
        if not cal.is_session(session):
            print(f"record-chains: {session} is not an NYSE session", file=sys.stderr)
            return 2
        target = args.clock
        if target == "auto":
            open_clocks = [
                c
                for c in SCHEDULE
                if store_mod.clock_instant(session, c) <= now_et
                < store_mod.shift_instant(
                    store_mod.clock_instant(session, c), store_mod.CLOCK_WINDOW_S
                )
            ]
            if not open_clocks:
                print(
                    "record-chains: no decision clock is open "
                    f"({now_et:%H:%M} ET, schedule {','.join(SCHEDULE)})",
                    file=sys.stderr,
                )
                return 0
            target = open_clocks[-1]
        elif target == store_mod.EVENING:
            # the evening tier observes the settled session: every delayed
            # publication has rolled by 18:05 ET, so there is no clock
            # window to probe for and no retry pass to run
            pass
        elif target not in SCHEDULE:
            print(
                f"record-chains: --clock {target} is not a decision clock "
                f"({','.join(SCHEDULE)}) or '{store_mod.EVENING}'",
                file=sys.stderr,
            )
            return 2
        if target != store_mod.EVENING:
            ready, why = _probe_clock_tier(
                target, session, transport=transport, sleep=sleep, clock=clock, cal=cal
            )
            if not ready:
                print(f"record-chains: {why}", file=sys.stderr)
                return 1
        symbols = list(CHAIN_UNIVERSE)
        if args.symbols:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        summary = store_mod.record_session(
            session,
            symbols,
            store=store_mod.ChainStore(paths.store_root()),
            transport=transport,
            clock=clock,
            sleep=sleep,
            cal=cal,
            dry_run=args.dry_run,
            tier=target,
        )
        rc = store_mod.exit_code(summary)
        print(f"[clock {target}] " + summary.line(rc))
        return rc
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
    forward_client: MassiveClient | None = None,
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
    if args.command == "outcome-table":  # pure mechanics: writes only --out, no state lock
        from tree_options.desk.outcomes import _cli as _outcomes_cli

        return _outcomes_cli(["--bundle", str(args.bundle), "--out", str(args.out),
                              "--sync", args.sync, "--half-spread", args.half_spread,
                              "--commission", args.commission,
                              "--cost-model", args.cost_model])
    if args.command == "longrun":  # its own per-run-dir lock; `status` is read-only
        from tree_options.desk import longrun

        return longrun.dispatch_cli(args)
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
        if args.command == "clock-coverage":
            rows = store.ChainStore(paths.store_root()).clock_coverage(
                args.session if args.session is not None else clock().astimezone(store.ET).date()
            )
            if not rows:
                print("clock-coverage: no clock-tier manifests captured for that session")
                return 3
            persistent: set[str] | None = None
            for row in rows:
                print(
                    f"clock {row['clock']}: ok {row['ok']} stale {row['stale']}"
                    + (f" stale_names={','.join(row['stale_names'])}" if row["stale_names"] else "")
                )
                names = set(row["stale_names"])
                persistent = names if persistent is None else (persistent & names)
            if persistent:
                print("persistent stale (every clock): " + ",".join(sorted(persistent)))
            short = [r for r in rows if r["ok"] < args.min_ok]
            if short:
                print(
                    f"ALERT: {len(short)} clock(s) below min-ok {args.min_ok}: "
                    + ", ".join(f"{r['clock']}={r['ok']}" for r in short),
                    file=sys.stderr,
                )
                return 1
            return 0
        if args.command == "features":
            if args.session is None:
                return econ_jobs.run_features(
                    latest_completed_session(clock(), cal), cal, idempotent=args.idempotent
                )
            if not cal.is_session(args.session):
                print(f"features: {args.session} is not an NYSE session", file=sys.stderr)
                return 2
            return econ_jobs.run_features(args.session, cal, idempotent=args.idempotent)
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
        if args.command == "forward-select":
            return _forward_select(args, clock=clock)
        if args.command == "forward-minutes":
            return _forward_minutes(args, client=forward_client, clock=clock, cal=cal)
        if args.command == "forward-verify":
            return _forward_verify(args, clock=clock, cal=cal)
        if args.command == "forward-coverage":
            return _forward_coverage(args, clock=clock, cal=cal)
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
        if args.command == "lab-run":
            from tree_options.desk.lab import _cli as _lab_cli

            return _lab_cli(
                ["--bundle", str(args.bundle), "--policy", args.policy,
                 "--sessions", str(args.sessions), "--boards-cap", str(args.boards_cap)]
                + (["--windows", str(args.windows)] if args.windows else [])
                + (["--lab-root", str(args.lab_root)] if args.lab_root else []))
        if args.command == "lab-scoreboard":
            from tree_options.desk.lab import default_root as _lab_root
            from tree_options.desk.lab_scoreboard import aggregate

            print(json.dumps(aggregate(_lab_root()), indent=2))
            return 0
        if args.command == "lab-overnight":
            from tree_options.desk.lab_overnight import _cli as _overnight_cli

            return _overnight_cli(
                ["--bundle", str(args.bundle)]
                + (["--windows", str(args.windows)] if args.windows else [])
                + (["--lab-root", str(args.lab_root)] if args.lab_root else []))
        if args.command == "challenge":
            if args.challenge_command == "standings":
                from tree_options.desk import challenge as challenge_mod

                root = Path(args.bundles_from) if args.bundles_from else paths.store_root()
                standings = challenge_mod.accumulate_standings(root)
                if not args.dry_run:
                    out = Path(root) / "evaluations" / "challenge" / "standings.json"
                    out.write_text(json.dumps(standings, indent=2, default=str))
                    print(f"standings: {standings['games_counted']} post-seal game(s) -> {out}")
                else:
                    print(f"standings (dry run): {standings['games_counted']} post-seal game(s)")
                for row in standings["policies"]:
                    print(
                        f"  {row['policy']} ({row['kind']}): games {row['games']}, "
                        f"boards {row['boards']}, entered {row['entered']}, "
                        f"pnl {row['closed_pnl_sum']}, sessions {row['sessions_distinct']}"
                    )
                return 0
            if args.challenge_command == "rule-check":
                from tree_options.desk import challenge as challenge_mod

                root = Path(args.bundles_from) if args.bundles_from else paths.store_root()
                standings = challenge_mod.accumulate_standings(root)
                checks = challenge_mod.rule_check(standings)
                if not checks:
                    print("rule-check: no model policies in the standings yet")
                    return 3
                for check in checks:
                    verdict = "ALL CLAUSES PASS (operator rules on promotion)" if check["all_pass"] else "not promotable yet"
                    print(f"{check['policy']}: {verdict}")
                    for cl in check["clauses"]:
                        mark = "x" if cl["pass"] else " "
                        print(f"  [{mark}] {cl['clause']}. {cl['name']} — {cl['detail']}")
                    print(f"  holm_p={check['holm_p']} ({'pass' if check['holm_pass'] else 'fail'})")
                return 0
            from tree_options.desk.challenge import _cli as _challenge_cli

            return _challenge_cli(
                ["run"]
                + (["--bundles-from", str(args.bundles_from)]
                   if args.bundles_from else [])
                + (["--windows", str(args.windows)] if args.windows else [])
                + (["--lab-root", str(args.lab_root)] if args.lab_root else [])
                + (["--rounds", str(args.rounds)]
                   if args.rounds is not None else [])
                + (["--dry-run"] if args.dry_run else []))
        if args.command == "supervised-previews":
            from tree_options.desk import enter_supervised

            try:
                doc = enter_supervised.write_previews(
                    now=clock(), cal=cal, session=args.session, queue_dir=args.queue_dir,
                    run_dir=args.run_dir, database=args.database, account_id=args.account,
                    dry_run=args.dry_run)
            except ValueError as error:
                print(f"supervised-previews: {error}", file=sys.stderr)
                return 2
            print(json.dumps(doc, indent=2, default=str))
            return 3 if doc["status"] == "queue_not_ready" else 0
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
