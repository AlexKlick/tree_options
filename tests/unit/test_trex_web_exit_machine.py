"""GET /api/exit-machine: the exit-machine watchdog's verdict for the banner.

Read-only, from ~/.local/state/trex/exit_watch.json (path injected so the
suite never reads the host's file). Missing or stale = "not reporting",
never healthy.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from tree_options.trex_web.app import create_app


def _client(tmp_path: Path, state: dict[str, object] | None) -> TestClient:
    path = tmp_path / "exit_watch.json"
    if state is not None:
        path.write_text(json.dumps(state))
    (tmp_path / "plans").mkdir(exist_ok=True)
    return TestClient(
        create_app(
            state_dir=str(tmp_path / "state"),
            plans_dir=str(tmp_path / "plans"),
            discovery_dir=str(tmp_path / "discovery"),
            gateway_state=str(tmp_path / "gateway.json"),
            exit_watch_state=str(path),
        )
    )


def test_monitor_down_carries_since_detail_and_books(tmp_path: Path) -> None:
    now = time.time()
    body = _client(tmp_path, {
        "status": "monitor_down", "since": now - 900, "detail": "no heartbeat for 15m",
        "checked_at": now - 10,
        "books": [{"plan": "putspread-20260922", "status": "monitor_down",
                   "heartbeat_age": 900, "detail": "x"}],
        "events": [{"at": now, "kind": "notify"}], "last_notified_at": now,
    }).get("/api/exit-machine").json()
    assert body["status"] == "monitor_down"
    assert body["since"] == pytest.approx(now - 900)
    assert body["books"][0]["plan"] == "putspread-20260922"
    assert body["watch_stale"] is False
    assert "events" not in body and "last_notified_at" not in body


def test_missing_or_stale_is_not_healthy(tmp_path: Path) -> None:
    body = _client(tmp_path, None).get("/api/exit-machine").json()
    assert body["status"] == "unknown" and body["watch_stale"] is True
    now = time.time()
    body = _client(tmp_path, {"status": "ok", "checked_at": now - 900}).get(
        "/api/exit-machine").json()
    assert body["status"] == "ok" and body["watch_stale"] is True
