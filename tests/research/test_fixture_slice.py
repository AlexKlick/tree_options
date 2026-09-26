"""The synthetic vertical slice — RL-1's machinery demonstration.

One benchmark + two strategy versions with a COMPLETE funded history,
compared on a common declared basis through the real adapter -> plan ->
engine path. This is the nonempty positive path the zero-row catalog
never exercised (audit §6 Phase B), PERMANENTLY labeled synthetic:
every expectation below is hand-calculated from the frozen fixture
(``data/research/fixtures/synthetic-slice-v1.json``, sha-pinned), and the
numbers are invented machinery-validation values — never investment
evidence.

Fixture hand math (five-bp fees, $10,000 start, SPY marks
400.00 -> 408.00 -> 390.00 -> 412.00):

    benchmark  buy 24 @ 400.00   fee 4.80
               final NAV = 395.20 + 24*412.00            = 10,283.20
    momentum   20@400 (fee 4.00), -20@408 (fee 4.08),
               20@388.00 (fee 3.88)
               final NAV = 2,388.04 + 20*412.00          = 10,628.04
    drift      10@400 (fee 2.00), 4@390 (fee 0.78)
               final NAV = 4,437.22 + 14*412.00          = 10,205.22
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from tree_options.research.catalog.fixture_slice import (
    build_synthetic_candidates,
    fixture_sha256,
)
from tree_options.research.comparison.engine import run_comparison
from tree_options.research.contracts import ComparisonSpec

D = Decimal
_WSTART = date(2024, 1, 2)
_WEND = date(2024, 3, 28)


def _slice_spec() -> ComparisonSpec:
    return ComparisonSpec(
        candidate_ids=("synthetic-momentum-v1", "synthetic-drift-v1"),
        starting_capital=D("10000"),
        common_start=_WSTART,
        common_end=_WEND,
        benchmark_candidate_id="synthetic-benchmark-v1",
    )


def _cands() -> dict[str, object]:
    return {c.id: c for c in build_synthetic_candidates()}


def test_fixture_is_present_pinned_and_synthetic_labeled() -> None:
    cands = _cands()
    assert set(cands) == {"synthetic-benchmark-v1",
                          "synthetic-momentum-v1",
                          "synthetic-drift-v1"}
    for c in cands.values():
        assert c.plot_funded_account is True
        assert c.funded_history.value == "reconstructed"
        assert c.artifact_hashes["synthetic-slice-fixture.json"] == fixture_sha256()
        assert c.warnings == ("research.fixture_slice_machinery_validation",)


def test_benchmark_and_two_versions_on_a_common_basis() -> None:
    """THE RL-1 comparison: benchmark + two strategy versions, common
    declared calendar, same capital/costs, nonempty rows, and final
    NAVs matching the hand calculation to the cent."""
    cands = _cands()
    res = run_comparison(
        _slice_spec(),
        (cands["synthetic-momentum-v1"], cands["synthetic-drift-v1"]),
        baseline=cands["synthetic-benchmark-v1"],
    )
    assert res.rejection is None
    by_id = {s.candidate_id: s for s in res.candidates}
    assert set(by_id) == {"synthetic-momentum-v1", "synthetic-drift-v1"}

    mom = by_id["synthetic-momentum-v1"]
    drf = by_id["synthetic-drift-v1"]
    assert mom.rejection_reason is None and drf.rejection_reason is None
    assert len(mom.rows_by_date) == 61 and len(drf.rows_by_date) == 61
    assert mom.final_ending_value == D("10628.04")
    assert drf.final_ending_value == D("10205.22")
    assert mom.fees_paid_total == D("11.96")  # 4.00 + 4.08 + 3.89, once each
    assert drf.fees_paid_total == D("2.78")
    # common basis: every declared session present for both
    assert set(mom.rows_by_date) == set(drf.rows_by_date)

    # paired difference against the benchmark exists and is nonempty
    assert set(res.paired_diff) == {"synthetic-momentum-v1",
                                    "synthetic-drift-v1"}
    final_day = max(mom.rows_by_date)
    mom_diff = res.paired_diff["synthetic-momentum-v1"][final_day.isoformat()]
    # 10,628.04 - 10,283.20 = 344.84
    assert mom_diff["value"] == "344.84"

    # the benchmark itself computes to its own hand oracle
    bench = run_comparison(
        ComparisonSpec(candidate_ids=("synthetic-benchmark-v1",),
                       starting_capital=D("10000"),
                       common_start=_WSTART, common_end=_WEND),
        (cands["synthetic-benchmark-v1"],),
    )
    assert bench.candidates[0].final_ending_value == D("10283.20")


def test_observed_drawdown_and_recovery_render_in_the_slice() -> None:
    cands = _cands()
    res = run_comparison(_slice_spec(), (cands["synthetic-benchmark-v1"],))
    s = res.candidates[0]
    assert s.drawdown  # the February dip produced drawdown cells
    recovered = [cell for cell in s.drawdown.values()
                 if cell["recovery_end_date"] is not None]
    assert recovered  # recovery to 412 is OBSERVED and reported


def test_nonempty_result_serializes_and_is_json_safe() -> None:
    cands = _cands()
    res = run_comparison(_slice_spec(),
                         (cands["synthetic-momentum-v1"],),
                         baseline=cands["synthetic-benchmark-v1"])
    wire = res.to_wire()
    text = json.dumps(wire)
    assert '"2024-03-28"' in text
    assert "344.84" in text


def test_worker_publishes_the_slice_end_to_end(tmp_path) -> None:
    """POST -> worker -> stored immutable result: the HTTP path with a
    NONEMPTY payload, readable repeatedly without recomputation."""
    from fastapi import FastAPI

    from tree_options.trex_web.research_view import attach as attach_research

    scopes = tmp_path / "scopes"
    scopes.mkdir()
    app = FastAPI()
    worker = attach_research(app, workspace=tmp_path / "ws",
                             candidate_scopes_root=scopes, start_worker=False)
    client = TestClient(app)

    cat = client.get("/api/research/candidates").json()["candidates"]
    synthetic_ids = {c["id"] for c in cat if c["evidence_kind"] == "synthetic_backtest"}
    assert synthetic_ids == {"synthetic-benchmark-v1",
                             "synthetic-momentum-v1",
                             "synthetic-drift-v1"}
    for c in cat:
        if c["evidence_kind"] == "synthetic_backtest":
            assert c["warnings"] == ["research.fixture_slice_machinery_validation"]

    run_id = client.post("/api/research/compare", json={
        "candidate_ids": ["synthetic-momentum-v1", "synthetic-drift-v1"],
        "benchmark_candidate_id": "synthetic-benchmark-v1",
        "starting_capital": "10000",
        "common_start": "2024-01-02",
        "common_end": "2024-03-28",
    }).json()["run_id"]
    assert worker.step() is True
    body = client.get(f"/api/research/runs/{run_id}/result").json()
    assert body["status"] == "completed"
    result = body["result"]
    finals = {s["candidate_id"]: s["final_ending_value"]
              for s in result["candidates"]}
    assert finals["synthetic-momentum-v1"] == "10628.04"
    assert finals["synthetic-drift-v1"] == "10205.22"
    # the stored artifact is bound to its inputs
    assert body["input_snapshot_sha256"]
    assert body["engine_sha256"]
    assert body["result_sha256"]
    again = client.get(f"/api/research/runs/{run_id}/result").json()
    assert again == body
