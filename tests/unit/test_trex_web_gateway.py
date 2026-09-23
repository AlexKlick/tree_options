"""GET /api/gateway: the watchdog's verdict for the cockpit banner.

Read-only, from ~/.local/state/trex/gateway.json (path injected here so the
suite never reads the host's real file). A missing or stale file must read
as "watchdog not reporting", never as healthy.
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
    path = tmp_path / "gateway.json"
    if state is not None:
        path.write_text(json.dumps(state))
    (tmp_path / "plans").mkdir(exist_ok=True)
    return TestClient(
        create_app(
            state_dir=str(tmp_path / "state"),
            plans_dir=str(tmp_path / "plans"),
            discovery_dir=str(tmp_path / "discovery"),
            gateway_state=str(path),
        )
    )


def test_needs_login_carries_link_since_and_retry_budget(tmp_path: Path) -> None:
    now = time.time()
    body = _client(tmp_path, {
        "status": "needs_login", "since": now - 3600, "detail": "login screen open",
        "checked_at": now - 20, "login_url": "https://example.invalid/vnc.html?path=vnc",
        "restarts": [now - 600], "restarts_left": 3, "next_restart_at": now + 6600,
        "ibc_phase": "login_dialog", "api_ok": False, "vnc_running": True,
        "events": [{"at": now, "kind": "notify"}],
    }).get("/api/gateway").json()
    assert body["status"] == "needs_login"
    assert body["since"] == pytest.approx(now - 3600)
    assert body["login_url"].endswith("path=vnc")
    assert body["restarts_left"] == 3
    assert body["last_restart_at"] == pytest.approx(now - 600)
    assert body["watch_stale"] is False
    assert "events" not in body  # the banner gets a verdict, not the ledger


def test_stale_file_is_flagged(tmp_path: Path) -> None:
    now = time.time()
    body = _client(tmp_path, {"status": "ok", "since": now - 9000,
                              "checked_at": now - 900}).get("/api/gateway").json()
    assert body["status"] == "ok"
    assert body["watch_stale"] is True
    assert body["age_seconds"] >= 900


def test_missing_or_corrupt_file_is_unknown_not_ok(tmp_path: Path) -> None:
    assert _client(tmp_path, None).get("/api/gateway").json()["status"] == "unknown"
    (tmp_path / "gateway.json").write_text("{nope")
    body = _client(tmp_path, None).get("/api/gateway").json()
    assert body["status"] == "unknown" and body["watch_stale"] is True
