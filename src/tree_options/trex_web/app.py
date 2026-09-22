"""FastAPI app for the trex read-only status panel."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

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


def create_app(
    state_dir: str | None = None,
    plans_dir: str | None = None,
) -> FastAPI:
    """Build the FastAPI app. Public for tests; production wires ``__main__``."""
    state_root = _resolve_state_root(state_dir)
    plans_root = _resolve_plans_root(plans_dir)

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
        return templates.TemplateResponse(
            request=request,
            name="plan.html",
            context={
                "view": view,
                "runbook": runbook,
                "gateway_reachable": gateway_reachable,
                "marks": marks,
                "marks_age": marks_age_seconds(marks),
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

    return app
