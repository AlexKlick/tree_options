"""Wave 4 lane H: ``GET /api/acceptance`` (``trex-acceptance-view/1``).

The oracle for the happy path is the frozen contract fixture
``tests/contracts/fixtures/wave3/acceptance_view.json``: served from a file
holding its document, the response must equal it byte for byte as JSON.
Every refusal maps to one of the contract's four unavailable reasons, and a
refused document is never served (``document`` null, ``live_authorized``
false).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tree_options.trex.clock import session_calendar
from tree_options.trex.contracts import acceptance as contract
from tree_options.trex.contracts.canonical import model_bytes
from tree_options.trex.contracts.wave3 import ACCEPTANCE_PATH, AcceptanceDir
from tree_options.trex_web import acceptance_view

FIXTURE = Path(__file__).resolve().parents[1] / "contracts/fixtures/wave3/acceptance_view.json"
CAL = session_calendar()


def Z(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def client(root: Path, now: str, env_dir: bool = False) -> TestClient:
    app = FastAPI()
    if env_dir:
        acceptance_view.attach(app, clock=lambda: Z(now))
    else:
        acceptance_view.attach(app, acceptance_dir=lambda: root, clock=lambda: Z(now))
    return TestClient(app)


def unavailable(now: str, reason: str) -> dict:
    return {
        "schema": "trex-acceptance-view/1",
        "now": now,
        "status": "unavailable",
        "unavailable_reason": reason,
        "document": None,
        "live_authorized": False,
    }


def write_doc(root: Path, doc: dict | bytes) -> None:
    path = AcceptanceDir(root).document()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(doc if isinstance(doc, bytes) else json.dumps(doc).encode())


def test_serves_the_verified_document_exactly_as_the_contract_fixture(tmp_path) -> None:
    expected = fixture()
    write_doc(tmp_path, expected["document"])
    response = client(tmp_path, expected["now"]).get(ACCEPTANCE_PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == expected
    contract.AcceptanceView.model_validate(response.json())


def test_missing_document(tmp_path) -> None:
    now = "2026-10-22T14:00:00Z"
    assert client(tmp_path, now).get(ACCEPTANCE_PATH).json() == unavailable(
        now, "acceptance_missing"
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(verdict="met"),
        lambda d: d.update(live_authorized=True),
        lambda d: d["streak"].update(current_sessions=3, best_sessions=3),
        lambda d: d["per_kind"]["option"].update(counted=10),
        lambda d: d["drill_results"][1].update(passed=True, reasons=[]),
        lambda d: d.update(drills_passed=7),
        lambda d: d["sessions"][0].update(heartbeat_minutes=390),
    ],
    ids=["verdict", "live", "streak", "counter", "drill", "drills_passed", "input"],
)
def test_a_hand_edited_document_is_invalid(tmp_path, mutate) -> None:
    expected = fixture()
    doc = expected["document"]
    mutate(doc)
    write_doc(tmp_path, doc)
    assert client(tmp_path, expected["now"]).get(ACCEPTANCE_PATH).json() == unavailable(
        expected["now"], "acceptance_invalid"
    )


@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b'{"schema": "trex-acceptance/1"}', b"\xff"])
def test_garbage_is_invalid(tmp_path, raw: bytes) -> None:
    write_doc(tmp_path, raw)
    now = "2026-10-22T14:00:00Z"
    assert client(tmp_path, now).get(ACCEPTANCE_PATH).json()["unavailable_reason"] == (
        "acceptance_invalid"
    )


def test_a_symlinked_or_oversized_document_is_invalid(tmp_path, monkeypatch) -> None:
    expected = fixture()
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps(expected["document"]))
    AcceptanceDir(tmp_path).document().symlink_to(real)
    got = client(tmp_path, expected["now"]).get(ACCEPTANCE_PATH).json()
    assert got == unavailable(expected["now"], "acceptance_invalid")
    AcceptanceDir(tmp_path).document().unlink()
    write_doc(tmp_path, expected["document"])
    monkeypatch.setattr(acceptance_view, "MAX_DOCUMENT_BYTES", 100)
    got = client(tmp_path, expected["now"]).get(ACCEPTANCE_PATH).json()
    assert got == unavailable(expected["now"], "acceptance_invalid")


def test_another_calendar_is_a_calendar_mismatch(tmp_path) -> None:
    expected = fixture()
    expected["document"]["calendar_sha256"] = "f" * 64  # not a derived field: it parses
    write_doc(tmp_path, expected["document"])
    assert client(tmp_path, expected["now"]).get(ACCEPTANCE_PATH).json() == unavailable(
        expected["now"], "acceptance_calendar_mismatch"
    )


def test_a_hidden_trailing_session_is_a_window_mismatch(tmp_path) -> None:
    expected = fixture()
    # computed on 2026-10-21 but re-dated after the 2026-10-22 close: the window
    # would hide an unrecorded 2026-10-22 (a trailing session resets the streak)
    expected["document"]["computed_at"] = "2026-10-22T21:00:00Z"
    write_doc(tmp_path, expected["document"])
    got = client(tmp_path, "2026-10-22T22:00:00Z").get(ACCEPTANCE_PATH).json()
    assert got == unavailable("2026-10-22T22:00:00Z", "acceptance_window_mismatch")


def test_a_document_from_the_future_is_invalid(tmp_path) -> None:
    expected = fixture()
    write_doc(tmp_path, expected["document"])  # computed 2026-10-21T21:00:00Z
    got = client(tmp_path, "2026-10-21T20:54:59Z").get(ACCEPTANCE_PATH).json()
    assert got == unavailable("2026-10-21T20:54:59Z", "acceptance_invalid")
    ok = client(tmp_path, "2026-10-21T20:55:00Z").get(ACCEPTANCE_PATH).json()
    assert ok["status"] == "ok"  # up to 300 s of clock skew is tolerated


def test_a_recomputed_document_is_served(tmp_path) -> None:
    doc = contract.compute_acceptance(
        sessions=[],
        lifecycles=[],
        drills=[],
        owners=["owner@example.com"],
        calendar=CAL,
        computed_at=Z("2026-10-22T21:00:00Z"),
    )
    write_doc(tmp_path, model_bytes(doc))
    got = client(tmp_path, "2026-10-22T21:00:01Z").get(ACCEPTANCE_PATH).json()
    assert got["status"] == "ok"
    assert got["document"]["verdict"] == "not_met"
    assert got["document"]["live_authorized"] is False and got["live_authorized"] is False


def test_the_directory_resolves_per_request_from_the_environment(tmp_path, monkeypatch) -> None:
    expected = fixture()
    write_doc(tmp_path / "a", expected["document"])
    monkeypatch.setenv("TREX_ACCEPTANCE_DIR", str(tmp_path / "b"))
    app = client(tmp_path, expected["now"], env_dir=True)
    assert app.get(ACCEPTANCE_PATH).json()["unavailable_reason"] == "acceptance_missing"
    monkeypatch.setenv("TREX_ACCEPTANCE_DIR", str(tmp_path / "a"))
    assert app.get(ACCEPTANCE_PATH).json() == expected


@pytest.mark.parametrize(
    ("configured", "served"),
    [
        ("owner@example.com", "ok"),
        (" Owner@Example.com ,", "ok"),  # the contract's own parser: trimmed, lower-cased
        ("", "ok"),  # nothing configured: nothing to compare with
        ("mallory@example.net", "acceptance_owners_mismatch"),
        ("owner@example.com,second@example.com", "acceptance_owners_mismatch"),
        ("not an email", "acceptance_owners_mismatch"),
    ],
)
def test_a_document_naming_other_owners_than_configured_is_refused(
    tmp_path, monkeypatch, configured: str, served: str
) -> None:
    """Integration fix (review P2): a document that chose its own owner list
    (drills attested by them) is never served as this desk's acceptance."""
    expected = fixture()
    assert expected["document"]["owners"] == ["owner@example.com"]
    write_doc(tmp_path, expected["document"])
    if configured:
        monkeypatch.setenv("TREX_OWNER_EMAILS", configured)
    got = client(tmp_path, expected["now"]).get(ACCEPTANCE_PATH).json()
    if served == "ok":
        assert got == expected
    else:
        assert got == unavailable(expected["now"], served)


def test_payload_is_pure_and_utc(tmp_path) -> None:
    expected = fixture()
    write_doc(tmp_path, expected["document"])
    now = Z(expected["now"]).astimezone(UTC)
    view = acceptance_view.acceptance_view(tmp_path, now, CAL)
    assert view.status == "ok" and view.live_authorized is False
    assert acceptance_view.acceptance_payload(tmp_path, now, CAL) == expected


def test_view_never_writes(tmp_path) -> None:
    client(tmp_path / "absent", "2026-10-22T14:00:00Z").get(ACCEPTANCE_PATH)
    assert not (tmp_path / "absent").exists()
