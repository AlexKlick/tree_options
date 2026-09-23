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
                refreshed = datetime.fromisoformat(
                    json.loads(marker.read_text()).get("last_refresh", "")
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


def _broker_ready(source: ChainSource) -> bool:
    """Lazy broker connect (Codex-arch #2): True when scans may run.

    A down gateway must degrade the loop to market/watch work, never
    crash it at startup or mid-serve. Sources without a ``connected``
    (tests' fakes) count as ready.
    """
    connected = getattr(source, "connected", None)
    if connected is None or connected():
        return True
    try:
        source.connect()  # type: ignore[attr-defined]
        return True
    except Exception as exc:
        log.warning("gateway connect failed (scans paused, market/watch continue): %s", exc)
        return False


def serve_tick(
    source: ChainSource,
    cfg: ScanConfig,
    state_dir: Path,
    now: datetime | None = None,
    repo: Path | None = None,
    market_transport: object | None = None,
    broker_ready: bool = True,
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
        return True
    return False


def serve(
    source: ChainSource,
    cfg: ScanConfig,
    state_dir: Path,
    repo: Path | None = None,
) -> None:
    import time

    lock = ensure_lock(state_dir / "discovery.lock")
    try:
        log.info("discovery serve loop up (auto_scan_et %s)", cfg.auto_scan_et)
        # A blocking time.sleep never pumps the ib event loop, so cached
        # account values would go stale between scans; prefer the source's
        # pumping sleep when it has one (tests use plain sources without).
        sleeper = getattr(source, "sleep", None) or time.sleep
        from tree_options.trex.discovery.market import urllib_transport

        while True:
            try:
                serve_tick(
                    source,
                    cfg,
                    state_dir,
                    repo=repo,
                    market_transport=urllib_transport,
                    broker_ready=_broker_ready(source),
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
