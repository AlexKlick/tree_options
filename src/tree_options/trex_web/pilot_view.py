"""``GET /api/pilot``: the research pilot for the ``#/pilot`` page (Wave 4 lane I).

Read only: registrations, today's usage and the run blockers
(``tree_options.research.pilot.view``, ``trex-pilot-view/1``). Sealing is
NOT a route here: the owner seals through ``POST /api/commands``
(``seal_registration``, once the coordinator's amendment lands) or the
loopback CLI. The pilot dir and owner list resolve per request.

Coordinator wiring (``trex_web/app.py``, beside ``attach_portfolio``)::

    from tree_options.trex_web.pilot_view import attach as attach_pilot

    attach_pilot(app, clock=clock)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from tree_options.research.pilot.store import PilotError, owners_from_env, pilot_dir_from_env
from tree_options.research.pilot.view import PILOT_VIEW_PATH, pilot_view

__all__ = ["attach"]


def _owners_or_none() -> frozenset[str] | None:
    try:
        return owners_from_env()
    except PilotError:
        return None


def attach(
    app: FastAPI,
    *,
    clock: Callable[[], datetime],
    pilot_dir: Callable[[], Path] = pilot_dir_from_env,
    owners: Callable[[], frozenset[str] | None] = _owners_or_none,
) -> None:
    """Mount ``GET /api/pilot``. ``owners`` returning None means the owner
    list is malformed: shown as not configured, and nothing can run."""

    @app.get(PILOT_VIEW_PATH)
    def pilot() -> JSONResponse:
        owner_set = owners()
        document = pilot_view(
            pilot_dir(),
            now=clock(),
            owners=owner_set or frozenset(),
            owners_configured=bool(owner_set),
        )
        return JSONResponse(document, headers={"Cache-Control": "no-store"})
