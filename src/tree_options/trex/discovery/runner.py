"""Discovery runner: gateway chain I/O -> scan artifacts (never orders).

Owns clientId 74 (see config reserved ids). ``run_once`` performs one
scan through a duck-typed :class:`ChainSource` (the real gateway adapter
or a fake in tests); ``serve_tick`` is one loop of the long-running
service: claim a spooled request, scan, complete — or an automatic
post-close rescan at the configured ET time.
"""

from __future__ import annotations

import fcntl
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

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


def serve_tick(
    source: ChainSource,
    cfg: ScanConfig,
    state_dir: Path,
    now: datetime | None = None,
    repo: Path | None = None,
) -> bool:
    """One serve-loop cycle: manual request first, then the auto rescan.

    Returns True when a scan ran (either mode)."""
    now = now or now_et()
    spool = state_dir / "spool"
    claim = claim_scan_request(spool, now=now)
    if claim is not None:
        request_id, _payload = claim
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
    if now >= due and (last is None or last.date() != now.date()):
        run_once(source, cfg, state_dir, "auto", repo=repo, now=now)
        state.write_text(now.isoformat())
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
        while True:
            try:
                serve_tick(source, cfg, state_dir, repo=repo)
            except Exception:
                log.exception("serve tick failed")
            import time

            time.sleep(POLL_SECONDS)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
