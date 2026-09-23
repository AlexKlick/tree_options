"""Desk HTTP GET: the byte cap and the Content-Length check.

P1 (Codex, 51ed532): ``HTTPResponse.read(amt)`` returns short data WITHOUT
raising when the connection ends before Content-Length (CPython 3.12
http.client says so in a comment), so a cut CSV could parse as a valid,
slightly different history. No network: ``urlopen`` is replaced by a fake.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from email.message import Message

import pytest

from tree_options.desk import http


class _Resp:
    def __init__(self, body: bytes, headers: dict[str, str], status: int = 200) -> None:
        self._fp = io.BytesIO(body)
        self.status = status
        self.headers = Message()
        for k, v in headers.items():
            self.headers[k] = v
        self.reads: list[int | None] = []

    def read(self, amt: int | None = None) -> bytes:
        self.reads.append(amt)
        return self._fp.read() if amt is None else self._fp.read(amt)

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _serve(monkeypatch: pytest.MonkeyPatch, resp: _Resp | Exception) -> list[dict[str, str]]:
    seen: list[dict[str, str]] = []

    def fake(req: urllib.request.Request, timeout: float) -> _Resp:
        seen.append(dict(req.header_items()))
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return seen


def test_whole_body_matching_content_length(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"DATE,VIX\n01/02/1990,17.24\n"
    seen = _serve(monkeypatch, _Resp(body, {"Content-Length": str(len(body))}))
    assert http.urllib_get("https://x.test/a.csv", headers={"Accept": "*/*"}) == (200, body)
    assert seen[0].get("Accept") == "*/*"


def test_short_body_is_refused_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    whole = b"DATE,VIX\n09/22/2026,14.210000\n"
    cut = whole[:-5]  # "14.2" would still parse as a finite decimal
    _serve(monkeypatch, _Resp(cut, {"Content-Length": str(len(whole))}))
    with pytest.raises(http.IncompleteBody):
        http.urllib_get("https://x.test/a.csv", headers={})


def test_advertised_length_over_the_cap_is_refused_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = _Resp(b"x", {"Content-Length": str(http.MAX_BYTES + 1)})
    _serve(monkeypatch, resp)
    with pytest.raises(http.BodyTooLarge):
        http.urllib_get("https://x.test/a.csv", headers={})
    assert resp.reads == []


def test_no_content_length_still_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"abc"
    _serve(monkeypatch, _Resp(body, {}))  # chunked: http.client raises on a cut chunk
    assert http.urllib_get("https://x.test/a", headers={}) == (200, body)
    monkeypatch.setattr(http, "MAX_BYTES", 2)
    _serve(monkeypatch, _Resp(body, {}))
    with pytest.raises(http.BodyTooLarge):
        http.urllib_get("https://x.test/a", headers={})


def test_malformed_content_length_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _Resp(b"abc", {"Content-Length": "three"}))
    with pytest.raises(http.IncompleteBody):
        http.urllib_get("https://x.test/a", headers={})


def test_http_errors_are_statuses_not_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    err = urllib.error.HTTPError("https://x.test/a", 404, "Not Found", Message(), None)
    _serve(monkeypatch, err)
    assert http.urllib_get("https://x.test/a", headers={}) == (404, b"")
