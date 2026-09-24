"""Trade-plan models for the trex execution lane.

A plan file is operator-authored TOML encoding a defined-risk book and its
hard caps. Loading validates the plan fail-closed: anything ambiguous or
over-budget raises rather than defaults. The engine treats these values as
the only degrees of freedom — there is deliberately no representation of a
roll, a widening, or a naked leg.

Two structure shapes share a plan's ``[[structures]]`` array:

- ``PutSpread``: the legacy long put debit vertical (the live book). A table
  with neither ``kind`` nor ``legs`` parses exactly as it always has.
- ``LegStructure``: a defined-risk multi-leg package (desk lane E1), picked
  by the presence of ``kind`` or ``legs``. Kinds: long_single,
  debit_vertical, credit_vertical, iron_condor, calendar, diagonal. Every
  right needs at least as many BUY legs as SELL legs, and the kind rules
  pin the strikes so each SELL is covered by a long that caps its loss.

Prices of a structure are quoted in its DEBIT ORIENTATION
(``LegStructure.package_legs``): the direction whose package value is
never negative. A debit kind opens by buying that package (``limit`` is
the cap paid); a credit kind opens by selling it (``limit`` is the floor
received). The book's committed risk sums ``max_loss()`` across kinds.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_CENT = Decimal("0.01")
_ZERO = Decimal(0)

Right = Literal["C", "P"]
Action = Literal["BUY", "SELL"]  # a leg's direction in the OPENED position; also an order side
Kind = Literal[
    "long_single", "debit_vertical", "credit_vertical", "iron_condor", "calendar", "diagonal"
]
DEBIT_KINDS: frozenset[str] = frozenset({"long_single", "debit_vertical", "calendar", "diagonal"})
CREDIT_KINDS: frozenset[str] = frozenset({"credit_vertical", "iron_condor"})
# IBKR's what-if initial margin for opening a package may exceed the
# computed max loss by at most this factor; above it IBKR is treating the
# package as undefined risk (a leg it doesn't see as covering) - refused.
WHATIF_MARGIN_FACTOR = Decimal("1.1")


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

    def package_legs(self) -> tuple[Leg, Leg]:
        """The two legs in (debit) order: BUY the long put, SELL the short.
        Leg index 0 is the long leg, 1 the short, as the broker BAG has
        always carried them."""
        return (
            Leg(right="P", action="BUY", strike=self.long_strike, expiry=self.expiry),
            Leg(right="P", action="SELL", strike=self.short_strike, expiry=self.expiry),
        )

    def as_spec(self) -> LegStructure:
        """This spread as a debit_vertical LegStructure with the same exits:
        touch on, the take-profit (if any) as a width fraction, no stop.
        Validates as a LegStructure, so it raises for a spread whose cap is
        not below its width (legal for PutSpread, a sure loss as a spec)."""
        take_profit = (
            TakeProfit(basis="width_frac", value=self.take_profit_frac)
            if self.take_profit_frac is not None
            else None
        )
        return LegStructure(
            id=self.id,
            underlying=self.underlying,
            kind="debit_vertical",
            legs=self.package_legs(),
            quantity=self.quantity,
            entry_date=self.entry_date,
            exit_deadline=self.exit_deadline,
            limit=self.limit_cap,
            exits=ExitRules(take_profit=take_profit, touch=True, breach=False),
        )


class Leg(BaseModel):
    """One option leg: ``action`` is its direction in the OPENED position
    (a credit vertical's short strike is SELL). Always ratio 1: a ratio
    spread is not representable. Legs carry no underlying of their own; a
    structure trades one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    right: Right
    action: Action
    strike: Decimal = Field(gt=0)
    expiry: date
    ratio: Literal[1] = 1

    @field_validator("strike", mode="before")
    @classmethod
    def _money(cls, value: object) -> object:
        return _dec_before(value)

    @property
    def sign(self) -> int:
        """+1 held long, -1 held short."""
        return 1 if self.action == "BUY" else -1

    def flipped(self) -> Leg:
        return self.model_copy(update={"action": "SELL" if self.action == "BUY" else "BUY"})


class TakeProfit(BaseModel):
    """Take-profit threshold, by basis:

    - width_frac: package mid >= value x width (debit verticals; the legacy
      ``take_profit_frac``), 0 <= value <= 1;
    - gain_frac: package mid >= entry x (1 + value) (debit kinds), value > 0;
    - credit_frac: value of the entry credit captured, i.e. package mid <=
      entry x (1 - value) (credit kinds), 0 < value < 1.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    basis: Literal["width_frac", "gain_frac", "credit_frac"]
    value: Decimal

    @field_validator("value", mode="before")
    @classmethod
    def _money(cls, value: object) -> object:
        return _dec_before(value)

    @model_validator(mode="after")
    def _range(self) -> TakeProfit:
        v = self.value
        ok = {
            "width_frac": _ZERO <= v <= 1,
            "gain_frac": v > 0,
            "credit_frac": _ZERO < v < 1,
        }[self.basis]
        if not ok:
            raise ValueError(f"take_profit {self.basis} value {v} out of range")
        return self


class StopLoss(BaseModel):
    """Stop-loss threshold, by basis (confirmed over ExitRules.stop_confirm_ticks):

    - debit_frac: package mid <= entry x (1 - value), i.e. that fraction of
      the debit lost (debit kinds), 0 < value < 1;
    - credit_mult: package mid (the cost to close) >= value x the entry
      credit (credit kinds), value > 1.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    basis: Literal["debit_frac", "credit_mult"]
    value: Decimal

    @field_validator("value", mode="before")
    @classmethod
    def _money(cls, value: object) -> object:
        return _dec_before(value)

    @model_validator(mode="after")
    def _range(self) -> StopLoss:
        v = self.value
        ok = _ZERO < v < 1 if self.basis == "debit_frac" else v > 1
        if not ok:
            raise ValueError(f"stop_loss {self.basis} value {v} out of range")
        return self


class ExitRules(BaseModel):
    """Exit rules beyond the always-on time stop (exit deadline) and expiry
    safety. ``touch``: exit when spot reaches the long strike (debit
    verticals and long singles). ``breach``: exit when spot crosses a short
    strike (credit verticals and condors). Both must be stated explicitly."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    take_profit: TakeProfit | None = None
    stop_loss: StopLoss | None = None
    stop_confirm_ticks: int = Field(default=3, ge=1)
    touch: bool
    breach: bool


# which exit rules each kind can carry (anything else is refused, not ignored)
_TOUCH_KINDS = frozenset({"long_single", "debit_vertical"})
_BREACH_KINDS = CREDIT_KINDS
_TAKE_PROFIT_KINDS: dict[str, frozenset[str]] = {
    "width_frac": frozenset({"debit_vertical"}),
    "gain_frac": DEBIT_KINDS,
    "credit_frac": CREDIT_KINDS,
}
_STOP_KINDS: dict[str, frozenset[str]] = {"debit_frac": DEBIT_KINDS, "credit_mult": CREDIT_KINDS}


class LegStructure(BaseModel):
    """A defined-risk multi-leg package (see the module docstring).

    ``limit`` is per package in debit-orientation price: the cap paid for a
    debit kind, the floor received for a credit kind. Max loss per package:

    - long_single, debit_vertical: the cap;
    - credit_vertical: width - floor; iron_condor: wider wing - floor;
    - calendar, diagonal: the cap. ASSUMPTION: American-style options,
      closed before the front expiry (the exit deadline must precede it).
      The long leg then expires no earlier than the short one at the same
      strike (calendar) or a strike at least as favorable (a protective
      diagonal: long call strike <= short call strike, long put strike >=
      short put strike), so it is worth at least as much and the package
      never goes below zero. A non-protective diagonal can, and is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    underlying: str = Field(min_length=1)
    kind: Kind
    legs: tuple[Leg, ...] = Field(min_length=1)
    quantity: int = Field(gt=0)
    entry_date: date
    exit_deadline: date
    limit: Decimal = Field(gt=0)
    exits: ExitRules
    deal_id: str | None = None
    # the package mid (debit orientation) the deal was priced at; the engine
    # aborts the entry (stale_deal) when the live mid has moved too far from
    # it. Required with a deal_id, optional otherwise.
    ref_mid: Decimal | None = Field(default=None, gt=0)

    @field_validator("limit", "ref_mid", mode="before")
    @classmethod
    def _money(cls, value: object) -> object:
        return _dec_before(value)

    @model_validator(mode="after")
    def _validate_structure(self) -> LegStructure:
        where = f"{self.id}: {self.kind}"
        if self.deal_id is not None and self.ref_mid is None:
            raise ValueError(f"{where}: a deal ({self.deal_id}) needs its ref_mid")
        seen = [(g.right, g.strike, g.expiry) for g in self.legs]
        if len(seen) != len(set(seen)):
            raise ValueError(f"{where}: duplicate legs (same right, strike and expiry)")
        for right in ("C", "P"):
            buys = sum(1 for g in self.legs if g.right == right and g.action == "BUY")
            sells = sum(1 for g in self.legs if g.right == right and g.action == "SELL")
            if sells > buys:
                raise ValueError(
                    f"{where}: uncovered SELL: {sells} short {right} leg(s) against {buys} long"
                )
        if self.exit_deadline >= self.first_expiry:
            raise ValueError(
                f"{where}: exit deadline {self.exit_deadline} must precede the first expiry "
                f"{self.first_expiry}; never hold to expiry"
            )
        if self.entry_date >= self.exit_deadline:
            raise ValueError(
                f"{where}: entry date {self.entry_date} must precede exit deadline "
                f"{self.exit_deadline}"
            )
        _SHAPES[self.kind](self, where)
        self._validate_exits(where)
        return self

    def _validate_exits(self, where: str) -> None:
        ex = self.exits
        if ex.touch and self.kind not in _TOUCH_KINDS:
            raise ValueError(f"{where}: touch exit is for long singles and debit verticals")
        if ex.breach and self.kind not in _BREACH_KINDS:
            raise ValueError(f"{where}: breach exit is for credit verticals and condors")
        if ex.take_profit is not None and self.kind not in _TAKE_PROFIT_KINDS[ex.take_profit.basis]:
            raise ValueError(f"{where}: take_profit basis {ex.take_profit.basis} does not apply")
        if ex.stop_loss is not None:
            if self.kind not in _STOP_KINDS[ex.stop_loss.basis]:
                raise ValueError(f"{where}: stop_loss basis {ex.stop_loss.basis} does not apply")
            width = self.width
            if ex.stop_loss.basis == "credit_mult" and width is not None:
                # the package can't cost more than its width to close: a stop
                # at or above it, even at the floor credit, would never fire
                if ex.stop_loss.value * self.limit >= width:
                    raise ValueError(
                        f"{where}: stop_loss credit_mult {ex.stop_loss.value} x floor "
                        f"{self.limit} reaches the width {width}; it would never fire"
                    )

    # -- derived -------------------------------------------------------------

    @property
    def is_credit(self) -> bool:
        return self.kind in CREDIT_KINDS

    @property
    def direction(self) -> int:
        """+1 debit (opened by paying), -1 credit (opened by receiving)."""
        return -1 if self.is_credit else 1

    @property
    def open_side(self) -> Action:
        """The order side that opens the debit-orientation package."""
        return "SELL" if self.is_credit else "BUY"

    @property
    def close_side(self) -> Action:
        return "BUY" if self.is_credit else "SELL"

    def package_legs(self) -> tuple[Leg, ...]:
        """The legs in DEBIT ORIENTATION, in authored order (a leg's index is
        stable): as authored for debit kinds, every action flipped for credit
        kinds. The package's value is never negative, so its prices are."""
        return tuple(g.flipped() for g in self.legs) if self.is_credit else self.legs

    @property
    def first_expiry(self) -> date:
        return min(g.expiry for g in self.legs)

    @property
    def width(self) -> Decimal | None:
        """The package's maximum value (debit orientation) where strikes fix
        it: the strike distance of a vertical, the wider wing of a condor.
        None for long singles, calendars and diagonals."""
        if self.kind in ("debit_vertical", "credit_vertical"):
            a, b = self.legs
            return abs(a.strike - b.strike)
        if self.kind == "iron_condor":
            put_wing, call_wing = _condor_wings(self.legs)
            return max(put_wing, call_wing)
        return None

    def max_loss_per_package(self) -> Decimal:
        """Worst-case loss of one package (price units, before x100)."""
        if self.is_credit:
            width = self.width
            assert width is not None  # credit kinds are verticals and condors
            return width - self.limit
        return self.limit

    def max_loss(self) -> Decimal:
        """Worst-case dollars at risk: per package x100 x quantity."""
        return self.max_loss_per_package() * 100 * self.quantity


def _one_of_each(legs: tuple[Leg, ...]) -> tuple[Leg, Leg] | None:
    """(BUY leg, SELL leg) of a two-leg structure, or None."""
    if len(legs) != 2:
        return None
    buys = [g for g in legs if g.action == "BUY"]
    sells = [g for g in legs if g.action == "SELL"]
    if len(buys) != 1 or len(sells) != 1:
        return None
    return buys[0], sells[0]


def _condor_wings(legs: tuple[Leg, ...]) -> tuple[Decimal, Decimal]:
    puts = sorted((g for g in legs if g.right == "P"), key=lambda g: g.strike)
    calls = sorted((g for g in legs if g.right == "C"), key=lambda g: g.strike)
    return puts[1].strike - puts[0].strike, calls[1].strike - calls[0].strike


def _shape_long_single(s: LegStructure, where: str) -> None:
    if len(s.legs) != 1 or s.legs[0].action != "BUY":
        raise ValueError(f"{where}: exactly one BUY leg")


def _shape_vertical(s: LegStructure, where: str) -> None:
    pair = _one_of_each(s.legs)
    if pair is None:
        raise ValueError(f"{where}: one BUY and one SELL leg")
    buy, sell = pair
    if buy.right != sell.right or buy.expiry != sell.expiry:
        raise ValueError(f"{where}: both legs share one right and one expiry")
    # the debit vertical buys the strike nearer the view (put: higher, call:
    # lower); the credit vertical sells it
    buy_higher = buy.strike > sell.strike
    wants_buy_higher = (buy.right == "P") == (s.kind == "debit_vertical")
    if buy_higher != wants_buy_higher:
        raise ValueError(
            f"{where}: strikes inverted for a {buy.right} {s.kind} "
            f"(BUY {buy.strike}, SELL {sell.strike})"
        )
    width = abs(buy.strike - sell.strike)
    if not s.limit < width:
        what = "cap" if s.kind == "debit_vertical" else "credit floor"
        raise ValueError(f"{where}: limit ({what}) {s.limit} must be below the width {width}")


def _shape_iron_condor(s: LegStructure, where: str) -> None:
    puts = sorted((g for g in s.legs if g.right == "P"), key=lambda g: g.strike)
    calls = sorted((g for g in s.legs if g.right == "C"), key=lambda g: g.strike)
    if len(s.legs) != 4 or len(puts) != 2 or len(calls) != 2:
        raise ValueError(f"{where}: two put legs and two call legs")
    if len({g.expiry for g in s.legs}) != 1:
        raise ValueError(f"{where}: all four legs share one expiry")
    long_p, short_p = puts
    short_c, long_c = calls
    if (long_p.action, short_p.action, short_c.action, long_c.action) != (
        "BUY",
        "SELL",
        "SELL",
        "BUY",
    ) or not (long_p.strike < short_p.strike < short_c.strike < long_c.strike):
        raise ValueError(f"{where}: needs long put < short put < short call < long call")
    wing = max(_condor_wings(s.legs))
    if not s.limit < wing:
        raise ValueError(f"{where}: limit (credit floor) {s.limit} must be below the wing {wing}")


def _shape_two_expiry(s: LegStructure, where: str) -> None:
    pair = _one_of_each(s.legs)
    if pair is None:
        raise ValueError(f"{where}: one BUY and one SELL leg")
    buy, sell = pair
    if buy.right != sell.right:
        raise ValueError(f"{where}: both legs share one right")
    if not buy.expiry > sell.expiry:
        raise ValueError(f"{where}: the BUY leg must expire after the SELL leg")
    if s.kind == "calendar":
        if buy.strike != sell.strike:
            raise ValueError(f"{where}: both legs share one strike (else it is a diagonal)")
        return
    if buy.strike == sell.strike:
        raise ValueError(f"{where}: strikes must differ (same strike is a calendar)")
    protective = buy.strike < sell.strike if buy.right == "C" else buy.strike > sell.strike
    if not protective:
        raise ValueError(
            f"{where}: not protective: the long {buy.right} strike {buy.strike} must be "
            f"{'below' if buy.right == 'C' else 'above'} the short strike {sell.strike}"
        )


_SHAPES = {
    "long_single": _shape_long_single,
    "debit_vertical": _shape_vertical,
    "credit_vertical": _shape_vertical,
    "iron_condor": _shape_iron_condor,
    "calendar": _shape_two_expiry,
    "diagonal": _shape_two_expiry,
}


def validate_package_order(struct: LegStructure, side: str, qty: int, limit: Decimal) -> None:
    """Refuse (ValueError) any order on ``struct`` whose fill could realize a
    loss beyond ``struct.max_loss()``. ``limit`` is a debit-orientation
    package price.

    - side BUY or SELL; 0 < qty <= struct.quantity;
    - limit finite and positive;
    - opening (``struct.open_side``): a debit kind pays at most the cap
      (limit <= struct.limit), a credit kind receives at least the floor
      (limit >= struct.limit);
    - closing a credit kind (BUY-to-close) pays at most the width, the
      package's maximum value: loss <= width - floor = max loss per package.
    Closing a debit kind (SELL) at any positive price loses at most the
    debit paid, which the cap already bounds."""
    where = f"{struct.id}: {side} {qty} @ {limit}"
    if side not in ("BUY", "SELL"):
        raise ValueError(f"{where}: side must be BUY or SELL")
    if not 0 < qty <= struct.quantity:
        raise ValueError(f"{where}: quantity must be 1..{struct.quantity} (the structure's)")
    if not limit.is_finite():
        raise ValueError(f"{where}: limit must be finite")
    if not limit > 0:
        raise ValueError(f"{where}: limit must be positive (debit-orientation prices only)")
    if side == struct.open_side:
        if struct.is_credit and limit < struct.limit:
            raise ValueError(f"{where}: below the credit floor {struct.limit}")
        if not struct.is_credit and limit > struct.limit:
            raise ValueError(f"{where}: above the debit cap {struct.limit}")
    elif struct.is_credit:
        width = struct.width
        assert width is not None  # credit kinds are verticals and condors
        if limit > width:
            raise ValueError(f"{where}: BUY-to-close above the width {width}")


def margin_within_max_loss(
    struct: LegStructure,
    qty: int,
    init_margin_change: Decimal | None,
    factor: Decimal = WHATIF_MARGIN_FACTOR,
) -> bool:
    """True when IBKR's what-if initial-margin change for opening ``qty``
    packages is at most ``factor`` x their computed max loss (dollars).
    No number (None, NaN, infinite) is False: fail closed. A margin that
    shrinks (<= 0) passes: the package reduces the account's risk."""
    if init_margin_change is None or not init_margin_change.is_finite():
        return False
    return init_margin_change <= factor * struct.max_loss_per_package() * 100 * qty


def _is_leg_table(raw: object) -> bool:
    return isinstance(raw, LegStructure) or (
        isinstance(raw, Mapping) and ("kind" in raw or "legs" in raw)
    )


def parse_structure(raw: Mapping[str, Any]) -> PutSpread | LegStructure:
    """One ``[[structures]]`` table: ``kind`` or ``legs`` makes it a
    LegStructure (which forbids unknown keys, so a legacy table carrying
    either is refused, never half-parsed); anything else is a PutSpread."""
    if _is_leg_table(raw):
        return LegStructure(**raw)
    return PutSpread(**raw)


class TradePlan(BaseModel):
    """The whole book: structures plus a whole-book debit ceiling.

    ``structures`` holds the legacy put spreads (what the monitor and entry
    runner execute); ``leg_structures`` the multi-leg ones. Both arrive
    through the one ``structures`` input, routed by shape."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    account_mode: str = Field(pattern="^(paper|live)$")
    structures: list[PutSpread] = Field(default_factory=list)
    leg_structures: list[LegStructure] = Field(default_factory=list)
    total_debit_cap: Decimal = Field(gt=0)
    entry_window_start: str  # "HH:MM" ET
    entry_window_end: str

    @model_validator(mode="before")
    @classmethod
    def _route_structures(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and isinstance(data.get("structures"), list):
            raw = data["structures"]
            legacy = [s for s in raw if not _is_leg_table(s)]
            multi = [*(data.get("leg_structures") or []), *(s for s in raw if _is_leg_table(s))]
            data = {**data, "structures": legacy, "leg_structures": multi}
        return data

    @field_validator("total_debit_cap", mode="before")
    @classmethod
    def _money(cls, value: object) -> object:
        return _dec_before(value)

    @model_validator(mode="after")
    def _validate_book(self) -> TradePlan:
        if not self.structures and not self.leg_structures:
            raise ValueError(f"{self.id}: no structures; refusing an empty book")
        ids = [s.id for s in self.structures] + [s.id for s in self.leg_structures]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate structure ids: {ids}")
        committed = self.committed_at_caps
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
        """Worst-case dollars at risk across the book: each put spread's
        debit at its cap, each multi-leg structure's max_loss()."""
        return sum((s.max_debit for s in self.structures), Decimal(0)) + sum(
            (s.max_loss() for s in self.leg_structures), Decimal(0)
        )

    def all_specs(self) -> list[LegStructure]:
        """Every structure as a LegStructure (put spreads via as_spec())."""
        return [s.as_spec() for s in self.structures] + list(self.leg_structures)


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


def load_legacy_plan(path: Path | str) -> TradePlan:
    """``load_plan`` for the legacy runners (trex-monitor, trex-enter). They
    execute put spreads only, so a plan carrying multi-leg structures is
    refused rather than half-run: those belong to the desk runtime."""
    plan = load_plan(path)
    if plan.leg_structures:
        ids = [s.id for s in plan.leg_structures]
        raise ValueError(
            f"{path}: multi-leg structures {ids} are not executable by the legacy "
            "put-spread runners"
        )
    return plan
