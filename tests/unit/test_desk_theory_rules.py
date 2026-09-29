"""The theory lane's ``"builtin": "theory"`` long-run rules against hand-derived choices on
a hand-built board (every expected id below is worked out from the fixture by hand, not by
running the rule)."""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import pytest

from tree_options.desk import longrun, outcomes
from tree_options.desk.longrun import Board
from tree_options.desk.theory_rules import (
    HORIZONS,
    STRUCTURES,
    TheoryParams,
    otm_pct,
    row_bullish,
    rule_theory,
    slot_index,
)

CONTEXT = {"time_of_day": "morning", "session_ordinal": 12, "underlyings": {
    # U1 fell over 20 sessions but rose yesterday; U2 the opposite
    "U1": {"ret_1s_pct": "0.50", "ret_5s_pct": "-1.00", "ret_20s_pct": "-2.00",
           "rv_20s_ann_pct": "15.0"},
    "U2": {"ret_1s_pct": "-0.30", "ret_5s_pct": "1.20", "ret_20s_pct": "3.10",
           "rv_20s_ann_pct": "25.0"}}}

# board order a..f; otm = +moneyness (calls) / -moneyness (puts):
#   a -0.50, b +2.00, c +1.50, d +3.00, e +1.00, f None
ROWS: list[dict[str, Any]] = [
    {"id": "a", "structure": "put_credit", "direction": "bullish", "underlying": "U1",
     "dte": 30, "short_strike_moneyness_pct": "0.50", "max_loss": "250", "reward_risk": "1.2"},
    {"id": "b", "structure": "call_credit", "direction": "bearish", "underlying": "U1",
     "dte": 10, "short_strike_moneyness_pct": "2.00", "max_loss": "180", "reward_risk": "1.8"},
    {"id": "c", "structure": "call_debit", "direction": "bullish", "underlying": "U2",
     "dte": 40, "short_strike_moneyness_pct": "1.50", "max_loss": "120", "reward_risk": "3.0"},
    {"id": "d", "structure": "put_debit", "direction": "bearish", "underlying": "U2",
     "dte": 20, "short_strike_moneyness_pct": "-3.00", "max_loss": "90", "reward_risk": "4.1"},
    {"id": "e", "structure": "put_credit", "direction": "bullish", "underlying": "U2",
     "dte": 45, "short_strike_moneyness_pct": "-1.00", "max_loss": "270", "reward_risk": "0.9"},
    {"id": "f", "structure": "call_credit", "direction": "bearish", "underlying": "U2",
     "dte": 5, "short_strike_moneyness_pct": None, "max_loss": "200", "reward_risk": "1.5"},
]
CREDITS = ["put_credit", "call_credit"]
CLOCKS = ("10:00", "10:45", "11:30", "12:15", "13:00", "13:45", "14:30", "15:15")


def board(session: str = "2026-06-01", clock: str = "10:45",
          context: dict[str, Any] | None = CONTEXT, rows: list[dict[str, Any]] = ROWS) -> Board:
    return Board(f"s:{session}T{clock}", session, clock, [dict(r) for r in rows], context)


def pick(**knobs: Any) -> tuple[str | None, str | None]:
    knobs.setdefault("structures", list(STRUCTURES))
    return rule_theory({"name": "t", "builtin": "theory", **knobs})(board())


def test_row_helpers_hand_cases() -> None:
    assert [otm_pct(r) for r in ROWS] == [-0.5, 2.0, 1.5, 3.0, 1.0, None]
    assert [row_bullish(r) for r in ROWS] == [True, False, True, False, True, False]
    # an explicit direction wins over the structure's default
    assert not row_bullish({"structure": "call_debit", "direction": "bearish"})
    assert row_bullish({"structure": "put_credit"})


def test_slot_index_steps_by_one_per_clock_and_per_calendar_day() -> None:
    day = [slot_index("2026-06-01", c) for c in CLOCKS]
    assert [b - a for a, b in pairwise(day)] == [1] * 7
    assert slot_index("2026-06-02", "10:00") - slot_index("2026-06-01", "10:00") == 1


def test_default_and_direction_rules() -> None:
    assert pick() == ("a", None)
    assert pick(horizon="hold:5") == ("a", "hold:5")
    assert pick(direction="bullish") == ("a", None)
    assert pick(direction="bearish") == ("b", None)
    assert pick(structures=["put_debit"], horizon="eod") == ("d", "eod")


def test_momentum_follows_the_20_session_sign_of_the_rows_underlying() -> None:
    # U1 down -> its bearish rows (b); U2 up -> its bullish rows (c, e)
    assert pick(direction="momentum_20s") == ("b", None)
    assert pick(direction="momentum_20s", structures=["call_debit", "put_credit"]) == ("c", None)
    assert pick(direction="momentum_20s", structures=["put_debit"]) == (None, None)


def test_cross_sectional_trades_the_weakest_bearish_and_the_strongest_bullish() -> None:
    # 20s: U1 -2.00 < U2 3.10; 5s: U1 -1.00 < U2 1.20
    assert pick(direction="xs_weak_20s") == ("b", None)  # the only bearish U1 row
    assert pick(direction="xs_strong_20s") == ("c", None)  # bullish U2 rows c, e
    assert pick(direction="xs_strong_20s", structures=["put_credit"]) == ("e", None)
    assert pick(direction="xs_weak_5s", horizon="hold:5") == ("b", "hold:5")
    assert pick(direction="xs_strong_5s") == ("c", None)
    assert pick(direction="xs_weak_20s", structures=["put_debit"]) == (None, None)  # d is U2
    # a third, weaker underlying takes the weak side; the strong side is unchanged
    three = {**CONTEXT, "underlyings": {**CONTEXT["underlyings"],
                                        "U3": {"ret_5s_pct": "-0.50", "ret_20s_pct": "-5.00"}}}
    rows = [*ROWS, {"id": "g", "structure": "put_debit", "direction": "bearish",
                    "underlying": "U3", "dte": 30}]
    weak = rule_theory({"structures": list(STRUCTURES), "direction": "xs_weak_20s"})
    strong = rule_theory({"structures": list(STRUCTURES), "direction": "xs_strong_20s"})
    assert weak(board(context=three, rows=rows)) == ("g", None)
    assert strong(board(context=three, rows=rows)) == ("c", None)
    # 5s: U1 -1.00 is still the weakest of (-1.00, 1.20, -0.50)
    assert rule_theory({"structures": list(STRUCTURES), "direction": "xs_weak_5s"})(
        board(context=three, rows=rows)) == ("b", None)


def test_cross_sectional_ties_and_missing_returns_are_ineligible() -> None:
    tie = {**CONTEXT, "underlyings": {"U1": {"ret_20s_pct": "1.00"},
                                      "U2": {"ret_20s_pct": "1.00"}}}
    lone = {**CONTEXT, "underlyings": {"U1": {"ret_20s_pct": "-2.00"},
                                       "U2": {"ret_20s_pct": None}}}
    top_tie = {**CONTEXT, "underlyings": {"U1": {"ret_20s_pct": "-2.00"},
                                          "U2": {"ret_20s_pct": "3.10"},
                                          "U3": {"ret_20s_pct": "3.10"}}}
    for direction in ("xs_weak_20s", "xs_strong_20s"):
        rule = rule_theory({"structures": list(STRUCTURES), "direction": direction})
        assert rule(board(context=tie)) == (None, None)
        assert rule(board(context=lone)) == (None, None)
        assert rule(board(context=None)) == (None, None)
    assert rule_theory({"structures": list(STRUCTURES), "direction": "xs_strong_20s"})(
        board(context=top_tie)) == (None, None)
    assert rule_theory({"structures": list(STRUCTURES), "direction": "xs_weak_20s"})(
        board(context=top_tie)) == ("b", None)


def test_reversal_fades_the_one_session_sign() -> None:
    # U1 up yesterday -> fade with bearish U1 rows (b); U2 down -> bullish U2 rows (c, e)
    assert pick(direction="reversal_1s") == ("b", None)
    assert pick(direction="reversal_1s", structures=["put_debit", "put_credit"]) == ("e", None)


def test_null_or_zero_return_and_missing_context_are_ineligible() -> None:
    flat = {**CONTEXT, "underlyings": {"U1": {"ret_1s_pct": "0.00", "ret_20s_pct": None},
                                       "U2": {"ret_1s_pct": None, "ret_20s_pct": "0"}}}
    for direction in ("momentum_20s", "reversal_1s"):
        rule = rule_theory({"structures": list(STRUCTURES), "direction": direction})
        assert rule(board(context=flat)) == (None, None)
        assert rule(board(context=None)) == (None, None)
    assert rule_theory({"structures": list(STRUCTURES), "rv_max": 99})(
        board(context=None)) == (None, None)


def test_entry_filters_are_inclusive_and_missing_fields_fail() -> None:
    assert pick(structures=CREDITS, otm_min=1.0) == ("b", None)  # b 2.0, e 1.0; f None out
    assert pick(structures=CREDITS, otm_min=1.0, key="max_otm") == ("b", None)
    assert pick(structures=CREDITS, otm_min=1.0, key="min_otm") == ("e", None)
    assert pick(dte_max=10) == ("b", None)  # b (10) and f (5)
    assert pick(structures=["call_credit"], dte_max=7) == ("f", None)
    assert pick(rv_max=20) == ("a", None)  # U1 rows only
    assert pick(rv_min=20) == ("c", None)  # U2 rows only
    assert pick(max_loss_min=200, key="max_max_loss") == ("e", None)  # a 250, e 270, f 200
    assert pick(max_loss_max=100) == ("d", None)
    assert pick(key="max_reward_risk") == ("d", None)
    assert pick(key="min_max_loss") == ("d", None)
    assert pick(time_of_day=["open"]) == (None, None)
    assert pick(time_of_day=["morning", "close"]) == ("a", None)


def test_require_all_needs_every_structure_on_one_underlying() -> None:
    assert pick(structures=CREDITS, require_all=True) == ("a", None)
    # otm >= 0 leaves U1 with only b and U2 with only e: no underlying holds both
    assert pick(structures=CREDITS, require_all=True, otm_min=0) == (None, None)
    only_u1 = [r for r in ROWS if r["underlying"] == "U1" or r["id"] == "e"]
    rule = rule_theory({"structures": CREDITS, "require_all": True, "key": "board_order"})
    assert rule(board(rows=only_u1)) == ("a", None)
    no_pair = [r for r in ROWS if r["id"] in ("a", "c", "d", "f")]  # pc on U1, cc on U2
    assert rule(board(rows=no_pair)) == (None, None)


def test_pair_trades_both_structures_of_the_best_underlying_as_one_choice() -> None:
    # U1 holds a (put_credit) + b (call_credit); U2 holds e + f. Under
    # board_order each underlying is ranked by its best-ordered leg: U1's best
    # is a (index 0), U2's is e (index 4) -> U1, legs joined in structures order
    assert pick(structures=CREDITS, require_all=True, pair=True) == ("a+b", None)
    assert pick(structures=CREDITS, require_all=True, pair=True,
                horizon="hold:5") == ("a+b", "hold:5")
    # a cheaper-risk U2 put_credit (g, max_loss 50) makes U2 the best underlying
    # under min_max_loss (U1's best-ordered leg b = 180, U2's g = 50): g+f
    rows = [*ROWS, {"id": "g", "structure": "put_credit", "direction": "bullish",
                    "underlying": "U2", "dte": 20, "max_loss": "50", "reward_risk": "2.0"}]
    cheap = rule_theory({"structures": CREDITS, "require_all": True, "pair": True,
                         "key": "min_max_loss"})
    assert cheap(board(rows=rows)) == ("g+f", None)
    # entry filters apply to both legs: otm >= 1 drops a, so U1 holds only b ->
    # no underlying with both structures -> no trade
    assert pick(structures=CREDITS, require_all=True, pair=True, otm_min=1.0) == (None, None)
    no_pair = [r for r in ROWS if r["id"] in ("a", "c", "d", "f")]  # pc on U1, cc on U2
    assert cheap(board(rows=no_pair)) == (None, None)
    # the separator is the harness's pair grammar, kept in sync deliberately
    from tree_options.desk.theory_rules import PAIR_SEP

    assert PAIR_SEP == longrun.PAIR_SEP and "a+b" == "a" + PAIR_SEP + "b"


def test_slot_alternation_is_balanced_and_flips_day_to_day() -> None:
    rule = rule_theory({"structures": CREDITS, "require_all": True, "alternate": "slot",
                        "horizon": "hold:5"})
    monday = [rule(board("2026-06-01", c)) for c in CLOCKS]
    tuesday = [rule(board("2026-06-02", c)) for c in CLOCKS]
    # the first put_credit row is a, the first call_credit row is b
    assert {m[0] for m in monday} == {"a", "b"} and all(m[1] == "hold:5" for m in monday)
    assert [m[0] for m in monday].count("a") == 4
    assert all(x[0] != y[0] for x, y in pairwise(monday))
    assert all(x[0] != y[0] for x, y in zip(monday, tuesday, strict=True))


def test_rules_are_deterministic_and_read_only_the_board() -> None:
    rule = rule_theory({"structures": list(STRUCTURES), "direction": "momentum_20s"})
    first = board()
    assert rule(first) == rule(first) == rule(board())
    assert first.rows == ROWS and first.context == CONTEXT  # nothing mutated


@pytest.mark.parametrize("entry, match", [
    ({"structures": CREDITS, "dte_maxx": 3}, "unknown theory knobs"),
    ({"structures": "put_credit"}, "must be a list"),
    ({}, "must be a list"),
    ({"structures": ["iron_condor"]}, "structures"),
    ({"structures": ["put_credit", "put_credit"]}, "structures"),
    ({"structures": CREDITS, "horizon": "hold:7"}, "horizon"),
    ({"structures": CREDITS, "direction": "up"}, "direction"),
    ({"structures": CREDITS, "key": "max_width"}, "key"),
    ({"structures": CREDITS, "alternate": "random"}, "alternate"),
    ({"structures": CREDITS, "time_of_day": ["noon"]}, "time_of_day"),
    ({"structures": CREDITS, "time_of_day": []}, "time_of_day"),
    ({"structures": CREDITS, "dte_min": 30, "dte_max": 10}, "min above max"),
    ({"structures": CREDITS, "pair": True}, "pair needs require_all"),
    ({"structures": list(STRUCTURES), "require_all": True, "pair": True}, "pair needs"),
    ({"structures": CREDITS, "require_all": True, "pair": True, "alternate": "slot"},
     "alternate"),
])
def test_invalid_entries_are_refused(entry: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        rule_theory(entry)


def test_params_object_is_accepted_directly() -> None:
    params = TheoryParams(structures=("call_credit",), horizon="expiry", bounds=(("dte", 6, None),))
    assert rule_theory(params)(board()) == ("b", "expiry")


def test_longrun_config_registers_the_theory_builtin() -> None:
    entries = [{"name": "theory_shortvol_alt_h5", "kind": "control", "builtin": "theory",
                "structures": CREDITS, "require_all": True, "alternate": "slot",
                "horizon": "hold:5"},
               {"name": "shortvol_pair_h5", "kind": "control", "builtin": "theory",
                "structures": CREDITS, "require_all": True, "pair": True,
                "horizon": "hold:5"},
               {"name": "theory_bear_h5", "kind": "rule", "builtin": "theory",
                "structures": list(STRUCTURES), "direction": "bearish", "horizon": "hold:5"}]
    specs = longrun.policies_from_config(entries, builtin=False)
    assert [s.name for s in specs] == ["theory_shortvol_alt_h5", "shortvol_pair_h5",
                                       "theory_bear_h5"]
    assert specs[2].rule is not None and specs[2].rule(board()) == ("b", "hold:5")
    assert specs[0].rule is not None and specs[0].rule(board()) in (("a", "hold:5"),
                                                                    ("b", "hold:5"))
    assert specs[1].rule is not None and specs[1].rule(board()) == ("a+b", "hold:5")
    with pytest.raises(ValueError, match="unknown theory knobs"):
        longrun.policies_from_config([{**entries[0], "otm_mni": 1}], builtin=False)


def test_vocabularies_match_the_outcome_engine_and_harness() -> None:
    assert HORIZONS == outcomes.EXIT_MODES
    assert set(STRUCTURES) == set(longrun.STRUCTURES) == set(outcomes.STRUCTURES)
