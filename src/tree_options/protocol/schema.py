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


class ProtocolMeta(_Strict):
    protocol_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    created: str
    owner: str
    change_policy: str = Field(min_length=1)


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
    max_registered_configs: int = Field(gt=0)


class PrimaryFillPolicy(_Strict):
    long_entry: Literal["ask"]
    long_exit: Literal["bid"]
    short_entry: Literal["bid"]
    short_exit: Literal["ask"]


class MoneyConfig(_Strict):
    price_tick: Decimal = Field(gt=0)
    fee_tick: Decimal = Field(gt=0)


class FillConfig(_Strict):
    primary: PrimaryFillPolicy
    price_improvement_fractions: tuple[str, ...] = Field(min_length=1)
    max_quote_age_seconds: int = Field(gt=0)
    same_session_execution: Literal["reject"]
    partial_fills: Literal["allowed_by_quote_size"]
    money: MoneyConfig

    @field_validator("price_improvement_fractions")
    @classmethod
    def _fractions_in_range(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for raw in v:
            f = Decimal(raw)
            if not (Decimal("0") < f <= Decimal("0.5")):
                raise ValueError(f"improvement fraction {raw} must be in (0, 0.5]")
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

    EXPECTED_INVARIANT_IDS: ClassVar[tuple[str, ...]] = tuple(
        f"INV-{i:02d}" for i in range(1, 15)
    )

    @field_validator("invariants")
    @classmethod
    def _exact_invariant_set(cls, v: tuple[Invariant, ...]) -> tuple[Invariant, ...]:
        ids = [inv.id for inv in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate invariant ids")
        if tuple(ids) != cls.EXPECTED_INVARIANT_IDS:
            raise ValueError(
                f"invariants must be exactly {cls.EXPECTED_INVARIANT_IDS}, got {ids}"
            )
        return v
