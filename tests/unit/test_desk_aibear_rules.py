"""AI-BEAR-001 sleeve rules and synthetic spread pricing."""

from __future__ import annotations

import math

import pytest

from tree_options.desk.aibear.params import load_params
from tree_options.desk.aibear.pricing import PricingParams, leg_iv, leg_mid, quote_spread
from tree_options.desk.aibear.rules import (
    bull_runs,
    entry_signal,
    episode_low,
    exit_reason,
    sleeve_rules,
)

DOC = load_params()
RULES = sleeve_rules(DOC)
PP = PricingParams.from_doc(DOC)
FLAT = PricingParams.from_doc(DOC, flat_skew=True)


def test_put_call_parity_holds_with_flat_iv() -> None:
    s, k, dte, iv, r = 100.0, 95.0, 120, 0.4, 0.04
    c = leg_mid(spot=s, strike=k, dte=dte, atm_iv=iv, rate=r, right="C", p=FLAT)
    p = leg_mid(spot=s, strike=k, dte=dte, atm_iv=iv, rate=r, right="P", p=FLAT)
    assert c - p == pytest.approx(s - k * math.exp(-r * dte / 365.0), abs=1e-9)


def test_skew_lifts_otm_put_iv_and_lowers_otm_call_iv() -> None:
    assert leg_iv(0.4, 100.0, 70.0, PP) == pytest.approx(0.4 * (1 + 0.5 * math.log(100 / 70)))
    assert leg_iv(0.4, 100.0, 110.0, PP) < 0.4 < leg_iv(0.4, 100.0, 90.0, PP)
    assert leg_iv(0.4, 100.0, 1e6, PP) == pytest.approx(0.4 * 0.5)  # the floor


def test_open_debit_exceeds_mid_exceeds_close_proceeds() -> None:
    q = quote_spread(
        spot=100.0,
        k_long=100.0,
        k_short=70.0,
        dte=270,
        atm_iv=0.45,
        rate=0.04,
        right="P",
        cost_class="single",
        p=PP,
    )
    assert q.open_debit > q.mid > q.close_proceeds > 0.0
    # 5 % of each leg's mid on both legs, plus 2 x $0.65 / 100 shares
    assert q.open_debit - q.mid == pytest.approx(0.05 * q.long_mid + 0.05 * q.short_mid + 0.013)


def test_a_worthless_spread_is_abandoned_not_paid_to_close() -> None:
    q = quote_spread(
        spot=200.0,
        k_long=100.0,
        k_short=90.0,
        dte=5,
        atm_iv=0.2,
        rate=0.0,
        right="P",
        cost_class="NDX",
        p=PP,
    )
    assert q.close_proceeds == 0.0


def test_core_enters_only_in_topping_bear_or_relief() -> None:
    core = RULES["core"]
    closes = [1.0] * 6
    for s, want in [
        ("BULL", False),
        ("TOPPING", True),
        ("BEAR", True),
        ("RELIEF", True),
        ("CAPITULATION", False),
        ("NOT_EVALUABLE", False),
    ]:
        assert entry_signal(core, ["BULL", s], closes, 1) is want, s


def test_falls_enters_on_the_first_bear_close_after_relief_or_topping() -> None:
    falls = RULES["falls"]
    c = [1.0, 1.0]
    assert entry_signal(falls, ["RELIEF", "BEAR"], c, 1)
    assert entry_signal(falls, ["TOPPING", "BEAR"], c, 1)
    assert not entry_signal(falls, ["BEAR", "BEAR"], c, 1)
    assert not entry_signal(falls, ["RELIEF", "RELIEF"], c, 1)
    assert not entry_signal(falls, ["BEAR"], [1.0], 0)


def test_relief_needs_an_up_close_right_after_capitulation() -> None:
    relief = RULES["relief"]
    assert entry_signal(relief, ["CAPITULATION", "CAPITULATION"], [10.0, 11.0], 1)
    assert entry_signal(relief, ["CAPITULATION", "BEAR"], [10.0, 11.0], 1)
    assert not entry_signal(relief, ["CAPITULATION", "BEAR"], [10.0, 9.0], 1)
    assert not entry_signal(relief, ["BEAR", "BEAR"], [10.0, 11.0], 1)


def test_episode_low_spans_the_whole_capitulation_run() -> None:
    states = ["BEAR", "CAPITULATION", "CAPITULATION", "CAPITULATION", "BEAR"]
    closes = [50.0, 40.0, 35.0, 38.0, 41.0]
    assert episode_low(states, closes, 4) == 35.0
    assert episode_low(states, closes, 1) is None


def test_bull_runs_count_consecutive_bull_closes() -> None:
    assert bull_runs(["BULL", "BULL", "BEAR", "BULL"]) == [1, 2, 0, 1]


def _exit(rule_name: str, **kw):
    base = dict(
        states=["BEAR"] * 3,
        closes=[1.0] * 3,
        j=2,
        bull_run=0,
        held_sessions=1,
        dte_left=200,
        value_mid=1.0,
        width=30.0,
        stop_low=None,
    )
    base.update(kw)
    return exit_reason(RULES[rule_name], **base)


def test_exit_order_take_profit_then_stops_then_time() -> None:
    assert _exit("core", value_mid=24.0) == "take_profit"  # 0.80 x 30
    assert _exit("core", value_mid=23.9) is None
    assert _exit("core", bull_run=10) == "regime_stop"
    assert _exit("core", bull_run=9) is None
    assert _exit("core", bull_run=10, use_regime_stop=False) is None
    assert _exit("core", dte_left=90) == "dte_exit"
    assert _exit("core", value_mid=24.0, bull_run=10) == "take_profit"
    assert _exit("falls", states=["BEAR", "BEAR", "RELIEF"], width=10.0) == "regime_stop"
    assert _exit("falls", held_sessions=25, width=10.0, dte_left=30) == "time_stop"
    assert _exit("relief", closes=[1.0, 1.0, 0.9], stop_low=0.95, width=10.0) == "episode_low_stop"
