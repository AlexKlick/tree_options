"""Source custody regressions with tiny boards and no provider calls."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from tree_options.desk import longrun

NOW = datetime(2026, 9, 30, tzinfo=UTC)


def board():
    return longrun.Board(
        "s:2026-06-01T10:00",
        "2026-06-01",
        "10:00",
        [{"id": "row", "premium": "1.20"}],
        {"nested": {"trend": "up"}},
    )


def plan(path, boards=None, policies=None, meta=None):
    path.mkdir(exist_ok=True)
    return longrun._plan(
        path,
        boards or [board()],
        policies or [longrun.PolicySpec("m", "model", provider="fixture", prompt="Choose")],
        longrun.Protocol(draws=1000, random_seeds=200),
        meta,
        lambda: NOW,
    )


@pytest.mark.parametrize("surface", ["rows", "context"])
def test_resume_refuses_changed_payload_under_unchanged_board_ids(tmp_path, surface):
    path = tmp_path / "run"
    plan(path)
    before = (path / "plan.json").read_bytes()
    changed = board()
    if surface == "rows":
        changed.rows[0]["premium"] = "9.00"
    else:
        changed.context["nested"]["trend"] = "down"
    with pytest.raises(ValueError, match="boards changed"):
        plan(path, boards=[changed])
    assert (path / "plan.json").read_bytes() == before


def test_board_payload_fingerprint_is_independent_of_mapping_key_order():
    original = board()
    reordered = replace(original, rows=[{"premium": "1.20", "id": "row"}])
    assert longrun.boards_fingerprint([original]) == longrun.boards_fingerprint([reordered])


@pytest.mark.parametrize("change", [{"prompt": "Different"}, {"provider": "other"}, {"repeats": 2}])
def test_resume_refuses_changed_policy_identity(tmp_path, change):
    path = tmp_path / "run"
    original = longrun.PolicySpec("m", "model", provider="fixture", prompt="Choose")
    plan(path, policies=[original])
    before = (path / "plan.json").read_bytes()
    with pytest.raises(ValueError, match="polic"):
        plan(path, policies=[replace(original, **change)])
    assert (path / "plan.json").read_bytes() == before


def test_resume_refuses_changed_source_metadata(tmp_path):
    path = tmp_path / "run"
    plan(path, meta={"source_sha256": "a" * 64})
    before = (path / "plan.json").read_bytes()
    with pytest.raises(ValueError, match="metadata"):
        plan(path, meta={"source_sha256": "b" * 64})
    assert (path / "plan.json").read_bytes() == before


def test_pre_custody_plan_cannot_be_silently_adopted(tmp_path):
    path = tmp_path / "run"
    prior = plan(path)
    prior.pop("custody_version", None)
    (path / "plan.json").write_text(json.dumps(prior))
    before = (path / "plan.json").read_bytes()
    with pytest.raises(ValueError, match="custody"):
        plan(path)
    assert (path / "plan.json").read_bytes() == before


def test_outcome_table_bytes_are_bound_before_reusing_receipts(tmp_path, monkeypatch):
    monkeypatch.setitem(longrun.PLUGINS["boards"], "custody_fixture", lambda params, ctx: [board()])
    table = tmp_path / "outcomes.jsonl"
    row = {
        "snapshot": board().snapshot,
        "candidate_id": "row",
        "exit_mode": "intraday",
        "status": "closed",
        "gross": 10,
        "net": 8,
    }
    table.write_text(json.dumps(row) + "\n")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "boards": {"plugin": "custody_fixture"},
                "outcome": {"plugin": "v2", "table": str(table)},
                "policies": [{"name": "no_trade", "kind": "control", "builtin": "no_trade"}],
                "builtin_controls": False,
                "protocol": {"draws": 1000, "random_seeds": 200},
            }
        )
    )
    path = tmp_path / "run"
    longrun.run_from_config(config, run_dir=path, score_only=True)
    before = (path / "plan.json").read_bytes()
    row["net"] = 800
    table.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="metadata"):
        longrun.run_from_config(config, run_dir=path, score_only=True)
    assert (path / "plan.json").read_bytes() == before


def test_conflicting_duplicate_outcome_identity_is_refused(tmp_path):
    table = tmp_path / "outcomes.jsonl"
    row = {
        "snapshot": board().snapshot,
        "candidate_id": "row",
        "exit_mode": "intraday",
        "status": "closed",
        "gross": 10,
        "net": 8,
    }
    table.write_text(json.dumps(row) + "\n" + json.dumps({**row, "net": 9}) + "\n")
    with pytest.raises(ValueError, match="collision"):
        longrun.plugin("outcome", "v2")({"table": str(table)}, longrun.PluginContext(tmp_path))


def test_identical_duplicate_outcomes_are_idempotent_and_bind_exact_loaded_bytes(tmp_path):
    table = tmp_path / "outcomes.jsonl"
    row = {
        "snapshot": board().snapshot,
        "candidate_id": "row",
        "exit_mode": "intraday",
        "status": "closed",
        "gross": 10,
        "net": 8,
    }
    content = (
        json.dumps(row) + "\n" + json.dumps(dict(reversed(list(row.items())))) + "\n"
    ).encode()
    table.write_bytes(content)
    context = longrun.PluginContext(tmp_path)
    lookup = longrun.plugin("outcome", "v2")({"table": str(table)}, context)
    assert lookup(board().snapshot, "row", "intraday") == {
        "gross": 10.0,
        "net": 8.0,
        "exit_at": None,
    }
    assert (
        context.shared["source_hashes"]["outcome_table_sha256"]
        == hashlib.sha256(content).hexdigest()
    )


def test_resume_refuses_changed_scoring_engine_before_reusing_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(longrun, "longrun_engine_identity", lambda: "a" * 64, raising=False)
    path = tmp_path / "run"
    original = plan(path)
    before = (path / "plan.json").read_bytes()
    monkeypatch.setattr(longrun, "longrun_engine_identity", lambda: "b" * 64, raising=False)
    with pytest.raises(ValueError, match="engine"):
        plan(path)
    assert original["engine_sha256"] == "a" * 64
    assert (path / "plan.json").read_bytes() == before
