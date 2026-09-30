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


def plus_board(*ids):
    return replace(board(), rows=[{"id": cid} for cid in ids])


@pytest.mark.parametrize("ids", [("a+b",), ("a+b", "a", "b")])
def test_whole_plus_row_keeps_single_choice_receipt(ids):
    current = plus_board(*ids)
    assert longrun._validated(current, "a+b", "intraday", "") == {
        "note": "",
        "choice": "a+b",
        "horizon": "intraday",
        "row": 0,
    }


def test_bound_outcomes_distinguish_whole_plus_row_and_actual_package():
    calls = []
    prices = {"a+b": 99, "a": 1, "b": 2, "c": 3}

    def outcome(snapshot, candidate, horizon):
        calls.append(candidate)
        return {
            "gross": prices[candidate],
            "net": prices[candidate],
            "exit_at": "2026-06-01T20:00:00+00:00",
        }

    current = plus_board("a+b", "a", "b", "c")
    cache = longrun.OutcomeCache(outcome)
    cache.bind_boards([current])
    assert cache.get(current.snapshot, "a+b", None) == (99, 99)
    assert calls == ["a+b"]
    assert cache.get(current.snapshot, "a+c", None) == (4, 4)
    assert cache.exit_at(current.snapshot, "a+c", None) == "2026-06-01T20:00:00+00:00"
    assert calls == ["a+b", "a", "c"]
    for invalid in ("a+missing", "a+a", "missing"):
        with pytest.raises(ValueError, match="choice"):
            cache.get(current.snapshot, invalid, None)
    with pytest.raises(ValueError, match="snapshot"):
        cache.get("unknown", "a", None)


def test_prior_unbound_package_cannot_be_adopted_as_whole_row():
    cache = longrun.OutcomeCache(lambda snapshot, cid, horizon: {"gross": 1, "net": 1})
    current = plus_board("a+b", "a", "b")
    assert cache.get(current.snapshot, "a+b", None) == (2, 2)
    with pytest.raises(ValueError, match="unbound"):
        cache.bind_boards([current])
    assert cache.get(current.snapshot, "a+b", None) == (2, 2)


def test_board_binding_is_immutable_and_rejects_conflicting_snapshot_membership():
    cache = longrun.OutcomeCache(lambda *args: None)
    current = plus_board("a", "b")
    cache.bind_boards([current])
    cache.bind_boards([current])
    with pytest.raises(ValueError, match="binding"):
        cache.bind_boards([plus_board("a", "c")])
    fresh = longrun.OutcomeCache(lambda *args: None)
    with pytest.raises(ValueError, match="snapshot"):
        fresh.bind_boards([current, plus_board("a", "c")])
    fresh.bind_boards([current])


@pytest.mark.parametrize("right", ["2026-06-01T21:00:00", "bad-time"])
def test_pair_exit_refuses_contradictory_or_malformed_instants_without_cached_success(right):
    def outcome(snapshot, cid, horizon):
        return {
            "gross": 1,
            "net": 1,
            "exit_at": "2026-06-01T20:00:00+00:00" if cid == "a" else right,
        }

    cache = longrun.OutcomeCache(outcome)
    for _ in range(2):
        with pytest.raises(ValueError, match="exit"):
            cache.get(board().snapshot, "a+b", None)


def test_pair_completion_stays_unknown_if_either_exit_is_missing():
    assert longrun._later_exit(None, "2026-06-01T20:00:00+00:00") is None
    assert longrun._later_exit("2026-06-01T20:00:00+00:00", None) is None
    assert longrun._later_exit(None, None) is None
    with pytest.raises(ValueError, match="exit"):
        longrun._later_exit(None, "2026-06-01T20:00:00")


def test_direct_score_binds_whole_row_identity_before_any_outcome_lookup():
    current = plus_board("a+b", "a", "b")
    specs = [longrun.PolicySpec("whole", "rule", rule=lambda b: ("a+b", None))]
    arms = longrun.arms_of(specs)
    receipts = {arms[0].name: {current.snapshot: {"ok": True, "choice": "a+b", "horizon": None}}}
    cache = longrun.OutcomeCache(
        lambda snapshot, cid, horizon: {
            "gross": 99 if cid == "a+b" else 1,
            "net": 99 if cid == "a+b" else 1,
        }
    )
    result = longrun.score_run(
        [current], arms, receipts, cache, longrun.Protocol(draws=1000, random_seeds=200)
    )
    assert result["standings"][0]["net_total"] == 99


def test_engine_identity_binds_runtime_versions(monkeypatch):
    before = longrun.longrun_engine_identity()
    monkeypatch.setattr(longrun.np, "__version__", "different-runtime")
    assert longrun.longrun_engine_identity() != before


def test_engine_identity_binds_actual_helper_source_without_executing_it(tmp_path, monkeypatch):
    from tree_options.desk import skill

    before = longrun.longrun_engine_identity()
    source = tmp_path / "helper-source"
    source.write_text("raise RuntimeError('source must only be hashed')")
    monkeypatch.setattr(skill, "__file__", str(source))
    assert longrun.longrun_engine_identity() != before
    source.write_text("raise RuntimeError('changed source must only be hashed')")
    changed = longrun.longrun_engine_identity()
    source.write_text("raise RuntimeError('source must only be hashed')")
    assert longrun.longrun_engine_identity() != changed
    monkeypatch.setattr(skill, "__file__", None)
    with pytest.raises(RuntimeError, match="source unavailable"):
        longrun.longrun_engine_identity()


def test_whole_plus_skill_keeps_single_row_decomposition_and_live_excess():
    from tree_options.desk import skill

    current = plus_board("a+b", "a", "b")
    arms = longrun.arms_of([longrun.PolicySpec("whole", "rule", rule=lambda b: ("a+b", None))])
    receipts = {arms[0].name: {current.snapshot: {"ok": True, "choice": "a+b", "horizon": None}}}
    cache = longrun.OutcomeCache(
        lambda snapshot, cid, horizon: {
            "gross": 99 if cid == "a+b" else 1,
            "net": 99 if cid == "a+b" else 1,
        }
    )
    cache.bind_boards([current])
    digest = skill.skill_section(
        [current], arms, receipts, cache, longrun.Protocol(draws=1000, random_seeds=200)
    )
    entry = digest["arms"][arms[0].name]
    assert entry.get("decomposition") != "n/a"
    assert entry["net_total"] == 99
    live = skill.progress_skill([current], arms, receipts, cache.get, {})
    assert live["arms"][arms[0].name]["excess"] is not None
    assert "pair arm" not in live["arms"][arms[0].name].get("note", "")


def test_rule_run_binds_whole_plus_before_execution_and_scoring(tmp_path):
    current = plus_board("a+b", "a", "b")
    result = longrun.run_longrun(
        tmp_path / "run",
        boards=[current],
        policies=[longrun.PolicySpec("whole", "rule", rule=lambda b: ("a+b", None))],
        outcome=lambda snapshot, cid, horizon: {
            "gross": 99 if cid == "a+b" else 1,
            "net": 99 if cid == "a+b" else 1,
        },
        ask=None,
        quota_ok=lambda: (True, "fixture"),
        protocol=longrun.Protocol(draws=1000, random_seeds=200),
    )
    assert result["status"] == "finished"
    receipt = longrun.load_receipts(longrun.receipts_path(tmp_path / "run", "whole"))[
        current.snapshot
    ]
    assert receipt["ok"] and receipt["row"] == 0 and "legs" not in receipt
    digest = json.loads((tmp_path / "run" / "digest.json").read_text())
    assert digest["standings"][0]["net_total"] == 99


def test_unbound_valid_package_memo_can_bind_without_changing_meaning():
    cache = longrun.OutcomeCache(lambda snapshot, cid, horizon: {"gross": 1, "net": 1})
    current = plus_board("a", "b")
    assert cache.get(current.snapshot, "a+b", None) == (2, 2)
    cache.bind_boards([current])
    assert cache.get(current.snapshot, "a+b", None) == (2, 2)
