"""Trade-plan models for the trex execution lane.

A plan file is operator-authored TOML encoding a defined-risk book and its
hard caps. Loading validates the plan fail-closed: anything ambiguous or
over-budget raises rather than defaults. The engine treats these values as
the only degrees of freedom — there is deliberately no representation of a
roll, a widening, or a naked leg.
"""

from __future__ import annotations

import tomllib
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_CENT = Decimal("0.01")


def cents(value: Decimal) -> Decimal:
    """Quantize to cents, half-up (the way exchanges round displayed mids)."""
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _dec_before(value: object) -> object:
    """TOML floats arrive as float; money must never ride binary floats."""
    if isinstance(value, float):
        return repr(value)
    return value


class PutSpread(BaseModel):
    """One long put vertical: buy the higher strike, sell the lower.

    Max loss is the debit paid (capped by ``limit_cap`` per spread); max
    value is the width. The engine flattens on or before ``exit_deadline``,
    which must precede ``expiry`` — holding to expiry is never modeled.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    underlying: str = Field(min_length=1)
    entry_date: date
    expiry: date
    long_strike: Decimal
    short_strike: Decimal
    quantity: int = Field(gt=0)
    limit_cap: Decimal = Field(gt=0)
    exit_deadline: date
    take_profit_frac: Decimal | None = Field(default=None, ge=0, le=1)

    @field_validator("long_strike", "short_strike", "limit_cap", "take_profit_frac", mode="before")
    @classmethod
    def _money(cls, value: object) -> object:
        return _dec_before(value)

    @model_validator(mode="after")
    def _validate_structure(self) -> PutSpread:
        if self.long_strike <= self.short_strike:
            raise ValueError(
                f"{self.id}: long strike {self.long_strike} must exceed short strike "
                f"{self.short_strike} (put debit spread)"
            )
        if self.exit_deadline >= self.expiry:
            raise ValueError(
                f"{self.id}: exit deadline {self.exit_deadline} must precede expiry "
                f"{self.expiry} — never hold to expiry"
            )
        if self.entry_date >= self.exit_deadline:
            raise ValueError(
                f"{self.id}: entry date {self.entry_date} must precede exit deadline "
                f"{self.exit_deadline}"
            )
        return self

    @property
    def width(self) -> Decimal:
        return self.long_strike - self.short_strike

    @property
    def max_debit(self) -> Decimal:
        """Worst-case cash at risk if filled at the cap (per-contract x100)."""
        return self.limit_cap * self.quantity * 100

    @property
    def max_value(self) -> Decimal:
        """Value at full width — the lottery leg of the plan, never assumed."""
        return self.width * self.quantity * 100


class TradePlan(BaseModel):
    """The whole book: structures plus a whole-book debit ceiling."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    account_mode: str = Field(pattern="^(paper|live)$")
    structures: list[PutSpread] = Field(min_length=1)
    total_debit_cap: Decimal = Field(gt=0)
    entry_window_start: str  # "HH:MM" ET
    entry_window_end: str

    @field_validator("total_debit_cap", mode="before")
    @classmethod
    def _money(cls, value: object) -> object:
        return _dec_before(value)

    @model_validator(mode="after")
    def _validate_book(self) -> TradePlan:
        ids = [s.id for s in self.structures]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate structure ids: {ids}")
        committed = sum((s.max_debit for s in self.structures), Decimal(0))
        if committed > self.total_debit_cap:
            raise ValueError(
                f"sum of per-structure caps {committed} exceeds book cap "
                f"{self.total_debit_cap}"
            )
        if self.account_mode == "live" and committed > Decimal(5000):
            # Guard rail, not a policy: a live book above this size needs a
            # fresh operator-authored plan, not a TOML edit.
            raise ValueError(f"live book committed {committed} above the 5000 hard rail")
        return self

    @property
    def committed_at_caps(self) -> Decimal:
        return sum((s.max_debit for s in self.structures), Decimal(0))


def load_plan(path: Path | str) -> TradePlan:
    """Load and validate a plan file. Any ambiguity raises."""
    p = Path(path)
    with p.open("rb") as fh:
        raw = tomllib.load(fh)
    plan_id = raw.pop("id", p.stem)
    structures = raw.pop("structures", [])
    if not structures:
        raise ValueError(f"{p}: no [[structures]] entries — refusing an empty book")
    return TradePlan(
        id=str(plan_id),
        structures=structures,
        **raw,
    )
