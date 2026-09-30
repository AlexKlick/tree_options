from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from tree_options.research.quant import FrozenUniverse, QuantSnapshot
from tree_options.research.quant_backtest import ReplayPeriod
from tree_options.research.quant_campaign import CampaignSpec, Proposal, run_campaign
from tree_options.research.runstate.store import open_runstate_store
from tree_options.strategy_lab.contracts import Observation
from tree_options.time.calendar import StaticSessionCalendar


@pytest.fixture
def inputs(tmp_path):
    import hashlib
    import json

    sessions = [date(2026, 9, n) for n in (21, 22, 23, 24, 25, 28, 29, 30)]
    path = tmp_path / "calendar.json"
    path.write_text(
        json.dumps(
            {
                "calendar": "fixture",
                "timezone": "America/New_York",
                "open": "09:30",
                "close": "16:00",
                "sessions": [s.isoformat() for s in sessions],
            }
        )
    )
    checksum = tmp_path / "calendar.sha256"
    checksum.write_text(hashlib.sha256(path.read_bytes()).hexdigest())
    calendar = StaticSessionCalendar(path, checksum)
    periods = []
    for decision in sessions[::2]:
        at = calendar.session_close(decision)
        snapshot = QuantSnapshot(
            FrozenUniverse(decision, ("A", "B"), "fixture", "a" * 64),
            at,
            tuple(
                Observation(s, at, at, {"close": Decimal("10")}, "fixture", f"{s}-{decision}")
                for s in ("A", "B")
            ),
        )
        execution = calendar.nth_after(decision, 1)
        periods.append(
            ReplayPeriod(
                snapshot,
                calendar.session_open(execution),
                calendar.session_close(execution),
                {"A": Decimal("10"), "B": Decimal("10")},
                {"A": Decimal("11"), "B": Decimal("9")},
            )
        )
    spec = CampaignSpec(
        "Compare concentration on common frozen periods",
        tuple(periods[:2]),
        (periods[2],),
        (periods[3],),
        "b" * 40,
        "c" * 64,
        (Proposal("equal_weight_us_equities", 1, "Concentration hypothesis"),),
        max_candidates=3,
    )
    return spec, calendar


def test_common_inputs_budget_and_durable_resume(tmp_path, inputs):
    spec, calendar = inputs
    workspace = tmp_path / "research"
    result = run_campaign(workspace, spec, calendar)
    assert result["execution_authorized"] is False
    assert result["exact_external_economics"] is False
    assert result["candidate_count"] == 2
    assert result["winner"]["parameters"] == {"top_n": 1}
    assert Decimal(result["holdout"]["candidate"]["mean_net_return"]) > Decimal(
        result["holdout"]["control"]["mean_net_return"]
    )
    assert run_campaign(workspace, spec, calendar) == result
    with open_runstate_store(workspace) as store:
        assert store.verify()["ok"]
        assert store.get("result", result["campaign_id"]) == result
        attempts = [
            r for r in store.all("quant_provenance") if r.get("schema") == "quant-search-attempt/1"
        ]
        assert len(attempts) == 2
        assert all(r["status"] == "REGISTERED_BEFORE_EVALUATION" for r in attempts)
        graph = result["graph"]
        assert any(n["stage"] == "freeze_winner" for n in graph)
        assert graph[-1]["stage"] == "review_proposal"


def test_reflection_receives_training_only_and_deduplicates(tmp_path, inputs):
    spec, calendar = inputs
    spec = replace(spec, max_candidates=4)
    seen = []

    def propose(feedback):
        seen.append(feedback)
        assert set(feedback) == {
            "schema",
            "hypothesis",
            "generation",
            "training",
            "allowed_strategies",
            "remaining_candidates",
            "objective",
        }
        assert all(r["split"] == "train" for r in feedback["training"])
        return [
            Proposal("equal_weight_us_equities", 2, "Diversify training risk"),
            Proposal("equal_weight_us_equities", 2, "Same bytes of effective config"),
        ]

    result = run_campaign(
        tmp_path / "research",
        spec,
        calendar,
        proposer=propose,
        proposer_identity="fake-reflector/1",
    )
    assert len(seen) == 1
    assert result["candidate_count"] == 3
    assert result["reflection_calls"] == 1
    assert (
        run_campaign(
            tmp_path / "research",
            spec,
            calendar,
            proposer=propose,
            proposer_identity="fake-reflector/1",
        )
        == result
    )
    assert len(seen) == 1


def test_purged_splits_budget_and_proposal_contract(inputs):
    spec, _ = inputs
    with pytest.raises(ValueError, match="chronological"):
        replace(spec, validation=spec.train)
    with pytest.raises(ValueError, match="candidate"):
        replace(spec, max_candidates=True)
    with pytest.raises(ValueError, match="candidate"):
        replace(spec, max_candidates=33)
    with pytest.raises(ValueError, match="supported"):
        Proposal("robust_value_5metric", 1, "Do not weaken value gate")
    with pytest.raises(ValueError, match="top_n"):
        Proposal("momentum_12_1", True, "Invalid boolean sizing")
    with pytest.raises(ValueError, match="risk_penalty"):
        replace(spec, risk_penalty=Decimal("1e999999"))


def test_changed_inputs_and_proposer_are_refused_on_resume(tmp_path, inputs):
    spec, calendar = inputs
    root = tmp_path / "research"
    run_campaign(root, spec, calendar)
    with pytest.raises(ValueError, match="binding"):
        run_campaign(
            root, replace(spec, hypothesis="Changed hypothesis consumes a new experiment"), calendar
        )
    with pytest.raises(ValueError, match="binding"):
        run_campaign(root, spec, calendar, proposer=lambda _: [], proposer_identity="different/1")


def test_failed_reflection_is_not_implicitly_retried(tmp_path, inputs):
    spec, calendar = inputs
    calls = []

    def unavailable(_):
        calls.append(1)
        raise TimeoutError("unavailable")

    for _ in range(2):
        with pytest.raises(ValueError, match="reflection"):
            run_campaign(
                tmp_path / "research",
                spec,
                calendar,
                proposer=unavailable,
                proposer_identity="fake/1",
            )
    assert len(calls) == 1


def test_no_signal_or_missing_economics_cannot_win(tmp_path, inputs):
    spec, calendar = inputs
    spec = replace(
        spec, seeds=(Proposal("momentum_12_1", 1, "Missing historical inputs must exclude"),)
    )
    result = run_campaign(tmp_path / "research", spec, calendar)
    assert result["winner"]["strategy_id"] == "equal_weight_us_equities"
    assert result["candidate_count"] == 2
    assert result["excluded_candidates"]


def test_registered_risk_penalty_changes_selection_on_common_validation(tmp_path, inputs):
    spec, calendar = inputs
    first = replace(spec.train[1], closes={"A": Decimal("20"), "B": Decimal("5")})
    second = replace(spec.validation[0], closes={"A": Decimal("9"), "B": Decimal("11")})
    spec = replace(
        spec, train=(spec.train[0],), validation=(first, second), risk_penalty=Decimal("0")
    )
    return_only = run_campaign(tmp_path / "return", spec, calendar)
    risk_aware = run_campaign(
        tmp_path / "risk", replace(spec, risk_penalty=Decimal("10")), calendar
    )
    assert return_only["winner"]["parameters"] == {"top_n": 1}
    assert risk_aware["winner"]["parameters"] == {}


def test_stop_file_halts_and_same_identity_resumes(tmp_path, inputs):
    spec, calendar = inputs
    root = tmp_path / "research"
    root.mkdir()
    (root / "STOP").touch()
    with pytest.raises(ValueError, match="halted"):
        run_campaign(root, spec, calendar)
    with open_runstate_store(root) as store:
        assert not store.all("quant_experiment")
    (root / "STOP").unlink()
    assert run_campaign(root, spec, calendar)["disposition"] == "REVIEW_REQUIRED"


def test_spec_roundtrip_and_empty_parameters_contract(inputs):
    from tree_options.research.quant_campaign_io import proposal_from_dict, spec_from_dict

    spec, _ = inputs
    assert spec_from_dict(spec.to_dict()).to_dict() == spec.to_dict()
    assert (
        proposal_from_dict(
            {"strategy_id": "momentum_12_1", "parameters": {}, "rationale": "All eligible symbols"}
        ).top_n
        is None
    )


def test_holdout_outcomes_never_select_winner(tmp_path, inputs):
    spec, calendar = inputs
    original = run_campaign(tmp_path / "original", spec, calendar)
    poisoned = replace(spec.holdout[0], closes={"A": Decimal("1"), "B": Decimal("10000")})
    changed = run_campaign(tmp_path / "changed", replace(spec, holdout=(poisoned,)), calendar)
    assert changed["winner"] == original["winner"]
    assert Decimal(original["holdout"]["candidate"]["mean_net_return"]) > 0
    assert Decimal(changed["holdout"]["candidate"]["mean_net_return"]) < 0
