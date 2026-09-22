"""FastAPI app for the trex read-only status panel."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from tree_options.trex.clock import now_et
from tree_options.trex_web.payoff import (
    build_payoff_chart,
    build_pnl_history_chart,
    payoff_series,
    pnl_history_series,
    summarize_book,
)
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

_TEMPLATE_DIR = Path(__file__).parent / "templates"


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


def _plans_payload(state_root: Path, plans_root: Path) -> dict[str, object]:
    """GET /api/plans body: one summary per plan TOML (no marks data)."""
    plans: list[dict[str, object]] = []
    for view in list_plans(state_root, plans_root):
        rb = compute_runbook_status_from_view(view)
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
            }
        )
    return {
        "now": now_et().isoformat(),
        "gateway_reachable": probe_gateway(),
        "plans": plans,
    }


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
        }
        if st.entry_fill is not None and st.filled_qty > 0:
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

    book_summary: dict[str, object] | None = None
    if legs:
        committed = sum(entry * qty * 100 for _, _, entry, qty in legs)
        max_gain = sum(
            (lo - sh - entry) * qty * 100 for lo, sh, entry, qty in legs
        )
        book_summary = {
            "committed": committed,
            "max_gain": max_gain,
            "max_loss": -committed,
            "short_floor": min(sh for _, sh, _, _ in legs),
        }

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
        "payoffs": payoffs,
        "history": history,
        "events": view.events,
    }


def create_app(
    state_dir: str | None = None,
    plans_dir: str | None = None,
    static_dir: str | None = None,
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

    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

    app = FastAPI(
        title="trex options cockpit",
        docs_url=None,  # operator tool; no Swagger UI in prod
        redoc_url=None,
        openapi_url=None,
    )

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

    @app.get("/api/plans")
    def api_plans() -> dict[str, object]:
        return _plans_payload(state_root, plans_root)

    @app.get("/api/plans/{plan_id}")
    def api_plan_detail(plan_id: str) -> dict[str, object]:
        payload = _plan_payload(plan_id, state_root, plans_root)
        if payload is None:
            raise HTTPException(status_code=404, detail=f"plan {plan_id!r} not found")
        return payload

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> object:
        plans = list_plans(state_root, plans_root)
        runs: list[dict[str, object]] = []
        for view in plans:
            runs.append(
                {
                    "view": view,
                    "runbook": compute_runbook_status_from_view(view),
                    "gateway_reachable": probe_gateway(),
                }
            )
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "runs": runs,
                "plans_root": str(plans_root),
                "state_root": str(state_root),
            },
        )

    @app.get("/plan/{plan_id}", response_class=HTMLResponse)
    def plan_detail(request: Request, plan_id: str) -> object:
        view = load_plan_view(plan_id, state_root, plans_root)
        if view is None:
            raise HTTPException(status_code=404, detail=f"plan {plan_id!r} not found")
        runbook = compute_runbook_status_from_view(view)
        gateway_reachable = probe_gateway()
        marks = load_marks(state_root, plan_id)
        payoff_charts: dict[str, object] = {}
        legs: list[tuple[float, float, float, int]] = []
        raw_spots = marks.get("spots") if marks else None
        for s in view.plan.structures:
            st = view.structures.get(s.id)
            if st is None or st.entry_fill is None or st.filled_qty <= 0:
                continue
            legs.append(
                (float(s.long_strike), float(s.short_strike), float(st.entry_fill), int(st.filled_qty))
            )
            spot = None
            if isinstance(raw_spots, dict):
                raw = raw_spots.get(s.underlying)
                if isinstance(raw, str):
                    try:
                        spot = float(raw)
                    except ValueError:
                        spot = None
            chart = build_payoff_chart(
                float(s.long_strike),
                float(s.short_strike),
                float(st.entry_fill),
                int(st.filled_qty),
                spot,
            )
            if chart is not None:
                payoff_charts[s.id] = chart
        book_summary = summarize_book(legs) if legs else None
        history_chart = None
        raw_history = marks.get("history") if marks else None
        if isinstance(raw_history, list):
            history_chart = build_pnl_history_chart(
                [h for h in raw_history if isinstance(h, dict)]
            )
        return templates.TemplateResponse(
            request=request,
            name="plan.html",
            context={
                "view": view,
                "runbook": runbook,
                "gateway_reachable": gateway_reachable,
                "marks": marks,
                "marks_age": marks_age_seconds(marks),
                "payoff_charts": payoff_charts,
                "book_summary": book_summary,
                "history_chart": history_chart,
            },
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
        app.mount(
            "/static", StaticFiles(directory=str(static_root), html=True), name="static"
        )

    return app
