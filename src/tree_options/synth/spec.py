"""WorldSpec: the frozen recipe for a synthetic world (M2 packet §3.B).

A world is a pure function of (WorldSpec, calendar). Rates are per listed
security-year expectations; the generator scales them per session. All
parameters live HERE — never in the frozen research protocol.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from tree_options.schemas.common import IdStr, StrictModel

DEFAULT_SECTORS: tuple[str, ...] = (
    "TECH",
    "FIN",
    "HLTH",
    "ENRG",
    "INDU",
    "DISC",
    "STPL",
    "MATR",
    "COMM",
    "UTIL",
    "REIT",
)


class ActionRates(StrictModel):
    """Per listed-security-year event expectations (engineering defaults,
    not claims about any real market)."""

    split: float = Field(default=0.08, ge=0)
    reverse_split: float = Field(default=0.01, ge=0)
    cash_dividend: float = Field(default=0.30, ge=0)
    stock_dividend: float = Field(default=0.005, ge=0)
    rename: float = Field(default=0.02, ge=0)
    merger: float = Field(default=0.01, ge=0)
    bankruptcy: float = Field(default=0.004, ge=0)
    voluntary_delisting: float = Field(default=0.006, ge=0)
    coverage_lapse: float = Field(default=0.002, ge=0)
    ipo_per_year: float = Field(default=8.0, ge=0)


class AlphaSpec(StrictModel):
    """The single v1 planted-effect family: next-session returns load on the
    prior session's cross-sectionally centered total return (momentum when
    coefficient > 0), knowable from published bars only."""

    family: Literal["linear_momentum"] = "linear_momentum"
    coefficient: float = Field(gt=0)


class WorldSpec(StrictModel):
    spec_version: Literal["v1"] = "v1"
    world_id: IdStr
    seed: int
    kind: Literal["null", "alpha"]
    n_securities: int = Field(default=500, ge=10, le=5000)
    n_sessions: int | None = Field(default=None, ge=40)
    sectors: tuple[str, ...] | None = None  # None -> DEFAULT_SECTORS
    rates: ActionRates = Field(default_factory=ActionRates)
    publication_hour_utc: int = Field(default=23, ge=0, le=23)
    initial_listing_fraction: float = Field(default=0.6, gt=0, le=1)
    alpha: AlphaSpec | None = None

    @model_validator(mode="after")
    def _kind_matches_alpha(self) -> WorldSpec:
        if self.kind == "alpha" and self.alpha is None:
            raise ValueError("kind='alpha' requires an AlphaSpec")
        if self.kind == "null" and self.alpha is not None:
            raise ValueError("kind='null' must not carry an AlphaSpec")
        if self.sectors is not None and len(set(self.sectors)) != len(self.sectors):
            raise ValueError("sector names must be unique")
        return self
