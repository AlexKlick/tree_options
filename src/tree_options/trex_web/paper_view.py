"""Safe paper account projections and proposal-only owner workspace controls."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from tree_options.research.quant_campaign_io import decode_json
from tree_options.trex.paper_workspace import (
    DeploymentRequest,
    PaperWorkspace,
    WorkspaceRefused,
    workspace_from_environment,
)
from tree_options.trex.supervised import SupervisedRefused
from tree_options.trex_web.workspace_guard import controls_enabled, require_workspace_operator


def attach(app: FastAPI, workspace: PaperWorkspace | None = None) -> None:
    def state() -> PaperWorkspace:
        return workspace or workspace_from_environment()

    async def bounded_json(request: Request) -> Any:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 8192:
                raise HTTPException(413, detail="bounded_paper_request_required")
            body.extend(chunk)
        return decode_json(bytes(body))

    @app.get("/api/paper/accounts")
    def accounts() -> dict[str, Any]:
        return state().accounts() | dict(controls_enabled=controls_enabled())

    @app.get("/api/paper/deployments")
    def deployments() -> dict[str, Any]:
        try:
            return state().deployments()
        except (WorkspaceRefused, ValueError, OSError):
            raise HTTPException(503, detail="paper_projection_unavailable") from None

    @app.get("/api/paper/allocations")
    def allocations() -> dict[str, Any]:
        try:
            return state().allocations()
        except (WorkspaceRefused, ValueError, OSError):
            raise HTTPException(503, detail="paper_allocation_projection_unavailable") from None

    @app.get("/api/paper/setup")
    def setup() -> dict[str, Any]:
        return dict(
            schema="trex.paper.setup/v1",
            environment="BROKER PAPER",
            live_money=False,
            credentials_in_browser=False,
            preferred_qualification_provider="ibkr",
            qualification_providers=["ibkr", "snaptrade"],
            execution_providers=["snaptrade"],
            checklist=[
                dict(
                    step="ibkr_first",
                    description="Qualify the existing IBKR paper account through its supervised owner first. Read-only qualification does not enable this workspace to execute equities.",
                ),
                dict(
                    step="snaptrade_alternate",
                    description="Optionally connect dedicated Alpaca Paper through SnapTrade Personal API access. Populate private credentials and account identity locally. Its bounded canary is separate from the IBKR desk.",
                ),
                dict(
                    step="private_catalog",
                    description="Configure private TREX account binding and catalog; select only aliases in the cockpit.",
                ),
                dict(
                    step="read_only_qualification",
                    description="Run the local qualification command. Fresh verified paper identity, balances, positions, orders and exclusive ownership are required.",
                ),
                dict(
                    step="timestamped_quotes",
                    description="Verify an authoritative timestamped quote source. Snapshot receipt time is insufficient.",
                ),
                dict(
                    step="bounded_approval",
                    description="Review and approve an exact local one-share BUY limit canary, cap at most 100 USD and TTL at most 900 seconds.",
                ),
                dict(
                    step="execution_evidence",
                    description="Monitor broker readback and reconciliation. Exact fills, fees and corrections require a separately verified source.",
                ),
            ],
            supported_execution_strategy="operational-canary/1",
            campaign_deployment_supported=False,
            maximum_canary_orders=1,
            maximum_canary_gross_notional_usd="100",
        )

    @app.post("/api/paper/deployments", status_code=201)
    async def propose(request: Request) -> dict[str, Any]:
        require_workspace_operator(request)
        try:
            parsed = DeploymentRequest.model_validate(await bounded_json(request))
            return await run_in_threadpool(state().propose, parsed)
        except WorkspaceRefused as error:
            raise HTTPException(409, detail=str(error)) from None
        except (ValueError, ValidationError):
            raise HTTPException(422, detail="invalid_paper_proposal") from None
        except (OSError, SupervisedRefused):
            raise HTTPException(503, detail="paper_workspace_unavailable") from None

    @app.post("/api/paper/allocations", status_code=201)
    async def create_allocation(request: Request) -> dict[str, Any]:
        require_workspace_operator(request)
        try:
            payload = await bounded_json(request)
            if not isinstance(payload, dict) or set(payload) != {
                "idempotency_key",
                "account_alias",
            }:
                raise ValueError
            if not isinstance(payload["idempotency_key"], str) or (
                payload["account_alias"] is not None
                and not isinstance(payload["account_alias"], str)
            ):
                raise ValueError
            return await run_in_threadpool(
                state().create_allocation,
                payload["idempotency_key"],
                account_alias=payload["account_alias"],
            )
        except WorkspaceRefused as error:
            raise HTTPException(409, detail=str(error)) from None
        except ValueError:
            raise HTTPException(422, detail="invalid_allocation_request") from None
        except (OSError, SupervisedRefused):
            raise HTTPException(503, detail="paper_workspace_unavailable") from None

    @app.post("/api/paper/allocations/{plan_id}/sleeves/{sleeve_id}/bind")
    async def bind_sleeve(plan_id: str, sleeve_id: str, request: Request) -> dict[str, Any]:
        require_workspace_operator(request)
        try:
            payload = await bounded_json(request)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"account_alias"}
                or not isinstance(payload["account_alias"], str)
            ):
                raise ValueError
            return await run_in_threadpool(
                state().bind_sleeve, plan_id, sleeve_id, payload["account_alias"]
            )
        except WorkspaceRefused as error:
            raise HTTPException(409, detail=str(error)) from None
        except ValueError:
            raise HTTPException(422, detail="invalid_sleeve_binding") from None
        except (OSError, SupervisedRefused):
            raise HTTPException(503, detail="paper_workspace_unavailable") from None

    @app.post("/api/paper/deployments/{deployment_id}/halt")
    def halt(deployment_id: str, request: Request) -> dict[str, Any]:
        require_workspace_operator(request)
        try:
            return state().halt(deployment_id)
        except WorkspaceRefused as error:
            raise HTTPException(409, detail=str(error)) from None
        except (ValueError, OSError, SupervisedRefused):
            raise HTTPException(503, detail="paper_halt_unavailable") from None
