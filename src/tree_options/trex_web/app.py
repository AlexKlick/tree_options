"""FastAPI app for the trex read-only cockpit (SPA shell + JSON API)."""

from __future__ import annotations

import json
import os
import re
import socket
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from tree_options.trex.clock import now_et
from tree_options.trex.discovery.artifact import write_request, write_scan_request
from tree_options.trex_web.discovery_view import discovery_payload
from tree_options.trex_web.payoff import (
    payoff_series,
    pnl_history_series,
    summarize_book,
)
from tree_options.trex_web.portfolio import plan_realized, plan_unrealized, portfolio_rollup
from tree_options.trex_web.positions import merge_net_positions, net_positions
from tree_options.trex_web.reader import (
    compute_runbook_status_from_view,
    list_plans,
    load_marks,
    load_plan_view,
    marks_age_seconds,
)

# Defaults picked to match the deployed host conventions. Both can be
# overridden via ``TREX_STATE`` / ``TREX_PLANS_DIR`` (the env vars) or the
# ``--state-dir`` / ``--plans-dir`` CLI flags in ``__main__.main``.
DEFAULT_STATE_ROOT = Path("~/.local/state/trex").expanduser()
DEFAULT_PLANS_ROOT = Path("~/documents/tree_options/plans").expanduser()

# IB Gateway paper API. The monitor + enter runners connect here; a TCP probe
# from the web lane confirms the container is up without contacting the
# broker (the gateway's API requires an IBKR session handshake, which is
# the monitor's job, not the GUI's).
DEFAULT_GATEWAY_HOST = "127.0.0.1"
DEFAULT_GATEWAY_PORT = 4002
GATEWAY_PROBE_TIMEOUT_SECONDS = 1.0

# Discovery artifacts + scan-on-demand spool (written ONLY here — the one
# directory the systemd unit's ReadWritePaths allows).
DEFAULT_DISCOVERY_ROOT = Path("~/.local/state/trex-discovery").expanduser()
DEFAULT_DISCOVERY_CONFIG = Path("~/.config/trex/discovery.toml").expanduser()

# shadow_key format: "QQQ|20261016|642|657" (strikes via {:g})
# strikes bounded (<= 6 integer + 3 decimal digits): the key becomes a
# filename, and an unbounded one can exceed NAME_MAX and wedge the request
_SCENARIO_KEY_RE = re.compile(r"^[A-Z.]{1,6}\|\d{8}\|\d{1,6}(\.\d{1,3})?\|\d{1,6}(\.\d{1,3})?$")

# Served at / when the built SPA is missing (fresh clone, interrupted
# build): tell the operator exactly what to run instead of crashing.
_FALLBACK_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>trex cockpit - not built</title>
<style>body{background:#101725;color:#e9edf6;font:16px/1.6 system-ui,sans-serif;
display:grid;place-items:center;min-height:100vh;margin:0}code{background:#0b1322;
padding:2px 6px;border-radius:6px}</style></head><body>
<div style="max-width:34rem"><h1>trex cockpit: web app not built</h1>
<p>The JSON API is live, but the SPA bundle is missing from this install.</p>
<p>Build it on the host (node is interactive-shells only):</p>
<pre><code>cd web &amp;&amp; npm ci &amp;&amp; npm run build</code></pre>
<p>then restart <code>trex-web.service</code>. No server-side change is needed.</p>
</div></body></html>
"""


def _resolve_state_root(override: str | None) -> Path:
    raw = os.environ.get("TREX_STATE") or override or str(DEFAULT_STATE_ROOT)
    return Path(raw).expanduser()


def _resolve_plans_root(override: str | None) -> Path:
    raw = os.environ.get("TREX_PLANS_DIR") or override or str(DEFAULT_PLANS_ROOT)
    return Path(raw).expanduser()


def probe_gateway(
    host: str = DEFAULT_GATEWAY_HOST,
    port: int = DEFAULT_GATEWAY_PORT,
    timeout: float = GATEWAY_PROBE_TIMEOUT_SECONDS,
) -> bool:
    """TCP-level liveness check on the IB Gateway. Cheap; does not authenticate.
    The web lane never logs in to the gateway — that's the monitor's job."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (TimeoutError, OSError):
        return False


def _fnum(raw: object) -> float | None:
    """marks.json string -> float (None on junk); the web lane's only coercion."""
    if raw is None:
        return None
    try:
        return float(str(raw))
    except ValueError:
        return None


def _bars_series(bars_list: object) -> dict[str, object] | None:
    """Daily closes -> chart series; a PRICE axis fits the data (level
    extent), never anchored at $0."""
    from tree_options.trex.series import decimate_pairs, level_extent

    if not isinstance(bars_list, list):
        return None
    pts = [
        (int(b["t"]), float(b["c"]))
        for b in bars_list
        if isinstance(b, dict) and b.get("t") is not None and b.get("c") is not None
    ]
    pts = decimate_pairs(pts, 600)
    if not pts:
        return None
    y_lo, y_hi = level_extent(pts)
    return {
        "points": [[t, v] for t, v in pts],
        "y_lo": y_lo,
        "y_hi": y_hi,
        "last": {"ts_ms": pts[-1][0], "value": pts[-1][1], "pos": True},
    }


def _marks_payload(marks: dict[str, Any] | None) -> dict[str, Any] | None:
    """JSON view of marks.json: Decimals-as-strings coerced to numbers."""
    if not marks:
        return None
    structures: dict[str, dict[str, Any]] = {}
    raw_rows = marks.get("structures")
    if isinstance(raw_rows, dict):
        for sid, row in raw_rows.items():
            if not isinstance(row, dict):
                continue
            structures[sid] = {
                "qty": row.get("qty"),
                "entry": _fnum(row.get("entry")),
                "bid": _fnum(row.get("bid")),
                "ask": _fnum(row.get("ask")),
                "mark": _fnum(row.get("mark")),
                "unrealized": _fnum(row.get("unrealized")),
                # fill packages without a reported price (R3-02): the entry
                # average covers the priced subset only — disclose, don't drop
                "unpriced": row.get("unpriced"),
            }
    spots: dict[str, float] = {}
    raw_spots = marks.get("spots")
    if isinstance(raw_spots, dict):
        for sym, px in raw_spots.items():
            val = _fnum(px)
            if val is not None:
                spots[sym] = val
    raw_ts = marks.get("ts")
    return {
        "ts": raw_ts if isinstance(raw_ts, str) else None,
        "age_seconds": marks_age_seconds(marks),
        "total_unrealized": _fnum(marks.get("total_unrealized")),
        "spots": spots,
        "structures": structures,
    }


def _plans_payload(
    state_root: Path,
    plans_root: Path,
    discovery_dir: Path | None = None,
) -> dict[str, object]:
    """GET /api/plans body: per-plan summaries + portfolio + account."""
    plans: list[dict[str, object]] = []
    rollup_entries: list[tuple[Any, dict[str, Any] | None]] = []
    position_rows: list[tuple[str, list[dict[str, Any]]]] = []
    for view in list_plans(state_root, plans_root):
        rb = compute_runbook_status_from_view(view)
        marks = load_marks(state_root, view.plan.id)
        marks_view = _marks_payload(marks)
        u_open, u_filled = plan_unrealized(view, marks)
        realized, _partial = plan_realized(view)
        specs = [
            {
                "id": s.id,
                "underlying": s.underlying,
                "long_strike": float(s.long_strike),
                "short_strike": float(s.short_strike),
                "expiry": s.expiry.isoformat(),
            }
            for s in view.plan.structures
        ]
        states = {
            sid: {
                "open_qty": st.open_qty,
                "entry_fill": float(st.entry_fill) if st.entry_fill is not None else None,
                "filled_qty": st.filled_qty,
                "entry_unpriced_qty": st.entry_unpriced_qty,
                "exit_unpriced_qty": st.exit_unpriced_qty,
            }
            for sid, st in view.structures.items()
            if st is not None
        }
        position_rows.append(
            (
                view.plan.id,
                net_positions(
                    specs, states, marks_view.get("structures") if marks_view else None
                ),
            )
        )
        plans.append(
            {
                "id": view.plan.id,
                "account_mode": str(view.plan.account_mode),
                "entry_date": rb.entry_date.isoformat(),
                "entry_window_start": str(view.plan.entry_window_start),
                "entry_window_end": str(view.plan.entry_window_end),
                "structure_count": len(view.plan.structures),
                "total_debit_cap": float(view.plan.total_debit_cap),
                "committed_at_caps": float(view.plan.committed_at_caps),
                "state_present": view.state_present,
                "armed": view.armed,
                "heartbeat": view.heartbeat.isoformat() if view.heartbeat else None,
                "worst_state": view.worst_state.value if view.worst_state else None,
                "window_state": rb.window_state,
                "open_qty": view.total_open_qty,
                "days_to_expiry": view.days_to_expiry,
                "days_to_deadline": view.days_to_deadline,
                "unrealized_open": u_open,
                "unrealized_filled": u_filled,
                "realized": realized,
            }
        )
        rollup_entries.append((view, marks))
    portfolio = portfolio_rollup(rollup_entries)
    account_payload, accounts_seen = _freshest_account(state_root, discovery_dir)
    return {
        "now": now_et().isoformat(),
        "gateway_reachable": probe_gateway(),
        "plans": plans,
        "portfolio": portfolio,
        "net_positions": merge_net_positions(position_rows),
        "account": account_payload,
        "accounts_seen": accounts_seen,
    }


def _freshest_account(
    state_root: Path, discovery_dir: Path | None
) -> tuple[dict[str, Any] | None, list[str]]:
    """Freshest account.json across plan run dirs (+ discovery copy)."""
    from tree_options.trex.account import freshest

    candidates = sorted(state_root.glob("*/account.json"))
    if discovery_dir is not None:
        candidates.append(discovery_dir / "account.json")
    winner = freshest(candidates)
    if winner is None:
        return None, []
    path, payload = winner
    payload = dict(payload)
    # plain numbers at the boundary (repo convention); keep the broker ts
    for key in ("net_liquidation", "cash", "buying_power"):
        try:
            payload[key] = float(payload[key])
        except (KeyError, TypeError, ValueError):
            pass
    payload["source"] = str(path)
    payload["age_seconds"] = _account_age(payload)
    seen: list[str] = []
    for candidate in candidates:
        data = freshest([candidate])
        if data is None:
            continue
        account_id = str(data[1].get("account_id", ""))
        if account_id and account_id not in seen:
            seen.append(account_id)
    return payload, seen


def _account_age(payload: dict[str, Any]) -> int | None:
    from tree_options.trex.account import account_age_seconds

    return account_age_seconds(payload)


def _plan_payload(
    plan_id: str, state_root: Path, plans_root: Path
) -> dict[str, object] | None:
    """GET /api/plans/{id} body: everything the plan detail view renders.

    Plain numbers + ISO strings (the client formats); prebuilt _usd label
    strings ride along inside each payoff payload.
    """
    view = load_plan_view(plan_id, state_root, plans_root)
    if view is None:
        return None
    rb = compute_runbook_status_from_view(view)
    marks = load_marks(state_root, plan_id)
    marks_view = _marks_payload(marks)
    raw_spot_map: object = marks_view.get("spots", {}) if marks_view else {}
    spot_map: dict[str, float] = raw_spot_map if isinstance(raw_spot_map, dict) else {}

    specs: list[dict[str, object]] = []
    structures: dict[str, dict[str, object]] = {}
    legs: list[tuple[float, float, float, int]] = []
    payoffs: list[dict[str, object]] = []
    for s in view.plan.structures:
        specs.append(
            {
                "id": s.id,
                "underlying": s.underlying,
                "entry_date": s.entry_date.isoformat(),
                "expiry": s.expiry.isoformat(),
                "exit_deadline": s.exit_deadline.isoformat(),
                "long_strike": float(s.long_strike),
                "short_strike": float(s.short_strike),
                "width": float(s.width),
                "quantity": s.quantity,
                "limit_cap": float(s.limit_cap),
                "days_to_expiry": view.days_to_expiry.get(s.id),
                "days_to_deadline": view.days_to_deadline.get(s.id),
            }
        )
        st = view.structures.get(s.id)
        if st is None:
            continue
        structures[s.id] = {
            "state": st.state.value,
            "entry_fill": float(st.entry_fill) if st.entry_fill is not None else None,
            "filled_qty": st.filled_qty,
            "open_qty": st.open_qty,
            "exit_fill": float(st.exit_fill) if st.exit_fill is not None else None,
            "exit_filled_qty": st.exit_filled_qty,
            "entry_cycles": st.entry_cycles,
            "exit_cycles": st.exit_cycles,
            "exit_reason": st.exit_reason,
            "close_reason": st.close_reason,
            "touch_ts": st.touch_ts.isoformat() if st.touch_ts else None,
            "updated_at": st.updated_at.isoformat() if st.updated_at else None,
            "realized_pnl": float(st.realized_pnl) if st.realized_pnl is not None else None,
            # price coverage (R3-02): a nonzero count means the entry/exit
            # average spans the priced packages only — no whole-position
            # payoff or cost may be derived from it
            "entry_unpriced_qty": st.entry_unpriced_qty,
            "exit_unpriced_qty": st.exit_unpriced_qty,
        }
        # a payoff labels max loss with the full entry basis: with unpriced
        # fills that label would be fabricated, so the series is withheld
        if st.entry_fill is not None and st.filled_qty > 0 and not st.entry_unpriced_qty:
            entry_f = float(st.entry_fill)
            long_f, short_f = float(s.long_strike), float(s.short_strike)
            legs.append((long_f, short_f, entry_f, st.filled_qty))
            series = payoff_series(
                long_f, short_f, entry_f, st.filled_qty, spot_map.get(s.underlying)
            )
            if series is not None:
                series["structure_id"] = s.id
                series["underlying"] = s.underlying
                payoffs.append(series)

    book_summary: dict[str, Any] | None = summarize_book(legs) if legs else None

    history: dict[str, object] | None = None
    raw_history = marks.get("history") if marks else None
    if isinstance(raw_history, list):
        history = pnl_history_series([h for h in raw_history if isinstance(h, dict)])

    return {
        "now": now_et().isoformat(),
        "gateway_reachable": probe_gateway(),
        "plan": {
            "id": view.plan.id,
            "account_mode": str(view.plan.account_mode),
            "entry_window_start": str(view.plan.entry_window_start),
            "entry_window_end": str(view.plan.entry_window_end),
            "total_debit_cap": float(view.plan.total_debit_cap),
            "committed_at_caps": float(view.plan.committed_at_caps),
            "structures": specs,
        },
        "runbook": {
            "now_et": rb.now_et.isoformat(),
            "entry_date": rb.entry_date.isoformat(),
            "window_state": rb.window_state,
            "monitor_armed": rb.monitor_armed,
            "heartbeat": rb.heartbeat.isoformat() if rb.heartbeat else None,
            "deadline_breached": rb.deadline_breached,
            "expiry_within_a_week": rb.expiry_within_a_week,
            "days_to_exit_deadline": rb.days_to_exit_deadline,
            "days_to_expiry": rb.days_to_expiry,
        },
        "state_present": view.state_present,
        "structures": structures,
        "marks": marks_view,
        "book_summary": book_summary,
        "net_positions": net_positions(specs, structures, marks_view.get("structures") if marks_view else None),
        "payoffs": payoffs,
        "history": history,
        "events": view.events,
    }


def create_app(
    state_dir: str | None = None,
    plans_dir: str | None = None,
    static_dir: str | None = None,
    discovery_dir: str | None = None,
    gateway_state: str | None = None,
    exit_watch_state: str | None = None,
    desk_paper_dir: str | None = None,
    desk_store_dir: str | None = None,
    market_cache_dir: str | None = None,
    desk_state_dir: str | None = None,
) -> FastAPI:
    """Build the FastAPI app. Public for tests; production wires ``__main__``.

    ``static_dir`` overrides the built-SPA directory (default: the
    package's ``static/``). The mount is created only when the directory
    exists, so the pytest gate and fresh clones never need a node build.
    """
    state_root = _resolve_state_root(state_dir)
    plans_root = _resolve_plans_root(plans_dir)
    static_root = (
        Path(static_dir).expanduser() if static_dir else Path(__file__).parent / "static"
    )
    discovery_root = (
        Path(discovery_dir).expanduser()
        if discovery_dir
        else Path(os.environ.get("TREX_DISCOVERY_DIR", str(DEFAULT_DISCOVERY_ROOT)))
    )
    discovery_config = Path(
        os.environ.get("TREX_DISCOVERY_CONFIG", str(DEFAULT_DISCOVERY_CONFIG))
    )
    from tree_options.trex_web.gateway_view import (
        DEFAULT_EXIT_WATCH_STATE,
        DEFAULT_GATEWAY_STATE,
        exit_machine_view,
        gateway_view,
    )

    gateway_state_path = (
        Path(gateway_state).expanduser()
        if gateway_state
        else Path(os.environ.get("TREX_GATEWAY_STATE", str(DEFAULT_GATEWAY_STATE)))
    )
    exit_watch_path = (
        Path(exit_watch_state).expanduser()
        if exit_watch_state
        else Path(os.environ.get("TREX_EXIT_WATCH_STATE", str(DEFAULT_EXIT_WATCH_STATE)))
    )
    from tree_options.desk.paths import paper_dir as _desk_paper_dir
    from tree_options.desk.paths import queue_dir as _desk_queue_dir
    from tree_options.desk.paths import state_root as _desk_state_root
    from tree_options.desk.paths import store_root as _desk_store_root

    desk_paper_root = (
        Path(desk_paper_dir).expanduser() if desk_paper_dir else _desk_paper_dir()
    )
    desk_panel_path = desk_paper_root / "ohlc-panel.json"
    desk_store_root = (
        Path(desk_store_dir).expanduser() if desk_store_dir else _desk_store_root()
    )
    # the desk's job state (signals + the miner's entry queue). Default:
    # desk.paths' own conventions (env-overridable); an explicit override
    # pins BOTH under one root, the way the deployed unit sees them.
    desk_state_root = (
        Path(desk_state_dir).expanduser() if desk_state_dir else _desk_state_root()
    )
    desk_signals_dir = desk_state_root / "signals"
    desk_queue_dir = desk_state_root / "queue" if desk_state_dir else _desk_queue_dir()
    # the discovery lane's market cache (bars/news/viewchain envelopes);
    # derived from the already-resolved discovery root, never a fresh env read
    market_cache_root = (
        Path(market_cache_dir).expanduser()
        if market_cache_dir
        else discovery_root / "market" / "cache"
    )

    app = FastAPI(
        title="trex options cockpit",
        docs_url=None,  # operator tool; no Swagger UI in prod
        redoc_url=None,
        openapi_url=None,
    )

    from tree_options.trex_web.desk_view import attach as attach_desk_evidence

    attach_desk_evidence(app, database=desk_state_root / "evidence" / "desk.sqlite3")

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "ok": True,
            "state_root": str(state_root),
            "plans_root": str(plans_root),
            "state_root_exists": state_root.exists(),
            "plans_root_exists": plans_root.exists(),
            "gateway_reachable": probe_gateway(),
        }

    @app.get("/api/gateway")
    def api_gateway() -> dict[str, object]:
        """Gateway watchdog verdict for the cockpit banner (read-only)."""
        return gateway_view(gateway_state_path, time.time())

    @app.get("/api/exit-machine")
    def api_exit_machine() -> dict[str, object]:
        """Exit-machine watchdog verdict for the cockpit banner (read-only)."""
        return exit_machine_view(exit_watch_path, time.time())

    @app.get("/api/plans")
    def api_plans() -> dict[str, object]:
        return _plans_payload(state_root, plans_root, discovery_root)

    @app.get("/api/plans/{plan_id}")
    def api_plan_detail(plan_id: str) -> dict[str, object]:
        payload = _plan_payload(plan_id, state_root, plans_root)
        if payload is None:
            raise HTTPException(status_code=404, detail=f"plan {plan_id!r} not found")
        return payload

    @app.get("/api/discovery")
    def api_discovery() -> dict[str, object]:
        return discovery_payload(discovery_root, discovery_config)

    @app.get("/api/stats")
    def api_stats() -> dict[str, object]:
        from tree_options.trex_web.stats import stats_payload

        return stats_payload(state_root, plans_root, discovery_root, now_et())

    @app.get("/api/market")
    def api_market() -> dict[str, object]:
        """Market snapshot written by the discovery lane's market cycle.
        Freshness = the snapshot's own last_refresh, never transport."""
        from tree_options.trex.discovery.watchlist import read_watchlist
        from tree_options.trex_web.discovery_view import _age

        path = discovery_root / "market.json"
        doc: dict[str, Any] | None = None
        if path.exists():
            try:
                doc = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                doc = None
        wl = read_watchlist(discovery_root)  # never writes (GET)
        base: dict[str, Any]
        if doc is None:
            base = {
                "last_refresh": None,
                "symbols": {},
                "errors": {},
                "age_seconds": None,
            }
        else:
            base = {
                "last_refresh": doc.get("last_refresh"),
                "symbols": doc.get("symbols", {}),
                "errors": doc.get("errors", {}),
                "age_seconds": _age(doc.get("last_refresh"), now_et()),
            }
        base["watchlist"] = [row.get("symbol") for row in wl.get("symbols", [])]
        base["watch_origins"] = {
            row.get("symbol"): row.get("origin") for row in wl.get("symbols", [])
        }
        base["proposals"] = [
            p for p in wl.get("proposals", []) if p.get("status") == "pending"
        ]
        base["last_proposal_run"] = wl.get("last_proposal_run")
        base["now"] = now_et().isoformat()
        return base

    @app.post("/api/market/propose", status_code=202)
    def api_market_propose() -> dict[str, object]:
        """Ask the discovery runner's LLM chain for watchlist ideas. The
        result lands as PENDING proposals the operator approves/dismisses."""
        request_id = uuid.uuid4().hex[:12]
        try:
            write_request(
                discovery_root / "spool",
                "propose",
                request_id,
                {"request_ts": now_et().isoformat()},
            )
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"spool unwritable ({exc}); trex-web ReadWritePaths missing?",
            ) from exc
        return {"accepted": True, "request_id": request_id}

    @app.get("/api/market/{sym}")
    def api_market_symbol(sym: str) -> dict[str, object]:
        """Symbol detail: quote + daily bars + news, assembled from the
        discovery lane's cache envelopes. Cold caches return None sections
        (the UI offers a refresh, which warms them via the spool)."""
        import re as _re

        from tree_options.trex.discovery.market import MarketCache
        from tree_options.trex_web.discovery_view import _age

        sym_up = sym.upper()
        if not _re.match(r"^[A-Z.]{1,6}$", sym_up):
            raise HTTPException(status_code=404, detail="unknown symbol")
        now = now_et()
        cache = MarketCache(discovery_root / "market" / "cache")
        quote_doc = None
        market_path = discovery_root / "market.json"
        if market_path.exists():
            try:
                quote_doc = json.loads(market_path.read_text())
            except (OSError, json.JSONDecodeError):
                quote_doc = None
        quote = (quote_doc or {}).get("symbols", {}).get(sym_up)
        if quote is None:
            quote = cache.get("quote", sym_up, now)
        bars_env = cache.get_envelope("bars", sym_up)
        news_env = cache.get_envelope("news", sym_up)
        bars_payload = bars_env.get("payload") if bars_env else None
        bars_list = bars_payload.get("bars") if isinstance(bars_payload, dict) else None
        bars_series = _bars_series(bars_list)
        news_payload = news_env.get("payload") if news_env else None
        news = (
            news_payload.get("items", [])
            if isinstance(news_payload, dict)
            else []
        )
        return {
            "now": now.isoformat(),
            "symbol": sym_up,
            "quote": quote,
            "quote_age_seconds": _age((quote or {}).get("source_as_of"), now),
            "bars": bars_series,
            "bars_age_seconds": _age(bars_env.get("fetched_at"), now) if bars_env else None,
            "news": news[:12] if isinstance(news, list) else [],
            "news_age_seconds": _age(news_env.get("fetched_at"), now) if news_env else None,
        }

    @app.get("/api/market/{sym}/history")
    def api_market_symbol_history(
        sym: str, request: Request, range: str = "3y", max_points: int = 600
    ) -> Response:
        """Long-term OHLCV from the desk panel (5y, nightly) — NOT the
        viewer's 365-day envelope. ETag/304 keeps the 60 s re-poll free."""
        from tree_options.trex_web.symbol_history import (
            clamp_request,
            history_age_seconds,
            history_payload,
        )

        sym_up = sym.upper()
        if not re.match(r"^[A-Z.]{1,6}$", sym_up):
            raise HTTPException(status_code=404, detail="unknown symbol")
        range_key, points_cap = clamp_request(range, max_points)
        payload, etag = history_payload(desk_panel_path, sym_up, range_key, points_cap)
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        now = now_et()
        body = dict(payload)
        body["now"] = now.isoformat()
        body["history_age_seconds"] = history_age_seconds(
            payload.get("panel_last_session"), now
        )
        return JSONResponse(body, headers={"ETag": etag})

    @app.get("/api/market/{sym}/options")
    def api_market_symbol_options(
        sym: str, window: int = 5, max_expiries: int = 6
    ) -> dict[str, object]:
        """The desk's RECORDED per-name options surface (features cards +
        ATM-term + chain slice around ATM + iv30 history) plus the LIVE
        delayed viewchain the discovery lane warmed (``live``; null when no
        envelope). Read-only, all sections nullable."""
        from tree_options.trex_web.options_view import options_payload

        sym_up = sym.upper()
        if not re.match(r"^[A-Z.]{1,6}$", sym_up):
            raise HTTPException(status_code=404, detail="unknown symbol")
        return options_payload(
            desk_store_root, sym_up, window, max_expiries, now_et(), market_cache_root
        )

    @app.get("/api/market/{sym}/ideas")
    def api_market_symbol_ideas(sym: str) -> dict[str, object]:
        """The advisory idea-context payload for the symbol's Ideas tab:
        desk signals (xsmom + PEAD + next report), the miner's entry queue
        filtered to the name, the paper positions on it, the sealed
        scratch ledger rows and research-ledger context that mention it,
        plus the protocol boundary itself. Read-only and nullable — the
        desk advises, only allowed_direction signals may point a trade."""
        from tree_options.trex_web.ideas_view import ideas_payload

        sym_up = sym.upper()
        if not re.match(r"^[A-Z.]{1,6}$", sym_up):
            raise HTTPException(status_code=404, detail="unknown symbol")
        return ideas_payload(
            sym_up,
            desk_signals_dir,
            desk_queue_dir,
            desk_store_root,
            desk_paper_root,
            state_root,
            plans_root,
            now_et(),
        )

    @app.post("/api/market/watch", status_code=202)
    def api_market_watch(body: dict[str, Any]) -> dict[str, object]:
        """Spool a watchlist mutation for the discovery runner (202/503)."""
        op = str(body.get("op", ""))
        symbol = body.get("symbol")
        request_id = uuid.uuid4().hex[:12]
        try:
            write_request(
                discovery_root / "spool",
                "watch",
                request_id,
                {
                    "request_ts": now_et().isoformat(),
                    "op": op,
                    "symbol": str(symbol).upper() if symbol else None,
                    "proposal_id": body.get("proposal_id"),
                },
            )
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"spool unwritable ({exc}); trex-web ReadWritePaths missing?",
            ) from exc
        return {"accepted": True, "request_id": request_id, "op": op}

    @app.post("/api/market/refresh", status_code=202)
    def api_market_refresh(body: dict[str, Any] | None = None) -> dict[str, object]:
        """Spool a forced market refresh (warms quotes + bars + news)."""
        symbols = None
        if isinstance(body, dict) and isinstance(body.get("symbols"), list):
            symbols = [str(s).upper() for s in body["symbols"]][:12]
        request_id = uuid.uuid4().hex[:12]
        try:
            write_request(
                discovery_root / "spool",
                "market",
                request_id,
                {"request_ts": now_et().isoformat(), "symbols": symbols},
            )
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"spool unwritable ({exc}); trex-web ReadWritePaths missing?",
            ) from exc
        return {"accepted": True, "request_id": request_id, "symbols": symbols}

    @app.post("/api/discovery/backtest", status_code=202)
    def api_discovery_backtest(body: dict[str, Any]) -> dict[str, object]:
        """Spool a valuation-scenario request for one structure key. Only
        the key crosses the wire; the runner resolves prices from its own
        scan/shadow artifacts."""
        key = str(body.get("key", ""))
        if not _SCENARIO_KEY_RE.match(key):
            raise HTTPException(status_code=422, detail="key must be SYM|yyyymmdd|short|long")
        request_id = uuid.uuid4().hex[:12]
        try:
            write_request(
                discovery_root / "spool",
                "backtest",
                request_id,
                {"request_ts": now_et().isoformat(), "key": key},
            )
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"spool unwritable ({exc}); trex-web ReadWritePaths missing?",
            ) from exc
        return {"accepted": True, "request_id": request_id, "key": key}

    @app.get("/api/discovery/backtest")
    def api_discovery_backtest_get(key: str) -> dict[str, object]:
        """The materialized scenario (404 until the runner writes it)."""
        from tree_options.trex.discovery.backtest import read_artifact
        from tree_options.trex_web.discovery_view import _age

        if not _SCENARIO_KEY_RE.match(key):
            raise HTTPException(status_code=422, detail="key must be SYM|yyyymmdd|short|long")
        doc = read_artifact(discovery_root, key)
        if doc is None:
            raise HTTPException(status_code=404, detail="scenario not materialized yet")
        doc["age_seconds"] = _age(doc.get("generated_at"), now_et())
        return doc

    @app.post("/api/discovery/scan", status_code=202)
    def api_discovery_scan() -> dict[str, object]:
        """Scan-on-demand: drop a spool request for the discovery runner.

        The unit's ReadWritePaths allows writing ONLY the spool dir; any
        other failure surfaces as a clean 503, never a 500 trace.
        """
        request_id = uuid.uuid4().hex[:12]
        request_ts = now_et()
        spool = discovery_root / "spool"
        try:
            write_scan_request(spool, request_id, request_ts)
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"spool unwritable ({exc}); trex-web ReadWritePaths missing?",
            ) from exc
        return {
            "accepted": True,
            "request_id": request_id,
            "request_ts": request_ts.isoformat(),
            "spool_pending": True,
            "note": "consumed by trex-discovery --serve",
        }

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def spa_shell() -> Response:
        index = static_root / "index.html"
        if index.is_file():
            # no-cache = always revalidate (ETag keeps it cheap): a
            # heuristically cached shell keeps pointing at the previous
            # deploy's hashed bundle long after a rebuild
            return FileResponse(
                index, media_type="text/html", headers={"Cache-Control": "no-cache"}
            )
        return HTMLResponse(_FALLBACK_SHELL, status_code=503)

    @app.get("/plan/{plan_id}", response_class=HTMLResponse, include_in_schema=False)
    def plan_bookmark_shim(plan_id: str) -> HTMLResponse:
        """Legacy ``/plan/<id>`` bookmarks -> the SPA hash route.

        A tiny page whose inline script rewrites to ``../#/plan/<id>``:
        the relative ``..`` resolves to the app root under / (loopback)
        AND under a stripped prefix (/trex/ through the family portal).
        NEVER an HTTP redirect — an absolute Location would escape the
        portal prefix and land on the wrong app.
        """
        # json.dumps gives a double-quoted JS literal; the <, >, & escapes
        # keep a hostile id from breaking out of the <script> element
        # (json alone does not escape forward slashes).
        safe_id = (
            json.dumps(plan_id)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )
        return HTMLResponse(
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<title>trex cockpit</title></head><body>"
            "<p>Opening the cockpit plan view…</p>"
            "<script>location.replace('../#/plan/' + encodeURIComponent("
            + safe_id
            + "))</script></body></html>"
        )

    @app.get("/plan/{plan_id}/book.json")
    def plan_book_json(plan_id: str) -> JSONResponse:
        path = state_root / plan_id / "book.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="no book.json yet")
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=503, detail=f"book.json unreadable: {exc}") from exc
        return JSONResponse(payload)

    @app.get("/plan/{plan_id}/events.jsonl")
    def plan_events_jsonl(plan_id: str) -> Response:
        path = state_root / plan_id / "events.jsonl"
        if not path.exists():
            raise HTTPException(status_code=404, detail="no events yet")
        try:
            body = path.read_text()
        except OSError as exc:
            raise HTTPException(status_code=503, detail=f"events.jsonl unreadable: {exc}") from exc
        return Response(content=body, media_type="application/x-ndjson")

    if static_root.exists():
        # Root mount LAST: the built shell references './assets/...', so it
        # must resolve wherever the app is mounted (/ loopback, /trex/
        # portal-stripped). Routes registered above (API, raw JSON, the
        # bookmark shim) still win for their exact paths.
        app.mount("/", StaticFiles(directory=str(static_root), html=True), name="spa")

    return app
