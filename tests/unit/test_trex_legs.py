"""trex multi-leg plan schema (desk lane E1): Leg / LegStructure / ExitRules.

Defined-risk kinds only, validated fail-closed; max loss per structure in
dollars (per package x100 x quantity). Every expected number is a literal
worked out by hand in the comment beside it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tree_options.trex.plan import (
    ExitRules,
    Leg,
    LegStructure,
    PutSpread,
    TradePlan,
    load_legacy_plan,
    load_plan,
    margin_within_max_loss,
    parse_structure,
    validate_package_order,
)

ENTRY = date(2026, 9, 24)
DEADLINE = date(2026, 10, 9)
FRONT = date(2026, 10, 16)
BACK = date(2026, 11, 20)


def _leg(right: str, action: str, strike: str, expiry: date = FRONT) -> dict[str, Any]:
    return {"right": right, "action": action, "strike": strike, "expiry": expiry}


def _exits(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"touch": False, "breach": False}
    base.update(overrides)
    return base


def _spec(kind: str, legs: list[dict[str, Any]], limit: Any, **overrides: Any) -> LegStructure:
    return LegStructure(**_raw(kind, legs, limit, **overrides))


def _raw(kind: str, legs: list[dict[str, Any]], limit: Any, **overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": "s1",
        "underlying": "SPY",
        "kind": kind,
        "legs": legs,
        "quantity": 1,
        "entry_date": ENTRY,
        "exit_deadline": DEADLINE,
        "limit": limit,
        "exits": _exits(),
    }
    raw.update(overrides)
    return raw


PUT_DEBIT = [_leg("P", "BUY", "185"), _leg("P", "SELL", "150")]
CALL_DEBIT = [_leg("C", "BUY", "100"), _leg("C", "SELL", "110")]
PUT_CREDIT = [_leg("P", "SELL", "100"), _leg("P", "BUY", "95")]
CALL_CREDIT = [_leg("C", "SELL", "110"), _leg("C", "BUY", "115")]
CONDOR = [
    _leg("P", "BUY", "90"),
    _leg("P", "SELL", "95"),
    _leg("C", "SELL", "105"),
    _leg("C", "BUY", "115"),
]
CALENDAR = [_leg("C", "SELL", "100", FRONT), _leg("C", "BUY", "100", BACK)]
CALL_DIAGONAL = [_leg("C", "SELL", "100", FRONT), _leg("C", "BUY", "95", BACK)]
PUT_DIAGONAL = [_leg("P", "SELL", "100", FRONT), _leg("P", "BUY", "105", BACK)]


class TestKindsAndMaxLoss:
    @pytest.mark.parametrize(
        ("kind", "legs", "limit", "qty", "per_package", "dollars"),
        [
            # debit kinds: max loss is the debit cap
            ("long_single", [_leg("C", "BUY", "100", BACK)], "3.20", 2, "3.20", "640"),
            ("long_single", [_leg("P", "BUY", "90")], "1.05", 1, "1.05", "105"),
            ("debit_vertical", PUT_DEBIT, "0.50", 5, "0.50", "250"),
            ("debit_vertical", CALL_DEBIT, "4.00", 1, "4.00", "400"),
            ("calendar", CALENDAR, "1.10", 3, "1.10", "330"),
            ("diagonal", CALL_DIAGONAL, "6.00", 1, "6.00", "600"),
            ("diagonal", PUT_DIAGONAL, "7.25", 2, "7.25", "1450"),
            # credit kinds: width minus the credit floor
            ("credit_vertical", PUT_CREDIT, "1.50", 2, "3.50", "700"),  # (5 - 1.50)
            ("credit_vertical", CALL_CREDIT, "1.20", 1, "3.80", "380"),  # (5 - 1.20)
            ("iron_condor", CONDOR, "2.00", 1, "8.00", "800"),  # max(5, 10) - 2
        ],
    )
    def test_max_loss(
        self,
        kind: str,
        legs: list[dict[str, Any]],
        limit: str,
        qty: int,
        per_package: str,
        dollars: str,
    ) -> None:
        s = _spec(kind, legs, limit, quantity=qty)
        assert s.max_loss_per_package() == Decimal(per_package)
        assert s.max_loss() == Decimal(dollars)

    @pytest.mark.parametrize(
        ("kind", "legs", "width"),
        [
            ("debit_vertical", PUT_DEBIT, "35"),
            ("debit_vertical", CALL_DEBIT, "10"),
            ("credit_vertical", PUT_CREDIT, "5"),
            ("iron_condor", CONDOR, "10"),  # the wider wing
            ("long_single", [_leg("P", "BUY", "90")], None),
            ("calendar", CALENDAR, None),
            ("diagonal", CALL_DIAGONAL, None),
        ],
    )
    def test_width(self, kind: str, legs: list[dict[str, Any]], width: str | None) -> None:
        s = _spec(kind, legs, "1.00")
        assert s.width == (None if width is None else Decimal(width))

    @pytest.mark.parametrize(
        ("kind", "legs", "limit", "direction", "open_side", "close_side"),
        [
            ("long_single", [_leg("P", "BUY", "90")], "1.00", 1, "BUY", "SELL"),
            ("debit_vertical", PUT_DEBIT, "1.00", 1, "BUY", "SELL"),
            ("calendar", CALENDAR, "1.00", 1, "BUY", "SELL"),
            ("diagonal", PUT_DIAGONAL, "1.00", 1, "BUY", "SELL"),
            ("credit_vertical", PUT_CREDIT, "1.00", -1, "SELL", "BUY"),
            ("iron_condor", CONDOR, "1.00", -1, "SELL", "BUY"),
        ],
    )
    def test_sides(
        self,
        kind: str,
        legs: list[dict[str, Any]],
        limit: str,
        direction: int,
        open_side: str,
        close_side: str,
    ) -> None:
        s = _spec(kind, legs, limit)
        assert (s.direction, s.open_side, s.close_side) == (direction, open_side, close_side)

    def test_package_legs_debit_orientation(self) -> None:
        # debit kinds: as authored
        debit = _spec("debit_vertical", PUT_DEBIT, "0.50")
        assert [(g.right, g.action, g.strike) for g in debit.package_legs()] == [
            ("P", "BUY", Decimal("185")),
            ("P", "SELL", Decimal("150")),
        ]
        # credit kinds: every action flipped, order kept (leg index stable)
        condor = _spec("iron_condor", CONDOR, "2.00")
        assert [(g.right, g.action, g.strike) for g in condor.package_legs()] == [
            ("P", "SELL", Decimal("90")),
            ("P", "BUY", Decimal("95")),
            ("C", "BUY", Decimal("105")),
            ("C", "SELL", Decimal("115")),
        ]
        assert [g.action for g in condor.legs] == ["BUY", "SELL", "SELL", "BUY"]

    def test_first_expiry_is_the_front_month(self) -> None:
        assert _spec("calendar", CALENDAR, "1.10").first_expiry == FRONT

    def test_toml_floats_become_exact_decimals(self) -> None:
        s = _spec("debit_vertical", [_leg("P", "BUY", 187.5), _leg("P", "SELL", 150.0)], 0.1)
        assert s.legs[0].strike == Decimal("187.5") and s.limit == Decimal("0.1")


def _refused(kind: str, legs: list[dict[str, Any]], limit: str = "1.00", **overrides: Any) -> str:
    """The refusal messages only (str(ValidationError) also echoes the input,
    which would let any needle naming a kind or field match trivially)."""
    with pytest.raises(ValidationError) as err:
        _spec(kind, legs, limit, **overrides)
    return "; ".join(str(e["msg"]) for e in err.value.errors())


class TestRefusals:
    @pytest.mark.parametrize(
        ("kind", "legs", "limit", "needle"),
        [
            ("long_single", [_leg("P", "SELL", "90")], "1.00", "uncovered"),
            ("long_single", PUT_DEBIT, "1.00", "exactly one BUY leg"),
            (
                "debit_vertical",
                [_leg("P", "BUY", "150"), _leg("P", "SELL", "185")],
                "1",
                "strikes inverted",
            ),
            (
                "debit_vertical",
                [_leg("C", "BUY", "110"), _leg("C", "SELL", "100")],
                "1",
                "strikes inverted",
            ),
            (
                "debit_vertical",
                [_leg("P", "BUY", "185"), _leg("C", "SELL", "150")],
                "1",
                "uncovered",
            ),
            (
                "debit_vertical",
                [_leg("P", "BUY", "185"), _leg("P", "BUY", "150")],
                "1",
                "one BUY and one SELL",
            ),
            (
                "debit_vertical",
                [_leg("P", "BUY", "185"), _leg("P", "SELL", "150", BACK)],
                "1",
                "one right and one expiry",
            ),
            # paying the width is a sure loss
            ("debit_vertical", PUT_DEBIT, "35", "must be below the width"),
            ("debit_vertical", PUT_DEBIT, "36", "must be below the width"),
            (
                "credit_vertical",
                [_leg("P", "SELL", "95"), _leg("P", "BUY", "100")],
                "1",
                "strikes inverted",
            ),
            (
                "credit_vertical",
                [_leg("C", "SELL", "115"), _leg("C", "BUY", "110")],
                "1",
                "strikes inverted",
            ),
            ("credit_vertical", PUT_CREDIT, "5", "must be below the width"),
            # 3 legs, all covered
            ("iron_condor", [*CONDOR[:2], CONDOR[3]], "1", "two put legs and two call legs"),
            ("iron_condor", [_leg("P", "BUY", "96"), *CONDOR[1:]], "1", "long put < short put"),
            (
                "iron_condor",
                [*CONDOR[:2], _leg("C", "SELL", "94"), CONDOR[3]],
                "1",
                "long put < short put",
            ),
            (
                "iron_condor",
                [*CONDOR[:3], _leg("C", "BUY", "115", BACK)],
                "1",
                "share one expiry",
            ),
            ("iron_condor", CONDOR, "10", "must be below the wing"),  # floor at the wider wing
            (
                "calendar",
                [_leg("C", "BUY", "100", FRONT), _leg("C", "SELL", "100", BACK)],
                "1",
                "BUY leg must expire after",
            ),
            (
                "calendar",
                [_leg("C", "SELL", "100", FRONT), _leg("C", "BUY", "105", BACK)],
                "1",
                "share one strike",
            ),
            (
                "calendar",
                [_leg("C", "SELL", "100", FRONT), _leg("P", "BUY", "100", BACK)],
                "1",
                "uncovered",
            ),
            (
                "diagonal",
                [_leg("C", "SELL", "100", FRONT), _leg("C", "BUY", "105", BACK)],
                "1",
                "protective",
            ),
            (
                "diagonal",
                [_leg("P", "SELL", "100", FRONT), _leg("P", "BUY", "95", BACK)],
                "1",
                "protective",
            ),
            ("diagonal", CALENDAR, "1", "strikes must differ"),
            (
                "diagonal",
                [_leg("C", "BUY", "95", FRONT), _leg("C", "SELL", "100", BACK)],
                "1",
                "BUY leg must expire after",
            ),
            (
                "debit_vertical",
                [_leg("P", "BUY", "185"), _leg("P", "BUY", "185")],
                "1",
                "duplicate",
            ),
            ("iron_condor", [*CONDOR[:3], _leg("C", "SELL", "120")], "1", "uncovered"),
        ],
    )
    def test_refused(self, kind: str, legs: list[dict[str, Any]], limit: str, needle: str) -> None:
        assert needle in _refused(kind, legs, limit)

    def test_deadline_must_precede_the_first_expiry(self) -> None:
        assert "never hold" in _refused("debit_vertical", PUT_DEBIT, exit_deadline=FRONT)
        # a calendar's first expiry is the FRONT (short) leg's
        late = date(2026, 10, 30)
        assert "never hold" in _refused("calendar", CALENDAR, exit_deadline=late)

    def test_entry_must_precede_the_deadline(self) -> None:
        assert "precede exit deadline" in _refused("debit_vertical", PUT_DEBIT, entry_date=DEADLINE)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"quantity": 0},
            {"limit": "0"},
            {"limit": "-1"},
            {"kind": "strangle"},
            {"legs": []},
            {"stop_los": {"basis": "debit_frac", "value": "0.5"}},  # typo: never ignored
        ],
    )
    def test_malformed(self, overrides: dict[str, Any]) -> None:
        raw = _raw("debit_vertical", PUT_DEBIT, "0.50")
        LegStructure(**raw)  # the base is valid: only the override breaks it
        with pytest.raises(ValidationError):
            LegStructure(**{**raw, **overrides})

    @pytest.mark.parametrize(
        "leg",
        [
            {**_leg("P", "BUY", "185"), "ratio": 2},
            {**_leg("P", "BUY", "0")},
            {**_leg("X", "BUY", "185")},
            {**_leg("P", "HOLD", "185")},
            {**_leg("P", "BUY", "185"), "underlying": "QQQ"},  # one underlying per structure
        ],
    )
    def test_malformed_leg(self, leg: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            Leg(**leg)


class TestExitRules:
    def test_defaults(self) -> None:
        rules = ExitRules(touch=True, breach=False)
        assert rules.stop_confirm_ticks == 3
        assert rules.take_profit is None and rules.stop_loss is None

    @pytest.mark.parametrize("missing", ["touch", "breach"])
    def test_touch_and_breach_are_explicit(self, missing: str) -> None:
        raw = {"touch": False, "breach": False}
        del raw[missing]
        with pytest.raises(ValidationError):
            ExitRules(**raw)

    @pytest.mark.parametrize(
        ("kind", "legs", "limit", "exits"),
        [
            ("debit_vertical", PUT_DEBIT, "0.50", _exits(touch=True)),
            ("long_single", [_leg("C", "BUY", "100")], "2", _exits(touch=True)),
            ("credit_vertical", PUT_CREDIT, "1.50", _exits(breach=True)),
            ("iron_condor", CONDOR, "2.00", _exits(breach=True)),
            (
                "debit_vertical",
                PUT_DEBIT,
                "0.50",
                _exits(take_profit={"basis": "width_frac", "value": "0.45"}),
            ),
            (
                "debit_vertical",
                PUT_DEBIT,
                "0.50",
                _exits(take_profit={"basis": "gain_frac", "value": "1.5"}),
            ),
            (
                "calendar",
                CALENDAR,
                "1.10",
                _exits(
                    take_profit={"basis": "gain_frac", "value": "0.3"},
                    stop_loss={"basis": "debit_frac", "value": "0.5"},
                ),
            ),
            (
                "credit_vertical",
                PUT_CREDIT,
                "1.50",
                _exits(
                    take_profit={"basis": "credit_frac", "value": "0.5"},
                    stop_loss={"basis": "credit_mult", "value": "2"},
                ),
            ),  # stop at 3.00 < width 5
            ("iron_condor", CONDOR, "2.00", _exits(stop_confirm_ticks=5)),
        ],
    )
    def test_accepted(
        self, kind: str, legs: list[dict[str, Any]], limit: str, exits: dict[str, Any]
    ) -> None:
        _spec(kind, legs, limit, exits=exits)

    @pytest.mark.parametrize(
        ("kind", "legs", "limit", "exits", "needle"),
        [
            ("credit_vertical", PUT_CREDIT, "1.50", _exits(touch=True), "touch"),
            ("calendar", CALENDAR, "1.10", _exits(touch=True), "touch"),
            ("debit_vertical", PUT_DEBIT, "0.50", _exits(breach=True), "breach"),
            (
                "credit_vertical",
                PUT_CREDIT,
                "1.50",
                _exits(take_profit={"basis": "width_frac", "value": "0.5"}),
                "width_frac",
            ),
            (
                "credit_vertical",
                PUT_CREDIT,
                "1.50",
                _exits(take_profit={"basis": "gain_frac", "value": "0.5"}),
                "gain_frac",
            ),
            (
                "calendar",
                CALENDAR,
                "1.10",
                _exits(take_profit={"basis": "width_frac", "value": "0.5"}),
                "width_frac",
            ),
            (
                "debit_vertical",
                PUT_DEBIT,
                "0.50",
                _exits(take_profit={"basis": "credit_frac", "value": "0.5"}),
                "credit_frac",
            ),
            (
                "debit_vertical",
                PUT_DEBIT,
                "0.50",
                _exits(stop_loss={"basis": "credit_mult", "value": "2"}),
                "credit_mult",
            ),
            (
                "credit_vertical",
                PUT_CREDIT,
                "1.50",
                _exits(stop_loss={"basis": "debit_frac", "value": "0.5"}),
                "debit_frac",
            ),
            # 4 x 1.50 = 6.00 >= width 5: a stop that can never fire
            (
                "credit_vertical",
                PUT_CREDIT,
                "1.50",
                _exits(stop_loss={"basis": "credit_mult", "value": "4"}),
                "never fire",
            ),
        ],
    )
    def test_refused_for_kind(
        self, kind: str, legs: list[dict[str, Any]], limit: str, exits: dict[str, Any], needle: str
    ) -> None:
        assert needle in _refused(kind, legs, limit, exits=exits)

    @pytest.mark.parametrize(
        "exits",
        [
            _exits(stop_confirm_ticks=0),
            _exits(stop_loss={"basis": "credit_mult", "value": "1"}),
            _exits(stop_loss={"basis": "debit_frac", "value": "1"}),
            _exits(stop_loss={"basis": "debit_frac", "value": "0"}),
            _exits(take_profit={"basis": "credit_frac", "value": "1"}),
            _exits(take_profit={"basis": "gain_frac", "value": "0"}),
            _exits(take_profit={"basis": "width_frac", "value": "1.01"}),
            _exits(take_profit={"basis": "percent", "value": "0.5"}),
        ],
    )
    def test_out_of_range(self, exits: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            ExitRules(**exits)


def _live_spread(**overrides: Any) -> PutSpread:
    base: dict[str, Any] = {
        "id": "nvda-oct",
        "underlying": "NVDA",
        "entry_date": date(2026, 9, 22),
        "expiry": date(2026, 10, 16),
        "long_strike": 185.0,
        "short_strike": 150.0,
        "quantity": 5,
        "limit_cap": 0.50,
        "exit_deadline": date(2026, 10, 9),
    }
    base.update(overrides)
    return PutSpread(**base)


class TestLegacyAsSpec:
    def test_live_spread_maps_to_a_debit_vertical(self) -> None:
        spec = _live_spread().as_spec()
        assert spec.kind == "debit_vertical"
        assert (spec.id, spec.underlying, spec.quantity) == ("nvda-oct", "NVDA", 5)
        assert (spec.entry_date, spec.exit_deadline) == (date(2026, 9, 22), date(2026, 10, 9))
        assert spec.limit == Decimal("0.5")
        assert [(g.right, g.action, g.strike, g.expiry, g.ratio) for g in spec.legs] == [
            ("P", "BUY", Decimal("185.0"), date(2026, 10, 16), 1),
            ("P", "SELL", Decimal("150.0"), date(2026, 10, 16), 1),
        ]
        assert spec.exits.touch is True and spec.exits.breach is False
        assert spec.exits.take_profit is None and spec.exits.stop_loss is None
        assert spec.deal_id is None
        assert spec.max_loss() == Decimal("250") == _live_spread().max_debit
        assert spec.width == Decimal("35")
        assert spec.package_legs() == _live_spread().package_legs()

    def test_take_profit_is_a_width_fraction(self) -> None:
        tp = _live_spread(take_profit_frac="0.45").as_spec().exits.take_profit
        assert tp is not None and (tp.basis, tp.value) == ("width_frac", Decimal("0.45"))


LIVE_PLAN = """
id = "putspread-20260922"
account_mode = "paper"
total_debit_cap = 1840.00
entry_window_start = "09:45"
entry_window_end = "12:00"

[[structures]]
id = "nvda-oct"
underlying = "NVDA"
entry_date = 2026-09-22
expiry = 2026-10-16
long_strike = 185.0
short_strike = 150.0
quantity = 5
limit_cap = 0.50
exit_deadline = 2026-10-09

[[structures]]
id = "qqq-nov"
underlying = "QQQ"
entry_date = 2026-09-22
expiry = 2026-11-20
long_strike = 600.0
short_strike = 475.0
quantity = 4
limit_cap = 2.40
exit_deadline = 2026-11-06

[[structures]]
id = "nvda-nov"
underlying = "NVDA"
entry_date = 2026-09-22
expiry = 2026-11-20
long_strike = 185.0
short_strike = 150.0
quantity = 3
limit_cap = 2.10
exit_deadline = 2026-11-06
"""

CONDOR_TABLE = """
[[structures]]
id = "spy-ic"
underlying = "SPY"
kind = "iron_condor"
quantity = 1
entry_date = 2026-09-24
exit_deadline = 2026-10-09
limit = 2.00
deal_id = "d-0001"
ref_mid = 2.15

[[structures.legs]]
right = "P"
action = "BUY"
strike = 90.0
expiry = 2026-10-16

[[structures.legs]]
right = "P"
action = "SELL"
strike = 95.0
expiry = 2026-10-16

[[structures.legs]]
right = "C"
action = "SELL"
strike = 105.0
expiry = 2026-10-16

[[structures.legs]]
right = "C"
action = "BUY"
strike = 115.0
expiry = 2026-10-16

[structures.exits]
touch = false
breach = true

[structures.exits.take_profit]
basis = "credit_frac"
value = 0.5
"""


class TestPlanLoading:
    def test_live_plan_loads_exactly_as_before(self, tmp_path: Path) -> None:
        path = tmp_path / "plan.toml"
        path.write_text(LIVE_PLAN)
        plan = load_plan(path)
        assert [type(s) for s in plan.structures] == [PutSpread, PutSpread, PutSpread]
        assert [s.id for s in plan.structures] == ["nvda-oct", "qqq-nov", "nvda-nov"]
        assert plan.leg_structures == []
        assert plan.structures[0] == _live_spread()
        # 0.50*5*100 + 2.40*4*100 + 2.10*3*100
        assert plan.committed_at_caps == Decimal("1840")
        assert load_legacy_plan(path) == plan

    def test_mixed_plan_routes_tables_by_shape(self, tmp_path: Path) -> None:
        path = tmp_path / "plan.toml"
        path.write_text(LIVE_PLAN.replace("1840.00", "2640.00") + CONDOR_TABLE)
        plan = load_plan(path)
        assert [s.id for s in plan.structures] == ["nvda-oct", "qqq-nov", "nvda-nov"]
        (ic,) = plan.leg_structures
        assert (ic.id, ic.kind, ic.deal_id, ic.limit) == (
            "spy-ic",
            "iron_condor",
            "d-0001",
            Decimal("2.0"),
        )
        assert ic.exits.breach is True and ic.exits.take_profit is not None
        assert str(ic.ref_mid) == "2.15"  # the TOML float's repr, never binary noise
        # 1840 legacy + (10 - 2) * 100 condor
        assert plan.committed_at_caps == Decimal("2640")
        assert [s.id for s in plan.all_specs()] == ["nvda-oct", "qqq-nov", "nvda-nov", "spy-ic"]
        assert plan.all_specs()[0] == _live_spread().as_spec()

    def test_legacy_runners_refuse_multi_leg_plans(self, tmp_path: Path) -> None:
        path = tmp_path / "plan.toml"
        path.write_text(LIVE_PLAN.replace("1840.00", "2640.00") + CONDOR_TABLE)
        with pytest.raises(ValueError, match="spy-ic"):
            load_legacy_plan(path)

    def test_book_cap_counts_max_loss_of_every_kind(self, tmp_path: Path) -> None:
        path = tmp_path / "plan.toml"
        path.write_text(LIVE_PLAN.replace("1840.00", "2639.99") + CONDOR_TABLE)
        with pytest.raises(ValueError, match="exceeds book cap"):
            load_plan(path)

    def test_live_hard_rail_sums_max_loss_across_kinds(self) -> None:
        condor = _spec("iron_condor", CONDOR, "2.00", quantity=6)  # 6 * 800 = 4800
        legacy = _live_spread()  # 250
        with pytest.raises(ValueError, match="hard rail"):
            TradePlan(
                id="p",
                account_mode="live",
                structures=[legacy, condor],
                total_debit_cap="10000",
                entry_window_start="09:45",
                entry_window_end="12:00",
            )
        ok = TradePlan(
            id="p",
            account_mode="live",
            structures=[legacy, _spec("iron_condor", CONDOR, "2.00", quantity=5)],
            total_debit_cap="10000",
            entry_window_start="09:45",
            entry_window_end="12:00",
        )
        assert ok.committed_at_caps == Decimal("4250")

    def test_ids_are_unique_across_kinds(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            TradePlan(
                id="p",
                account_mode="paper",
                structures=[_live_spread(), _spec("iron_condor", CONDOR, "2.00", id="nvda-oct")],
                total_debit_cap="10000",
                entry_window_start="09:45",
                entry_window_end="12:00",
            )

    def test_multi_leg_only_plan(self) -> None:
        plan = TradePlan(
            id="p",
            account_mode="paper",
            structures=[_spec("iron_condor", CONDOR, "2.00")],
            total_debit_cap="800",
            entry_window_start="09:45",
            entry_window_end="12:00",
        )
        assert plan.structures == [] and len(plan.leg_structures) == 1

    def test_empty_book_still_refused(self) -> None:
        with pytest.raises(ValueError, match="empty book"):
            TradePlan(
                id="p",
                account_mode="paper",
                structures=[],
                total_debit_cap="800",
                entry_window_start="09:45",
                entry_window_end="12:00",
            )

    @pytest.mark.parametrize("key", ["kind", "legs"])
    def test_either_key_makes_a_multi_leg_table(self, key: str) -> None:
        raw: dict[str, Any] = {
            "id": "x",
            "underlying": "SPY",
            "entry_date": "2026-09-24",
            "expiry": "2026-10-16",
            "long_strike": "185",
            "short_strike": "150",
            "quantity": 1,
            "limit_cap": "0.5",
            "exit_deadline": "2026-10-09",
            key: "debit_vertical" if key == "kind" else [],
        }
        # routed to LegStructure, which forbids the legacy fields: never half-parsed
        with pytest.raises(ValidationError):
            parse_structure(raw)

    def test_parse_structure_dispatch(self) -> None:
        legacy = parse_structure(_live_spread().model_dump())
        assert isinstance(legacy, PutSpread)
        spec = parse_structure(_spec("debit_vertical", PUT_DEBIT, "0.50").model_dump())
        assert isinstance(spec, LegStructure)


class TestDealReferenceMid:
    """ref_mid: the package mid the deal was priced at (the engine's
    stale_deal abort measures from it). Required with a deal_id."""

    def test_a_deal_needs_its_reference_mid(self) -> None:
        with pytest.raises(ValidationError, match="ref_mid"):
            _spec("iron_condor", CONDOR, "2.00", deal_id="d-0001")

    @pytest.mark.parametrize("bad", ["0", "-0.10", "NaN", "Infinity"])
    def test_reference_mid_is_positive(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            _spec("iron_condor", CONDOR, "2.00", deal_id="d-0001", ref_mid=bad)

    def test_optional_without_a_deal(self) -> None:
        assert _spec("iron_condor", CONDOR, "2.00").ref_mid is None
        spec = _spec("iron_condor", CONDOR, "2.00", ref_mid=2.15)  # a TOML-style float
        assert str(spec.ref_mid) == "2.15"
        deal = _spec("iron_condor", CONDOR, "2.00", deal_id="d-0001", ref_mid="2.15")
        assert (deal.deal_id, deal.ref_mid) == ("d-0001", Decimal("2.15"))


class TestLegacyRunnersRefuse:
    """trex-monitor / trex-enter execute put spreads only: a plan carrying a
    multi-leg structure is refused before any broker connection."""

    @pytest.mark.parametrize("runner", ["monitor", "enter"])
    def test_refused_before_connecting(
        self, runner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import importlib
        import logging

        module = importlib.import_module(f"tree_options.trex.{runner}")

        def never(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("connected to a broker for a refused plan")

        monkeypatch.setattr(module, "IbkrTrex", never)
        monkeypatch.setattr(logging, "basicConfig", lambda **_k: None)
        path = tmp_path / "plan.toml"
        path.write_text(LIVE_PLAN.replace("1840.00", "2640.00") + CONDOR_TABLE)
        with pytest.raises(ValueError, match="spy-ic"):
            module.main(["--plan", str(path), "--state-dir", str(tmp_path / "state")])


class TestWhatIfMargin:
    # credit put vertical 100/95, floor 1.50: max loss 3.50 * 100 * 2 = 700; bound 1.1x = 770
    SPEC = _spec("credit_vertical", PUT_CREDIT, "1.50", quantity=2)

    @pytest.mark.parametrize(
        ("margin", "ok"),
        [
            ("770", True),
            ("770.00", True),
            ("770.01", False),
            ("700", True),
            ("0", True),  # debit-style margin: nothing held back
            ("-120", True),  # the package reduces margin (offsets a holding)
            ("1E308", False),  # an undefined-risk margin
            (None, False),  # IBKR gave no number: fail closed
            ("NaN", False),
            ("Infinity", False),
        ],
    )
    def test_bound(self, margin: str | None, ok: bool) -> None:
        value = None if margin is None else Decimal(margin)
        assert margin_within_max_loss(self.SPEC, 2, value) is ok

    def test_bound_scales_with_the_order_quantity(self) -> None:
        # one package: 3.50 * 100 * 1.1 = 385
        assert margin_within_max_loss(self.SPEC, 1, Decimal("385")) is True
        assert margin_within_max_loss(self.SPEC, 1, Decimal("385.01")) is False


class TestPackageOrderBounds:
    """validate_package_order: no order the adapter sends can realize a loss
    beyond max_loss(). Opening pays at most the cap (debit) / receives at
    least the floor (credit); a credit BUY-to-close pays at most the width;
    quantities never exceed the structure's; prices are finite and positive."""

    DEBIT = _spec("debit_vertical", PUT_DEBIT, "0.50", quantity=2)  # cap 0.50
    CREDIT = _spec("credit_vertical", PUT_CREDIT, "1.50", quantity=2)  # floor 1.50, width 5
    CONDOR_ = _spec("iron_condor", CONDOR, "2.00", quantity=1)  # floor 2.00, width 10
    SINGLE = _spec("long_single", [_leg("C", "BUY", "100", BACK)], "3.20", quantity=1)
    CAL = _spec("calendar", CALENDAR, "1.10", quantity=3)

    @pytest.mark.parametrize(
        ("spec", "side", "qty", "limit"),
        [
            (DEBIT, "BUY", 2, "0.50"),  # at the cap
            (DEBIT, "BUY", 1, "0.01"),
            (DEBIT, "SELL", 2, "40.00"),  # closing a debit: any positive price
            (DEBIT, "SELL", 2, "0.01"),
            (CREDIT, "SELL", 2, "1.50"),  # at the floor
            (CREDIT, "SELL", 2, "4.99"),
            (CREDIT, "BUY", 2, "5.00"),  # closing a credit at the width
            (CREDIT, "BUY", 1, "0.05"),
            (CONDOR_, "BUY", 1, "10.00"),  # the wider wing
            (SINGLE, "BUY", 1, "3.20"),
            (SINGLE, "SELL", 1, "99.00"),
            (CAL, "BUY", 3, "1.10"),
            (CAL, "SELL", 3, "2.40"),
        ],
    )
    def test_accepted(self, spec: LegStructure, side: str, qty: int, limit: str) -> None:
        validate_package_order(spec, side, qty, Decimal(limit))

    @pytest.mark.parametrize(
        ("spec", "side", "qty", "limit", "needle"),
        [
            (DEBIT, "BUY", 2, "0.51", "cap"),
            (CREDIT, "SELL", 2, "1.49", "floor"),
            # a floor-4 five-wide credit sold at 0.10 would risk 4.90, not 1.00
            (CREDIT, "SELL", 2, "0.10", "floor"),
            (CREDIT, "BUY", 2, "5.01", "width"),
            (CONDOR_, "BUY", 1, "10.01", "width"),
            (SINGLE, "BUY", 1, "3.21", "cap"),
            (CAL, "BUY", 1, "1.11", "cap"),
            (DEBIT, "BUY", 3, "0.40", "quantity"),
            (CREDIT, "BUY", 3, "1.00", "quantity"),
            (DEBIT, "BUY", 0, "0.40", "quantity"),
            (DEBIT, "BUY", 1, "0", "positive"),
            (DEBIT, "SELL", 1, "-0.10", "positive"),
            (DEBIT, "BUY", 1, "Infinity", "finite"),
            (DEBIT, "SELL", 1, "Infinity", "finite"),
            (DEBIT, "BUY", 1, "NaN", "finite"),
            (CREDIT, "SELL", 1, "sNaN", "finite"),
            (DEBIT, "HOLD", 1, "0.40", "side"),
        ],
    )
    def test_refused(
        self, spec: LegStructure, side: str, qty: int, limit: str, needle: str
    ) -> None:
        with pytest.raises(ValueError, match=needle):
            validate_package_order(spec, side, qty, Decimal(limit))
