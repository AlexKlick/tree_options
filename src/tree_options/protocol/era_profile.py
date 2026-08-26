"""The owner-ratified REAL-LANE ERA PROFILE (2026-08-26).

THE SURFACE (and why): a PROTOCOL-ADJACENT RATIFIED ARTIFACT, not a schema
block. Protocol models are ``extra="forbid"`` and the 0.2.2 packet is NOT
landed (``research_protocol.yaml`` stays 0.2.1 until the post-closeout
owner flip), so a schema block would either require the flip now or lie
about what the standing protocol declares. The repo's existing pattern for
exactly this situation is the typed, content-hash-bound, committed decision
artifact — ``seal.verified_inputs.CalendarDecisionArtifact`` +
``data/g4/calendar-decision.json`` — and this module follows it: a
``StrictModel`` (``extra="forbid"``) whose ``content_sha256`` binds the
typed body under a domain-separated canonical hash, plus the committed JSON
the tests pin. When 0.2.2 lands, the era-profile seat the verdict names
("geometry values + units declaration + embargo deviation record + holdout
boundary rule") is THIS artifact, referenced from the amendment record.

WHAT IT BINDS (theory-panel-verdict.md §2 P0-1/P0-2, §3, D3, D4, D6, D7 —
owner ruling 2026-08-26):

- GEOMETRY: ``OptionsSplitOverride(H=5, E=2, val=6, test=13, roll=13,
  min_train=34)`` on the full Friday calendar — verdict D4's ruling. Three
  disjoint window-A-shaped test quarters (2025-08-01..10-24,
  10-31..2026-01-23, 2026-01-30..2026-05-01), 39 of 65 tested Fridays.
- UNITS (verdict D3): every session count is a GRID FRIDAY on the
  Friday-only decision calendar — the re-denomination bundle is recorded
  so no mixed ordinal space can creep in (INV-06).
- THE E2 DEVIATION (verdict D4): the era embargo is 2 grid Fridays, below
  the protocol's ordinal embargo shape (E5, i.e. G = H+E = 10); the
  deviation rides the config hash and this record, and the priced
  alternative is named.
- THE HOLDOUT BOUNDARY RULE (verdict D7.2): sealed-window DECISION sessions
  are excluded (refused at registration), execution-tail consumption is
  disclosed, never banned.
- THE SEAL-CONSUMPTION DISCLOSURE (verdict D7.2's record requirement):
  wave-1 artifacts CONTAIN window-A executions/marks (through 2026-06-26
  under this geometry) but ZERO window-A decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import StringConstraints, field_validator, model_validator

from tree_options.data.digest import canonical_bytes, sha256_hex
from tree_options.schemas.common import IdStr, StrictModel

ERA_PROFILE_SCHEMA_VERSION = "m4-real-lane-era-profile/1"
ERA_PROFILE_DOMAIN = b"tree-options-m4-real-lane-era-profile-v1"
ERA_PROFILE_ID = "real-lane-era-1"
# The owner ruling this record transcribes (single sitting, 2026-08-26).
ERA_PROFILE_OWNER_DECISION = "m4-real-lane-theory-panel-ruling-2026-08-26"
ERA_PROFILE_DECIDED = datetime(2026, 8, 26, 0, 0, tzinfo=UTC)
ERA_PROFILE_CITATION = (
    "~/documents/tree_options-logs/theory-panel-verdict.md — §2 P0-1/P0-2, "
    "§3 checklist, D3, D4, D6, D7 (owner ruling 2026-08-26)"
)
ERA_PROFILE_SCOPE = "lane-2 evaluation folds (massive-derived-free/1)"

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

# The ratified geometry as the single in-code source (the record below is
# pinned to this tuple by a model validator; the accessor hands the trial
# driver the equivalent OptionsSplitOverride).
RATIFIED_ERA_GEOMETRY = (5, 2, 6, 13, 13, 34)


class EraFoldGeometry(StrictModel):
    """The owner-ratified fold geometry, in `OptionsSplitOverride` field
    order: (label_horizon_sessions, embargo_sessions, val_sessions,
    test_sessions, roll_sessions, min_train_sessions)."""

    label_horizon_sessions: Literal[5]
    embargo_sessions: Literal[2]
    val_sessions: Literal[6]
    test_sessions: Literal[13]
    roll_sessions: Literal[13]
    min_train_sessions: Literal[34]

    def as_tuple(self) -> tuple[int, int, int, int, int, int]:
        return (
            self.label_horizon_sessions,
            self.embargo_sessions,
            self.val_sessions,
            self.test_sessions,
            self.roll_sessions,
            self.min_train_sessions,
        )


class FridayGridUnits(StrictModel):
    """(verdict D3) The unit declaration: H = 5 GRID FRIDAYS on the
    Friday-only decision calendar, and the re-denomination bundle that
    follows from it. A mixed ordinal space is forbidden (INV-06)."""

    decision_grid: Literal["friday_only_grid_derived_from_the_nyse_fixture"]
    label_horizon_sessions: Literal[5]
    label_horizon_unit: Literal["grid_fridays"]
    momentum_windows_weeks: tuple[Literal[1, 5, 20], ...]  # mom_1/5/20
    arm_a_hold_weeks: Literal[4]
    end_buffer_sessions: Literal[6]
    end_buffer_unit: Literal["grid_fridays"]
    cohort_stride_sessions: Literal[4]
    mom_20_warmup_grid_indices: Literal[21]
    embargo_unit: Literal["grid_fridays"]


class EmbargoDeviation(StrictModel):
    """(verdict D4) The E2-vs-protocol-E5 deviation, recorded rather than
    smuggled: the era's embargo is 2 grid Fridays while the protocol's
    ordinal embargo shape is E5 (G = H + E = 10). The H=5 label purge is
    intact and load-bearing either way."""

    era_embargo_sessions: Literal[2]
    protocol_embargo_sessions: Literal[5]
    protocol_gap_sessions_at_e5: Literal[10]
    rides: Literal["the trial config hash (OptionsSplitOverride) + this record"]
    priced_alternative: IdStr


class HoldoutBoundaryRule(StrictModel):
    """(verdict D7.2, the critic's boundary rule) Decision sessions stay OUT
    of the sealed window; execution-tail consumption is disclosed."""

    sealed_window_id: Literal["final-holdout-window-a"]
    decision_sessions: Literal["excluded_refused_before_registration"]
    execution_tail: Literal["tagged_and_counted_never_refused"]


class SealConsumptionDisclosure(StrictModel):
    """(verdict D7.2's record requirement) What wave-1 artifacts under this
    geometry provably contain: window-A executions and marks (the execution
    tail is END_BUFFER=6 grid Fridays deep past the last test session,
    clamped at the world's last session) and ZERO window-A decisions."""

    last_test_session: Literal["2026-05-01"]
    deepest_execution_mark_session: Literal["2026-06-26"]
    window_a_decision_sessions: Literal[0]
    payload_seat: Literal["holdout_seal"]


class RealLaneEraProfile(StrictModel):
    """The ratified record. `content_sha256` binds the typed body (the
    CalendarDecisionArtifact contract), so the committed JSON and this model
    cannot drift."""

    schema_version: Literal["m4-real-lane-era-profile/1"]
    profile_id: Literal["real-lane-era-1"]
    owner_decision_id: IdStr
    decided: datetime
    ruling_citation: IdStr
    scope: IdStr
    geometry: EraFoldGeometry
    units: FridayGridUnits
    embargo_deviation: EmbargoDeviation
    holdout_boundary_rule: HoldoutBoundaryRule
    seal_consumption_disclosure: SealConsumptionDisclosure
    content_sha256: Sha256

    @field_validator("decided")
    @classmethod
    def _aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("era profile decision timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _geometry_is_the_ratified_one(self) -> RealLaneEraProfile:
        if self.geometry.as_tuple() != RATIFIED_ERA_GEOMETRY:
            raise ValueError(
                f"geometry {self.geometry.as_tuple()} is not the owner-ratified "
                f"real-lane geometry {RATIFIED_ERA_GEOMETRY} (theory-panel D4, "
                "2026-08-26); a geometry change is a new ratified record, never "
                "an edit of this one"
            )
        return self

    @model_validator(mode="after")
    def _content_hash_binds_body(self) -> RealLaneEraProfile:
        core = self.model_copy(update={"content_sha256": ""})
        expected = sha256_hex(ERA_PROFILE_DOMAIN + canonical_bytes(core))
        if self.content_sha256 != expected:
            raise ValueError("era profile content_sha256 does not bind the typed body")
        return self


def build_real_lane_era_profile() -> RealLaneEraProfile:
    """The one constructor of the ratified record: values fixed by the
    owner ruling, nothing caller-supplied. Deterministic (no clock read —
    `decided` is the ruling date), so the committed artifact and a fresh
    build are byte-identical."""
    geometry = EraFoldGeometry(
        label_horizon_sessions=5,
        embargo_sessions=2,
        val_sessions=6,
        test_sessions=13,
        roll_sessions=13,
        min_train_sessions=34,
    )
    units = FridayGridUnits(
        decision_grid="friday_only_grid_derived_from_the_nyse_fixture",
        label_horizon_sessions=5,
        label_horizon_unit="grid_fridays",
        momentum_windows_weeks=(1, 5, 20),
        arm_a_hold_weeks=4,
        end_buffer_sessions=6,
        end_buffer_unit="grid_fridays",
        cohort_stride_sessions=4,
        mom_20_warmup_grid_indices=21,
        embargo_unit="grid_fridays",
    )
    embargo_deviation = EmbargoDeviation(
        era_embargo_sessions=2,
        protocol_embargo_sessions=5,
        protocol_gap_sessions_at_e5=10,
        rides="the trial config hash (OptionsSplitOverride) + this record",
        priced_alternative=(
            "E5 costs the third fold: mt24/val6 yields 3 folds ending "
            "2026-04-17 (verdict D4); mt29/val6 ends 2026-03-20 and mt27/val6 "
            "gives full tail isolation at fold-0 fit 6 (thin)"
        ),
    )
    holdout_boundary_rule = HoldoutBoundaryRule(
        sealed_window_id="final-holdout-window-a",
        decision_sessions="excluded_refused_before_registration",
        execution_tail="tagged_and_counted_never_refused",
    )
    seal_consumption_disclosure = SealConsumptionDisclosure(
        last_test_session="2026-05-01",
        deepest_execution_mark_session="2026-06-26",
        window_a_decision_sessions=0,
        payload_seat="holdout_seal",
    )
    core = RealLaneEraProfile.model_construct(
        schema_version=ERA_PROFILE_SCHEMA_VERSION,
        profile_id=ERA_PROFILE_ID,
        owner_decision_id=ERA_PROFILE_OWNER_DECISION,
        decided=ERA_PROFILE_DECIDED,
        ruling_citation=ERA_PROFILE_CITATION,
        scope=ERA_PROFILE_SCOPE,
        geometry=geometry,
        units=units,
        embargo_deviation=embargo_deviation,
        holdout_boundary_rule=holdout_boundary_rule,
        seal_consumption_disclosure=seal_consumption_disclosure,
        content_sha256="",
    )
    digest = sha256_hex(ERA_PROFILE_DOMAIN + canonical_bytes(core))
    return RealLaneEraProfile.model_validate(
        {
            "schema_version": ERA_PROFILE_SCHEMA_VERSION,
            "profile_id": ERA_PROFILE_ID,
            "owner_decision_id": ERA_PROFILE_OWNER_DECISION,
            "decided": ERA_PROFILE_DECIDED,
            "ruling_citation": ERA_PROFILE_CITATION,
            "scope": ERA_PROFILE_SCOPE,
            "geometry": geometry,
            "units": units,
            "embargo_deviation": embargo_deviation,
            "holdout_boundary_rule": holdout_boundary_rule,
            "seal_consumption_disclosure": seal_consumption_disclosure,
            "content_sha256": digest,
        }
    )


def real_lane_split_override():
    """The ratified geometry as the trial driver's `OptionsSplitOverride`
    (imported lazily so this protocol-adjacent module never creates an
    import-time dependency on the trials package)."""
    from tree_options.trials.options_run import OptionsSplitOverride

    return OptionsSplitOverride(
        label_horizon_sessions=5,
        embargo_sessions=2,
        val_sessions=6,
        test_sessions=13,
        roll_sessions=13,
        min_train_sessions=34,
    )


__all__ = [
    "ERA_PROFILE_CITATION",
    "ERA_PROFILE_DECIDED",
    "ERA_PROFILE_DOMAIN",
    "ERA_PROFILE_ID",
    "ERA_PROFILE_OWNER_DECISION",
    "ERA_PROFILE_SCHEMA_VERSION",
    "ERA_PROFILE_SCOPE",
    "RATIFIED_ERA_GEOMETRY",
    "EmbargoDeviation",
    "EraFoldGeometry",
    "FridayGridUnits",
    "HoldoutBoundaryRule",
    "RealLaneEraProfile",
    "SealConsumptionDisclosure",
    "build_real_lane_era_profile",
    "real_lane_split_override",
]
