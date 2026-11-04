"""Recovery serves evidence without granting any route a write exception."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tree_options.research.runstate.worker import ResearchWorker
from tree_options.trex_web.app import create_app
from tree_options.trex_web.recovery_mode import ENV, enabled


@pytest.mark.parametrize("value", ["", "yes", "2", "enabled"])
def test_invalid_recovery_setting_refuses_startup(value: str) -> None:
    with pytest.raises(ValueError, match="recovery_mode_invalid"):
        enabled({ENV: value})


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@pytest.mark.parametrize(
    "path", ["/api/commands", "/api/discovery/scan", "/api/desk/supervised/resume", "/new-route"]
)
def test_recovery_denies_all_mutations_before_auth_or_route_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    monkeypatch.setenv(ENV, "1")
    monkeypatch.setenv("TREX_WORKSPACE_CONTROLS", "1")
    app = create_app(state_dir=str(tmp_path), discovery_dir=str(tmp_path / "discovery"))
    response = TestClient(app).request(method, path, content=b"not even JSON")
    assert response.status_code == 403
    assert response.json()["reason"] == "recovery_read_only"
    assert not list(tmp_path.iterdir()), "a refused request must not create a queue or spool"


@pytest.mark.parametrize("path", ["/", "/index.html"])
def test_recovery_shell_and_runtime_status_explain_the_same_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setenv(ENV, "true")
    (tmp_path / "index.html").write_text("<html><head></head><body>paper desk</body></html>")
    client = TestClient(create_app(static_dir=str(tmp_path)))
    assert client.get("/api/runtime-mode").json() == {
        "schema": "trex-runtime-mode/1",
        "recovery_read_only": True,
        "live_authorized": False,
    }
    response = client.get(path)
    assert 'name="trex-recovery-mode" content="read-only"' in response.text
    assert response.headers["cache-control"] == "no-store"


def test_normal_paper_mode_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    (tmp_path / "index.html").write_text("<html><head></head><body>paper desk</body></html>")
    client = TestClient(create_app(static_dir=str(tmp_path)))
    assert client.get("/api/runtime-mode").json()["recovery_read_only"] is False
    assert "trex-recovery-mode" not in client.get("/").text


def test_recovery_allows_head_and_refuses_websockets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV, "1")
    client = TestClient(create_app(state_dir=str(tmp_path)))
    # HEAD is a read: the boundary passes it to routing (FastAPI answers 405 for
    # a GET-only route); it is never refused as a recovery mutation.
    assert client.head("/api/runtime-mode").status_code != 403
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/api/any-socket"):
            pass
    assert closed.value.code == 1008


@pytest.mark.parametrize("recovery", [True, False])
def test_recovery_never_starts_the_research_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: bool
) -> None:
    # The worker is a background writer the HTTP boundary cannot see: recovery
    # must not start it, whatever TREX_RESEARCH_WORKER the unit stack inherits.
    started: list[object] = []
    monkeypatch.setattr(ResearchWorker, "start", lambda self: started.append(self))
    monkeypatch.setenv("TREX_RESEARCH_WORKER", "1")
    monkeypatch.setenv("RESEARCH_WORKSPACE_DIR", str(tmp_path / "research"))
    create_app(state_dir=str(tmp_path / "state"), recovery_read_only=recovery)
    assert bool(started) is not recovery


def test_explicit_recovery_flag_cannot_be_disabled_by_an_environment_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV, "0")
    client = TestClient(create_app(recovery_read_only=True))
    assert client.post("/api/commands", json={}).status_code == 403
    assert client.get("/api/runtime-mode").json()["recovery_read_only"] is True
