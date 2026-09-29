"""desk.skill: the exact counterfactual decomposition, the block bootstrap,
the confidence sequences, the power table and the redigest CLI. Every
oracle is hand-computed (or a plain transcription of the cited formula);
no network, no model."""

from __future__ import annotations

import fcntl
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.unit.test_desk_longrun import (
    SESSIONS6,
    FakeAsk,
    _score_case,
    boards_for,
    ok,
    run,
    table_outcome,
)
from tree_options.desk import lab, longrun, skill
from tree_options.desk.longrun import Board, OutcomeCache, PolicySpec, Protocol

# ------------------------------------------------------------ hand fixture
#
#   board  session  rows (direction)            h1 values          h2 values
#   b1     d1       A+ B+ C- D-  (f = 1/2)       12  4 -6 -2  m=2   20  0  4 -8  m=4   base 3
#   b2     d1       E+ F- G-     (f = 1/3)        9 -3  0     m=2   -6  6  3     m=1   base 1.5
#   b3     d2       H+ I-        (f = 1/2)        5  nf       m=2.5 -10 2      m=-4  base -0.75
#   b4     d2       J+ K+        (one-sided)      1  3        m=2   -5 -3      m=-4  base -1
#
#   the arm: b1 A@h2 (20), b2 F@h1 (-3), b3 skip, b4 K@h1 (3): net 20, rho 3/4
#   base 0.75 x 2.75 = 2.0625; participation 3.5 - 2.0625 = 1.4375
#   horizon (4-3) + (2-1.5) + (2+1) = 4.5
#   direction (10-4) + (-1.5-2) + 0 = 2.5; xbar = 2/3
#     tilt (2/3-1/2)12 + (2/3-1/3)10.5 + 0 = 5.5; timing (1/3)12 - (2/3)10.5 = -3
#   selection (20-10) + (-3+1.5) + (3-2) = 9.5          sum = 20 exactly

BULL, BEAR = "put_credit", "call_credit"
H = ("h1", "h2")
FIXTURE: dict[str, tuple[str, list[tuple[str, str, float | None, float | None]]]] = {
    "b1": ("2026-06-01", [("A", BULL, 12, 20), ("B", "call_debit", 4, 0),
                          ("C", BEAR, -6, 4), ("D", "put_debit", -2, -8)]),
    "b2": ("2026-06-01", [("E", BULL, 9, -6), ("F", BEAR, -3, 6), ("G", BEAR, 0, 3)]),
    "b3": ("2026-06-02", [("H", BULL, 5, -10), ("I", BEAR, None, 2)]),
    "b4": ("2026-06-02", [("J", BULL, 1, -5), ("K", BULL, 3, -3)]),
}
DECISIONS: list[tuple[str | None, str | None]] = [("A", "h2"), ("F", "h1"), (None, None),
                                                   ("K", "h1")]
#   selection = structure + underlying + row: b1 A is the only put_credit (m_s 20 vs
#   m_d 10: structure +10); b2 F shares call_credit with G but not its underlying
#   (m_s -1.5, m_su -3: underlying -1.5); b4 J, K share structure and underlying (row +1)
UNDERLYING = {"A": "X", "B": "X", "C": "Y", "D": "Y", "E": "X", "F": "X", "G": "Y",
              "H": "X", "I": "Y", "J": "X", "K": "X"}


def fixture_boards() -> list[Board]:
    return [Board(name, session, "10:00",
                  [{"id": rid, "structure": structure, "underlying": UNDERLYING[rid]}
                   for rid, structure, _, _ in rows])
            for name, (session, rows) in FIXTURE.items()]


def fixture_get(snapshot: str, rid: str, horizon: str | None) -> tuple[float, float] | None:
    for row_id, _, v1, v2 in FIXTURE[snapshot][1]:
        if row_id == rid:
            value = v1 if horizon == "h1" else v2
            return None if value is None else (value + 2.0, value)  # gross = net + 2 (cost)
    return None


def test_decomposition_matches_the_hand_computation_and_sums_exactly() -> None:
    book = skill.ValueBook(fixture_get, H)
    dec = skill.decompose(book, fixture_boards(), DECISIONS)
    totals = {name: dec.total(name) for name in skill.COMPONENTS}
    expect = {"base": 2.0625, "participation": 1.4375, "horizon": 4.5,
              "direction_tilt": 5.5, "direction_timing": -3.0, "selection": 9.5}
    assert totals == pytest.approx(expect, abs=1e-12)
    assert dec.rho == 0.75 and dec.bullish_share == pytest.approx(2 / 3)
    assert dec.realized.tolist() == [20.0, -3.0, 0.0, 3.0]
    # the identity holds per board (hence per session and in total)
    per_board = sum(dec.parts[name] for name in skill.COMPONENTS)
    assert per_board == pytest.approx(dec.realized, abs=1e-12)
    assert float(dec.excess.sum()) == pytest.approx(16 - 5 + 1)  # v - m(b, h): 16, -5, 1
    assert float(dec.alpha.sum()) == pytest.approx(6.5)
    assert float(dec.vs_random.sum()) == pytest.approx(20 - 2.0625)
    # the no-fill row of b3 counts 0 in the random-row mean (m(b3, h1) = 2.5)
    assert book.mean(fixture_boards()[2], "h1") == 2.5
    # gross basis: every entered trade filled, so the cost drag is 3 x 2
    gross = skill.decompose(book, fixture_boards(), DECISIONS, "gross")
    assert float(gross.realized.sum() - dec.realized.sum()) == pytest.approx(6.0)
    assert dec.selection_split["structure"].tolist() == [10.0, 0.0, 0.0, 0.0]
    assert dec.selection_split["underlying"].tolist() == [0.0, -1.5, 0.0, 0.0]
    assert dec.selection_split["row"].tolist() == [0.0, 0.0, 0.0, 1.0]


def test_decompose_refuses_a_choice_that_is_not_a_board_row() -> None:
    book = skill.ValueBook(fixture_get, H)
    with pytest.raises(ValueError, match="not a row"):
        skill.decompose(book, fixture_boards()[:1], [("Z", "h1")])


def test_arm_skill_reports_the_identity_and_an_honest_verdict() -> None:
    book = skill.ValueBook(fixture_get, H)
    boards = fixture_boards()
    doc = skill.arm_skill(book, boards, DECISIONS, window=boards,
                          options=skill.SkillOptions(), draws=2000, seed=1, bound=None,
                          base_block=1)
    assert doc["net_total"] == 20.0 and doc["identity_residual"] == 0.0
    assert doc["components"]["selection"] == 9.5 and doc["cost_drag"] == 6.0
    assert doc["excess_total"] == 12.0 and doc["alpha_total"] == 6.5
    assert doc["horizon_mix"] == {"h1": 2, "h2": 1} and doc["entered"] == 3
    # session sums: d1 = 20 - 3, d2 = 0 + 3
    assert doc["intervals"]["net"]["total"] == 20.0
    assert doc["intervals"]["net"]["session_ci95"] == [6.0, 34.0]  # {17,3} x2 -> 6/20/34
    assert doc["selection_split"] == {"structure": 10.0, "underlying": -1.5, "row": 1.0}
    assert doc["verdict"].endswith("Descriptive; nothing promoted.")
    assert "FIXED RULE" not in doc["verdict"]  # kind defaults to model
    cs = doc["cs_in_sample"]
    assert cs["eb"] is None and cs["eb_unavailable"] == "no a-priori bound"
    # two sessions, L = 1: one block per parity -> no forward CS, and the verdict says so
    fwd = doc["cs_forward"]
    assert fwd["cs_total"] is None and fwd["reliable"] is False and fwd["per_parity"] == [1, 1]
    assert "forward CS n/a ([1, 1] blocks per parity < 20)" in doc["verdict"]
    assert doc["intervals"]["net"]["reliable"] is False  # 2 blocks < 10


# ------------------------------------------------------------ block bootstrap


def test_circular_block_bootstrap_hand_cases() -> None:
    # block 2 over [1, 0, 0, 0]: each block sums to 1 w.p. 1/2 -> totals 0/1/2 at 1/4, 1/2, 1/4
    sums = skill.block_bootstrap_sums([1.0, 0.0, 0.0, 0.0], 2, draws=8000, seed=3)
    values, counts = np.unique(sums, return_counts=True)
    assert values.tolist() == [0.0, 1.0, 2.0]
    assert counts / counts.sum() == pytest.approx([0.25, 0.5, 0.25], abs=0.02)
    # one block covering the whole circle: every resample is a rotation
    assert set(skill.block_bootstrap_sums([3.0, -1.0, 5.0], 3, draws=1000, seed=1)) == {7.0}
    # block 1 is the i.i.d. bootstrap: [0, 10] -> 0/10/20
    assert skill._pct(skill.block_bootstrap_sums([0.0, 10.0], 1, draws=4000, seed=2)) == [
        0.0, 20.0]


def test_block_bootstrap_widens_for_a_persistent_series() -> None:
    # [5]*10 + [-5]*10: i.i.d. var of the sum 20 x 25 = 500; a circular block of 10 sums
    # to 50 - 10|s| patterns with mean square 850, two blocks -> 1700; ESS = 20 x 500/1700
    series = [5.0] * 10 + [-5.0] * 10
    report = skill.interval_report(series, 10, draws=20000, seed=5)
    assert report["ess_sessions"] == pytest.approx(20 * 500 / 1700, rel=0.08)
    assert report["width_ratio"] > 1.4 and report["block"] == 10
    assert (report["blocks"], report["reliable"]) == (2.0, False)
    iid = skill.interval_report(series, 1, draws=20000, seed=5)
    assert iid["width_ratio"] == 1.0 and iid["ess_sessions"] == pytest.approx(20.0)
    assert (iid["blocks"], iid["reliable"]) == (20.0, True)


def test_forward_cs_splits_odd_and_even_blocks() -> None:
    # 91 sessions of +3, block 2: 45 full blocks of 6 (the 91st session is dropped),
    # 23 odd and 22 even: zero variance -> the mean block excess 6 exactly, x 45
    fwd = skill.forward_cs([3.0] * 91, 2)
    assert (fwd["blocks"], fwd["sessions_used"], fwd["per_parity"]) == (45, 90, [23, 22])
    assert fwd["cs_total"] == [270.0, 270.0] and fwd["significant"] is True
    assert fwd["reliable"] is True
    # too few blocks per parity: no CS at all (never an asymptotic CS on a handful)
    short = skill.forward_cs([3.0] * 9, 2)
    assert short["cs_total"] is None and short["significant"] is False
    assert short["per_parity"] == [2, 2] and "fewer than 20" in short["reason"]
    # the parities disagree -> the intersection is the overlap of the two CSs
    rng = np.random.default_rng(4)
    noisy = rng.normal(1.0, 1.0, size=200)
    fwd = skill.forward_cs(noisy, 2, 0.05)
    blocks = noisy.reshape(-1, 2).sum(axis=1)
    o_lo, o_hi = skill.asymptotic_cs(blocks[0::2], 0.025)
    e_lo, e_hi = skill.asymptotic_cs(blocks[1::2], 0.025)
    assert fwd["cs_total"] == [round(max(o_lo[-1], e_lo[-1]) * 100, 2),
                               round(min(o_hi[-1], e_hi[-1]) * 100, 2)]
    assert fwd["reliable"] is True and fwd["significant"] is True


def test_horizon_span_and_block_length_rules() -> None:
    assert skill.horizon_span("intraday", None, 10) == 1
    assert skill.horizon_span("eod", None, 10) == 1 and skill.horizon_span(None, None, 10) == 1
    assert skill.horizon_span("hold:5", None, 10) == 5
    assert skill.horizon_span("hold:5", None, 3) == 3  # capped at the sessions left
    assert skill.horizon_span("expiry", {"dte": 30}, 50) == 21  # round(30 x 252 / 365)
    assert skill.horizon_span("expiry", {"dte": 30}, 12) == 12
    assert skill.horizon_span("expiry", {}, 50) == 15  # no dte: the documented default
    assert skill.block_length([1, 1, 1, 5]) == 2  # p75 = 1 + 0.25 x 4
    assert skill.block_length([21, 21], max_block=20) == 20 and skill.block_length([]) == 1


# ------------------------------------------------------------ confidence sequences


def _asymp_oracle(x: list[float], alpha: float, t_star: float) -> tuple[float, float]:
    t = len(x)
    mean = sum(x) / t
    sd = math.sqrt(sum((v - mean) ** 2 for v in x) / t)
    rho2 = (-2 * math.log(alpha) + math.log(-2 * math.log(alpha) + 1)) / t_star
    radius = sd * math.sqrt(2 * (t * rho2 + 1) / (t * t * rho2)
                            * math.log(math.sqrt(t * rho2 + 1) / alpha))
    return mean - radius, mean + radius


def test_asymptotic_cs_matches_the_published_formula() -> None:
    x = [1.0, 3.0, -2.0, 4.0, 0.5, 2.5]
    lo, hi = skill.asymptotic_cs(x, 0.05, t_star=6)
    assert lo[0] == -math.inf and hi[0] == math.inf
    for t in range(2, 7):
        assert (lo[t - 1], hi[t - 1]) == pytest.approx(_asymp_oracle(x[:t], 0.05, 6))
    # rho tuned to t* is the tightest choice at t*
    rng = np.random.default_rng(0)
    y = rng.normal(size=400)
    width = {ts: float(np.subtract(*skill.asymptotic_cs(y, 0.05, ts)[::-1])[-1])
             for ts in (100, 400, 1600)}
    assert width[400] < width[100] and width[400] < width[1600]


def _eb_oracle(y: list[float], alpha: float, c: float) -> tuple[float, float]:
    mu_prev, s2_prev = 0.5, 0.25
    total_y = total_sq = 0.0
    sum_lam = sum_lam_y = sum_vpsi = 0.0
    for t, value in enumerate(y, start=1):
        lam = min(math.sqrt(2 * math.log(2 / alpha) / (s2_prev * t * math.log(1 + t))), c)
        v = 4 * (value - mu_prev) ** 2
        psi = (-math.log(1 - lam) - lam) / 4
        sum_lam += lam
        sum_lam_y += lam * value
        sum_vpsi += v * psi
        total_y += value
        mu = (0.5 + total_y) / (t + 1)
        total_sq += (value - mu) ** 2
        mu_prev, s2_prev = mu, (0.25 + total_sq) / (t + 1)
    center = sum_lam_y / sum_lam
    margin = (math.log(2 / alpha) + sum_vpsi) / sum_lam
    return max(0.0, center - margin), min(1.0, center + margin)


def test_eb_cs_matches_the_published_formula_by_hand() -> None:
    # two steps by hand on [0, 1]: lam = c = 1/2 both times, center 1/2,
    # margin log 40 + (1 + 2.25)(log 2 - 1/2)/4 -> the whole unit interval
    lo, hi = skill.eb_cs([1.0, 0.0], 0.0, 1.0)
    assert (lo[-1], hi[-1]) == (0.0, 1.0)
    rng = np.random.default_rng(11)
    y = (rng.random(300) < 0.3).astype(float)
    lo, hi = skill.eb_cs(y, 0.0, 1.0, 0.05, 0.5)
    for t in (50, 150, 300):
        assert (lo[t - 1], hi[t - 1]) == pytest.approx(_eb_oracle(y[:t].tolist(), 0.05, 0.5))
    # rescaling: values in [-10, 10] map affinely
    lo2, hi2 = skill.eb_cs(20 * y - 10, -10.0, 10.0)
    assert (lo2[-1], hi2[-1]) == pytest.approx((20 * lo[-1] - 10, 20 * hi[-1] - 10))
    with pytest.raises(ValueError, match="outside"):
        skill.eb_cs([11.0], -10.0, 10.0)


def test_confidence_sequences_cover_uniformly_in_time() -> None:
    rng = np.random.default_rng(7)
    misses_eb = misses_asymp = 0
    for _ in range(200):
        y = (rng.random(300) < 0.3).astype(float)
        lo, hi = skill.eb_cs(y, 0.0, 1.0)
        misses_eb += bool(np.any((lo > 0.3) | (hi < 0.3)))
        z = rng.normal(size=300)
        alo, ahi = skill.asymptotic_cs(z, 0.05)
        misses_asymp += bool(np.any((alo[20:] > 0) | (ahi[20:] < 0)))
    assert misses_eb / 200 <= 0.05 and misses_asymp / 200 <= 0.10


def test_monitor_maps_the_mean_to_the_population_total() -> None:
    doc = skill.monitor([8.0] * 6, 10, alpha=0.05, bound=None)
    assert doc["excess_observed"] == 48.0 and doc["eb"] is None
    assert doc["asymptotic"]["cs_total"] == [80.0, 80.0]  # zero variance: the mean x 10
    assert doc["significant"] is True
    eb = skill.monitor([100.0] * 40, 40, alpha=0.05, bound=850.0)
    assert eb["eb"]["bound"] == 850.0 and eb["eb"]["cs_total"][0] < 4000 < eb["eb"]["cs_total"][1]
    over = skill.monitor([900.0], 1, alpha=0.05, bound=850.0)
    assert over["eb"] is None and "exceeds" in over["eb_unavailable"]


def test_excess_bound_from_board_rows() -> None:
    rows = [{"id": "a", "width": "5"}, {"id": "b", "width": "1"}]
    assert skill.excess_bound([Board("s", "2026-06-01", "10:00", rows)]) == 850.0
    assert skill.excess_bound([Board("s", "2026-06-01", "10:00", [{"id": "a"}])]) is None


# ------------------------------------------------------------ power


def test_power_table_hand_case() -> None:
    boards = [Board("p1", "2026-06-01", "10:00", [{"id": "x"}, {"id": "y"}]),
              Board("p2", "2026-06-02", "10:00", [{"id": "x"}, {"id": "y"}])]
    values = {("p1", "x"): 10.0, ("p1", "y"): -10.0, ("p2", "x"): 4.0, ("p2", "y"): 0.0}

    def get(snapshot: str, rid: str, horizon: str | None) -> tuple[float, float]:
        return values[(snapshot, rid)], values[(snapshot, rid)]

    table = skill.power_table(skill.ValueBook(get, ("intraday",)), boards, ("intraday",))
    z = 1.959963984540054 + 0.8416212335729143
    doc = table["horizons"]["intraday"]
    # within-board variances 100 and 4 -> 52; board means 0, 2 -> 1; raw 53
    assert doc["sd_excess"] == round(math.sqrt(52), 2) and doc["sd_raw"] == round(math.sqrt(53), 2)
    assert doc["vrf_iid"] == round(53 / 52, 3) and doc["block"] == 1
    row = doc["rows"][0]
    assert row["trades"] == 250
    assert row["mde_excess"] == round(z * math.sqrt(52 / 250), 2)
    assert row["mde_raw_iid"] == round(z * math.sqrt(53 / 250), 2)
    assert row["mde_bullish_bet_dependent"] is None  # no row is bullish


def test_power_table_bullish_bet_column_hand_case() -> None:
    # b1: bull 6, bear -2 (m 2, bull excess +4); b2: bull 0, bear 2 (m 1, bull excess -1);
    # one bullish row per board (no pick noise); intraday blocks = sessions:
    # centred (2.5, -2.5) -> long-run variance 12.5 / 2 = 6.25 -> sd 2.5
    rows = [{"id": "u", "structure": "put_credit"}, {"id": "d", "structure": "call_credit"}]
    boards = [Board("p1", "2026-06-01", "10:00", rows), Board("p2", "2026-06-02", "10:00", rows)]
    values = {("p1", "u"): 6.0, ("p1", "d"): -2.0, ("p2", "u"): 0.0, ("p2", "d"): 2.0}

    def get(snapshot: str, rid: str, horizon: str | None) -> tuple[float, float]:
        return values[(snapshot, rid)], values[(snapshot, rid)]

    doc = skill.power_table(skill.ValueBook(get, ("intraday",)), boards,
                            ("intraday",))["horizons"]["intraday"]
    assert doc["sd_bullish_bet_dependent"] == 2.5
    z = 1.959963984540054 + 0.8416212335729143
    assert doc["rows"][1]["mde_bullish_bet_dependent"] == round(z * 2.5 / math.sqrt(500), 2)


def test_power_table_long_run_variance_uses_horizon_blocks() -> None:
    # board means 1, 1, -1, -1 over four sessions, within variance 1 each; hold:2 spans
    # 2, 2, 1, 1 sessions -> block 2 -> batch sums (2, -2) -> long-run 8/4 = 2 -> raw 3
    means = [1.0, 1.0, -1.0, -1.0]
    boards = [Board(f"q{i}", f"2026-06-0{i + 1}", "10:00", [{"id": "u"}, {"id": "d"}])
              for i in range(4)]

    def get(snapshot: str, rid: str, horizon: str | None) -> tuple[float, float]:
        m = means[int(snapshot[1:])]
        value = m + 1 if rid == "u" else m - 1
        return value, value

    book = skill.ValueBook(get, ("hold:2",))
    doc = skill.power_table(book, boards, ("hold:2",))["horizons"]["hold:2"]
    assert doc["block"] == 2 and doc["batches"] == 2
    assert doc["vrf_iid"] == 2.0 and doc["vrf_dependent"] == 3.0


# ------------------------------------------------------------ harness wiring


def test_score_run_carries_the_hand_checked_skill_section() -> None:
    boards, arms, receipts = _score_case()
    proto = Protocol(draws=2000, random_seeds=200, incumbent="m", cutoff="2026-06-01")
    doc = longrun.score_run(boards, arms, receipts, OutcomeCache(table_outcome), proto)
    section = doc["skill"]
    assert section["schema"] == skill.SKILL_SCHEMA and section["base_horizons"] == list(
        skill.MENU_HORIZONS)
    arms_doc = section["arms"]
    # rows l (bear, -4), w (bull, +10), n (bull, no fill = 0): m = 2, m_bull 5, m_bear -4,
    # delta 9, f = 2/3 on every board and horizon
    assert arms_doc["m#1"]["components"] == {
        "base": 8.0, "participation": 0.0, "horizon": 0.0, "direction_tilt": 12.0,
        "direction_timing": 0.0, "selection": 20.0}
    assert arms_doc["m#2"]["components"] == {
        "base": 6.0, "participation": 0.0, "horizon": 0.0, "direction_tilt": 9.0,
        "direction_timing": 0.0, "selection": 15.0}
    assert arms_doc["first_row"]["components"]["direction_tilt"] == -24.0
    acd = arms_doc["always_call_debit"]
    assert (acd["net_total"], acd["components"]["selection"]) == (0.0, -20.0)
    assert arms_doc["no_trade"]["verdict"].startswith("NO TRADES")
    assert arms_doc["m#1"]["selection_split"] == {"structure": 20.0, "underlying": 0.0,
                                                  "row": 0.0}
    assert "FIXED RULE" in arms_doc["first_row"]["verdict"]
    assert "FIXED RULE" not in arms_doc["m#1"]["verdict"]
    for name, arm in arms_doc.items():
        assert arm["identity_residual"] == 0.0, name
        standing = next(r for r in doc["standings"] if r["arm"] == name)
        assert arm["net_total"] == standing["net_total"]  # same boards, same outcomes
        assert "nothing promoted" in arm["verdict"]
    md = longrun.digest_markdown(doc)
    assert "## Skill accounting" in md and "- m#1: " in md
    assert doc["promotion"]["promoted"] is False


def test_skill_options_validate_and_menu_matches_lab() -> None:
    assert skill.MENU_HORIZONS == lab.V2_HORIZONS
    assert skill.SkillOptions.from_mapping({"max_block": 10}).max_block == 10
    with pytest.raises(ValueError, match="unknown skill option"):
        skill.SkillOptions.from_mapping({"blocks": 3})
    with pytest.raises(ValueError, match="alpha"):
        skill.SkillOptions(alpha=0.7)


def test_progress_shows_the_live_excess_and_cs(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run(run_dir, FakeAsk())  # the model always picks w
    progress = json.loads((run_dir / "progress.json").read_text())
    arms = progress["skill"]["arms"]
    assert arms["m#1"]["decided"] == 6 and arms["m#1"]["entered"] == 6
    assert arms["m#1"]["excess"] == 48.0  # 6 x (10 - 2)
    assert arms["first_row"]["excess"] == -36.0  # 6 x (-4 - 2)
    assert arms["no_trade"]["excess"] == 0.0
    assert arms["no_trade"]["in_sample_significant"] is False
    view = longrun.cockpit_view(run_dir)
    assert view["progress"]["skill"]["arms"]["m#1"]["excess"] == 48.0
    assert view["digest"]["skill"]["m#1"]["excess_total"] == 48.0


# ------------------------------------------------------------ redigest CLI


def _table_file(path: Path, boards: list[Board]) -> None:
    with path.open("w") as stream:
        for board in boards:
            for cid in board.ids:
                value = table_outcome(board.snapshot, cid, None)
                for mode in ("intraday", "eod", "hold:5", "expiry"):
                    stream.write(json.dumps({
                        "snapshot": board.snapshot, "candidate_id": cid, "exit_mode": mode,
                        "status": "no_fill" if value is None else "closed",
                        "gross": None if value is None else value["gross"],
                        "net": None if value is None else value["net"]}) + "\n")


def test_redigest_rescores_from_receipts_with_zero_model_calls(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    from tree_options.desk.__main__ import run_cli

    boards = boards_for(SESSIONS6[:3])
    ask = FakeAsk()
    monkeypatch.setitem(longrun.PLUGINS["boards"], "pytest", lambda p, c: boards)
    monkeypatch.setitem(longrun.PLUGINS["ask"], "pytest", lambda p, c: ask)
    _table_file(tmp_path / "table.jsonl", boards)
    config = tmp_path / "longrun.json"
    config.write_text(json.dumps({
        "out_root": "out", "incumbent": "m", "concurrency": 2,
        "boards": {"plugin": "pytest"},
        "outcome": {"plugin": "v2", "table": str(tmp_path / "table.jsonl")},
        "ask": {"plugin": "pytest"}, "quota": {"plugin": "always"},
        "protocol": {"draws": 1000, "random_seeds": 200},
        "policies": [{"name": "m", "kind": "model", "repeats": 2}]}))
    assert run_cli(["longrun", "run", "--config", str(config)]) == 0
    run_dir = Path(json.loads(capsys.readouterr().out)["run_dir"])
    before = json.loads((run_dir / "digest.json").read_text())

    def refuse(params: Any, ctx: Any) -> Any:
        raise AssertionError("redigest must not build a boards or ask plug-in")

    monkeypatch.setitem(longrun.PLUGINS["boards"], "pytest", refuse)
    monkeypatch.setitem(longrun.PLUGINS["ask"], "pytest", refuse)
    calls = len(ask.calls)
    assert run_cli(["longrun", "redigest", "--run-dir", str(run_dir)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["model_calls"] == 0 and printed["complete"] is True
    assert len(ask.calls) == calls
    after = json.loads((run_dir / "digest.json").read_text())
    assert (run_dir / "digest.pre-redigest.json").is_file()
    assert after["redigest"]["model_calls"] == 0
    assert [(r["arm"], r["net_total"], r["net_ci95"]) for r in after["standings"]] == [
        (r["arm"], r["net_total"], r["net_ci95"]) for r in before["standings"]]
    assert after["skill"]["arms"]["m#1"]["excess_total"] == 48.0
    assert after["promotion"]["promoted"] is False
    # a live run (its lock held) is never touched in place; --out writes elsewhere
    stamp = (run_dir / "digest.json").stat().st_mtime_ns
    with open(run_dir / ".lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_cli(["longrun", "redigest", "--run-dir", str(run_dir)]) == 3
        out = tmp_path / "side"
        assert run_cli(["longrun", "redigest", "--run-dir", str(run_dir), "--out",
                        str(out)]) == 0
    capsys.readouterr()
    assert (out / "digest.json").is_file() and (out / "digest.md").is_file()
    assert (run_dir / "digest.json").stat().st_mtime_ns == stamp
    # a session window re-scores only its boards (2 per session here, +8 excess each),
    # needs --out, and refuses an empty window or a malformed spec
    s0, s1, s2 = sorted({b.session for b in boards})
    window = tmp_path / "window"
    assert run_cli(["longrun", "redigest", "--run-dir", str(run_dir), "--out", str(window),
                    "--sessions", f"{s1}:"]) == 0
    capsys.readouterr()
    doc = json.loads((window / "digest.json").read_text())
    assert doc["redigest"]["sessions"] == {"first": s1, "last": None, "boards": 4}
    assert doc["skill"]["arms"]["m#1"]["excess_total"] == 32.0
    assert run_cli(["longrun", "redigest", "--run-dir", str(run_dir), "--out", str(window),
                    "--sessions", f":{s0}"]) == 0
    capsys.readouterr()
    assert json.loads((window / "digest.json").read_text())["skill"]["arms"]["m#1"][
        "excess_total"] == 16.0
    assert run_cli(["longrun", "redigest", "--run-dir", str(run_dir),
                    "--sessions", f"{s1}:{s2}"]) == 2  # no --out
    assert run_cli(["longrun", "redigest", "--run-dir", str(run_dir), "--out", str(window),
                    "--sessions", "2001-01-01:2001-01-02"]) == 2  # empty window
    assert run_cli(["longrun", "redigest", "--run-dir", str(run_dir), "--out", str(window),
                    "--sessions", s1]) == 2  # not FIRST:LAST
    capsys.readouterr()
    assert (run_dir / "digest.json").stat().st_mtime_ns == stamp
    # no table anywhere: refused, never a bundle parse
    cfg = json.loads((run_dir / "config.json").read_text())
    cfg["outcome"] = {"plugin": "v2"}
    (run_dir / "config.json").write_text(json.dumps(cfg))
    assert run_cli(["longrun", "redigest", "--run-dir", str(run_dir), "--out",
                    str(out)]) == 2


def test_partial_arms_are_labelled_and_scored_on_their_own_boards() -> None:
    boards = boards_for(SESSIONS6[:2])
    arms = longrun.arms_of([PolicySpec("m", "model"), PolicySpec(
        "first_row", "control", rule=longrun.rule_first_row())])
    receipts = {"m": {boards[0].snapshot: ok("w")},
                "first_row": {b.snapshot: ok("l") for b in boards}}
    doc = longrun.score_run(boards, arms, receipts, OutcomeCache(table_outcome),
                            Protocol(draws=1000, random_seeds=200))
    assert doc["boards"]["scored"] == 1  # the paired standings shrink to the common board
    m = doc["skill"]["arms"]["m"]
    assert m["boards"] == 1 and m["complete"] is False and m["excess_total"] == 8.0
    assert m["verdict"].startswith("PARTIAL (1/4 boards): ")
    assert m["cs_in_sample"]["population"] == 4
    assert doc["skill"]["arms"]["first_row"]["boards"] == 4
