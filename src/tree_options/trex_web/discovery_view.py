"""GET /api/discovery projection (pure; reads discovery state files only)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from tree_options.trex.discovery.artifact import (
    read_latest,
    read_runs,
    read_scan_result,
    spool_pending,
)
from tree_options.trex.discovery.config import config_echo, load_scan_config


def _fnum(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _age(iso: Any, now: datetime) -> int | None:
    if not isinstance(iso, str):
        return None
    try:
        stamped = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return max(0, int((now - stamped).total_seconds()))


def _latest_view(latest: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    if latest is None:
        return None
    payload = latest.get("payload", {})
    dq = payload.get("data_quality", {})
    return {
        "run_id": latest.get("run_id"),
        "generated_at": payload.get("generated_at"),
        "age_seconds": _age(payload.get("generated_at"), now),
        "mode": payload.get("mode"),
        "request_id": payload.get("request_id"),
        "git_sha": (latest.get("stamp") or {}).get("git_sha"),
        "config_hash": (latest.get("stamp") or {}).get("config_hash"),
        "effective_target_modes": payload.get("effective_target_modes", []),
        "data_quality": {
            "underlyings_requested": dq.get("underlyings_requested", 0),
            "underlyings_scanned": dq.get("underlyings_scanned", 0),
            "chains_available": dq.get("chains_available", False),
            "greeks_available": dq.get("greeks_available", False),
            "expiries_scanned": dq.get("expiries_scanned", 0),
            "rows_quoted": dq.get("rows_quoted", 0),
            "rows_unquoted": dq.get("rows_unquoted", 0),
            "notes": [str(n) for n in dq.get("notes", [])],
        },
        "candidates": payload.get("candidates", []),
        "rejected": payload.get("rejected", []),
    }


def discovery_payload(
    discovery_dir: Path,
    config_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    from tree_options.trex.clock import now_et

    now = now or now_et()
    config: dict[str, Any] | None = None
    config_present = config_path.exists()
    if config_present:
        try:
            config = config_echo(load_scan_config(config_path))
        except (ValueError, FileNotFoundError):
            config = None
            config_present = False

    spool = discovery_dir / "spool"
    result = read_scan_result(spool)
    return {
        "now": now.isoformat(),
        "state_dir": str(discovery_dir),
        "config_present": config_present,
        "config": config,
        "latest": _latest_view(read_latest(discovery_dir), now),
        "runs": read_runs(discovery_dir, limit=10),
        "spool": {
            "pending": spool_pending(spool),
            "last_result": result,
        },
    }
