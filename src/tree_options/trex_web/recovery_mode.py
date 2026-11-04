"""A process-wide read-only boundary, independent of every route's authority.

Recovery never adds trading authority. The flag is frozen when the app starts;
an invalid value refuses startup rather than silently enabling mutations.
"""

from __future__ import annotations

from collections.abc import Mapping

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

ENV = "TREX_RECOVERY_READ_ONLY"
META = '<meta name="trex-recovery-mode" content="read-only">'


def enabled(environment: Mapping[str, str]) -> bool:
    value = environment.get(ENV, "0").strip().lower()
    if value not in {"0", "1", "false", "true"}:
        raise ValueError("recovery_mode_invalid")
    return value in {"1", "true"}


class RecoveryModeMiddleware:
    def __init__(self, app: ASGIApp, *, read_only: bool) -> None:
        self.app = app
        self.read_only = read_only

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.read_only and scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if (
            self.read_only
            and scope["type"] == "http"
            and scope.get("method") not in {"GET", "HEAD"}
        ):
            response = JSONResponse(
                status_code=403,
                content={"error": "recovery_read_only", "reason": "recovery_read_only"},
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
