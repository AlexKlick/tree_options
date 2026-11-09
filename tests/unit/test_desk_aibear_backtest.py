"""AI-BEAR-001 scorer: execution timing, exits, baselines, the pass bar."""

from __future__ import annotations

import json
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest

from tree_options.desk.aibear import backtest as bt
from tree_options.desk.aibear.params import ParamsError, load_params, params_path
from tree_options.desk.aibear.pricing import PricingParams, quote_spread
from tree_options.desk.aibear.rules import bull_runs, sleeve_rules

DOC = load_params()
RULES = sleeve_rules(DOC)
PP = PricingParams.from_doc(DOC)


def business_days(n: int, start: date = date(2001, 3, 1)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def instrument(states: list[str], closes: list[float], iv: float = 0.3) -> bt.Instrument:
    return bt.Instrument(
        name="X",
        cost_class="single",
        dates=business_days(len(closes)),
        closes=closes,
        states=states,
        bull_run=bull_runs(states),
        iv=[iv] * len(closes),
        rate=[0.0] * len(closes),
    )


def test_entry_is_at_the_next_close_and_priced_at_spot_100() -> None:
    inst = instrument(["BULL", "TOPPING"] + ["BEAR"] * 60, [100.0, 100.0, 80.0] + [80.0] * 59)
    t = bt.Simulator(inst, RULES["core"], PP).trade(1)
    assert t is not None
    assert t.entry_date == inst.dates[2].isoformat()  # signal at 1, entry at 2
    # the entry close (80) is normalized to 100: same debit as a fresh quote
    q = quote_spread(
        spot=100.0,
        k_long=100.0,
        k_short=70.0,
        dte=270,
        atm_iv=0.3,
        rate=0.0,
        right="P",
        cost_class="single",
        p=PP,
    )
    assert t.debit == pytest.approx(q.open_debit)


def test_take_profit_is_decided_at_one_close_and_executed_at_the_next() -> None:
    closes = [100.0, 100.0, 100.0, 40.0, 40.0, 40.0] + [40.0] * 10
    inst = instrument(["TOPPING"] * len(closes), closes)
    t = bt.Simulator(inst, RULES["core"], PP).trade(1)
    assert t is not None
    assert t.exit_reason == "take_profit"
    assert t.exit_date == inst.dates[4].isoformat()  # decided at 3, executed at 4
    assert t.r > 1.0


def test_ten_bull_closes_stop_the_core_position() -> None:
    states = ["TOPPING", "TOPPING", "TOPPING"] + ["BULL"] * 30
    inst = instrument(states, [100.0] * len(states))
    t = bt.Simulator(inst, RULES["core"], PP).trade(1)
    assert t is not None
    assert t.exit_reason == "regime_stop"
    # BULL from index 3: the 10th BULL close is index 12, executed at 13
    assert t.exit_date == inst.dates[13].isoformat()
    t2 = bt.Simulator(inst, RULES["core"], PP).trade(1, use_regime_stop=False)
    assert t2 is not None and t2.exit_reason != "regime_stop"


def test_a_regime_stop_starts_the_cooldown() -> None:
    states = (["TOPPING"] * 3 + ["BULL"] * 12) * 4
    inst = instrument(states, [100.0] * len(states))
    trades = bt.run_strategy(bt.Simulator(inst, RULES["core"], PP), inst.dates[0])
    for a, b in pairwise(trades):
        assert a.exit_reason == "regime_stop"
        gap = inst.dates.index(date.fromisoformat(b.signal_date)) - a.exit_index
        assert gap >= RULES["core"].cooldown_after_regime_stop


def test_an_unfinished_position_is_marked_at_the_last_close() -> None:
    inst = instrument(["TOPPING"] * 8, [100.0] * 8)
    t = bt.Simulator(inst, RULES["core"], PP).trade(1)
    assert t is not None and t.exit_reason == "open_at_end"
    assert t.exit_date == inst.dates[-1].isoformat()


def test_the_random_null_is_seeded_and_never_exceeds_the_trade_count() -> None:
    rng = np.random.default_rng(3)
    n = 400
    states = list(rng.choice(["BULL", "TOPPING", "BEAR"], size=n))
    closes = list(100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.02, n))))
    sim = bt.Simulator(instrument(states, closes), RULES["falls"], PP)
    a = bt.null_total(sim, date(2001, 3, 1), 5, seed=11)
    b = bt.null_total(sim, date(2001, 3, 1), 5, seed=11)
    assert a == b
    assert a[1] <= 5


def _synthetic_inputs(n: int, seed: int) -> bt.Inputs:
    rng = np.random.default_rng(seed)
    dates = business_days(n, date(2000, 1, 3))
    ndx = list(1000.0 * np.exp(np.cumsum(rng.normal(0.0, 0.015, n))))
    x = list(50.0 * np.exp(np.cumsum(rng.normal(0.0, 0.03, n))))
    vix = list(np.abs(rng.normal(20.0, 4.0, n)) + 8.0)
    return bt.Inputs(
        series={"NDX": list(zip(dates, ndx, strict=True)), "X": list(zip(dates, x, strict=True))},
        vix=list(zip(dates, vix, strict=True)),
        vxn=list(zip(dates, [v * 1.3 for v in vix], strict=True)),
        dtb3=[(d, 4.0) for d in dates],
        sha256={},
    )


def test_states_and_iv_at_d_ignore_everything_after_d() -> None:
    n, k = 700, 450
    inp = _synthetic_inputs(n, 5)
    end = inp.series["X"][-1][0]
    base = bt.build_instrument("X", inp, DOC, end=end)
    rng = np.random.default_rng(9)

    def poison(points: list[tuple[date, float]]) -> list[tuple[date, float]]:
        return points[: k + 1] + [(d, float(rng.uniform(1.0, 5000.0))) for d, _ in points[k + 1 :]]

    bad = bt.Inputs(
        series={n_: poison(p) for n_, p in inp.series.items()},
        vix=poison(inp.vix),
        vxn=poison(inp.vxn),
        dtb3=inp.dtb3,
        sha256={},
    )
    got = bt.build_instrument("X", bad, DOC, end=end)
    assert got.states[: k + 1] == base.states[: k + 1]
    assert got.iv[: k + 1] == base.iv[: k + 1]


def test_month_clusters_sum_trades_entered_in_the_same_month() -> None:
    def tr(day: str, r: float) -> bt.Trade:
        return bt.Trade("core", "X", day, day, day, "dte_exit", "BEAR", 1.0, 1.0 + r, r, r, 0, 0)

    trades = [tr("2001-03-01", 1.0), tr("2001-03-20", -0.5), tr("2001-04-02", 2.0)]
    assert bt.monthly_sums(trades) == [0.5, 2.0]
    s = bt.summarize(trades, draws=200, seed=1)
    assert s["trades"] == 3 and s["months"] == 2 and s["total_r"] == 2.5


def _row(total: float, lo: float, eras: dict[str, float], **extra: float) -> dict:
    return {
        "summary": {"total_r": total, "ci95_total_r": [lo, total + 1]},
        "eras": {k: {"total_r": v} for k, v in eras.items()},
        **extra,
    }


def test_the_sealed_pass_bar() -> None:
    bears = {"dotcom": 3.0, "gfc": 1.0, "bear-2022": -1.0}
    assert bt.verdict("falls", _row(5.0, 0.5, bears), 0.01, 0.05)[2] == "PASS"
    assert bt.verdict("falls", _row(5.0, -0.5, bears), 0.01, 0.05)[2] == "INSURANCE"
    assert bt.verdict("falls", _row(5.0, 0.5, bears), 0.20, 0.05)[2] == "FAIL"
    one_bear = {"dotcom": 3.0, "gfc": -1.0, "bear-2022": -1.0}
    assert bt.verdict("falls", _row(5.0, 0.5, one_bear), 0.01, 0.05)[2] == "FAIL"
    # core P4: loses at most half the ladder's bull-era R per year
    ok = _row(5.0, 0.5, bears, bull_r_per_year=-1.0, ladder_bull_r_per_year=-3.0)
    bad = _row(5.0, 0.5, bears, bull_r_per_year=-2.0, ladder_bull_r_per_year=-3.0)
    assert bt.verdict("core", ok, 0.01, 0.05)[2] == "PASS"
    assert bt.verdict("core", bad, 0.01, 0.05)[1]["P4"] is False


def test_an_edited_parameter_file_is_refused_by_the_sealed_loader(tmp_path: Path) -> None:
    edited = tmp_path / "p.json"
    edited.write_bytes(params_path().read_bytes() + b" ")
    with pytest.raises(ParamsError, match="not the sealed"):
        load_params(edited, require_sealed=True)


def test_results_are_written_deterministically(tmp_path: Path) -> None:
    t = bt.Trade(
        "core",
        "X",
        "2001-03-01",
        "2001-03-02",
        "2001-04-02",
        "dte_exit",
        "BEAR",
        1.0,
        2.0,
        1.0,
        1.0,
        0,
        5,
    )
    a = bt.write_outputs(tmp_path / "a", {"b": 1, "a": 2}, [t])
    b = bt.write_outputs(tmp_path / "b", {"a": 2, "b": 1}, [t])
    assert a == b
    assert json.loads((tmp_path / "a" / "trades.jsonl").read_text())["exit_date"] == "2001-04-02"
