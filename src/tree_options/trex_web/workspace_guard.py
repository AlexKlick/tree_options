"""Explicit local operator controls for research and paper proposal mutations.

This cockpit has no multi-user authentication contract. New controls therefore
remain local-only and off by default; forwarding headers confer no authority.
Broker credentials and effects are never accepted through this boundary.
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def controls_enabled() -> bool:
    return os.environ.get("TREX_WORKSPACE_CONTROLS") == "1"


def require_workspace_operator(request: Request) -> None:
    """Require explicit enablement, loopback transport and exact browser origin."""
    origin = request.headers.get("origin")
    parsed = urlsplit(str(request.url))
    if (
        not controls_enabled()
        or request.client is None
        or not _loopback(request.client.host)
        or not _loopback(parsed.hostname)
        or parsed.username is not None
        or origin != f"{parsed.scheme}://{parsed.netloc}"
        or request.headers.get("sec-fetch-site", "same-origin") != "same-origin"
        or any(
            key in request.headers
            for key in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")
        )
    ):
        raise HTTPException(status_code=403, detail="local_operator_controls_required")
