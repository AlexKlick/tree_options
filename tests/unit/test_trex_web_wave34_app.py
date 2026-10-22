"""Waves 3 and 4 integration: the read views ``create_app`` mounts.

The lanes ship ``attach(app, ...)``; the coordinator wires them in
``trex_web/app.py``. Through the real ``create_app`` (temp roots only, never
the host's state): ``GET /api/today``, ``/api/activity``, ``/api/acceptance``
and ``/api/pilot`` answer their frozen schemas, read only, and Today reports
the process's family ingress status (``family_commands_disabled``) because
``create_app`` wires it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tree_options.research.pilot.view import PilotView
from tree_options.trex.clock import session_calendar
from tree_options.trex.contracts import acceptance as acc
from tree_options.trex.contracts import wave3 as w
from tree_options.trex.contracts.canonical import model_bytes
from tree_options.trex_web.app import create_app

NOW = datetime(2026, 10, 22, 14, 2, tzinfo=UTC)


def make_client(root: Path) -> TestClient:
    app = create_app(
        state_dir=str(root / "legacy"),
        plans_dir=str(root / "plans"),
        discovery_dir=str(root / "discovery"),
        gateway_state=str(root / "trex" / "gateway.json"),
        desk_state_dir=str(root / "desk-state"),
        desk_store_dir=str(root / "desk-store"),
        desk_run_dir=str(root / "desk-run"),
        supervised_dir=str(root / "supervised"),
        command_clock=lambda: NOW,
    )
    return TestClient(app)


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "desk-run").mkdir()
    monkeypatch.setenv("TREX_ACCEPTANCE_DIR", str(tmp_path / "acceptance"))
    monkeypatch.setenv("TREX_PILOT_DIR", str(tmp_path / "pilot"))
    monkeypatch.setenv("TREX_FAMILY_COMMAND_KEY_FILE", str(tmp_path / "no-such.key"))
    return tmp_path


def get(client: TestClient, path: str) -> dict[str, Any]:
    response = client.get(path)
    assert response.status_code == 200, (path, response.text)
    assert response.headers["cache-control"] == "no-store", path
    body: dict[str, Any] = response.json()
    return body


def test_every_wave3_and_wave4_read_view_is_mounted(roots: Path) -> None:
    client = make_client(roots)
    today = get(client, w.TODAY_PATH)
    w.TodayView.model_validate(today)
    assert today["now"] == "2026-10-22T14:02:00Z" and today["execution_enabled"] is False
    activity = get(client, w.ACTIVITY_PATH)
    w.ActivityPage.model_validate(activity)
    acceptance = get(client, w.ACCEPTANCE_PATH)
    acc.AcceptanceView.model_validate(acceptance)
    assert (acceptance["status"], acceptance["unavailable_reason"]) == (
        "unavailable",
        "acceptance_missing",
    )
    pilot = get(client, "/api/pilot")
    PilotView.model_validate(pilot)


def test_the_views_read_the_shared_acceptance_dir(roots: Path) -> None:
    """Today, Activity and GET /api/acceptance resolve TREX_ACCEPTANCE_DIR
    through one resolver, so one document is seen by all three."""
    document = acc.compute_acceptance(
        sessions=[],
        lifecycles=[],
        drills=[],
        owners=["owner@example.com"],
        calendar=session_calendar(),
        computed_at=datetime(2026, 10, 21, 21, 0, tzinfo=UTC),
    )
    path = w.AcceptanceDir(roots / "acceptance").document()
    path.parent.mkdir(parents=True)
    path.write_bytes(model_bytes(document))
    client = make_client(roots)
    assert get(client, w.ACCEPTANCE_PATH)["status"] == "ok"
    assert get(client, w.TODAY_PATH)["acceptance"]["status"] == "ok"
    sources = {s["source"]: s["status"] for s in get(client, w.ACTIVITY_PATH)["sources"]}
    assert sources["drill"] == "ok"


def test_today_reports_family_ingress_from_the_process_environment(roots: Path) -> None:
    client = make_client(roots)
    blockers = {b["code"]: b for b in get(client, w.TODAY_PATH)["blockers"]}
    found = blockers["family_commands_disabled"]
    assert found["scope"] == "commands"
    assert "key_file_missing" in found["message"] and "owners_missing" in found["message"]
    assert str(roots) not in found["message"]  # problem codes only, never a path or key


def test_the_read_views_never_write(roots: Path) -> None:
    def tree() -> list[str]:
        return sorted(str(p.relative_to(roots)) for p in roots.rglob("*"))

    client = make_client(roots)
    before = tree()
    for path in (w.TODAY_PATH, w.ACTIVITY_PATH, w.ACCEPTANCE_PATH, "/api/pilot"):
        get(client, path)
    assert tree() == before
