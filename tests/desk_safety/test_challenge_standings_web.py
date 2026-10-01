"""GET /api/desk/standings: the sealed rule's ladder, read-only on the
cockpit. Oracle discipline: every expectation is hand arithmetic over the
fixture digests (the same discipline as tests/unit/test_challenge_standings.py);
nothing imports the accumulator to compute its own answer.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tree_options.trex_web.app import create_app

D_SEAL = "20261001T162152Z"  # the registration sample's last digest
D_POST1 = "20261002T010000Z"
D_POST2 = "20261003T010000Z"


def _run_summary(
    policy: str, *, pnl: int, minimum: int, boards: int, entered: int,
    by_session: dict[str, float], calls: int = 0, fails: int = 0,
) -> dict:
    return {
        "schema": "desk-lab-run/1",
        "policy": policy,
        "status": "ok",
        "boards_shown": boards,
        "model_calls": calls,
        "model_failures": fails,
        "summary": {
            "entered": entered,
            "closed_capital_proxy": str(5000 + pnl),
            "minimum_closed_capital_proxy": str(minimum),
            "by_session": [
                {"session": s, "closed_pnl": str(v)} for s, v in by_session.items()
            ],
        },
    }


def _digest(store: Path, digest_id: str, *, status: str = "ok",
            runs: dict[str, dict] | None = None) -> None:
    runs_dir = store / "evaluations" / "challenge" / digest_id / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (store / "evaluations" / "challenge" / digest_id / "digest.json").write_text(
        json.dumps({"schema": "desk-challenge/1", "status": status, "at": digest_id})
    )
    for policy, summary in (runs or {}).items():
        target = runs_dir / policy
        target.mkdir(parents=True, exist_ok=True)
        (target / "summary.json").write_text(json.dumps(summary))


@pytest.fixture
def store(tmp_path: Path) -> Path:
    s = tmp_path / "store"
    # the seal-time registration sample NEVER counts
    _digest(s, D_SEAL, runs={
        "gepa:alpha": _run_summary("gepa:alpha", pnl=999, minimum=5000, boards=99,
                                   entered=99, by_session={"2026-09-30": 999.0}),
    })
    # two counted games (hand arithmetic below):
    #   gepa:alpha  boards 30+25, pnl 8+(-3)=5, entered 5+4, worst min 4980,
    #               sessions 2026-10-02 + 2026-10-03, calls 20, fails 1
    #   no_trade    boards 30+25, pnl 1+0=1, entered 0, min 5000/4999 -> 4999
    _digest(s, D_POST1, runs={
        "gepa:alpha": _run_summary("gepa:alpha", pnl=8, minimum=4990, boards=30,
                                   entered=5, by_session={"2026-10-02": 8.0}, calls=10),
        "no_trade": _run_summary("no_trade", pnl=1, minimum=5000, boards=30,
                                 entered=0, by_session={"2026-10-02": 1.0}),
    })
    _digest(s, D_POST2, runs={
        "gepa:alpha": _run_summary("gepa:alpha", pnl=-3, minimum=4980, boards=25,
                                   entered=4, by_session={"2026-10-03": -3.0},
                                   calls=10, fails=1),
        "no_trade": _run_summary("no_trade", pnl=0, minimum=4999, boards=25,
                                 entered=0, by_session={"2026-10-03": 0.0}),
    })
    return s


def _client(store: Path) -> TestClient:
    return TestClient(create_app(
        state_dir=str(store.parent / "legacy"),
        plans_dir=str(store.parent / "plans"),
        discovery_dir=str(store.parent / "discovery"),
        desk_state_dir=str(store.parent / "desk-state"),
        desk_store_dir=str(store),
    ))


def _computed_hand_oracle() -> dict:
    """The expected accumulated standings, hand-derived from the fixture
    digests above (STARTING_CAPITAL 5000): per-policy sums over the two
    post-seal games only."""
    return {
        "games_counted": 2,
        "policies": {
            "gepa:alpha": {
                "kind": "model", "games": 2, "boards": 55, "entered": 9,
                "closed_pnl_sum": "5", "worst_minimum_capital": "4980",
                "model_calls": 20, "model_failures": 1,
                "sessions_distinct": 2,
                "session_pnl": {
                    f"{D_POST1}:2026-10-02": 8.0,
                    f"{D_POST2}:2026-10-03": -3.0,
                },
            },
            "no_trade": {
                "kind": "rules", "games": 2, "boards": 55, "entered": 0,
                "closed_pnl_sum": "1", "worst_minimum_capital": "4999",
                "model_calls": 0, "model_failures": 0,
                "sessions_distinct": 2,
                "session_pnl": {
                    f"{D_POST1}:2026-10-02": 1.0,
                    f"{D_POST2}:2026-10-03": 0.0,
                },
            },
        },
    }


def test_absent_file_is_computed_from_the_digests_and_never_written(store: Path) -> None:
    response = _client(store).get("/api/desk/standings")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    doc = response.json()
    expected = _computed_hand_oracle()
    assert doc["schema"] == "desk-challenge-standings/1"
    assert doc["games_counted"] == expected["games_counted"]
    assert doc["registration_sample_through"] == D_SEAL
    rows = {row["policy"]: row for row in doc["policies"]}
    assert set(rows) == set(expected["policies"])
    for policy, want in expected["policies"].items():
        for key, value in want.items():
            assert rows[policy][key] == value, f"{policy}.{key}"
    # the untrusted note rides along verbatim; no rule_check ran server-side
    assert doc["untrusted_note"].startswith("Model output is untrusted prose")
    assert "clauses" not in doc and "checks" not in doc
    # read-only: the recompute RETURNS, it never writes the store
    assert not (store / "evaluations" / "challenge" / "standings.json").exists()


def test_present_fresh_file_is_served_verbatim(store: Path) -> None:
    # a hand-made standings file (NOT the accumulator's output) is served
    # exactly as it stands while fresh — the nightly rebuild owns the file
    on_disk = {
        "schema": "desk-challenge-standings/1",
        "games_counted": 7,
        "registration_sample_through": D_SEAL,
        "cost_baseline_per_game": 14.6,
        "policies": [{"policy": "gepa:alpha", "kind": "model", "games": 7,
                      "boards": 700, "entered": 40, "closed_pnl_sum": "123.45",
                      "worst_minimum_capital": "4900", "model_calls": 700,
                      "model_failures": 3, "sessions_distinct": 30,
                      "session_pnl": {}}],
        "untrusted_note": "hand-written fixture note",
    }
    path = store / "evaluations" / "challenge" / "standings.json"
    path.write_text(json.dumps(on_disk, indent=2))
    response = _client(store).get("/api/desk/standings")
    assert response.status_code == 200
    assert response.json() == on_disk  # byte-for-byte the file, digests ignored


def test_stale_or_torn_file_falls_back_to_the_recompute(store: Path) -> None:
    path = store / "evaluations" / "challenge" / "standings.json"
    path.write_text(json.dumps({"games_counted": 999}))
    old = time.time() - 27 * 3600
    os.utime(path, (old, old))  # a day older than the 26 h freshness window
    doc = _client(store).get("/api/desk/standings").json()
    assert doc["games_counted"] == _computed_hand_oracle()["games_counted"]
    path.write_text('{"torn": ')  # fresh again, but unreadable
    doc = _client(store).get("/api/desk/standings").json()
    assert doc["games_counted"] == 2  # still recomputed, never a 500


def test_the_store_root_resolution_follows_desk_store_dir(tmp_path: Path) -> None:
    # the route reads through create_app's desk_store_dir — the same root
    # every other /api/desk/* evidence route uses — never a second resolver.
    # A decoy standings.json under the desk STATE root must lose.
    store = tmp_path / "store"
    _digest(store, D_POST1, runs={
        "no_trade": _run_summary("no_trade", pnl=1, minimum=5000, boards=10,
                                 entered=0, by_session={"2026-10-02": 1.0}),
    })
    real = store / "evaluations" / "challenge" / "standings.json"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text(json.dumps({"schema": "desk-challenge-standings/1",
                                "games_counted": 1, "policies": [],
                                "untrusted_note": "store-side file"}))
    decoy_root = tmp_path / "desk-state"
    decoy = decoy_root / "evaluations" / "challenge" / "standings.json"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text(json.dumps({"games_counted": "decoy"}))
    app = create_app(state_dir=str(tmp_path / "legacy"),
                     plans_dir=str(tmp_path / "plans"),
                     discovery_dir=str(tmp_path / "discovery"),
                     desk_state_dir=str(decoy_root),
                     desk_store_dir=str(store))
    doc = TestClient(app).get("/api/desk/standings").json()
    assert doc["untrusted_note"] == "store-side file"


def test_the_route_is_read_only(store: Path) -> None:
    assert _client(store).post("/api/desk/standings").status_code == 405


def test_an_empty_store_answers_an_empty_standings_200(tmp_path: Path) -> None:
    empty = tmp_path / "empty-store"
    doc = _client(empty).get("/api/desk/standings").json()
    assert doc["schema"] == "desk-challenge-standings/1"
    assert doc["games_counted"] == 0 and doc["policies"] == []
    assert doc["cost_baseline_per_game"] == pytest.approx(14.60)
