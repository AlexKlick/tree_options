"""The gateway watchdog's verdict, shaped for the cockpit banner (pure).

Source: ``~/.local/state/trex/gateway.json`` written once a minute by the
trex-gateway-watch timer. A missing, corrupt or stale file reads as
"watchdog not reporting" — never as a healthy gateway.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_GATEWAY_STATE = Path("~/.local/state/trex/gateway.json").expanduser()
WATCH_STALE_AFTER_S = 300  # the timer fires every 60s

_FIELDS = (
    "status", "since", "detail", "checked_at", "login_url", "restarts_left",
    "next_restart_at", "ibc_phase", "api_ok", "vnc_running",
)


def gateway_view(path: Path, now: float) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        return {k: None for k in _FIELDS} | {
            "status": "unknown", "age_seconds": None, "watch_stale": True,
            "last_restart_at": None,
        }
    out = {k: raw.get(k) for k in _FIELDS}
    checked = raw.get("checked_at")
    age = now - checked if isinstance(checked, (int, float)) else None
    restarts = raw.get("restarts")
    out["age_seconds"] = round(age) if age is not None else None
    out["watch_stale"] = age is None or age > WATCH_STALE_AFTER_S
    out["last_restart_at"] = restarts[-1] if isinstance(restarts, list) and restarts else None
    if not isinstance(out["status"], str):
        out["status"] = "unknown"
    return out
