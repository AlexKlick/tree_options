"""Serve one validated publication; no source databases or lifecycle mutations."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from tree_options.research.experiments.io import Unavailable, read


def attach(app: FastAPI, *, snapshot: Path | None = None) -> None:
    def response(experiment_id: str | None = None) -> JSONResponse:
        path = snapshot or os.environ.get("TREX_EXPERIMENT_SNAPSHOT")
        try:
            if not path:
                raise Unavailable("index_not_configured")
            index = read(Path(path), datetime.now(UTC))
        except Unavailable as exc:
            return JSONResponse(
                {
                    "status": "unavailable",
                    "reason": str(exc),
                    "next_step": "Restore the experiment publication; original study owners retain their records.",
                    "execution_authorized": False,
                },
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        if experiment_id is None:
            return JSONResponse(index.model_dump(), headers={"Cache-Control": "no-store"})
        row = next((r for r in index.experiments if r.id == experiment_id), None)
        if row is None:
            return JSONResponse({"reason": "experiment_not_found"}, status_code=404)
        return JSONResponse(
            {
                "published_at": index.published_at,
                "source_release": index.source_release,
                "experiment": row.model_dump(),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/experiments")
    def experiments() -> JSONResponse:
        return response()

    @app.get("/api/experiments/{experiment_id}")
    def experiment(experiment_id: str) -> JSONResponse:
        return response(experiment_id)
