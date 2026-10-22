"""Wave 4 lane I: ``GET /api/pilot`` (lane-local ``trex-pilot-view/1``).

The view composes the frozen C3.5 shapes (registrations, ``BudgetUsage``,
``registration_run_blockers``) for the ``#/pilot`` page. Null rule as in
C3.2: unknown is null with a reason, never zero. The checked-in fixture
``tests/research/fixtures/pilot/pilot_view.json`` is the contract with the
TypeScript mirror (``web/src/lib/pilotApi.ts``); regenerate it with
``PILOT_VIEW_REGEN_FIXTURE=1``.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tree_options.research.pilot.store import PilotStore
from tree_options.research.pilot.usage import UsageMeter
from tree_options.research.pilot.view import PilotView
from tree_options.trex.contracts import wave3 as w
from tree_options.trex.contracts.commands import CommandKind
from tree_options.trex_web.pilot_view import attach

REPO = Path(__file__).resolve().parents[2]
DRAFTS = REPO / "data" / "research" / "pilot"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pilot" / "pilot_view.json"
OWNER = "owner@example.com"
T0 = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
SEALED_AT = datetime(2026, 10, 22, 13, 0, tzinfo=UTC)
BASELINE = "eq-baseline-spy-weekly"
CHALLENGER = "eq-challenger-spy-reversal"
OBSERVATION = "qsl-observation"
SEAL_FIELDS = ("status", "sealed_by", "sealed_at", "sha256")


def reviewed_sha(program_id: str) -> str:
    document = json.loads((DRAFTS / f"{program_id}.draft.json").read_text())
    for name in SEAL_FIELDS:
        document.pop(name)
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get(root: Path, owners: frozenset[str] = frozenset({OWNER})) -> tuple[int, Any, Any]:
    app = FastAPI()
    attach(app, pilot_dir=lambda: root, clock=lambda: T0, owners=lambda: owners)
    client = TestClient(app, base_url="http://127.0.0.1:8090")
    response = client.get("/api/pilot")
    return response.status_code, response.json(), response.headers


def fixture_state(root: Path) -> Path:
    store = PilotStore(root)
    store.import_drafts(DRAFTS)
    store.seal(
        OBSERVATION,
        reviewed_sha256=reviewed_sha(OBSERVATION),
        sealed_by=OWNER,
        owners={OWNER},
        at=SEALED_AT,
    )
    UsageMeter(root).charge(
        BASELINE,
        T0.date(),
        requests=3,
        compute_seconds=120,
        storage_bytes=2048,
        at=datetime(2026, 10, 22, 13, 30, tzinfo=UTC),
    )
    broken = root / "usage" / CHALLENGER / "2026-10-22.json"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"{broken")
    return root


def rows(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["program_id"]: row for row in body["programs"]}


def test_the_view_lists_every_program_in_role_order(tmp_path: Path) -> None:
    status, body, headers = get(fixture_state(tmp_path))
    assert status == 200
    assert headers["cache-control"] == "no-store"
    assert body["schema"] == "trex-pilot-view/1"
    assert body["now"] == "2026-10-22T14:00:00Z"
    assert (body["status"], body["unavailable_reason"]) == ("ok", None)
    assert [row["program_id"] for row in body["programs"]] == [OBSERVATION, BASELINE, CHALLENGER]
    assert body["roster_problems"] == []
    assert body["owners_configured"] is True
    assert body["seal_command_available"] is ("seal_registration" in get_args(CommandKind))
    assert body["execution_enabled"] is False
    assert body["live_authorized"] is False
    PilotView.model_validate(body)


def test_a_draft_shows_the_hash_the_owner_seals_and_cannot_run(tmp_path: Path) -> None:
    _status, body, _headers = get(fixture_state(tmp_path))
    row = rows(body)[BASELINE]
    assert (row["state"], row["role"], row["unavailable_reason"]) == ("draft", "baseline", None)
    assert row["review_sha256"] == reviewed_sha(BASELINE)
    assert row["registration"]["status"] == "draft"
    assert row["run_blockers"] == ["registration_not_sealed"]
    assert row["usage"]["requests"] == 3
    assert row["usage"]["compute_seconds"] == 120
    assert row["usage"]["storage_bytes"] == 2048
    assert row["usage_unavailable_reason"] is None


def test_unknown_usage_is_null_with_a_reason(tmp_path: Path) -> None:
    _status, body, _headers = get(fixture_state(tmp_path))
    row = rows(body)[CHALLENGER]
    assert row["usage"] is None
    assert row["usage_unavailable_reason"] == "budget_usage_unknown:unreadable"
    assert row["run_blockers"] == ["registration_not_sealed", "budget_usage_unknown"]


def test_a_sealed_program_shows_its_seal(tmp_path: Path) -> None:
    _status, body, _headers = get(fixture_state(tmp_path))
    row = rows(body)[OBSERVATION]
    assert row["state"] == "sealed"
    assert row["registration"]["sealed_by"] == OWNER
    assert row["review_sha256"] == row["registration"]["sha256"] == reviewed_sha(OBSERVATION)
    # zero pilot budgets: the existing lane gets no new pilot jobs
    assert row["run_blockers"] == [
        "budget_requests_exhausted",
        "budget_compute_exhausted",
        "budget_storage_exhausted",
    ]


def test_without_owners_a_sealed_program_cannot_run(tmp_path: Path) -> None:
    _status, body, _headers = get(fixture_state(tmp_path), owners=frozenset())
    assert body["owners_configured"] is False
    assert rows(body)[OBSERVATION]["run_blockers"][0] == "registration_sealer_not_owner"


def test_an_unreadable_registration_is_an_unavailable_row(tmp_path: Path) -> None:
    root = fixture_state(tmp_path)
    (root / "registrations" / f"{BASELINE}.draft.json").write_bytes(b"{broken")
    _status, body, _headers = get(root)
    row = rows(body)[BASELINE]
    assert row == {
        "program_id": BASELINE,
        "role": None,
        "state": "unavailable",
        "unavailable_reason": "registration_unreadable",
        "registration": None,
        "review_sha256": None,
        "usage": row["usage"],
        "usage_unavailable_reason": None,
        "run_blockers": ["registration_unreadable"],
    }
    assert body["roster_problems"] == [
        "pilot_role_count:baseline",
        f"pilot_control_unknown:{BASELINE}",
    ]
    PilotView.model_validate(body)


def test_an_empty_pilot_dir_is_an_empty_roster_not_an_error(tmp_path: Path) -> None:
    _status, body, _headers = get(tmp_path / "never-created")
    assert (body["status"], body["programs"]) == ("ok", [])
    assert body["roster_problems"] == [f"pilot_role_count:{role}" for role in w.PILOT_ROLES]


def test_an_unreadable_pilot_dir_is_unavailable(tmp_path: Path) -> None:
    (tmp_path / "registrations").write_text("not a directory")
    _status, body, _headers = get(tmp_path)
    assert (body["status"], body["unavailable_reason"]) == ("unavailable", "pilot_dir_unreadable")
    assert (body["programs"], body["roster_problems"]) == (None, None)
    PilotView.model_validate(body)


@pytest.mark.parametrize(
    "mutation",
    [
        {"status": "unavailable"},
        {"programs": None},
        {"execution_enabled": True},
        {"live_authorized": True},
    ],
    ids=["status-without-reason", "known-without-programs", "execution", "live"],
)
def test_the_view_model_refuses_ambiguous_nulls(tmp_path: Path, mutation: dict[str, Any]) -> None:
    _status, body, _headers = get(fixture_state(tmp_path))
    with pytest.raises(ValueError):
        PilotView.model_validate({**body, **mutation})


def test_the_typescript_fixture_is_the_served_view(tmp_path: Path) -> None:
    _status, body, _headers = get(fixture_state(tmp_path))
    # the fixture pins the page's input as served before the seal_registration
    # amendment; that one flag follows the running contract
    body = {**body, "seal_command_available": False}
    if os.environ.get("PILOT_VIEW_REGEN_FIXTURE") == "1":
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    assert json.loads(FIXTURE.read_text()) == body
