"""Refusal economics survive probes, memoization, selection and projection."""

from copy import deepcopy
from dataclasses import replace

import pytest

from tests.unit.test_desk_measurement_governance import fixture
from tree_options.desk import longrun
from tree_options.desk.cost import CostProvenance


def refusal():
    return dict(
        status="no_price",
        pricing_status="NO_PRICE",
        pricing_reason="no_delta",
        exit_reason="no_delta",
        gross=None,
        net=None,
        exit_at=None,
        cost_model="derived-spread/1",
        cost_provenance=CostProvenance.measured_corpus().as_dict(),
        pricing_key=None,
        exit_mode="intraday",
    )


def case(*, refused="a", chosen="a"):
    boards, arms, receipts, _, protocol = fixture()
    for records in receipts.values():
        for record in records.values():
            record["choice"] = chosen

    def outcome(snapshot, candidate, horizon):
        return refusal() if candidate == refused else dict(gross=20, net=10, exit_at=None)

    return boards, arms, receipts, longrun.OutcomeCache(outcome), protocol


def test_no_price_selected_memo_is_attributed_to_each_arm_and_blocks_review():
    inputs = case()
    inputs[3].get(inputs[0][0].snapshot, "a", None)  # counterfactual probe first
    doc = longrun.score_run(*inputs)
    assert doc["pricing_status"] == "DATA_GATED"
    assert doc["no_price"]["total"] == 48
    assert doc["no_price"]["by_arm"] == {"candidate": 24, "inc": 24}
    assert doc["no_price"]["by_reason"] == {"no_delta": 48}
    assert doc["headline"].startswith("DATA_GATED")
    assert doc["evaluation_valid"] is False
    for row in doc["standings"]:
        assert row["no_price"]["total"] == 24
        assert row["pricing_status"] == "DATA_GATED"
    for finalist in doc["walk_forward"]["finalists"]:
        assert finalist["rule_check"]["pricing_complete"] is False
        assert finalist["eligible_for_operator_review"] is False
    for arm in doc["skill"]["arms"].values():
        assert arm["pricing_status"] == "DATA_GATED"
        assert "alpha" not in arm
    assert longrun.score_run(*inputs)["no_price"] == doc["no_price"]


def test_unused_unpriced_probe_is_not_a_selected_refusal():
    inputs = case(refused="b")
    assert inputs[3].get(inputs[0][0].snapshot, "b", None) is None
    doc = longrun.score_run(*inputs)
    assert doc["no_price"]["total"] == 0
    # An unknown counterfactual is also unsuitable for statistical review.
    assert doc["pricing_status"] == "DATA_GATED"
    assert doc["pricing_coverage"]["counterfactual_refusals"] > 0
    assert all(row["no_price"]["total"] == 0 for row in doc["standings"])


def test_pair_refusal_survives_cache_and_selected_package_identity():
    inputs = case(chosen="a+b")
    doc = longrun.score_run(*inputs)
    assert doc["no_price"]["total"] == 48
    assert all(
        entry["candidate_id"] == "a+b"
        for row in doc["standings"]
        for entry in row["no_price"]["entries"]
    )


def test_refusal_cannot_carry_numeric_profit():
    raw = refusal()
    raw["net"] = 0
    with pytest.raises(ValueError, match="NO_PRICE"):
        longrun.OutcomeCache(lambda *_: raw).get("s", "a", None)


def test_projection_preserves_gate_alpha_and_derived_provenance():
    doc = longrun.score_run(*case())
    projected = longrun._project_digest(doc)
    assert projected["no_price"] == doc["no_price"]
    assert projected["pricing_status"] == "DATA_GATED"
    assert projected["walk_forward"]["alpha"] == 0.05
    assert projected["cost_provenance"]["joint_cells_measured"] is False
    assert projected["cost_model"] == "derived-spread/1"


def test_refusal_fact_is_copied_and_protocol_binds_model():
    raw = refusal()
    cache = longrun.OutcomeCache(lambda *_: raw)
    cache.get("s", "a", None)
    saved = deepcopy(raw)
    raw["cost_provenance"]["execution_authorized"] = True
    assert cache.observation("s", "a", None) == saved
    protocol = replace(fixture()[-1], cost_model="derived-spread/1")
    assert protocol.to_json()["cost_model"] == "derived-spread/1"
    with pytest.raises(ValueError, match="cost_model"):
        replace(protocol, cost_model="unknown")


def test_config_preserves_registered_floor_and_rejects_old_semantics():
    cfg = {
        "protocol": {"min_test_entries": 73, "scoring_version": longrun.SCORING_VERSION},
        "outcome": {"cost_model": "derived-spread/1"},
    }
    registered = longrun.protocol_from_config(cfg)
    assert registered.min_test_entries == 73
    assert registered.cost_model == "derived-spread/1"
    cfg["protocol"]["scoring_version"] = "shared-null/v1"
    with pytest.raises(ValueError, match="scoring_version"):
        longrun.protocol_from_config(cfg)


def test_priced_derived_basis_is_sensitivity_only_even_with_profitable_evidence():
    boards, arms, receipts, _, protocol = fixture()

    def priced(snapshot, candidate, horizon):
        return dict(
            gross=120,
            net=100 if candidate == "a" else -30,
            exit_at=None,
            pricing_status="PRICED_SIMULATION",
            cost_model="derived-spread/1",
            cost_provenance=CostProvenance.measured_corpus().as_dict(),
        )

    doc = longrun.score_run(
        boards,
        arms,
        receipts,
        longrun.OutcomeCache(priced),
        replace(protocol, min_test_entries=1, cost_model="derived-spread/1"),
    )
    assert doc["pricing_status"] == "PRICED_SIMULATION"
    assert doc["no_price"]["total"] == 0
    assert doc["pricing_assessment"] == "derived_retrospective_sensitivity"
    assert doc["assessment_class"] == "retrospective_descriptive"
    assert doc["promotion"]["pre_registered_at"] is None
    for finalist in doc["walk_forward"]["finalists"]:
        assert finalist["rule_check"]["cost_basis_confirmatory"] is False
        assert finalist["eligible_for_operator_review"] is False


def test_table_preserves_typed_refusal_and_cannot_relabel_flat_as_derived(tmp_path):
    import json

    table = tmp_path / "outcomes.jsonl"
    raw = {**refusal(), "snapshot": "s", "candidate_id": "a", "exit_mode": "intraday"}
    table.write_text(json.dumps(raw) + "\n")
    fn = longrun._v2_outcome(
        {"table": str(table), "cost_model": "derived-spread/1"},
        longrun.PluginContext(config_dir=tmp_path),
    )
    assert fn("s", "a", None)["status"] == "no_price"
    raw.update(status="filled", gross=10, net=5, pricing_status=None, cost_model=None)
    table.write_text(json.dumps(raw) + "\n")
    with pytest.raises(ValueError, match="explicit priced simulation"):
        longrun._v2_outcome(
            {"table": str(table), "cost_model": "derived-spread/1"},
            longrun.PluginContext(config_dir=tmp_path),
        )


def test_derived_live_plugin_refuses_missing_leg_inputs_without_flat_fallback(tmp_path):
    from tests.unit.test_desk_derived_cost import prepared

    index, candidate = prepared()
    ctx = longrun.PluginContext(config_dir=tmp_path, shared={"v2": {"index": index}})
    fn = longrun._v2_outcome({"cost_model": "derived-spread/1"}, ctx)
    value = fn("s:2026-09-08T10:00", candidate["id"], "hold:1")
    assert value["status"] == "no_price"
    assert value["net"] is None
    assert value["pricing_reason"] == "delta_unavailable"
    assert value["cost_provenance"]["exact_execution_economics"] is False


def test_engine_custody_includes_derived_surface_bytes(monkeypatch):
    from pathlib import Path

    from tree_options.desk import cost

    original = longrun.longrun_engine_identity()
    read_bytes = Path.read_bytes

    def changed(path):
        content = read_bytes(path)
        return content + b"\n# changed derived surface" if str(path) == cost.__file__ else content

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert longrun.longrun_engine_identity() != original


@pytest.mark.parametrize("status", ["filled", "no_price"])
def test_derived_table_facts_cannot_be_relabelled_as_flat(tmp_path, status):
    import json

    row = {**refusal(), "snapshot": "s", "candidate_id": "a", "exit_mode": "intraday"}
    if status == "filled":
        row.update(status="filled", gross=100, net=90, pricing_status="PRICED_SIMULATION")
    table = tmp_path / "table.jsonl"
    table.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="cost_model differs"):
        longrun._v2_outcome(
            {"table": str(table), "cost_model": "flat"}, longrun.PluginContext(config_dir=tmp_path)
        )


@pytest.mark.parametrize("no_fill", [False, True])
def test_declared_derived_basis_cannot_downgrade_when_callback_has_no_cost_facts(no_fill):
    boards, arms, receipts, cache, protocol = fixture()
    if no_fill:
        cache = longrun.OutcomeCache(lambda *_: None)
    doc = longrun.score_run(
        boards,
        arms,
        receipts,
        cache,
        replace(protocol, cost_model="derived-spread/1", min_test_entries=1),
    )
    assert doc["cost_model"] == "derived-spread/1"
    assert doc["pricing_assessment"] == "derived_retrospective_sensitivity"
    assert doc["assessment_class"] == "retrospective_descriptive"
    assert doc["promotion"]["pre_registered_at"] is None
    assert all(
        f["rule_check"]["cost_basis_confirmatory"] is False
        and f["eligible_for_operator_review"] is False
        for f in doc["walk_forward"]["finalists"]
    )


@pytest.mark.parametrize("excluded_only", [False, True])
def test_skill_only_cost_refusal_gates_full_counterfactual_scope(excluded_only):
    boards, arms, receipts, _, protocol = fixture()
    excluded = boards[0].snapshot
    if excluded_only:
        receipts[arms[-1].name][excluded]["ok"] = False

    def outcome(snapshot, candidate, horizon):
        if horizon == "eod" and (not excluded_only or snapshot == excluded):
            return refusal()
        return dict(
            gross=120 if candidate == "a" else -30,
            net=100 if candidate == "a" else -30,
            exit_at=None,
        )

    doc = longrun.score_run(boards, arms, receipts, longrun.OutcomeCache(outcome), protocol)
    assert doc["no_price"]["total"] == 0
    assert doc["pricing_coverage"]["counterfactual_refusals"] > 0
    assert doc["pricing_status"] == "DATA_GATED"
    assert doc["evaluation_valid"] is False
    assert doc["skill"]["status"] == "DATA_GATED"
    assert all(
        f["rule_check"]["pricing_complete"] is False and f["eligible_for_operator_review"] is False
        for f in doc["walk_forward"]["finalists"]
    )
    if excluded_only:
        assert doc["boards"]["excluded"] == 1


def test_engine_custody_includes_cost_calendar_helper_bytes(monkeypatch):
    from pathlib import Path

    from tree_options.desk import sessions

    original = longrun.longrun_engine_identity()
    read_bytes = Path.read_bytes

    def changed(path):
        content = read_bytes(path)
        return content + b"\n# changed DTE calendar" if str(path) == sessions.__file__ else content

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert longrun.longrun_engine_identity() != original
