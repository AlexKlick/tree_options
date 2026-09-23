"""The watchdogs' verdicts, shaped for the cockpit banners (pure).

Sources, written once a minute by their timers under ``~/.local/state/trex``:
``gateway.json`` (trex-gateway-watch) and ``exit_watch.json``
(trex-exit-watch). A missing, corrupt or stale file reads as "watchdog not
reporting" — never as healthy. The banners get a verdict, not the ledger
(events and notification bookkeeping stay on the box).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_GATEWAY_STATE = Path("~/.local/state/trex/gateway.json").expanduser()
DEFAULT_EXIT_WATCH_STATE = Path("~/.local/state/trex/exit_watch.json").expanduser()
WATCH_STALE_AFTER_S = 300  # the timers fire every 60s

_GATEWAY_FIELDS = (
    "status", "since", "detail", "checked_at", "login_url", "restarts_left",
    "next_restart_at", "ibc_phase", "api_ok", "vnc_running",
)
_EXIT_FIELDS = ("status", "since", "detail", "checked_at", "books")


def _watch_view(path: Path, now: float, fields: tuple[str, ...]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(view, raw); the view carries only ``fields`` plus freshness."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        return {k: None for k in fields} | {
            "status": "unknown", "age_seconds": None, "watch_stale": True,
        }, {}
    out = {k: raw.get(k) for k in fields}
    checked = raw.get("checked_at")
    age = now - checked if isinstance(checked, (int, float)) else None
    out["age_seconds"] = round(age) if age is not None else None
    out["watch_stale"] = age is None or age > WATCH_STALE_AFTER_S
    if not isinstance(out["status"], str):
        out["status"] = "unknown"
    return out, raw


def gateway_view(path: Path, now: float) -> dict[str, Any]:
    out, raw = _watch_view(path, now, _GATEWAY_FIELDS)
    restarts = raw.get("restarts")
    out["last_restart_at"] = restarts[-1] if isinstance(restarts, list) and restarts else None
    return out


def exit_machine_view(path: Path, now: float) -> dict[str, Any]:
    out, _ = _watch_view(path, now, _EXIT_FIELDS)
    if not isinstance(out["books"], list):
        out["books"] = []
    return out
