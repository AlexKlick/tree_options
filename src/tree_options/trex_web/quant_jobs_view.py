"""HTTP assignment/control boundary; no campaign execution in request handlers."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from tree_options.research.paths import assert_no_overlap_with_desk, workspace_root
from tree_options.research.quant_campaign_io import decode_json
from tree_options.research.quant_jobs import (
    dataset_catalog,
    datasets_root,
    enqueue,
    job_detail,
    jobs,
    set_stopped,
)
from tree_options.trex_web.workspace_guard import controls_enabled, require_workspace_operator


def attach(
    app: FastAPI, *, workspace: Path | None = None, datasets_dir: Path | None = None
) -> None:
    root = workspace or workspace_root()
    datasets = datasets_dir or datasets_root()
    assert_no_overlap_with_desk(workspace=root)

    @app.get("/api/research/quant/datasets")
    def catalog() -> dict:
        try:
            return {
                "datasets": dataset_catalog(datasets),
                "controls_enabled": controls_enabled(),
                "execution_authorized": False,
                "live_money": False,
            }
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(503, "dataset_registry_unavailable") from None

    @app.get("/api/research/quant/jobs")
    def list_jobs() -> dict:
        try:
            return {
                "jobs": jobs(root),
                "controls_enabled": controls_enabled(),
                "execution_authorized": False,
                "live_money": False,
            }
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(503, "research_state_unavailable") from None

    @app.post("/api/research/quant/jobs")
    async def submit(request: Request) -> JSONResponse:
        require_workspace_operator(request)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                raise HTTPException(413, "bounded_assignment_required")
        try:
            raw = decode_json(bytes(body))
            job, created = enqueue(root, raw, datasets_dir=datasets)
            return JSONResponse(job, status_code=202 if created else 200)
        except (ValueError, TypeError, ArithmeticError):
            raise HTTPException(400, "invalid_assignment_or_dataset") from None
        except (OSError, RuntimeError):
            raise HTTPException(503, "research_state_unavailable") from None

    @app.get("/api/research/quant/jobs/{run_id}")
    def detail(run_id: str) -> dict:
        try:
            return job_detail(root, run_id)
        except KeyError:
            raise HTTPException(404, "research_job_not_found") from None
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(503, "research_state_unavailable") from None

    @app.post("/api/research/quant/jobs/{run_id}/stop")
    def stop(run_id: str, request: Request) -> dict:
        return control(run_id, request, True)

    @app.post("/api/research/quant/jobs/{run_id}/resume")
    def resume(run_id: str, request: Request) -> dict:
        return control(run_id, request, False)

    def control(run_id: str, request: Request, stopped: bool) -> dict:
        require_workspace_operator(request)
        try:
            return {
                "job": set_stopped(root, run_id, stopped),
                "execution_authorized": False,
                "live_money": False,
            }
        except KeyError:
            raise HTTPException(404, "research_job_not_found") from None
        except ValueError:
            raise HTTPException(409, "research_control_conflict") from None
        except (OSError, RuntimeError):
            raise HTTPException(503, "research_state_unavailable") from None
