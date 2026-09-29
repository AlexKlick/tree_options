"""The desk forecaster: labels against the parity fixture's KNOWN spot path,
the leak guard on the exit side, the order-bias permutation, strict
parsing, proper scores and the decision rule against hand-computed cases,
and the harness integration (per-policy ask override, report section,
derived arms) with fake transports only. No network, no LLM."""

from __future__ import annotations

import copy
import json
import math
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.unit.test_desk_outcomes import _b, parity_bundle, parity_days, spot_path
from tree_options.desk import forecast, longrun, outcomes
from tree_options.desk import intraday_action_graph as iag
from tree_options.desk.forecast import Label
from tree_options.desk.longrun import Arm, Board, PolicySpec

DAYS = parity_days(12)          # 2026-08-03 .. 2026-08-18
CUTOFF = "2026-08-07"           # TRAIN = the first five sessions
ALIAS = {"QQQ": "U1", "SPY": "U2"}
H = forecast.FORECAST_HORIZONS


@pytest.fixture(scope="module")
def raw() -> dict[str, Any]:
    return parity_bundle(DAYS)


@pytest.fixture(scope="module")
def index(raw: dict[str, Any]) -> outcomes.OutcomeIndex:
    return outcomes.prepare_index(raw)


@pytest.fixture(scope="module")
def labels(index: outcomes.OutcomeIndex) -> dict[str, Any]:
    return forecast.label_table(index)


def _sid(i: int, clock: str) -> str:
    return f"s:{DAYS[i].isoformat()}T{clock}"


def _bps(end: Decimal, start: Decimal) -> float:
    return (float(end) / float(start) - 1.0) * 10_000.0


# ------------------------------------------------------------------ labels


@pytest.mark.parametrize("symbol", ["SPY", "QQQ"])
def test_labels_follow_the_known_spot_path_and_the_outcome_exit_clocks(
        index: outcomes.OutcomeIndex, labels: dict[str, Any], symbol: str) -> None:
    alias = ALIAS[symbol]
    for i in range(len(DAYS)):
        open_i, close_i = spot_path(symbol, i)
        first = index.slots[(DAYS[i], "10:00")]
        last = index.last_slot[DAYS[i]]
        morning = labels[_sid(i, "10:00")][alias]
        # intraday: the 10:45 clock still prices off the morning spot -> a tie is "not higher"
        assert morning["intraday"] == Label(0, 0.0, open_i, open_i, first + 1)
        # eod: this session's last clock (15:15 ET) prices off the close
        assert morning["eod"].y == 1 and morning["eod"].exit_slot == last
        assert morning["eod"].ret_bps == pytest.approx(_bps(close_i, open_i))
        # 11:30 -> 12:15 crosses the morning/close boundary of the fixture
        assert labels[_sid(i, "11:30")][alias]["intraday"].exit == close_i
        if i + 5 < len(DAYS):
            hold = morning["hold:5"]
            close_5 = spot_path(symbol, i + 5)[1]
            assert hold.y == int(close_5 > open_i) and hold.exit == close_5
            assert hold.exit_slot == index.last_slot[DAYS[i + 5]]
        else:
            assert morning["hold:5"] is None  # the target is past the data end
        closing = labels[_sid(i, "15:15")][alias]
        if i + 1 < len(DAYS):
            # from the last clock both intraday and eod exit at the next session's first clock
            open_next = spot_path(symbol, i + 1)[0]
            for horizon in ("intraday", "eod"):
                assert closing[horizon].exit == open_next
                assert closing[horizon].y == int(open_next > close_i)
                assert closing[horizon].exit_slot == last + 1
        else:
            assert closing["intraday"] is None and closing["eod"] is None


def test_exit_spot_without_a_bound_is_the_as_of_parity_spot(index: outcomes.OutcomeIndex) -> None:
    for day in DAYS[:3]:
        for clock in ("10:00", "12:15", "15:15"):
            at = iag._instant(day, clock)
            assert forecast.exit_spot(index, None, at) == outcomes.spot_asof(index, at)


def test_exit_side_never_reads_a_print_at_or_before_the_as_of_instant(
        raw: dict[str, Any]) -> None:
    day = DAYS[1]
    after = datetime(day.year, day.month, day.day, 14, 40, tzinfo=UTC)
    at = datetime(day.year, day.month, day.day, 14, 44, tzinfo=UTC)  # latest grid print 14:30
    assert forecast.exit_spot(outcomes.prepare_index(raw), after, at) == {}

    def planted(minute: int) -> outcomes.OutcomeIndex:
        copy_raw = copy.deepcopy(raw)
        for body in copy_raw["contracts"].values():
            body["results"].append(_b(day, 14, minute, "3.33"))
            body["results"].sort(key=lambda bar: bar["t"])
        return outcomes.prepare_index(copy_raw)

    assert forecast.exit_spot(planted(40), after, at) == {}      # AT the as-of instant: unseen
    assert forecast.exit_spot(planted(41), after, at) != {}      # after it: read (not inert)
    with pytest.raises(ValueError):
        forecast.exit_spot(planted(41), at, at)


def test_exit_slot_rules_and_refusals(index: outcomes.OutcomeIndex) -> None:
    slot = index.slots[(DAYS[0], "10:00")]
    assert forecast.exit_slot(index, slot, "intraday", lambda s: s >= slot + 3) == slot + 3
    # eod prefers the LAST marked clock of the session, walking backward
    last = index.last_slot[DAYS[0]]
    assert forecast.exit_slot(index, slot, "eod", lambda s: s in (slot + 1, slot + 2)) == slot + 2
    assert forecast.exit_slot(index, slot, "eod", lambda s: s == last + 4) == last + 4
    assert forecast.exit_slot(index, slot, "intraday", lambda s: False) is None
    with pytest.raises(ValueError):
        forecast.exit_slot(index, slot, "expiry", lambda s: True)


def test_label_summary_design_effect_hand_case() -> None:
    lab = Label(1, 1.0, Decimal(1), Decimal(1), 0)
    zero = Label(0, -1.0, Decimal(1), Decimal(1), 0)
    table = {"a": {"U1": {h: lab for h in H}, "U2": {h: lab for h in H}},
             "b": {"U1": {h: zero for h in H}, "U2": {h: zero for h in H}},
             "c": {"U1": {h: lab for h in H}, "U2": {h: lab for h in H}}}
    summary = forecast.label_summary(table, {"a": "2026-08-03", "b": "2026-08-04",
                                             "c": "2026-08-05"}, "2026-08-04")
    eod = summary["eod"]
    assert eod["labeled"] == 6 and eod["cross_underlying_corr"] == 1.0
    assert eod["base_rate_train"] == 0.5 and eod["base_rate_test"] == 1.0
    assert eod["design_effect"] == 2.0 and eod["effective_n"] == 3.0  # 3 boards x 1 each
    assert forecast.icc_anova([[1, 1], [0, 0]]) == pytest.approx(1.0)
    assert forecast.icc_anova([[1, 0], [1, 0]]) == pytest.approx(-1.0)


# ------------------------------------------------------- prompt and parsing


def test_permutation_is_seeded_per_board_and_repeat() -> None:
    items = ["U1", "U2", "U3"]
    first = forecast.permutation(7, "s:x", 1, items)
    assert sorted(first) == items and forecast.permutation(7, "s:x", 1, items) == first
    orders = {tuple(forecast.permutation(7, f"s:{k}", r, items)) for k in range(40)
              for r in (1, 2)}
    assert len(orders) == 6  # every order occurs: no fixed display position


def test_prompt_relabels_and_renders_only_the_public_context() -> None:
    context = {"time_of_day": "open", "session_ordinal": 3,
               "underlyings": {"U1": {"ret_1s_pct": "0.10"}, "U2": {"ret_1s_pct": "-0.20"},
                               "U3": {"ret_1s_pct": "0.30"}}}
    messages = forecast.forecast_prompt(context, ["U3", "U1", "U2"])
    content = json.loads(messages[0]["content"])
    assert set(content) == {"task", "context"}
    shown = content["context"]["underlyings"]
    assert list(shown) == ["U1", "U2", "U3"]
    assert [shown[k]["ret_1s_pct"] for k in shown] == ["0.30", "0.10", "-0.20"]
    assert "STRICTLY HIGHER" in content["task"] and "hold:5" in content["task"]


def test_prompt_never_carries_tickers_dates_levels_or_later_prints(
        raw: dict[str, Any], tmp_path: Path) -> None:
    def prompts(bundle: dict[str, Any]) -> dict[str, str]:
        path = tmp_path / "b.json"
        path.write_text(json.dumps(bundle))
        ctx = longrun.PluginContext(config_dir=tmp_path)
        boards = longrun.plugin("boards", "v2")({"bundle": str(path)}, ctx)
        return {b.snapshot: forecast.forecast_prompt(
            b.context or {}, forecast.permutation(1, b.snapshot, 1,
                                                  sorted((b.context or {})["underlyings"])))
            [0]["content"] for b in boards if b.session == DAYS[6].isoformat()}

    before = prompts(raw)
    for text in before.values():
        assert not any(s in text for s in ("SPY", "QQQ", "2026-", "611.", "527."))
    planted = copy.deepcopy(raw)
    later = DAYS[7]
    for body in planted["contracts"].values():
        body["results"].append(_b(later, 14, 5, "9.99"))
        body["results"].sort(key=lambda bar: bar["t"])
    assert prompts(planted) == before


def _reply(values: dict[str, float]) -> dict[str, Any]:
    return {"p_up": {k: {h: v for h in H} for k, v in values.items()}, "note": "x"}


def test_parse_maps_display_labels_back_to_canonical() -> None:
    reply = _reply({"U1": 0.7, "U2": 0.2, "U3": 0.5})
    reply["exp_ret_bps"] = {k: {h: 5 for h in H} for k in ("U1", "U2", "U3")}
    p_up, exp = forecast.parse_forecast(reply, ["U3", "U1", "U2"])
    assert p_up == {"U3": {h: 0.7 for h in H}, "U1": {h: 0.2 for h in H},
                    "U2": {h: 0.5 for h in H}}
    assert exp is not None and exp["U3"]["eod"] == 5.0


@pytest.mark.parametrize("mutate", [
    lambda r: r["p_up"].pop("U2"),                                   # an underlying missing
    lambda r: r["p_up"].update(U9={h: 0.5 for h in H}),              # an unknown label
    lambda r: r["p_up"]["U1"].pop("hold:5"),                         # a horizon missing
    lambda r: r["p_up"]["U1"].update(eod=True),                      # a bool is not a number
    lambda r: r["p_up"]["U1"].update(eod="0.6"),                     # never repaired
    lambda r: r["p_up"]["U1"].update(eod=1.2),
    lambda r: r["p_up"]["U1"].update(eod=math.nan),
    lambda r: r.pop("p_up"),
])
def test_parse_rejects_every_contract_break(mutate: Any) -> None:
    reply = _reply({"U1": 0.6, "U2": 0.4})
    mutate(reply)
    with pytest.raises(forecast.ForecastError):
        forecast.parse_forecast(reply, ["U1", "U2"])


def test_parse_drops_an_invalid_optional_block_only() -> None:
    reply = _reply({"U1": 0.6, "U2": 0.4})
    reply["exp_ret_bps"] = {"U1": {h: 1 for h in H}, "U2": {"eod": "big"}}
    p_up, exp = forecast.parse_forecast(reply, ["U1", "U2"])
    assert exp is None and p_up["U1"]["eod"] == 0.6


# ------------------------------------------------------------------ scoring


def test_brier_log_loss_and_murphy_hand_case() -> None:
    assert forecast.brier(np.array([0.8, 0.3]), np.array([1.0, 0.0])) == pytest.approx(0.065)
    clipped = (-math.log(0.99) - math.log(0.01)) / 2
    assert forecast.log_loss(np.array([1.0, 0.0]), np.array([1.0, 1.0])) == pytest.approx(clipped)
    doc = forecast.murphy(np.array([0.15, 0.15, 0.85, 0.85]), np.array([0.0, 1.0, 1.0, 1.0]))
    assert doc["brier"] == pytest.approx(0.1975)
    assert doc["reliability"] == pytest.approx(0.0725)
    assert doc["resolution"] == pytest.approx(0.0625)
    assert doc["uncertainty"] == pytest.approx(0.1875)
    assert doc["residual"] == pytest.approx(0.0)  # p constant inside every bin
    assert [b["n"] for b in doc["bins"]] == [2, 2] and doc["bins"][0]["bin"] == [0.1, 0.2]
    edge = forecast.murphy(np.array([1.0, 0.1]), np.array([1.0, 0.0]))
    assert [b["bin"] for b in edge["bins"]] == [[0.1, 0.2], [0.9, 1.0]]


def test_block_bootstrap_draws_contiguous_blocks() -> None:
    rows = np.array([[1.0], [2.0], [3.0]])
    whole = forecast.block_bootstrap(rows, 3, 200, 1)
    assert set(whole[:, 0]) == {6.0}
    # blocks of 2 from 3 sessions: starts {0,1} x {0,1}, truncated to 3 rows
    assert set(forecast.block_bootstrap(rows, 2, 2000, 1)[:, 0]) == {4.0, 5.0, 6.0, 7.0}
    single = forecast.block_bootstrap(rows, 1, 4000, 1)[:, 0]
    assert set(single) <= {3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0}
    assert single.mean() == pytest.approx(6.0, abs=0.1)


def test_momentum_fit_and_climatology_hand_cases() -> None:
    k = forecast.fit_momentum({"eod": [(1, 1), (1, 0), (-1, 0), (0, 1)], "intraday": [(1, 1)] * 3,
                               "hold:5": [(0, 1)]})
    assert k == {"eod": pytest.approx(1 / 6, abs=1e-6), "intraday": 0.45, "hold:5": 0.0}
    one, zero = Label(1, 1.0, Decimal(1), Decimal(1), 0), Label(0, 0.0, Decimal(1), Decimal(1), 0)
    table = {"a": {"U1": {"eod": one}}, "b": {"U1": {"eod": zero}}, "c": {"U1": {"eod": one}},
             "d": {"U1": {"eod": zero}}}
    sessions = {"a": "2026-08-03", "b": "2026-08-04", "c": "2026-08-05", "d": "2026-08-20"}
    clim = forecast.fit_climatology(table, sessions, "2026-08-07", ("eod", "intraday"))
    assert clim == {"U1": {"eod": pytest.approx(2 / 3, abs=1e-6), "intraday": 0.5}}
    assert forecast.momentum_sign({"underlyings": {"U1": {"ret_5s_pct": "-0.40"}}}, "U1") == -1
    assert forecast.momentum_sign({"underlyings": {"U1": {"ret_5s_pct": None}}}, "U1") == 0


def test_score_set_skill_against_the_baselines_hand_case() -> None:
    p = np.array([0.9, 0.1, 0.9, 0.1])
    y = np.array([1.0, 0.0, 1.0, 0.0])
    half = np.full(4, 0.5)
    doc = forecast.score_set(p, y, half, half, np.array([0, 0, 1, 1]), block=1, draws=1000,
                             seed=3)
    assert doc["brier"] == pytest.approx(0.01) and doc["brier_clim"] == pytest.approx(0.25)
    assert doc["bss_clim"] == pytest.approx(0.96) and doc["bss_mom"] == pytest.approx(0.96)
    assert doc["hit_rate"] == 1.0 and doc["sessions"] == 2
    assert doc["ci95"]["brier"] == [pytest.approx(0.01), pytest.approx(0.01)]


# ------------------------------------------------------------------ decision

PAYOFF = {"put_credit": {"intraday": {"a": 0, "b": 0}, "eod": {"a": 0.8, "b": 0.9},
                         "hold:5": {"a": 0, "b": 0}},
          "call_credit": {"intraday": {"a": 0, "b": 0}, "eod": {"a": 0.5, "b": 0.5},
                          "hold:5": {"a": 0, "b": 0}}}
ROWS = [{"id": "A", "structure": "put_credit", "direction": "bullish", "underlying": "U1",
         "max_gain": "100", "max_loss": "200"},
        {"id": "B", "structure": "call_credit", "direction": "bearish", "underlying": "U2",
         "max_gain": "150", "max_loss": "150"}]
VIEWS = {"U1": {"intraday": 0.5, "eod": 0.7, "hold:5": 0.5},
         "U2": {"intraday": 0.5, "eod": 0.3, "hold:5": 0.5}}


def test_cost_is_the_env_v2_round_trip() -> None:
    assert forecast.round_trip_cost() == pytest.approx(14.60)


def test_decide_ev_hand_case() -> None:
    # A: 0.7*0.8*100 - 0.3*0.9*200 - 14.6 = -12.6 ; B: 0.7*0.5*150 - 0.3*0.5*150 - 14.6 = 15.4
    assert forecast.expected_net(ROWS[0], 0.7, PAYOFF["put_credit"]["eod"], 14.6) \
        == pytest.approx(-12.6)
    choice, horizon, detail = forecast.decide_ev(ROWS, VIEWS, PAYOFF, 0.0, 14.6)
    assert (choice, horizon, detail["ev"]) == ("B", "eod", pytest.approx(15.4))
    # the edge gate is inclusive and symmetric: |0.3 - 0.5| passes tau = 0.2
    assert forecast.decide_ev(ROWS, VIEWS, PAYOFF, 0.2, 14.6)[0] == "B"
    assert forecast.decide_ev(ROWS, VIEWS, PAYOFF, 0.25, 14.6)[:2] == (None, None)
    assert forecast.decide_ev(ROWS, VIEWS, PAYOFF, None, 14.6)[:2] == (None, None)
    no_edge = forecast.decide_ev(ROWS, VIEWS, PAYOFF, 0.0, 40.0)  # the cost eats every EV
    assert no_edge[:2] == (None, None) and no_edge[2]["reason"] == "ev_not_positive"


def test_the_edge_gate_is_symmetric_around_one_half() -> None:
    # 0.5 - 0.3 is exactly 0.2 in binary floating point, 0.7 - 0.5 is not
    assert forecast.edge(0.7) == forecast.edge(0.3) == 0.2
    payoff = {"put_credit": {h: {"a": 0.8, "b": 0.3} for h in H}}
    views = {"U1": {"intraday": 0.5, "eod": 0.7, "hold:5": 0.5}}
    # EV = 0.7*0.8*100 - 0.3*0.3*200 - 14.6 = 23.4
    choice, horizon, detail = forecast.decide_ev(ROWS[:1], views, payoff, 0.2, 14.6)
    assert (choice, horizon, detail["ev"]) == ("A", "eod", pytest.approx(23.4))


def _board(rows: list[dict[str, Any]]) -> Board:
    return Board("s:2026-08-03T10:00", "2026-08-03", "10:00", rows)


def test_decide_direction_feeds_the_harness_row_choice() -> None:
    rows = [{"id": "r1", "underlying": "U1", "structure": "put_credit"},
            {"id": "r2", "underlying": "U1", "structure": "call_credit"},
            {"id": "r3", "underlying": "U2", "structure": "call_credit"},
            {"id": "r4", "underlying": "U2", "structure": "put_debit"}]
    views = {"U1": {"intraday": 0.52, "eod": 0.55, "hold:5": 0.6},
             "U2": {"intraday": 0.3, "eod": 0.5, "hold:5": 0.5}}
    assert forecast.decide_direction(_board(rows), views, 0.05)[:2] == ("r3", "intraday")
    # no bearish U2 row: the next strongest view (U1 hold:5, bullish) takes the first U1 bull row
    bullish_only = [r for r in rows if r["underlying"] == "U1"]
    assert forecast.decide_direction(_board(bullish_only), views, 0.05)[:2] == ("r1", "hold:5")
    assert forecast.decide_direction(_board(rows), views, 0.21)[:2] == (None, None)


def test_fit_tau_maximizes_train_net_and_refuses_a_losing_train() -> None:
    boards = [Board(f"s:2026-08-0{i}T10:00", f"2026-08-0{i}", "10:00", [{"id": "x"}])
              for i in (3, 4, 5)]
    edges = {b.snapshot: e for b, e in zip(boards, (0.10, 0.05, 0.02), strict=True)}
    nets = {b.snapshot: n for b, n in zip(boards, (10.0, -20.0, 5.0), strict=True)}

    def decide(board: Board, p_up: Any, tau: float | None) -> tuple[Any, Any, dict[str, Any]]:
        ok = tau is not None and edges[board.snapshot] >= tau
        return ("x", "eod", {}) if ok else (None, None, {})

    grid = (0.0, 0.02, 0.05, 0.1, 0.2)
    fit = forecast.fit_tau([(b, {}) for b in boards], decide, lambda s, c, h: nets[s], grid)
    assert fit["tau"] == 0.1 and fit["train_net"] == 10.0
    assert [r["train_net"] for r in fit["grid"]] == [-5.0, -5.0, -10.0, 10.0, 0.0]
    losing = forecast.fit_tau([(b, {}) for b in boards], decide, lambda s, c, h: -1.0, grid)
    assert losing["tau"] is None


# ------------------------------------------------------------ harness seams


def test_policy_ask_dispatches_per_policy_and_passes_the_arm() -> None:
    seen: list[Any] = []

    def plain(spec: PolicySpec, board: Board) -> tuple[Any, ...]:
        seen.append(("plain", spec.name))
        return None, None, "p"

    class WithArm:
        wants_arm = True

        def __call__(self, spec: PolicySpec, board: Board, arm: Arm) -> tuple[Any, ...]:
            seen.append(("arm", spec.name, arm.repeat))
            return board.ids[0], "eod", "a", {"forecast": {"p": 1}, "choice": "tampered"}

    ask = longrun.PolicyAsk(plain, {"fc": WithArm()})
    board = _board([{"id": "r1"}])
    fc, base = PolicySpec("fc", "model", repeats=2), PolicySpec("base", "model")
    rec = longrun.decide(Arm("fc#2", fc, 2), board, ask)
    assert rec["ok"] and rec["choice"] == "r1" and rec["forecast"] == {"p": 1}
    assert longrun.decide(Arm("base", base, 1), board, ask)["choice"] is None
    assert seen == [("arm", "fc", 2), ("plain", "base")]
    orphan = longrun.decide(Arm("base", base, 1), board, longrun.PolicyAsk(None, {}))
    assert orphan["ok"] is False and "no ask plug-in" in orphan["error"]


def test_a_failing_report_is_recorded_and_never_blocks_the_digest(tmp_path: Path) -> None:
    def broken(**_: Any) -> dict[str, Any]:
        raise RuntimeError("report exploded")

    boards = [Board(f"s:2026-06-0{d}T10:00", f"2026-06-0{d}", "10:00", [{"id": "w"}])
              for d in (1, 2)]
    result = longrun.run_longrun(
        tmp_path / "run", boards=boards, policies=[PolicySpec("m", "model"),
                                                   *longrun.builtin_controls()],
        outcome=lambda s, c, h: {"gross": 1.0, "net": 1.0},
        ask=lambda spec, board: ("w", None, ""), quota_ok=lambda: (True, "ok"),
        protocol=longrun.Protocol(draws=1000, random_seeds=200),
        reports={"broken": broken})
    doc = json.loads((Path(result["run_dir"]) / "digest.json").read_text())
    assert result["status"] == "finished"
    assert doc["reports"]["broken"]["status"] == "failed"
    assert "## Report: broken" in (Path(result["run_dir"]) / "digest.md").read_text()


# ---------------------------------------------------------------- end to end


class ForecastTransport:
    """Answers the forecast contract. ``features``: p from the SHOWN
    underlying's own features (position-invariant); ``first``: 0.7 for the
    first-listed underlying, 0.4 for the rest (pure position bias)."""

    def __init__(self, mode: str = "features") -> None:
        self.mode = mode
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str],
                 timeout: float) -> tuple[int, bytes]:
        payload = json.loads(body)
        self.calls.append(payload)
        shown = json.loads(payload["messages"][0]["content"])["context"]["underlyings"]
        p_up = {}
        for k, (label, feats) in enumerate(shown.items()):
            if self.mode == "first":
                p = 0.7 if k == 0 else 0.4
            else:
                r = feats.get("ret_1s_pct")
                p = 0.5 if r is None else 0.5 + max(-0.3, min(0.3, float(r) * 2))
            p_up[label] = {h: round(p, 4) for h in H}
        reply = {"p_up": p_up, "note": "fake"}
        envelope = {"choices": [{"message": {"content": json.dumps(reply)},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 400, "completion_tokens": 900}}
        return 200, json.dumps(envelope).encode()


def _config(tmp_path: Path, raw: dict[str, Any], *, tau: float = 0.0) -> Path:
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(raw))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "out_root": str(tmp_path / "out"), "incumbent": "fc",
        "boards": {"plugin": "v2", "bundle": str(bundle)},
        "outcome": {"plugin": "v2", "sync": 2},
        "quota": {"plugin": "always"}, "control_horizon": "intraday",
        "protocol": {"draws": 1000, "random_seeds": 200, "cutoff": CUTOFF},
        "policies": [{"name": "fc", "kind": "model", "repeats": 2,
                      "ask": {"plugin": "forecast", "provider": "minimax-flash",
                              "decision": "ev", "tau": tau, "cutoff": CUTOFF}}],
        "reports": [{"plugin": "forecast", "source": "fc", "cutoff": CUTOFF, "draws": 1000}]}))
    return config


@pytest.fixture
def key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_MINIMAX2", "test-key-material-2")


def _receipts(run_dir: Path, arm: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in
            longrun.receipts_path(run_dir, arm).read_text().splitlines() if line.strip()]


def test_forecaster_end_to_end_on_the_paired_scoreboard(
        tmp_path: Path, raw: dict[str, Any], labels: dict[str, Any], key_env: None) -> None:
    transport = ForecastTransport("features")
    result = longrun.run_from_config(_config(tmp_path, raw), shared={"transport": transport})
    assert result["status"] == "finished" and result["complete"] is True
    run_dir = Path(result["run_dir"])
    boards = [json.loads(line) for line in (run_dir / "boards.jsonl").read_text().splitlines()]
    assert len(transport.calls) == 2 * len(boards)
    assert {c["model"] for c in transport.calls} == {"MiniMax-M3.1-Flash-Preview"}
    doc = json.loads((run_dir / "digest.json").read_text())
    section = doc["reports"]["forecast"]
    assert section["status"] == "ok" and section["boards_scored"] == len(boards)
    # the receipts alone recompute the scores and the in-arm decisions
    recs = {r["snapshot"]: r for r in _receipts(run_dir, "fc#1")}
    rows_by = {b["snapshot"]: b["rows"] for b in boards}
    se: list[float] = []
    for snapshot, rec in recs.items():
        fc = rec["forecast"]
        assert fc["schema"] == forecast.FORECAST_SCHEMA and sorted(fc["perm"]) == ["U1", "U2"]
        choice, horizon, _ = forecast.decide_ev(rows_by[snapshot], fc["p_up"],
                                                section["payoff_map"], 0.0, 14.6)
        assert (rec["choice"], rec["horizon"]) == (choice, horizon)
        if snapshot.split("T")[0].removeprefix("s:") > CUTOFF:
            for alias, by_h in fc["p_up"].items():
                for h, p in by_h.items():
                    label = labels[snapshot][alias][h]
                    if label is not None:
                        se.append((p - label.y) ** 2)
    test_all = section["scores"]["fc#1"]["test"]["all"]
    assert test_all["n"] == len(se) and test_all["brier"] == pytest.approx(np.mean(se), abs=1e-6)
    # a position-invariant forecaster: both repeats agree although the orders differ
    aa = section["aa_order"]
    assert aa["status"] == "ok" and aa["mean_abs_diff"] == 0.0 and not aa["order_sensitive"]
    assert 0 < aa["same_order_share"] < 1
    # derived arms (tau fit on TRAIN) sit on the same paired scoreboard
    arms = {row["arm"] for row in doc["standings"]}
    assert {"fc#1", "fc#2", "fc.ev-fit#1", "fc.ev-fit#2", "fc.dir-fit#1",
            "fc.dir-fit#2"} <= arms
    assert longrun.receipts_path(run_dir, "fc.ev-fit#1").is_file()
    for fit in section["derived"]["fc.ev-fit"]["fits"].values():
        assert fit["train_boards"] == sum(1 for b in boards if b["session"] <= CUTOFF)
    assert doc["promotion"]["promoted"] is False
    assert "## Report: forecast" in (run_dir / "digest.md").read_text()


def test_the_order_check_catches_a_position_biased_forecaster(
        tmp_path: Path, raw: dict[str, Any], key_env: None) -> None:
    result = longrun.run_from_config(_config(tmp_path, raw), shared={
        "transport": ForecastTransport("first")}, limit=24)
    section = json.loads((Path(result["run_dir"]) / "digest.json").read_text())[
        "reports"]["forecast"]
    aa = section["aa_order"]
    assert aa["mean_abs_diff_same_order"] == 0.0
    assert aa["mean_abs_diff_other_order"] == pytest.approx(0.3)
    bias = {row["shown_as"]: row["mean_p"] for row in section["position_bias"]["fc#1"]}
    assert bias == {"U1": 0.7, "U2": 0.4}


def test_config_refuses_a_model_policy_without_any_ask(tmp_path: Path, raw: dict[str, Any],
                                                       key_env: None) -> None:
    config = _config(tmp_path, raw)
    cfg = json.loads(config.read_text())
    cfg["policies"].append({"name": "orphan", "kind": "model"})
    config.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="orphan"):
        longrun.run_from_config(config, shared={"transport": ForecastTransport()}, limit=2)


def test_smoke_reports_transport_stats_and_respects_the_quota(
        tmp_path: Path, raw: dict[str, Any], index: outcomes.OutcomeIndex,
        key_env: None) -> None:
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(raw))
    table = tmp_path / "table.jsonl"
    with table.open("w") as stream:
        for row in outcomes.outcome_table(index, costs=outcomes.CostModel(), leg_sync_minutes=2,
                                          modes=("intraday", "eod", "hold:5", "expiry")):
            stream.write(json.dumps(row, default=str) + "\n")
    refused = forecast.smoke(bundle=bundle, table=table, out_root=tmp_path / "s",
                             quota=lambda: (False, "left=1 planned=50"))
    assert refused["status"] == "refused"
    fake = ForecastTransport()
    summary = forecast.smoke(bundle=bundle, table=table, out_root=tmp_path / "s", boards=6,
                             concurrency=2, transport=fake, quota=lambda: (True, "left=90"),
                             now=datetime(2026, 9, 29, tzinfo=UTC))
    assert summary["status"] == "finished" and summary["calls"] == 12
    assert summary["parse_rate"] == 1.0 and summary["truncated"] == 0
    assert summary["tokens"]["completion_mean"] == 900.0
    calls = (Path(summary["run_dir"]) / "transport-calls.jsonl").read_text()
    assert "test-key-material-2" not in calls and "Authorization" not in calls


def test_label_table_dates_are_sessions(index: outcomes.OutcomeIndex,
                                        labels: dict[str, Any]) -> None:
    assert len(labels) == len(index.timeline)
    assert {s.split("T")[0].removeprefix("s:") for s in labels} == {
        d.isoformat() for d in DAYS}
    assert date.fromisoformat(CUTOFF) in DAYS
