"""(w7b) The owner-ratified real-lane era profile — the protocol-adjacent
artifact binding the theory-panel ruling's geometry, units, embargo
deviation, holdout boundary rule, and seal-consumption disclosure.

Ruling: ~/documents/tree_options-logs/theory-panel-verdict.md §2 P0-1/P0-2,
§3, D3, D4, D6, D7 — owner ruled 2026-08-26. The committed artifact is
`data/real-lane/era-profile.json`; this suite pins it to the model, to the
ratified values, and to a fresh deterministic build.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from tests.conftest import REPO_ROOT
from tree_options.protocol.era_profile import (
    RATIFIED_ERA_GEOMETRY,
    RealLaneEraProfile,
    build_real_lane_era_profile,
    real_lane_split_override,
)

ARTIFACT = REPO_ROOT / "data" / "real-lane" / "era-profile.json"


def test_the_committed_artifact_validates_and_binds() -> None:
    """The committed JSON is exactly the model: extra="forbid" plus the
    content hash over the typed body means neither the file nor the model
    can drift from the other."""
    artifact = RealLaneEraProfile.model_validate_json(ARTIFACT.read_text("utf-8"))
    assert artifact.profile_id == "real-lane-era-1"
    assert artifact.owner_decision_id == "m4-real-lane-theory-panel-ruling-2026-08-26"
    assert "theory-panel-verdict.md" in artifact.ruling_citation
    assert artifact.scope == "lane-2 evaluation folds (massive-derived-free/1)"


def test_the_artifact_is_a_fresh_deterministic_build() -> None:
    built = build_real_lane_era_profile()
    assert built.model_dump(mode="json") == json.loads(ARTIFACT.read_text("utf-8"))


def test_geometry_is_the_owner_ratified_one() -> None:
    """(verdict D4) mt34 / val 6 / E2 / test 13 / roll 13 / H5 — and the
    accessor hands the driver the equivalent `OptionsSplitOverride`."""
    from tree_options.trials.options_run import OptionsSplitOverride

    artifact = build_real_lane_era_profile()
    assert artifact.geometry.as_tuple() == RATIFIED_ERA_GEOMETRY == (5, 2, 6, 13, 13, 34)
    assert real_lane_split_override() == OptionsSplitOverride(
        label_horizon_sessions=5,
        embargo_sessions=2,
        val_sessions=6,
        test_sessions=13,
        roll_sessions=13,
        min_train_sessions=34,
    )


def test_units_declare_the_friday_grid_re_denomination() -> None:
    """(verdict D3) H = 5 GRID FRIDAYS on the Friday-only calendar, with the
    whole re-denomination bundle recorded — no mixed ordinal space."""
    units = build_real_lane_era_profile().units
    assert units.label_horizon_sessions == 5
    assert units.label_horizon_unit == "grid_fridays"
    assert units.momentum_windows_weeks == (1, 5, 20)
    assert units.arm_a_hold_weeks == 4
    assert units.end_buffer_sessions == 6 and units.end_buffer_unit == "grid_fridays"
    assert units.cohort_stride_sessions == 4
    assert units.mom_20_warmup_grid_indices == 21
    assert units.embargo_unit == "grid_fridays"


def test_the_e2_deviation_is_recorded_not_smuggled() -> None:
    """(verdict D4) The era embargo is 2 grid Fridays; the protocol's
    ordinal embargo shape is E5 (G = H+E = 10). The record names both, says
    where the deviation rides, and prices the alternative."""
    deviation = build_real_lane_era_profile().embargo_deviation
    assert deviation.era_embargo_sessions == 2
    assert deviation.protocol_embargo_sessions == 5
    assert deviation.protocol_gap_sessions_at_e5 == 10
    assert "config hash" in deviation.rides
    assert "mt24/val6" in deviation.priced_alternative


def test_the_holdout_boundary_rule_and_seal_disclosure() -> None:
    """(verdict D7.2) Decision sessions are refused at registration;
    execution-tail consumption is tagged and counted, never refused; and
    the record states what wave-1 artifacts provably contain — window-A
    executions/marks through 2026-06-26 and ZERO window-A decisions."""
    profile = build_real_lane_era_profile()
    rule = profile.holdout_boundary_rule
    assert rule.sealed_window_id == "final-holdout-window-a"
    assert rule.decision_sessions == "excluded_refused_before_registration"
    assert rule.execution_tail == "tagged_and_counted_never_refused"
    disclosure = profile.seal_consumption_disclosure
    assert disclosure.last_test_session == "2026-05-01"
    assert disclosure.deepest_execution_mark_session == "2026-06-26"
    assert disclosure.window_a_decision_sessions == 0
    assert disclosure.payload_seat == "holdout_seal"  # the w5 payload block


def test_a_tampered_body_refuses() -> None:
    """The content hash binds every field: one edited value (or one edited
    hash) is a ValidationError, so the committed record cannot be quietly
    rewritten."""
    body = json.loads(ARTIFACT.read_text("utf-8"))
    tampered = {**body, "geometry": {**body["geometry"], "min_train_sessions": 40}}
    with pytest.raises(ValidationError):
        RealLaneEraProfile.model_validate(tampered)
    with pytest.raises(ValidationError):
        RealLaneEraProfile.model_validate({**body, "content_sha256": "0" * 64})


def test_an_unratified_geometry_is_a_new_record_never_an_edit() -> None:
    """The geometry is pinned TWICE, and the first lock is the field-level
    Literal: any other value refuses before the model validators even run
    (the model-level `RATIFIED_ERA_GEOMETRY` tuple pin is the second lock,
    guarding a future widening of the Literals). A geometry change is a NEW
    ratified record, never an edit of this one."""
    body = json.loads(ARTIFACT.read_text("utf-8"))
    edited = {**body, "geometry": {**body["geometry"], "min_train_sessions": 29}}
    with pytest.raises(ValidationError) as exc:
        RealLaneEraProfile.model_validate(edited)
    assert "min_train_sessions" in str(exc.value)
    assert "should be 34" in str(exc.value)


def test_the_artifact_lives_under_data_with_the_other_ratified_records() -> None:
    """The surface choice: a committed protocol-ADJACENT artifact (the
    CalendarDecisionArtifact pattern), not a protocol schema block — the
    0.2.2 packet is not landed and the yaml stays 0.2.1."""
    assert ARTIFACT.is_file()
    assert ARTIFACT.relative_to(REPO_ROOT).parts[:2] == ("data", "real-lane")
