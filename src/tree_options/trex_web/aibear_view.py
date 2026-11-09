"""GET /api/aibear: the latest AI-BEAR monitor document, read-only.

The document is written by ``python -m tree_options.desk.aibear monitor``
under ``<TREX_DESK_STATE>/aibear/<session>.json``. This route only reads it.
The content is research and advisory (regime states, AI-BEAR-002 forward
shadow cards, the LLM read); nothing here can open, approve or close a trade.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from tree_options.desk.paths import state_root

AIBEAR_PATH = "/api/aibear"
_FIELDS = (
    "schema",
    "session",
    "generated_at",
    "params_sha256",
    "names",
    "signals_today",
    "forward",
    "llm",
    "llm_forward_score",
    "summary",
)


def _unavailable(reason: str) -> JSONResponse:
    return JSONResponse(
        {"status": "unavailable", "reason": reason, "execution_authorized": False},
        status_code=503,
        headers={"Cache-Control": "no-store"},
    )


def attach(app: FastAPI, *, state: Path | None = None) -> None:
    @app.get(AIBEAR_PATH)
    def aibear() -> JSONResponse:
        root = (state or state_root()) / "aibear"
        docs = (
            sorted(root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"))
            if root.is_dir()
            else []
        )
        if not docs:
            return _unavailable("no_monitor_document")
        try:
            doc = json.loads(docs[-1].read_text())
        except (OSError, ValueError):
            return _unavailable("monitor_document_unreadable")
        if not isinstance(doc, dict) or doc.get("schema") != "aibear-monitor/1":
            return _unavailable("monitor_document_unrecognized")
        body = {k: doc[k] for k in _FIELDS if k in doc}
        body["advisory_only"] = True
        body["execution_authorized"] = False
        return JSONResponse(body, headers={"Cache-Control": "no-store"})
