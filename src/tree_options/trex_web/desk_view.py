"""Read-only shadow-evidence projection on the existing cockpit application."""
from __future__ import annotations

import base64
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from tree_options.desk import production, scorecards
from tree_options.desk.contracts import ContractError
from tree_options.desk.evidence import EvidenceError
from tree_options.trex.clock import now_et, session_calendar

_ERRORS = (ContractError, EvidenceError, sqlite3.Error, OSError)


def attach(app: FastAPI, *, database: Path) -> None:
    @app.get('/api/desk/health')
    def health() -> JSONResponse:
        try:
            doc = production.health(database=database, now=now_et(), cal=session_calendar())
            return JSONResponse(doc, status_code=200 if doc['evidence_status'] == 'ready' else 503,
                                headers={'Cache-Control': 'no-store'})
        except _ERRORS:
            return _unavailable()

    @app.get('/api/desk/scorecards')
    def summary() -> JSONResponse:
        try:
            return JSONResponse(scorecards.build_scorecards(database), headers={'Cache-Control': 'no-store'})
        except _ERRORS:
            return _unavailable()

    @app.get('/desk/evidence')
    def page() -> HTMLResponse:
        html = Path(__file__).with_name('desk_evidence.html').read_text(encoding='utf-8')
        script = html.split('<script>', 1)[1].split('</script>', 1)[0]
        sha = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        return HTMLResponse(html, headers={'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
            'Content-Security-Policy': f"default-src 'none'; script-src 'sha256-{sha}'; "
                "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'none'; object-src 'none'"})


def _unavailable() -> JSONResponse:
    doc: dict[str, Any] = {'schema': 'desk-error/1', 'error': 'evidence_unavailable',
        'execution_enabled': False, 'evidence_status': 'unavailable'}
    return JSONResponse(doc, status_code=503, headers={'Cache-Control': 'no-store'})
