"""Independent scoring oracles: split custody, real participation and retrospective limits."""

from dataclasses import replace

import numpy as np
import pytest

from tree_options.desk import longrun


def fixture(*, repeats=1, future_choice="a", future_horizon=None):
    sessions = [f"2026-06-{n:02d}" for n in range(1, 25)]
    boards = [
        longrun.Board(
            f"s:{s}T10:00",
            s,
            "10:00",
            [{"id": "a", "structure": "call_debit"}, {"id": "b", "structure": "put_debit"}],
        )
        for s in sessions
    ]
    arms = longrun.arms_of(
        [
            longrun.PolicySpec("candidate", "model", repeats=repeats),
            longrun.PolicySpec("inc", "model", repeats=1),
        ]
    )
    receipts = {}
    for arm in arms:
        receipts[arm.name] = {
            b.snapshot: {
                "ok": True,
                "choice": ("a" if b.session <= sessions[11] else future_choice)
                if arm.policy.name == "candidate"
                else "b",
                "horizon": None if b.session <= sessions[11] else future_horizon,
            }
            for b in boards
        }

    def outcome(snapshot, candidate, horizon):
        return {
            "gross": 120.0 if horizon == "expiry" else 20.0 if candidate == "a" else -6.0,
            "net": 100.0 if horizon == "expiry" else 10.0 if candidate == "a" else -6.0,
            "exit_at": snapshot[2:12] + "T15:00:00+00:00",
        }

    protocol = longrun.Protocol(draws=1000, random_seeds=200, incumbent="inc", cutoff=sessions[11])
    return boards, arms, receipts, longrun.OutcomeCache(outcome), protocol


def score(**kwargs):
    return longrun.score_run(*fixture(**kwargs))


def test_heldout_participation_cannot_change_tune_ranking():
    traded, abstained = score(), score(future_choice=None)
    assert traded["walk_forward"]["ranking"] == abstained["walk_forward"]["ranking"]
    candidate = next(r for r in abstained["standings"] if r["policy"] == "candidate")
    assert candidate["vs_random_own"]["p_enter"] == 0.5
    assert candidate["vs_random_own"]["diff_total"] != candidate["vs_random"]["diff_total"]


def test_heldout_horizon_cannot_change_tune_null():
    original = score()
    changed = score(future_horizon="expiry")
    assert original["walk_forward"]["ranking"] == changed["walk_forward"]["ranking"]
    assert original["walk_forward"]["null_scope"] == "split_local_own_entry_rate"


def wf(net=10.0, entries=30, **kwargs):
    sessions = [f"d{i:02d}" for i in range(24)]
    own = {"candidate": np.zeros(24), "inc": np.zeros(24)}
    return longrun.walk_forward(
        {"candidate": np.full(24, net), "inc": np.full(24, -20.0)},
        np.full(24, -30.0),
        sessions,
        incumbent="inc",
        cutoff="d11",
        metric="total",
        max_finalists=2,
        draws=1000,
        seed=1,
        alpha=0.05,
        aa_valid=True,
        own_expected=own,
        test_entries={"candidate": np.array([0] * 12 + [entries] + [0] * 11), "inc": np.zeros(24)},
        **kwargs,
    )["finalists"][0]


@pytest.mark.parametrize(
    "net,entries,eligible",
    [(10.0, 30, True), (10.0, 29, False), (0.0, 30, False), (-1.0, 30, False), (10.0, 0, False)],
)
def test_positive_net_and_independent_entry_floor(net, entries, eligible):
    result = wf(net, entries)
    assert result["eligible_for_operator_review"] is eligible
    assert result["rule_check"]["test_net_positive"] is (net > 0)
    assert result["rule_check"]["test_entries_at_least_floor"] is (entries >= 30)


def test_repeat_copies_do_not_inflate_independent_test_entry_count():
    once, duplicate = score(repeats=1), score(repeats=4)
    for doc in [once, duplicate]:
        row = next(f for f in doc["walk_forward"]["finalists"] if f["policy"] == "candidate")
        assert row["test_entries"] == 12
        assert row["eligible_for_operator_review"] is False


def test_floor_and_null_version_are_serialized_and_reject_invalid_values():
    protocol = longrun.Protocol()
    assert protocol.to_json()["min_test_entries"] == 30
    assert protocol.to_json()["scoring_version"] == "split-local-own-rate/v2"
    for value in [0, -1, True, 1.5]:
        with pytest.raises(ValueError, match="min_test_entries"):
            replace(protocol, min_test_entries=value)
    with pytest.raises(ValueError, match="scoring_version"):
        replace(protocol, scoring_version="legacy")


def test_unevaluable_selected_decisions_do_not_count_towards_floor():
    boards, arms, receipts, _, protocol = fixture(repeats=4)

    def no_outcome(snapshot, candidate, horizon):
        if snapshot[2:12] > "2026-06-12":
            return None
        return {"gross": 20.0, "net": 10.0, "exit_at": snapshot[2:12] + "T15:00:00+00:00"}

    doc = longrun.score_run(boards, arms, receipts, longrun.OutcomeCache(no_outcome), protocol)
    for row in doc["walk_forward"]["finalists"]:
        assert row["test_entries"] == 0
        assert row["eligible_for_operator_review"] is False


def test_retrospective_scoring_never_claims_confirmatory_operator_eligibility():
    doc = longrun.score_run(*fixture(), retrospective=True)
    assert doc["assessment_class"] == "retrospective_descriptive"
    assert doc["promotion"]["pre_registered_at"] is None
    assert all(not r["eligible_for_operator_review"] for r in doc["walk_forward"]["finalists"])
    assert "RETROSPECTIVE" in doc["headline"]


def test_retrospective_cannot_reuse_statistical_eligibility():
    assert wf()["eligible_for_operator_review"] is True
    result = wf(retrospective=True)
    assert result["eligible_for_operator_review"] is False
    assert result["rule_check"]["confirmatory_assessment"] is False


def test_floor_change_refuses_resume_without_mutating_registration(tmp_path):
    from datetime import UTC, datetime

    boards, arms, _, _, protocol = fixture()
    policies = [a.policy for a in arms]
    path = tmp_path / "run"
    path.mkdir()
    longrun._plan(path, boards, policies, protocol, {}, lambda: datetime(2026, 6, 1, tzinfo=UTC))
    before = (path / "plan.json").read_bytes()
    with pytest.raises(ValueError, match="protocol"):
        longrun._plan(
            path,
            boards,
            policies,
            replace(protocol, min_test_entries=31),
            {},
            lambda: datetime(2026, 6, 1, tzinfo=UTC),
        )
    assert (path / "plan.json").read_bytes() == before


def test_projection_preserves_null_version_floor_and_independent_counts():
    doc = score()
    projection = longrun._project_digest(doc)
    assert projection["assessment_class"] == "registered_protocol"
    assert projection["walk_forward"]["scoring_version"] == "split-local-own-rate/v2"
    assert projection["walk_forward"]["min_test_entries"] == 30
    assert projection["walk_forward"]["entry_count_unit"] == "distinct_evaluated_decision_boards"
    assert projection["standings"][0]["vs_random_own"] == doc["standings"][0]["vs_random_own"]
    assert (
        projection["walk_forward"]["finalists"][0]["rule_check"]
        == doc["walk_forward"]["finalists"][0]["rule_check"]
    )


def test_test_abstention_null_expects_zero_without_using_tune_rate():
    doc = score(future_choice=None)
    assert doc["walk_forward"]["null_rates"]["candidate"] == {"tune": 1.0, "test": 0.0}
    result = next(r for r in doc["walk_forward"]["finalists"] if r["policy"] == "candidate")
    assert result["test"]["net_total"] == 0
    assert result["test"]["vs_random"]["diff_total"] == 0
    assert result["test_entries"] == 0


@pytest.mark.parametrize(
    "counts",
    [np.array([1] * 23), np.array([np.nan] * 24), np.array([0.5] * 24), np.array([-1] * 24)],
)
def test_invalid_entry_counts_refuse_normalization(counts):
    sessions = [f"d{i:02d}" for i in range(24)]
    with pytest.raises(ValueError, match="entry counts"):
        longrun.walk_forward(
            {"candidate": np.ones(24)},
            np.zeros(24),
            sessions,
            incumbent=None,
            cutoff="d11",
            metric="total",
            max_finalists=2,
            draws=1000,
            seed=1,
            alpha=0.05,
            aa_valid=True,
            own_expected={"candidate": np.zeros(24)},
            test_entries={"candidate": counts},
        )
