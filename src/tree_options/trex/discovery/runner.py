"""Discovery runner: gateway chain I/O -> scan artifacts (never orders).

Owns clientId 74 (see config reserved ids). ``run_once`` performs one
scan through a duck-typed :class:`ChainSource` (the real gateway adapter
or a fake in tests); ``serve_tick`` is one loop of the long-running
service: claim a spooled request, scan, complete — or an automatic
post-close rescan at the configured ET time.
"""

from __future__ import annotations

import fcntl
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from tree_options.trex import history
from tree_options.trex.account import AccountSnapshot, write_account
from tree_options.trex.clock import now_et
from tree_options.trex.discovery.artifact import (
    build_stamp,
    claim_scan_request,
    complete_scan,
    write_scan,
)
from tree_options.trex.discovery.config import ScanConfig, config_echo
from tree_options.trex.discovery.engine import ScanInput, scan, select_top

log = logging.getLogger("trex.discovery.runner")

POLL_SECONDS = 5
# A failed connect blocks for ib_async's 20s handshake timeout; retrying
# on every 5s tick starved the market/watch work the lazy connect exists
# to protect. Dead-gateway retries are spaced out instead.
RECONNECT_BACKOFF_SECONDS = 120
ACCOUNT_HISTORY_TTL_SECONDS = 60  # equity curve cadence; discovery owns it
ACCOUNT_HISTORY_MAX_LINES = 60_000


class ChainUnavailable(RuntimeError):
    pass


class ChainSource(Protocol):
    def chain(self, symbol: str) -> tuple[list[str], list[float]] | None: ...
    def put_rows(
        self, symbol: str, expiry: str, strikes: list[float], limit: int
    ) -> list[Any]: ...
    def account(self) -> AccountSnapshot | None: ...
    def cancel_all(self) -> None: ...


def ensure_lock(lock_path: Path):
    """Take the discovery flock non-blocking; OSError = already running."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise
    return fh


def _dte(expiry: str, now: datetime) -> int | None:
    try:
        exp = datetime.strptime(expiry, "%Y%m%d")
    except ValueError:
        return None
    return (exp.date() - now.date()).days


def _pick_expiry(expirations: list[str], now: datetime, cfg: ScanConfig) -> str | None:
    for raw in expirations:
        dte = _dte(raw, now)
        if dte is not None and cfg.dte_min <= dte <= cfg.dte_max:
            return raw
    return None


def run_once(
    source: ChainSource,
    cfg: ScanConfig,
    state_dir: Path,
    mode: str,
    repo: Path | None = None,
    now: datetime | None = None,
    request_id: str | None = None,
) -> Path:
    now = now or now_et()
    scans = []
    notes: list[str] = []
    scanned = 0
    chains_ok = True
    for symbol in cfg.underlyings:
        try:
            chain = source.chain(symbol)
        except Exception as exc:  # gateway hiccup: disclose, keep scanning
            log.warning("%s: chain query failed: %s", symbol, exc)
            chain = None
        if chain is None:
            chains_ok = False
            notes.append(f"{symbol} chain unavailable")
            continue
        expirations, strikes = chain
        expiry = _pick_expiry(expirations, now, cfg)
        if expiry is None:
            chains_ok = False
            notes.append(f"{symbol}: no expiry in dte {cfg.dte_min}-{cfg.dte_max}")
            continue
        rows = source.put_rows(symbol, expiry, strikes, cfg.max_quotes_per_underlying)
        inp = ScanInput(
            underlying=symbol,
            expiry=expiry,
            dte=_dte(expiry, now) or 0,
            rows=rows,
        )
        scans.append(scan(inp, cfg, None, now))
        scanned += 1
    scans = select_top(scans, cfg)
    source.cancel_all()

    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for r in scans:
        candidates.extend(_candidate_dicts(r.candidates))
        rejected.extend(_candidate_dicts(r.rejected))
        notes.extend(n for n in r.notes if n not in notes)

    payload = {
        "generated_at": now.isoformat(),
        "mode": mode,
        "request_id": request_id,
        "config": config_echo(cfg),
        "effective_target_modes": sorted({r.effective_target_mode for r in scans}),
        "data_quality": {
            "underlyings_requested": len(cfg.underlyings),
            "underlyings_scanned": scanned,
            "chains_available": chains_ok and scanned > 0,
            "greeks_available": any(r.greeks_available for r in scans),
            "expiries_scanned": len(scans),
            "rows_quoted": sum(r.rows_quoted for r in scans),
            "rows_unquoted": sum(r.rows_unquoted for r in scans),
            "notes": notes,
        },
        "candidates": candidates,
        "rejected": rejected,
    }
    stamp = build_stamp(repo, config_echo(cfg), now, runner=mode)
    try:
        account = source.account()
        if account is not None:
            write_account(state_dir / "account.json", account)
    except Exception:  # account is auxiliary: never fail the scan for it
        log.exception("account snapshot failed (scan continues)")
    return write_scan(state_dir, payload, stamp, now)


def _candidate_dicts(candidates: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in candidates:
        out.append(
            {
                "underlying": c.underlying,
                "expiry": c.expiry,
                "dte": c.dte,
                "short_strike": c.short_strike,
                "long_strike": c.long_strike,
                "width": c.width,
                "debit_mid": c.debit_mid,
                "debit_bid": c.debit_bid,
                "debit_ask": c.debit_ask,
                "short_mid": c.short_mid,
                "long_mid": c.long_mid,
                "short_delta": c.short_delta,
                "long_delta": c.long_delta,
                "short_spread_frac": c.short_spread_frac,
                "long_spread_frac": c.long_spread_frac,
                "yield_ratio": c.yield_ratio,
                "max_profit": c.max_profit,
                "max_loss": c.max_loss,
                "target_mode_used": c.target_mode_used,
                "rank": c.rank,
                "accepted": c.accepted,
                "rules": [{"rule": r.rule, "status": r.status, "detail": r.detail} for r in c.rules],
                "reasons": list(c.reasons),
            }
        )
    return out


def _maybe_account_history(source: ChainSource, state_dir: Path, now: datetime) -> None:
    """Append an account equity sample at most once per TTL window.

    The DISCOVERY loop owns account history (a per-plan monitor dies with
    its book; this service runs continuously). Failure-isolated: a broker
    hiccup never touches scan or spool handling. Money stays in
    Decimal-strings (book-lane convention).
    """
    try:
        path = state_dir / "account_history.jsonl"
        rows = history.read_tail(path, max_bytes=64_000)
        if rows:
            last_ts = rows[-1].get("ts")
            if isinstance(last_ts, str):
                try:
                    last = datetime.fromisoformat(last_ts)
                except ValueError:
                    last = None
                if last is not None and (now - last).total_seconds() < ACCOUNT_HISTORY_TTL_SECONDS:
                    return
        snap = source.account()
        if snap is None:
            return
        history.repair_torn_tail(path)  # no-op on healthy files; prevents
        # a torn tail from swallowing the appended record
        history.append_line(
            path,
            {
                "ts": snap.ts.isoformat(),
                "account_id": snap.account_id,
                "net_liquidation": str(snap.net_liquidation),
                "cash": str(snap.cash),
                "source": "discovery",
            },
        )
        # persist tracking inception ONCE: rotation truncates old rows and
        # the retained-window boundary must never masquerade as inception
        marker = state_dir / ".tracking_since"
        if not marker.exists():
            marker.write_text(snap.ts.isoformat())
        if history.count_lines(path) > ACCOUNT_HISTORY_MAX_LINES:
            history.rotate_halving(path, ACCOUNT_HISTORY_MAX_LINES)
    except Exception:
        log.exception("account history append failed (continuing)")


def _live_plan_identities(repo: Path | None) -> list[tuple[str, object, object, object]]:
    """(underlying, expiry, short, long) of every structure in every plan
    TOML - shadow positions must never shadow a real holding."""
    from tree_options.trex.plan import load_plan

    if repo is None:
        return []
    out: list[tuple[str, object, object, object]] = []
    for path in sorted((repo / "plans").glob("*.toml")):
        try:
            plan = load_plan(path)
        except (OSError, ValueError):
            continue
        for s in plan.structures:
            out.append((s.underlying, s.expiry, s.short_strike, s.long_strike))
    return out


def _post_scan_shadow(state_dir: Path, repo: Path | None, now: datetime) -> None:
    """After a successful scan: open + mark shadow alternatives.

    Failure-isolated AFTER the scan receipt is complete (Codex-2): a
    shadow problem never fails or delays the scan itself. Marks prefer
    the CBOE full chain (works post-close; IBKR delayed quotes stop) with
    the scan's own rows as fallback.
    """
    from tree_options.trex.discovery.artifact import read_latest
    from tree_options.trex.discovery.shadow import (
        ShadowBook,
        append_shadow_mark,
        load_shadow,
        mark_from_chain,
        mark_from_payload,
        open_from_scan,
        save_shadow,
        shadow_stats,
    )

    doc = read_latest(state_dir)
    if doc is None:
        return
    payload = doc.get("payload", {})
    book = load_shadow(state_dir) or ShadowBook()
    book = open_from_scan(
        book, payload, run_id=str(doc.get("run_id", "?")), now=now,
        excluded=_live_plan_identities(repo),
    )
    marked_from_chain = False
    if any(p.status == "open" for p in book.positions):
        try:
            from tree_options.trex.discovery.market import (
                MarketCache,
                fetch_chain_puts,
                urllib_transport,
            )

            cache = MarketCache(state_dir / "market" / "cache")
            chains: dict[str, Any] = {}
            for sym in {p.underlying for p in book.positions if p.status == "open"}:
                cached = cache.get("chain", sym, now)
                if cached is None:
                    cached = fetch_chain_puts(sym, urllib_transport)
                    cache.put("chain", sym, cached, now)
                chains[sym] = cached
            book = mark_from_chain(book, chains, now=now)
            marked_from_chain = True
        except Exception:
            log.exception("chain marking failed; falling back to scan rows")
    if not marked_from_chain:
        book = mark_from_payload(book, payload, now=now)
    save_shadow(state_dir, book)
    marks = [
        {"key": p.key, "value": p.last_mark, "pnl": p.pnl, "source": p.mark_source}
        for p in book.positions
        if p.status == "open" and p.pnl is not None
    ]
    append_shadow_mark(state_dir, str(doc.get("run_id", "?")), marks, now)
    stats = shadow_stats(book)
    log.info(
        "shadow: %d open / %d expired, mean pnl %s",
        stats["open"], stats["expired"], stats["mean_pnl"],
    )


def _market_tick(
    state_dir: Path,
    cfg: ScanConfig,
    now: datetime,
    transport: object | None,
    repo: Path | None = None,
) -> None:
    """TTL-gated market refresh + on-demand force requests.

    Market data NEVER requires the IBKR connection (Codex-arch #2): this
    runs whether or not the broker session is up. Failure-isolated from
    scan handling entirely.
    """
    from tree_options.trex.discovery.artifact import claim_request, complete_request

    if transport is None:
        return  # tests that don't exercise market data skip the wire
    from tree_options.trex.discovery.market import market_cycle
    from tree_options.trex.discovery.watchlist import load_watchlist, seed_symbols

    try:
        plans_dir = (repo / "plans") if repo is not None else None
        wl = load_watchlist(
            state_dir, seed=seed_symbols(cfg, plans_dir), now=now
        )
        watch_symbols = [row["symbol"] for row in wl.get("symbols", [])]
        watch = claim_request(state_dir / "spool", ["watch"], now=now)
        if watch is not None:
            from tree_options.trex.discovery.watchlist import apply_watch_op

            _kind, w_req_id, w_payload = watch
            result = apply_watch_op(
                state_dir,
                str(w_payload.get("op", "")),
                symbol=w_payload.get("symbol"),
                proposal_id=w_payload.get("proposal_id"),
                now=now,
            )
            complete_request(
                state_dir / "spool",
                "watch",
                w_req_id,
                {"request_id": w_req_id, **result, "finished_at": now_et().isoformat()},
            )
        forced = claim_request(state_dir / "spool", ["market"], now=now)
        force_symbols: list[str] | None = None
        if forced is not None:
            _kind, req_id, payload = forced
            raw = payload.get("symbols")
            force_symbols = (
                [str(s).upper() for s in raw] if isinstance(raw, list) and raw else None
            )
        # market.json mtime gates the cadence; force bypasses it
        marker = state_dir / "market.json"
        due = not marker.exists()
        if not due:
            try:
                snap = json.loads(marker.read_text())
                # cadence runs off the last ATTEMPT: last_refresh only moves
                # on success, and a dead source must not be retried per tick
                refreshed = datetime.fromisoformat(
                    snap.get("last_attempt") or snap.get("last_refresh", "")
                )
                due = (now - refreshed).total_seconds() >= cfg.market_refresh_seconds
            except (OSError, ValueError, json.JSONDecodeError):
                due = True
        if due or forced is not None:
            market_cycle(
                state_dir,
                cfg,
                now,
                symbols=(force_symbols or watch_symbols),
                force=forced is not None,
                transport=transport,  # type: ignore[arg-type]
            )
        if forced is not None:
            complete_request(
                state_dir / "spool",
                "market",
                req_id,
                {
                    "request_id": req_id,
                    "status": "ok",
                    "finished_at": now_et().isoformat(),
                },
            )
    except Exception:
        log.exception("market tick failed (scan handling unaffected)")


def _calendar_dte(expiry_yyyymmdd: str, now: datetime) -> int:
    """Calendar days from now to the expiry session's close (instant
    arithmetic via time/sessions - never naive date subtraction)."""
    from tree_options.time.sessions import session_close_instant

    expiry = datetime.strptime(expiry_yyyymmdd, "%Y%m%d").date()
    seconds = (session_close_instant(expiry) - now).total_seconds()
    return max(0, round(seconds / 86_400))


def _backtest_tick(
    state_dir: Path,
    now: datetime,
    allow_fetch: bool,
    bars_client: object | None = None,
) -> None:
    """Materialize one requested valuation scenario (M4).

    Broker-independent. Inputs come from the runner's own artifacts
    (latest scan / shadow book, market cache) - never from the request.
    Every claim completes, with an error receipt AND an error artifact
    when inputs are missing, so the UI never polls forever.
    """
    from tree_options.trex.discovery import backtest
    from tree_options.trex.discovery.artifact import claim_request, complete_request
    from tree_options.trex.discovery.market import (
        MarketCache,
        fetch_daily_bars,
        fetch_equity_quote,
        urllib_transport,
    )

    spool = state_dir / "spool"
    claim = claim_request(spool, ["backtest"], now=now)
    if claim is None:
        return
    _kind, req_id, payload = claim
    key = str(payload.get("key", ""))

    def _fail(detail: str) -> None:
        # the receipt must land even when the artifact cannot be written
        # (bad key, full disk): an unanswered claim is re-run forever
        try:
            backtest.write_artifact(
                state_dir,
                key,
                {"key": key, "generated_at": now.isoformat(), "label": backtest.LABEL,
                 "error": detail},
            )
        except OSError as exc:
            detail = f"{detail} (artifact unwritable: {type(exc).__name__})"
        complete_request(
            spool, "backtest", req_id,
            {"request_id": req_id, "key": key, "status": "error", "detail": detail,
             "finished_at": now_et().isoformat()},
        )

    try:
        structure = backtest.find_structure(state_dir, key)
        if structure is None:
            _fail("structure not found in the latest scan or the shadow book")
            return
        structure["dte"] = _calendar_dte(structure["expiry"], now)
        sym = structure["underlying"]
        cache = MarketCache(state_dir / "market" / "cache")

        # Spot must be CURRENT (Codex M456 #8): a stale quote mixed with a
        # recomputed DTE is a silently wrong valuation. Snapshot only while
        # fresh, then the TTL cache, then a live fetch; never an expired
        # envelope. The source + as-of ride along in the artifact.
        quote: dict[str, Any] | None = None
        spot_source = ""
        try:
            snap = json.loads((state_dir / "market.json").read_text())
            snap_at = datetime.fromisoformat(snap.get("last_refresh", ""))
            if (now - snap_at).total_seconds() <= SCENARIO_QUOTE_MAX_AGE_SECONDS:
                quote = snap.get("symbols", {}).get(sym)
                spot_source = "market snapshot"
        except (OSError, KeyError, TypeError, ValueError):
            quote = None
        if quote is None:
            quote = cache.get("quote", sym, now)
            spot_source = "quote cache (ttl)"
        if quote is None and allow_fetch:
            quote = fetch_equity_quote(sym, urllib_transport)
            cache.put("quote", sym, quote, now)
            spot_source = "live fetch"
        bid, ask = (quote or {}).get("bid"), (quote or {}).get("ask")
        if bid is None or ask is None:
            _fail(f"no fresh {sym} quote (refresh the market desk first)")
            return
        spot_now = (float(bid) + float(ask)) / 2
        structure["spot_source"] = spot_source
        structure["spot_as_of"] = (quote or {}).get("source_as_of")

        bars_env = cache.get_envelope("bars", sym)
        if (bars_env is None or cache.get("bars", sym, now) is None) and allow_fetch:
            fresh = fetch_daily_bars(sym, bars_client)  # type: ignore[arg-type]
            if fresh:
                cache.put("bars", sym, {"bars": fresh}, now)
                bars_env = cache.get_envelope("bars", sym)
        raw_bars = ((bars_env or {}).get("payload") or {}).get("bars") or []
        bars = [(int(b["t"]), float(b["c"])) for b in raw_bars if b.get("c") is not None]
        if not bars:
            _fail(f"no {sym} daily bars cached")
            return

        doc = backtest.materialize(
            state_dir, key, now, structure, spot_now, bars, (quote or {}).get("iv30")
        )
        complete_request(
            spool, "backtest", req_id,
            {"request_id": req_id, "key": key,
             "status": "error" if doc.get("error") else "ok",
             "detail": doc.get("error"), "finished_at": now_et().isoformat()},
        )
    except Exception as exc:
        log.exception("backtest %s failed", key)
        _fail(f"{type(exc).__name__}: {exc}")


PROPOSE_MIN_INTERVAL_SECONDS = 6 * 3600  # post-scan hook cadence cap
SCENARIO_QUOTE_MAX_AGE_SECONDS = 900  # snapshot freshness for scenario spot


def _proposal_context(state_dir: Path, symbols: list[str], now: datetime) -> dict[str, Any]:
    """Compact, runner-owned facts for the model (no prices invented)."""
    from tree_options.trex.discovery.market import MarketCache

    quotes: dict[str, Any] = {}
    try:
        quotes = json.loads((state_dir / "market.json").read_text()).get("symbols", {})
    except (OSError, ValueError):
        quotes = {}
    cache = MarketCache(state_dir / "market" / "cache")
    watch: list[dict[str, Any]] = []
    for sym in symbols:
        q = quotes.get(sym) or {}
        env = cache.get_envelope("news", sym)
        items = ((env or {}).get("payload") or {}).get("items") or []
        watch.append(
            {
                "symbol": sym,
                "change_pct": q.get("change_pct"),
                "iv30": q.get("iv30"),
                "headlines": [str(i.get("title", ""))[:100] for i in items[:3]],
            }
        )
    top: list[dict[str, Any]] = []
    try:
        payload = json.loads((state_dir / "latest.json").read_text()).get("payload", {})
        for row in payload.get("candidates", [])[:8]:
            top.append(
                {k: row.get(k) for k in (
                    "underlying", "expiry", "dte", "long_strike", "short_strike",
                    "debit_mid", "yield_ratio",
                )}
            )
    except (OSError, ValueError):
        pass
    return {"today": now.date().isoformat(), "watchlist": watch, "latest_scan_top": top}


def _run_proposals(
    state_dir: Path,
    cfg: ScanConfig,
    now: datetime,
    trigger: str,
    source_id: str,
    llm_transport: object,
    market_transport: object | None,
) -> dict[str, Any]:
    """One propose pass: context -> provider chain -> vet -> verify ->
    record as PENDING (the operator approves or dismisses). Never raises
    into the caller's scan handling; a failed pass records its notes."""
    from tree_options.trex.discovery import llm
    from tree_options.trex.discovery.config import llm_chain
    from tree_options.trex.discovery.market import fetch_equity_quote
    from tree_options.trex.discovery.watchlist import (
        blocked_symbols,
        load_watchlist,
        record_proposals,
    )

    doc = load_watchlist(state_dir)
    watched = {row["symbol"] for row in doc.get("symbols", [])}
    blocked = blocked_symbols(doc, now)
    run = llm.propose(
        llm_chain(cfg.llm_provider),
        _proposal_context(state_dir, sorted(watched), now),
        watched=watched,
        blocked=blocked,
        max_n=cfg.llm_max_proposals,
        model_override=cfg.llm_model,
        transport=llm_transport,  # type: ignore[arg-type]
    )
    vetted: list[dict[str, Any]] = []
    for prop in run["proposals"]:
        if prop["action"] == "add" and market_transport is not None:
            try:
                quote = fetch_equity_quote(prop["symbol"], market_transport)  # type: ignore[arg-type]
                real = quote.get("bid") is not None and quote.get("ask") is not None
            except Exception:
                real = False
            if not real:
                run["notes"].append(f"{prop['symbol']}: no CBOE quote - unverified ticker dropped")
                continue
        vetted.append(prop)
    record_proposals(
        state_dir,
        vetted,
        provenance={
            "provider": run["provider"],
            "model": run["model"],
            "trigger": trigger,
            "source_id": source_id,
        },
        now=now,
        run_note={
            "status": run["status"],
            "provider": run["provider"],
            "model": run["model"],
            "elapsed_s": run["elapsed_s"],
            "trigger": trigger,
            "notes": run["notes"][:8],
        },
    )
    return run


def _propose_tick(
    state_dir: Path,
    cfg: ScanConfig,
    now: datetime,
    llm_transport: object | None,
    market_transport: object | None,
) -> None:
    """On-demand proposals from the spool (MarketPage button)."""
    from tree_options.trex.discovery.artifact import claim_request, complete_request

    if llm_transport is None:
        return
    claim = claim_request(state_dir / "spool", ["propose"], now=now)
    if claim is None:
        return
    _kind, req_id, _payload = claim
    try:
        run = _run_proposals(
            state_dir, cfg, now, "operator", req_id, llm_transport, market_transport
        )
        result = {"status": run["status"], "provider": run["provider"],
                  "count": len(run["proposals"])}
    except Exception as exc:
        log.exception("proposal pass failed")
        result = {"status": "error", "detail": f"{type(exc).__name__}"}
    complete_request(
        state_dir / "spool", "propose", req_id,
        {"request_id": req_id, **result, "finished_at": now_et().isoformat()},
    )


def _post_scan_proposals(
    state_dir: Path,
    cfg: ScanConfig,
    now: datetime,
    run_id: str,
    llm_transport: object | None,
    market_transport: object | None,
) -> None:
    """Post-scan hook: at most one pass per PROPOSE_MIN_INTERVAL_SECONDS."""
    from tree_options.trex.discovery.watchlist import load_watchlist

    if llm_transport is None:
        return
    last = (load_watchlist(state_dir).get("last_proposal_run") or {}).get("at")
    if isinstance(last, str):
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < PROPOSE_MIN_INTERVAL_SECONDS:
                return
        except ValueError:
            pass
    _run_proposals(state_dir, cfg, now, "post-scan", run_id, llm_transport, market_transport)


class ConnectBackoff:
    """Spaces out reconnect attempts to a dead gateway."""

    def __init__(
        self,
        seconds: float = RECONNECT_BACKOFF_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.seconds = seconds
        self.clock = clock
        self.next_try = 0.0

    def allow(self) -> bool:
        return self.clock() >= self.next_try

    def failed(self) -> None:
        self.next_try = self.clock() + self.seconds

    def succeeded(self) -> None:
        self.next_try = 0.0


def _broker_ready(source: ChainSource, backoff: ConnectBackoff | None = None) -> bool:
    """Lazy broker connect (Codex-arch #2): True when scans may run.

    A down gateway must degrade the loop to market/watch work, never
    crash it at startup or mid-serve. Sources without a ``connected``
    (tests' fakes) count as ready. With a backoff, a failed connect is
    not retried until the backoff window passes.
    """
    connected = getattr(source, "connected", None)
    if connected is None or connected():
        return True
    if backoff is not None and not backoff.allow():
        return False
    try:
        source.connect()  # type: ignore[attr-defined]
    except Exception as exc:
        if backoff is not None:
            backoff.failed()
        log.warning(
            "gateway connect failed (scans paused, market/watch continue; retry in %ss): %s",
            backoff.seconds if backoff is not None else 0,
            exc,
        )
        return False
    if backoff is not None:
        backoff.succeeded()
    return True


def serve_tick(
    source: ChainSource,
    cfg: ScanConfig,
    state_dir: Path,
    now: datetime | None = None,
    repo: Path | None = None,
    market_transport: object | None = None,
    broker_ready: bool = True,
    llm_transport: object | None = None,
) -> bool:
    """One serve-loop cycle: manual request first, then the auto rescan.

    ``broker_ready=False`` (gateway down) skips broker work but still
    runs the market tick and completes scan claims with an error receipt
    so the UI never waits on a dead gateway. Returns True when a scan
    ran (either mode)."""
    now = now or now_et()
    if broker_ready:
        _maybe_account_history(source, state_dir, now)
    _market_tick(state_dir, cfg, now, market_transport, repo=repo)
    try:
        _backtest_tick(state_dir, now, allow_fetch=market_transport is not None)
    except Exception:
        log.exception("backtest tick failed (scan handling unaffected)")
    try:
        _propose_tick(state_dir, cfg, now, llm_transport, market_transport)
    except Exception:
        log.exception("propose tick failed (scan handling unaffected)")
    spool = state_dir / "spool"
    claim = claim_scan_request(spool, now=now)
    if claim is not None:
        request_id, _payload = claim
        if not broker_ready:
            complete_scan(
                spool,
                request_id,
                {
                    "request_id": request_id,
                    "status": "error",
                    "detail": "gateway unreachable (market/watch unaffected)",
                    "finished_at": now_et().isoformat(),
                },
            )
            return False
        try:
            run_once(source, cfg, state_dir, "manual", repo=repo, now=now, request_id=request_id)
            complete_scan(
                spool,
                request_id,
                {
                    "request_id": request_id,
                    "status": "ok",
                    "started_at": now.isoformat(),
                    "finished_at": now_et().isoformat(),
                },
            )
            try:
                _post_scan_shadow(state_dir, repo, now)
            except Exception:
                log.exception("post-scan shadow hook failed (scan unaffected)")
            try:
                _post_scan_proposals(
                    state_dir, cfg, now, request_id, llm_transport, market_transport
                )
            except Exception:
                log.exception("post-scan proposals failed (scan unaffected)")
            return True
        except Exception as exc:
            log.exception("scan for request %s failed", request_id)
            complete_scan(
                spool,
                request_id,
                {
                    "request_id": request_id,
                    "status": "error",
                    "detail": str(exc),
                    "finished_at": now_et().isoformat(),
                },
            )
            return True

    # auto rescan: at/after auto_scan_et, once per calendar day
    state = state_dir / ".last_auto_scan"
    hh, mm = cfg.auto_scan_et.split(":")
    due = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    last = None
    if state.exists():
        try:
            last = datetime.fromisoformat(state.read_text().strip())
        except ValueError:
            last = None
    if broker_ready and now >= due and (last is None or last.date() != now.date()):
        run_once(source, cfg, state_dir, "auto", repo=repo, now=now)
        state.write_text(now.isoformat())
        try:
            _post_scan_shadow(state_dir, repo, now)
        except Exception:
            log.exception("post-scan shadow hook failed (scan unaffected)")
        try:
            _post_scan_proposals(state_dir, cfg, now, "auto", llm_transport, market_transport)
        except Exception:
            log.exception("post-scan proposals failed (scan unaffected)")
        return True
    return False


def serve(
    source: ChainSource,
    cfg: ScanConfig,
    state_dir: Path,
    repo: Path | None = None,
) -> None:
    lock = ensure_lock(state_dir / "discovery.lock")
    try:
        log.info("discovery serve loop up (auto_scan_et %s)", cfg.auto_scan_et)
        # A blocking time.sleep never pumps the ib event loop, so cached
        # account values would go stale between scans; prefer the source's
        # pumping sleep when it has one (tests use plain sources without).
        sleeper = getattr(source, "sleep", None) or time.sleep
        from tree_options.trex.discovery.llm import urllib_post
        from tree_options.trex.discovery.market import urllib_transport

        backoff = ConnectBackoff()
        while True:
            try:
                serve_tick(
                    source,
                    cfg,
                    state_dir,
                    repo=repo,
                    market_transport=urllib_transport,
                    broker_ready=_broker_ready(source, backoff),
                    llm_transport=urllib_post,
                )
            except Exception:
                log.exception("serve tick failed")
            try:
                sleeper(POLL_SECONDS)
            except Exception:
                # a gateway that died mid-serve can take its event loop
                # (and this sleeper) with it; keep the loop alive
                log.exception("serve sleep failed (plain fallback)")
                time.sleep(POLL_SECONDS)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
