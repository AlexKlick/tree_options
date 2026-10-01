from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from tree_options.trex_web.workspace_guard import require_workspace_operator


def client(monkeypatch, enabled=True, loopback=True):
    monkeypatch.setenv("TREX_WORKSPACE_CONTROLS", "1" if enabled else "0")
    app = FastAPI()

    @app.post("/control")
    def control(request: Request):
        require_workspace_operator(request)
        return {"accepted": True, "broker_contacted": False}

    return TestClient(
        app, base_url="http://127.0.0.1", client=("127.0.0.1" if loopback else "testclient", 12345)
    )


def test_operator_controls_disabled_by_default(monkeypatch):
    assert (
        client(monkeypatch, False)
        .post("/control", headers={"origin": "http://127.0.0.1"})
        .status_code
        == 403
    )


def test_cross_origin_and_missing_origin_refused(monkeypatch):
    c = client(monkeypatch)
    for origin in (None, "https://attacker.example", "null", "http://127.0.0.1.evil"):
        headers = {} if origin is None else {"origin": origin}
        assert c.post("/control", headers=headers).status_code == 403


def test_remote_host_and_forwarded_headers_cannot_enable_controls(monkeypatch):
    c = client(monkeypatch)
    assert (
        c.post(
            "/control",
            headers={
                "host": "trex.example",
                "origin": "http://trex.example",
                "x-forwarded-for": "127.0.0.1",
            },
        ).status_code
        == 403
    )


def test_loopback_same_origin_requires_actual_loopback_peer(monkeypatch):
    # TestClient reports peer 'testclient', which is not operator loopback.
    c = client(monkeypatch, loopback=False)
    assert c.post("/control", headers={"origin": "http://127.0.0.1"}).status_code == 403


def test_real_loopback_peer_accepts_same_origin(monkeypatch):
    from starlette.types import ASGIApp

    class Loopback:
        def __init__(self, app: ASGIApp):
            self.app = app

        async def __call__(self, scope, receive, send):
            scope = {**scope, "client": ("127.0.0.1", 34567)}
            await self.app(scope, receive, send)

    c = client(monkeypatch)
    with TestClient(Loopback(c.app), base_url="http://127.0.0.1") as local:
        assert local.post("/control", headers={"origin": "http://127.0.0.1"}).status_code == 200
        assert (
            local.post(
                "/control", headers={"origin": "http://127.0.0.1", "sec-fetch-site": "cross-site"}
            ).status_code
            == 403
        )
