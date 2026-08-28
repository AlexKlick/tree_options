"""Typed model of the frozen research protocol.

Every model is `extra="forbid"`: an unknown key in the YAML is a load error,
not a silent pass-through. The set of invariants INV-01..INV-14 is exact —
a missing or duplicated invariant is a broken protocol and fails to load.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AmendmentRecord(_Strict):
    """The provenance of one protocol version bump: what changed, when, and
    under which owner decision. Empty for 0.1.0-shaped protocols (the
    frozen M0 baseline); 0.2.0 carries the G3 amendment packet record."""

    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    date: str
    decision: str = Field(min_length=1)
    changes: str = Field(min_length=1)


class ProtocolMeta(_Strict):
    protocol_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    created: str
    owner: str
    change_policy: str = Field(min_length=1)
    amendments: tuple[AmendmentRecord, ...] = ()


class Invariant(_Strict):
    id: str = Field(pattern=r"^INV-\d{2}$")
    statement: str = Field(min_length=1)
    enforced_by: tuple[str, ...] = Field(min_length=1)


class TimestampSemantics(_Strict):
    clock: Literal["utc"]
    session_timezone: str
    session_open: str
    session_close: str
    decision_instant: Literal["session_close"]
    availability_rule: str
    observation_rule: str
    execution_rule: str


class CalendarConfig(_Strict):
    implementation: Literal["static_json"]
    fixture: str
    checksum_file: str
    generator: str
    generator_dependency: str


class WindowRange(_Strict):
    min: int = Field(gt=0)
    max: int = Field(gt=0)
    default: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> WindowRange:
        if not (self.min <= self.default <= self.max):
            raise ValueError("window requires min <= default <= max")
        return self


class FoldConfig(_Strict):
    shape: Literal["anchored_expanding"]
    label_horizon_sessions: int = Field(ge=1)
    embargo_sessions: int = Field(ge=1)
    validation_window_sessions: WindowRange
    test_window_sessions: WindowRange
    roll_forward_sessions: int = Field(gt=0)
    min_train_sessions: int = Field(gt=0)
    purge_gap_rule: str = Field(min_length=1)

    @model_validator(mode="after")
    def _roll_fits(self) -> FoldConfig:
        if self.roll_forward_sessions > self.test_window_sessions.min:
            raise ValueError("roll_forward_sessions must be <= min test window")
        return self


class InnerLoopConfig(_Strict):
    # strict: this number is a pre-registration commitment — lax coercion
    # let YAML `true` normalize to 1, "32" to 32, and 32.0 to 32 (round 5,
    # NEW-7). No bool/str/float may become the cap.
    max_registered_configs: int = Field(gt=0, strict=True)


class PrimaryFillPolicy(_Strict):
    long_entry: Literal["ask"]
    long_exit: Literal["bid"]
    short_entry: Literal["bid"]
    short_exit: Literal["ask"]


class MoneyConfig(_Strict):
    price_tick: Decimal = Field(gt=0)
    fee_tick: Decimal = Field(gt=0)


class VwapFillPolicy(_Strict):
    """G3 (0.2.0): fill semantics for the vwap quote kind. Every field is a
    Literal pinning one ratified decision — a semantic change here is a
    protocol change, not a config tweak."""

    executable: Literal["session_vwap_conservative_tick"]
    participation_cap: Literal["bar_volume"]
    participation_scope: Literal["cumulative_per_contract_session"]
    zero_volume_session: Literal["unfillable"]
    publication_gate: Literal["received_timestamp"]
    session_stamp: Literal["close_of_bar_session"]
    bar_recency: Literal["previous_session"]
    midpoint_fraction: Literal["not_applicable"]


class FillConfig(_Strict):
    primary: PrimaryFillPolicy
    fraction_to_midpoint_sensitivity: tuple[str, ...] = Field(min_length=1)
    max_quote_age_seconds: int = Field(gt=0)
    reject_locked_quotes: bool
    fill_size_fraction: Decimal
    same_session_execution: Literal["reject"]
    partial_fills: Literal["allowed_by_quote_size"]
    money: MoneyConfig
    vwap: VwapFillPolicy

    @field_validator("fraction_to_midpoint_sensitivity")
    @classmethod
    def _fractions_in_range(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for raw in v:
            f = Decimal(raw)
            if not (Decimal("0") <= f <= Decimal("1")):
                raise ValueError(f"fraction_to_midpoint {raw} must be in [0, 1]")
        return v

    @field_validator("fill_size_fraction")
    @classmethod
    def _size_fraction(cls, v: Decimal) -> Decimal:
        if not (Decimal("0") < v <= Decimal("1")):
            raise ValueError(f"fill_size_fraction {v} must be in (0, 1]")
        return v


class LiquidityFlowConfig(_Strict):
    """G3 Ask D: the volume-flow liquidity regime for tiers with no
    two-sided market (no bid/ask, no open interest). Open interest and the
    spread term are DROPPED WITH DISCLOSURE — the filter records them
    NOT_APPLICABLE naming the absence, never a fabricated threshold pass.

    `flow_min_session_volume` is PENDING-era BY DESIGN: None until the
    coverage-era §4 census lands. Building a volume-flow filter from a
    protocol whose threshold is still None must fail closed — an unset
    threshold may never default to 0 (that would accept everything)."""

    regime: Literal["volume_flow"]
    # strict: YAML `true` must NOT coerce to 1 and activate the pending
    # regime (review P0); null stays the PENDING-era marker.
    flow_min_session_volume: int | None = Field(default=None, ge=1, strict=True)
    spread_term: Literal["dropped_no_two_sided_market"]
    open_interest_term: Literal["dropped_no_open_interest"]
    # 0.2.2 PRE-DRAFT MACHINERY (theory-panel §2 P0-1(a), owner ruled
    # 2026-08-26) — the yaml itself stays 0.2.1 this wave. The declared
    # disposition of the underlying-liquidity term: "evaluated" is the
    # standing default and the ONLY disposition 0.2.1 carries (the rule runs
    # and the declared Decimal("0") sentinel fails it honestly);
    # "dropped_no_equity_aggregates" is the ruled fallback for a lane with
    # no equity-aggregates dollar-volume source — the rule then answers
    # NOT_APPLICABLE with disclosure instead of failing on the sentinel.
    underlying_liquidity_term: Literal["evaluated", "dropped_no_equity_aggregates"] = "evaluated"
    # G3 Ask B: model-implied |delta| derived from the bar VWAP under the
    # shared pricer is an ACCEPTED provenance class for the delta rule.
    abs_delta_provenance_accepted: tuple[Literal["vendor", "model-derived-from-vwap"], ...]

    @field_validator("abs_delta_provenance_accepted")
    @classmethod
    def _provenance_nonempty(cls, v):
        if not v:
            raise ValueError("abs_delta_provenance_accepted must name at least one class")
        return v


class OptionCandidateDefaults(_Strict):
    dte_min: int = Field(gt=0)
    dte_max: int = Field(gt=0)
    abs_delta_min: Decimal = Field(gt=0)
    abs_delta_max: Decimal = Field(gt=0)
    standard_deliverable_only: bool
    min_open_interest: int = Field(gt=0)
    min_same_day_volume: int = Field(gt=0)
    volume_only_if_already_available: bool
    max_spread_fraction_of_midpoint: Decimal = Field(gt=0)
    min_underlying_20d_median_dollar_volume: Decimal = Field(gt=0)
    exclude_earnings_spanning_hold: bool
    # G3: None = the two-sided regime stands alone (M0 default); present =
    # the volume-flow regime is ratified for tiers without two-sided markets.
    liquidity_volume_flow: LiquidityFlowConfig | None = None

    @model_validator(mode="after")
    def _bands_ordered(self) -> OptionCandidateDefaults:
        if self.dte_min > self.dte_max:
            raise ValueError("dte_min must be <= dte_max")
        if self.abs_delta_min > self.abs_delta_max:
            raise ValueError("abs_delta_min must be <= abs_delta_max")
        return self


class UniverseConfig(_Strict):
    include_delisted: Literal[True]
    identity_keys: tuple[Literal["security_id", "figi", "cik"], ...]
    ticker_is_not_identity: Literal[True]


class ShortOptionsConfig(_Strict):
    policy: Literal["prohibited"]
    note: str


class TrialsConfig(_Strict):
    register_before_outcome: Literal[True]
    duplicate_trial_id: Literal["reject"]
    storage: Literal["sqlite"]


class ResearchProtocol(_Strict):
    meta: ProtocolMeta
    invariants: tuple[Invariant, ...]
    timestamp_semantics: TimestampSemantics
    calendar: CalendarConfig
    folds: FoldConfig
    inner_loop: InnerLoopConfig
    fills: FillConfig
    option_candidate_defaults: OptionCandidateDefaults
    universe: UniverseConfig
    short_options: ShortOptionsConfig
    trials: TrialsConfig

    EXPECTED_INVARIANT_IDS: ClassVar[tuple[str, ...]] = tuple(f"INV-{i:02d}" for i in range(1, 15))

    @model_validator(mode="after")
    def _version_carries_its_amendments(self) -> ResearchProtocol:
        """A declared version must CARRY the content that version means
        (review P1: a 0.2.0-shaped yaml with no amendment record and no
        volume-flow block validated silently). 0.1.0 is the frozen M0
        baseline: it carries no amendments and no volume-flow regime. Any
        later version must record its own amendment and carry the G3
        content (the vwap fill policy + the ratified liquidity regime)."""
        version = self.meta.protocol_version
        major_minor = tuple(int(p) for p in version.split(".")[:2])
        if version == "0.1.0":
            if self.meta.amendments:
                raise ValueError("0.1.0 predates amendments; records present")
            if self.option_candidate_defaults.liquidity_volume_flow is not None:
                raise ValueError("0.1.0 predates the volume-flow regime; block present")
            return self
        if not any(a.version == version for a in self.meta.amendments):
            raise ValueError(f"protocol {version} carries no amendment record for itself")
        if major_minor >= (0, 2):
            lf = self.option_candidate_defaults.liquidity_volume_flow
            if lf is None:
                raise ValueError(
                    f"protocol {version} declares >=0.2 without the ratified"
                    " liquidity_volume_flow block"
                )
            if "model-derived-from-vwap" not in lf.abs_delta_provenance_accepted:
                raise ValueError(
                    f"protocol {version} declares >=0.2 without the"
                    " model-derived-from-vwap provenance class the G3"
                    " amendment ratified"
                )
        return self

    @field_validator("invariants")
    @classmethod
    def _exact_invariant_set(cls, v: tuple[Invariant, ...]) -> tuple[Invariant, ...]:
        ids = [inv.id for inv in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate invariant ids")
        if tuple(ids) != cls.EXPECTED_INVARIANT_IDS:
            raise ValueError(f"invariants must be exactly {cls.EXPECTED_INVARIANT_IDS}, got {ids}")
        return v
