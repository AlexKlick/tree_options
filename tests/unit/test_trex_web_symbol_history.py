"""Long-term history endpoint: range cut, row decimation, honest degrade,
ETag/304 — the JSON contract of /api/market/{sym}/history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tree_options.trex_web.app import create_app


def _panel(names: list[str], days: list[str]) -> dict[str, Any]:
    return {
        n: {
            d: {
                "open": "100.00",
                "high": "101.00",
                "low": "99.00",
                "close": "100.50",
                "volume": 1_000,
            }
            for d in days
        }
        for n in names
    }


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    paper = tmp_path / "paper"
    paper.mkdir()
    days = [f"2024-01-{d:02d}" for d in range(1, 29)] + [
        f"2024-02-{d:02d}" for d in range(1, 29)
    ] + [f"2024-03-{d:02d}" for d in range(1, 29)]  # 84 sessions (28/day x 3)
    (paper / "ohlc-panel.json").write_text(
        json.dumps(_panel(["AAPL", "SPY"], days))
    )
    (paper / "ohlc-panel.json.lock").write_text("")
    app = create_app(state_dir=str(tmp_path), plans_dir=str(tmp_path), desk_paper_dir=str(paper))
    return TestClient(app)


def test_range_cut_and_decimation(client: TestClient) -> None:
    r = client.get("/api/market/AAPL/history?range=max&max_points=120")
    assert r.status_code == 200
    body = r.json()
    assert body["in_panel"] is True
    assert body["range_sessions"] == 84
    pts = body["points"]
    assert len(pts) <= 120
    assert len(pts[0]) == 6  # ts,o,h,l,c,v
    assert body["y_lo"] is not None and body["y_hi"] is not None
    assert body["vol_max"] == 1000
    assert body["last"]["date"] == "2024-03-28"


def test_range_window_cuts_from_last_session(client: TestClient) -> None:
    # every panel day is inside 365d of the last, so 1y == max here
    r = client.get("/api/market/SPY/history?range=1y")
    assert r.status_code == 200
    assert r.json()["range_sessions"] == 84


def test_rows_and_decimation_semantics(client: TestClient) -> None:
    # endpoint-level: 84 sessions under the 120 clamp floor -> nothing dropped
    body = client.get("/api/market/AAPL/history?range=max&max_points=130").json()
    pts = body["points"]
    assert len(pts) == 84
    # decimation semantics pinned directly (the endpoint clamps to >= 120)
    from tree_options.trex_web.symbol_history import _keep_indices

    keep = _keep_indices(84, 12)
    assert len(keep) <= 12
    assert keep[0] == 0 and keep[-1] == 83  # first/last exact
    assert keep == sorted(set(keep))
    assert _keep_indices(84, 120) == list(range(84))  # under cap: identity


def test_not_in_panel(client: TestClient) -> None:
    body = client.get("/api/market/PLTR/history").json()
    assert body["in_panel"] is False
    assert body["points"] == []
    assert body["error"] is None


def test_panel_busy_degrades(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from tree_options.trex_web import symbol_history as sh

    def _busy(*_a: object, **_k: object) -> Any:
        raise sh.PanelLocked("writer holds the lock")

    monkeypatch.setattr(sh, "read_panel_with_sha256", _busy)
    body = client.get("/api/market/AAPL/history").json()
    assert body["points"] is None
    assert "panel busy" in body["error"]


def test_missing_panel_degrades(client: TestClient) -> None:
    # panel path resolved at create_app time; point a fresh app at nothing
    app = create_app(desk_paper_dir="/nonexistent-paper-dir")
    body = TestClient(app).get("/api/market/AAPL/history").json()
    assert body["points"] is None
    assert body["error"] == "panel unavailable"


def test_etag_304(client: TestClient) -> None:
    r1 = client.get("/api/market/AAPL/history?range=max")
    assert r1.status_code == 200
    etag = r1.headers["ETag"]
    r2 = client.get("/api/market/AAPL/history?range=max", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    # a different range is a different resource
    r3 = client.get("/api/market/AAPL/history?range=5y", headers={"If-None-Match": etag})
    assert r3.status_code == 200


def test_bad_symbol_404_and_case_normalize(client: TestClient) -> None:
    assert client.get("/api/market/TOOLONGSYM/history").status_code == 404
    assert client.get("/api/market/aa-pl1/history").status_code == 404
    # the existing symbol endpoint upper-cases, and so does this one
    assert client.get("/api/market/aapl/history").json()["symbol"] == "AAPL"


def test_odd_params_coerced(client: TestClient) -> None:
    body = client.get("/api/market/AAPL/history?range=bogus&max_points=99999").json()
    assert body["range"] == "3y"
    assert body["error"] is None
