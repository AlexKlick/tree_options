"""Hand oracles for derived cost assumptions; never a broker fill ledger."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_desk_outcomes import E1, expiry_bundle
from tree_options.desk import outcomes
from tree_options.desk.cost import (
    CostModelError,
    CostProvenance,
    Leg,
    NoPriceLedger,
    SpreadCostModel,
    UnpricedCostError,
)


def leg(delta: str = "0.05", *, symbol: str = "SPY", dte: int = 10) -> Leg:
    return Leg(symbol, Decimal(delta), dte, "2026-09-08", "2026-09-08T09:59:00-04:00", False)


def prepared() -> tuple[outcomes.OutcomeIndex, dict[str, Any]]:
    index = outcomes.prepare_index(expiry_bundle())
    candidate = next(
        c for c in outcomes.board_candidates(index, E1, "10:00") if c["structure"] == "call_debit"
    )
    return index, candidate


def attach(candidate: dict[str, Any]) -> None:
    candidate["cost_legs"] = [
        {
            **leg(delta).to_json(),
            "ticker": candidate[name],
            "available_at": "2026-09-08T13:59:30+00:00",
        }
        for name, delta in (("long", "0.05"), ("short", "0.60"))
    ]


def evaluate(index: outcomes.OutcomeIndex, candidate: dict[str, Any]) -> dict[str, Any]:
    result = outcomes.candidate_outcome(
        index, E1, "10:00", candidate["id"], costs=SpreadCostModel(), exit_mode="hold:1"
    )
    assert result is not None
    return result


def test_full_quote_is_halved_once_and_both_package_legs_pay_twice() -> None:
    quote = SpreadCostModel().price([leg(), leg("0.60")])
    # (.02 / 2 + .19 / 2) * 100 * 2 + .65 * 4
    assert quote.total_round_trip == Decimal("23.600000")
    assert quote.spread_total == Decimal("21.000000")
    assert quote.commission_total == Decimal("2.600000")


@pytest.mark.parametrize(
    "delta,half",
    [
        ("0.10", "0.010000"),
        ("0.100001", "0.015000"),
        ("0.35", "0.025000"),
        ("0.350001", "0.030000"),
        ("0.50", "0.030000"),
        ("0.500001", "0.095000"),
    ],
)
def test_delta_bands_are_right_closed(delta: str, half: str) -> None:
    assert SpreadCostModel().half_spread_per_share(Decimal(delta), 14) == Decimal(half)


@pytest.mark.parametrize(
    "dte,half", [(21, "0.030000"), (22, "0.039990"), (45, "0.039990"), (46, "0.050010")]
)
def test_rounded_dte_marginal_ratios_remain_explicit_model_assumptions(dte: int, half: str) -> None:
    assert SpreadCostModel().half_spread_per_share(Decimal("0.4"), dte) == Decimal(half)


@pytest.mark.parametrize("delta", ["NaN", "Infinity", "-Infinity", "0.71", "-0.01"])
def test_unknown_delta_never_receives_a_default(delta: str) -> None:
    with pytest.raises(UnpricedCostError):
        SpreadCostModel().price([leg(), leg(delta)])


@pytest.mark.parametrize("dte", [True, 14.5, 6, 61])
def test_invalid_dte_is_typed_no_price(dte: Any) -> None:
    with pytest.raises(UnpricedCostError):
        SpreadCostModel().price([leg(), leg(dte=dte)])


@pytest.mark.parametrize("commission", ["NaN", "Infinity", "-1"])
def test_invalid_model_cost_is_explicit_refusal(commission: str) -> None:
    with pytest.raises(CostModelError):
        SpreadCostModel(commission_per_leg=Decimal(commission))


@pytest.mark.parametrize("multiplier", [True, 1.5, 0])
def test_multiplier_is_positive_integer(multiplier: Any) -> None:
    with pytest.raises(CostModelError):
        SpreadCostModel(multiplier=multiplier)


def test_provenance_says_derived_not_jointly_measured_and_no_fill_economics() -> None:
    facts = CostProvenance.measured_corpus().as_dict()
    assert facts["surface_kind"] == "derived_from_reported_marginals"
    assert facts["joint_cells_measured"] is False
    assert facts["describes_fill_clock"] is False
    assert facts["exact_execution_economics"] is False
    assert facts["corpus_revalidated"] is False
    assert SpreadCostModel().price([leg(), leg()]).to_json()["schema"] == "desk-derived-cost/1"


def test_real_iso_expiry_missing_delta_is_no_price_and_never_zero_profit() -> None:
    index, candidate = prepared()
    assert candidate["expiry"] == "2026-09-18"
    result = evaluate(index, candidate)
    assert result["status"] == "no_price"
    assert result["pricing_reason"] == "delta_unavailable"
    assert result["gross"] is None and result["net"] is None


def test_per_leg_cost_inputs_price_real_iso_expiry_as_modeled_simulation() -> None:
    index, candidate = prepared()
    attach(candidate)
    baseline = outcomes.candidate_outcome(index, E1, "10:00", candidate["id"], exit_mode="hold:1")
    assert baseline is not None
    result = evaluate(index, candidate)
    assert result["net"] == baseline["gross"] - Decimal("23.600000")
    assert result["pricing_status"] == "PRICED_SIMULATION"
    assert result["cost_model"] == "derived-spread/1"
    assert result["cost_provenance"]["exact_execution_economics"] is False


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("missing_leg", "incomplete_package"),
        ("duplicate_leg", "incomplete_package"),
        ("wrong_ticker", "incomplete_package"),
        ("malformed_ticker", "incomplete_package"),
        ("known_wrong_symbol", "incomplete_package"),
        ("unknown_symbol", "unknown_symbol"),
        ("wrong_dte", "inconsistent_dte"),
        ("missing_source", "invalid_provenance"),
        ("missing_flag", "invalid_provenance"),
        ("string_flag", "invalid_provenance"),
        ("future_event", "future_cost_input"),
        ("future_availability", "future_cost_input"),
        ("availability_before_event", "invalid_provenance"),
        ("naive_timestamp", "invalid_provenance"),
    ],
)
def test_package_and_pit_inputs_fail_closed(mutation: str, reason: str) -> None:
    index, candidate = prepared()
    attach(candidate)
    legs = candidate["cost_legs"]
    if mutation == "missing_leg":
        legs.pop()
    elif mutation == "duplicate_leg":
        legs[1] = deepcopy(legs[0])
    elif mutation == "wrong_ticker":
        legs[1]["ticker"] = "O:UNKNOWN"
    elif mutation == "malformed_ticker":
        legs[1]["ticker"] = {}
    elif mutation == "known_wrong_symbol":
        candidate["underlying"] = "QQQ"
        for item in legs:
            item["symbol"] = "QQQ"
    elif mutation == "unknown_symbol":
        candidate["underlying"] = "UNMEASURED"
        for item in legs:
            item["symbol"] = "UNMEASURED"
    elif mutation == "wrong_dte":
        legs[1]["dte"] = 14
    elif mutation == "missing_source":
        del legs[1]["source_timestamp_et"]
    elif mutation == "missing_flag":
        del legs[1]["is_eod_snapshot"]
    elif mutation == "string_flag":
        legs[1]["is_eod_snapshot"] = "false"
    elif mutation == "future_event":
        legs[1]["source_timestamp_et"] = "2026-09-08T10:01:00-04:00"
        legs[1]["available_at"] = "2026-09-08T14:01:00+00:00"
    elif mutation == "future_availability":
        legs[1]["available_at"] = "2026-09-08T14:01:00+00:00"
    elif mutation == "availability_before_event":
        legs[1]["available_at"] = "2026-09-08T13:58:00+00:00"
    else:
        legs[1]["source_timestamp_et"] = "2026-09-08T09:59:00"
    result = evaluate(index, candidate)
    assert result["status"] == "no_price"
    assert result["pricing_reason"] == reason
    assert result["exit_reason"] == reason
    assert result["net"] is None and result["gross"] is None


def test_summary_keeps_no_price_count_and_no_performance_denominator() -> None:
    index, _candidate = prepared()
    rows = list(outcomes.outcome_table(index, modes=("hold:1",), costs=SpreadCostModel()))
    summary = outcomes.summarize(rows)["modes"]["hold:1"]
    assert summary["status"]["no_price"] > 0
    assert summary["filled"] == 0
    assert summary["mean_net"] is None


def test_ledger_attributes_cached_selected_fact_to_each_named_arm_and_horizon() -> None:
    index, candidate = prepared()
    result = evaluate(index, candidate)
    ledger = NoPriceLedger()
    for arm in ("alpha", "beta"):
        for horizon in ("hold:1", "hold:3"):
            ledger.record_outcome(
                arm=arm,
                snapshot="s:2026-09-08T10:00",
                candidate_id=candidate["id"],
                exit_mode=horizon,
                outcome=result,
            )
    ledger.record_outcome(
        arm="alpha",
        snapshot="s:2026-09-08T10:00",
        candidate_id=candidate["id"],
        exit_mode="hold:1",
        outcome=result,
    )
    assert ledger.for_arm("alpha")["total"] == 2
    assert ledger.as_dict()["by_arm"] == {"alpha": 2, "beta": 2}
    conflicting = {**result, "pricing_reason": "unknown_symbol", "exit_reason": "unknown_symbol"}
    with pytest.raises(CostModelError, match="collision"):
        ledger.record_outcome(
            arm="alpha",
            snapshot="s:2026-09-08T10:00",
            candidate_id=candidate["id"],
            exit_mode="hold:1",
            outcome=conflicting,
        )


def test_model_refuses_missing_or_nonboolean_source_declaration() -> None:
    with pytest.raises(CostModelError):
        replace(leg(), is_eod_snapshot="false")


def test_explicit_derived_cli_refuses_unavailable_delta_without_fake_measurement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(expiry_bundle()))
    output = tmp_path / "outcomes.jsonl"
    assert (
        outcomes._cli(["--bundle", str(bundle), "--out", str(output), "--cost-model", "derived"])
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["costs"]["cost_model"] == "derived-spread/1"
    assert summary["costs"]["cost_provenance"]["joint_cells_measured"] is False
    assert summary["assessment"] == "DATA_GATED"
    rows = [json.loads(row) for row in output.read_text().splitlines()]
    refusals = [row for row in rows if row["status"] == "no_price"]
    assert refusals and all(row["net"] is None for row in refusals)
    assert summary["round_trip_cost"] is None
