"""GET /api/aibear serves the latest monitor document, read-only and advisory."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tree_options.trex_web.aibear_view import AIBEAR_PATH, attach


def _client(state: Path) -> TestClient:
    app = FastAPI()
    attach(app, state=state)
    return TestClient(app)


def test_no_document_is_unavailable_not_an_error(tmp_path: Path) -> None:
    r = _client(tmp_path).get(AIBEAR_PATH)
    assert r.status_code == 503
    assert r.json() == {
        "status": "unavailable",
        "reason": "no_monitor_document",
        "execution_authorized": False,
    }


def test_the_latest_session_document_is_served_with_no_execution_authority(tmp_path: Path) -> None:
    d = tmp_path / "aibear"
    d.mkdir()
    for s in ("2026-11-10", "2026-11-11"):
        (d / f"{s}.json").write_text(
            json.dumps({"schema": "aibear-monitor/1", "session": s, "names": {}, "secret": "x"})
        )
    (d / "book.json").write_text("{}")
    r = _client(tmp_path).get(AIBEAR_PATH)
    assert r.status_code == 200
    body = r.json()
    assert body["session"] == "2026-11-11"
    assert body["execution_authorized"] is False and body["advisory_only"] is True
    assert "secret" not in body  # only the published fields
    assert r.headers["cache-control"] == "no-store"


def test_a_torn_or_foreign_document_is_refused(tmp_path: Path) -> None:
    d = tmp_path / "aibear"
    d.mkdir()
    (d / "2026-11-10.json").write_text("{not json")
    assert _client(tmp_path).get(AIBEAR_PATH).json()["reason"] == "monitor_document_unreadable"
    (d / "2026-11-11.json").write_text(json.dumps({"schema": "other"}))
    assert _client(tmp_path).get(AIBEAR_PATH).json()["reason"] == "monitor_document_unrecognized"
