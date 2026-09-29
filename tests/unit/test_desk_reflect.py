"""desk.reflect: train-split dossiers, the theorist panel and its contract.

Oracles are hand-computed from the literal fixture numbers below, never
from the module's own helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, ClassVar

import pytest

from tree_options.desk import longrun, reflect

CUTOFF = "2026-08-14"
B1, B2, B3 = "s:2026-08-13T10:00", "s:2026-08-14T10:00", "s:2026-08-17T10:00"
SESSIONS = {B1: "2026-08-13", B2: "2026-08-14", B3: "2026-08-17"}
ROWS = {  # (id, structure, direction)
    B1: [("a1", "put_credit", "bullish"), ("a2", "call_credit", "bearish"),
         ("a3", "put_debit", "bearish")],
    B2: [("b1", "call_debit", "bullish"), ("b2", "put_debit", "bearish"),
         ("b3", "put_credit", "bullish")],
    B3: [("c1", "put_credit", "bullish"), ("c2", "call_credit", "bearish"),
         ("c3", "put_debit", "bearish")],
}
NO_FILL = None
# (snapshot, candidate) -> {mode: (net, exit_at) | NO_FILL}
NETS: dict[tuple[str, str], dict[str, Any]] = {
    (B1, "a1"): {"intraday": (10.0, "2026-08-13T15:00:00+00:00"),
                 "eod": (20.0, "2026-08-13T19:30:00+00:00"),
                 "hold:5": (888888.0, "2026-08-20T19:00:00+00:00"),  # exits after the cutoff
                 "expiry": (40.0, "2026-08-14T19:30:00+00:00"),
                 "hold:1": (777777.0, "2026-08-14T19:30:00+00:00")},  # not a v2 horizon
    (B1, "a2"): {"intraday": (-5.0, "2026-08-13T15:00:00+00:00"),
                 "eod": (-15.0, "2026-08-15T02:00:00+00:00"),  # 22:00 ET on the cutoff day
                 "hold:5": (888888.0, "2026-08-15T05:00:00+00:00"),  # 01:00 ET the day after
                 "expiry": (-60.0, "2026-08-14T20:00:00+00:00")},
    (B1, "a3"): dict.fromkeys(("intraday", "eod", "hold:5", "expiry"), NO_FILL),
    (B2, "b1"): {"intraday": (5.0, "2026-08-14T15:00:00+00:00"),
                 "eod": (7.0, "2026-08-14T19:30:00+00:00"),
                 "hold:5": (888888.0, "2026-08-21T19:00:00+00:00"),
                 "expiry": (888888.0, "2026-09-18T19:00:00+00:00")},
    (B2, "b2"): {"intraday": (-3.0, "2026-08-14T15:00:00+00:00"),
                 "eod": (-8.0, "2026-08-14T19:30:00+00:00"),
                 "hold:5": (888888.0, "2026-08-21T19:00:00+00:00"),
                 "expiry": (888888.0, "2026-09-18T19:00:00+00:00")},
    (B2, "b3"): {"intraday": (2.0, "2026-08-14T15:00:00+00:00"),
                 "eod": (1.0, "2026-08-14T19:30:00+00:00"),
                 "hold:5": (888888.0, "2026-08-21T19:00:00+00:00"),
                 "expiry": (888888.0, "2026-09-18T19:00:00+00:00")},
    **{(B3, cid): {m: (999999.0, "2026-08-17T19:00:00+00:00")
                   for m in ("intraday", "eod", "hold:5", "expiry")}
       for cid in ("c1", "c2", "c3")},
}
ROLE = "paper-trading policy choosing ONE defined-risk option spread board row, or skipping."
M_B_PROMPT = f"You are a test {ROLE} Prefer the eod horizon and skip when unsure."


def _row(cid: str, structure: str, direction: str) -> dict[str, Any]:
    return {"id": cid, "structure": structure, "direction": direction, "underlying": "U1",
            "width": "5", "observed_premium": "1.20", "max_loss": "380.00",
            "max_gain": "120.00", "reward_risk": "0.32", "dte": 12,
            "short_strike_moneyness_pct": "-1.10", "long_recent_move": "0.01",
            "short_recent_move": "0.02"}


def _receipt(arm: str, policy: str, snapshot: str, choice: str | None = None,
             horizon: str | None = None, note: str = "n", ok: bool = True) -> dict[str, Any]:
    return {"schema": longrun.RECEIPT_SCHEMA, "arm": arm, "policy": policy, "kind": "model",
            "snapshot": snapshot, "session": SESSIONS[snapshot], "ok": ok, "choice": choice,
            "horizon": horizon, "note": note, "at": "2026-09-29T01:00:00+00:00"}


def make_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "20260929T000000Z"
    (run_dir / "receipts").mkdir(parents=True)
    table = tmp_path / "table.jsonl"
    with table.open("w") as stream:
        for (snapshot, cid), modes in NETS.items():
            for mode, cell in modes.items():
                base = {"snapshot": snapshot, "candidate_id": cid, "exit_mode": mode,
                        "underlying": "QQQ"}
                if cell is NO_FILL:
                    base.update(net=None, gross=None, exit_at=None, status="no_fill")
                else:
                    base.update(net=f"{cell[0]:.2f}", gross="0", exit_at=cell[1],
                                status="closed")
                stream.write(json.dumps(base) + "\n")
    (run_dir / "config.json").write_text(json.dumps({
        "boards": {"plugin": "v2"}, "outcome": {"plugin": "v2", "table": str(table)},
        "quota": {"plugin": "always"}, "protocol": {"cutoff": CUTOFF},
        "policies": [{"name": "m-a", "kind": "model", "repeats": 2},
                     {"name": "m-b", "kind": "model", "prompt": M_B_PROMPT},
                     {"name": "r", "kind": "rule", "builtin": "no_trade"}]}))
    with (run_dir / "boards.jsonl").open("w") as stream:
        for i, (snapshot, rows) in enumerate(ROWS.items(), start=1):
            stream.write(json.dumps({
                "snapshot": snapshot, "session": SESSIONS[snapshot], "clock": "10:00",
                "rows": [_row(*r) for r in rows],
                "context": {"time_of_day": "open", "session_ordinal": i,
                            "underlyings": {"U1": {"ret_1s_pct": "0.10", "ret_5s_pct": "1.00",
                                                   "ret_20s_pct": "2.00",
                                                   "rv_20s_ann_pct": "20.0"}}}}) + "\n")
    receipts = {
        "m-a#1": [_receipt("m-a#1", "m-a", B1, "a1", "eod"),
                  _receipt("m-a#1", "m-a", B2, note="nothing clean"),
                  _receipt("m-a#1", "m-a", B3, "c1", "eod", note="LEAKNOTE-1")],
        "m-a#2": [_receipt("m-a#2", "m-a", B1, "a2", "intraday"),
                  _receipt("m-a#2", "m-a", B2, "b2", "hold:5"),
                  _receipt("m-a#2", "m-a", B3, note="LEAKNOTE-2")],
        "m-b": [_receipt("m-b", "m-b", B1, note="skip"),
                _receipt("m-b", "m-b", B2, ok=False),
                _receipt("m-b", "m-b", B3, "c2", "expiry", note="LEAKNOTE-3")],
        "r": [_receipt("r", "r", B1, note="RULENOTE")],
    }
    for arm, recs in receipts.items():
        longrun.receipts_path(run_dir, arm).write_text(
            "".join(json.dumps(r) + "\n" for r in recs))
    return run_dir


def _view_and_table(run_dir: Path, cutoff: str = CUTOFF
                    ) -> tuple[reflect.RunView, reflect.OutcomeTable]:
    view = reflect.load_run(run_dir, cutoff)
    table_path = Path(json.loads((run_dir / "config.json").read_text())["outcome"]["table"])
    return view, reflect.load_outcome_table(table_path, set(view.boards))


def _dossier(pack: dict[str, Any], policy: str) -> dict[str, Any]:
    return next(d for d in pack["dossiers"] if d["policy"] == policy)


# ------------------------------------------------------------------ leakage


def test_no_receipt_or_outcome_after_the_cutoff_enters_a_dossier(tmp_path: Path) -> None:
    run_dir = make_run(tmp_path)
    view, table = _view_and_table(run_dir)
    assert set(view.boards) == {B1, B2}  # the held-out board is never retained
    assert all(s in (B1, B2) for recs in view.receipts.values() for s in recs)
    assert all(key[0] in (B1, B2) for key in table.cells)
    pack, meta = reflect.build_pack(view, table, samples_per_arm=50)
    dump = json.dumps(pack)
    for marker in ("LEAKNOTE", "999999", "888888", "777777", '"c1"', "RULENOTE"):
        assert marker not in dump, marker
    assert not re.search(r"\d{4}-\d{2}-\d{2}", dump)  # no date anywhere
    assert not re.search(r"s:\d", dump) and "QQQ" not in dump  # no snapshot id, no ticker
    assert [d["policy"] for d in pack["dossiers"]] == ["m-a", "m-b"]  # model policies only
    assert meta["cutoff"] == CUTOFF and meta["train_last_session"] == CUTOFF
    # positive control: move the split past the held-out board and it leaks
    late_view, late_table = _view_and_table(run_dir, "2026-08-31")
    late = json.dumps(reflect.build_pack(late_view, late_table, samples_per_arm=50)[0])
    assert "999999" in late and "888888" in late and "LEAKNOTE" in late


def test_a_receipt_dated_after_the_cutoff_is_dropped_even_on_a_train_board(
        tmp_path: Path) -> None:
    run_dir = make_run(tmp_path)
    late = _receipt("m-b", "m-b", B1, "a1", "eod", note="LEAKNOTE-4")
    late["session"] = "2026-08-17"  # disagrees with its (train) board
    with longrun.receipts_path(run_dir, "m-b").open("a") as stream:
        stream.write(json.dumps(late) + "\n")
    view, table = _view_and_table(run_dir)
    assert B1 not in view.receipts["m-b"]
    assert "LEAKNOTE" not in json.dumps(reflect.build_pack(view, table, samples_per_arm=50)[0])


def test_outcomes_are_withheld_unless_realized_by_the_cutoff_in_et(tmp_path: Path) -> None:
    view, table = _view_and_table(make_run(tmp_path))
    cut = view.cutoff
    assert reflect.cell(table, B1, "a1", "hold:5", cut) == reflect.WITHHELD
    assert reflect.cell(table, B1, "a1", "expiry", cut) == 40.0
    assert reflect.cell(table, B1, "a2", "eod", cut) == -15.0  # 02:00Z = 22:00 ET on the day
    assert reflect.cell(table, B1, "a2", "hold:5", cut) == reflect.WITHHELD  # 01:00 ET next day
    assert reflect.cell(table, B1, "a3", "eod", cut) is None  # no fill
    assert reflect.cell(table, B1, "zz", "eod", cut) == reflect.WITHHELD  # absent
    assert (B1, "a1", "hold:1") not in table.cells  # only the v2 horizons are loaded
    assert table.tickers == frozenset({"QQQ"})


# ---------------------------------------------------------------- hindsight


def test_decision_hindsight_matches_hand_computation(tmp_path: Path) -> None:
    view, table = _view_and_table(make_run(tmp_path))
    policy = next(p for p in view.policies if p.name == "m-a")
    by = {(d.arm, d.board.snapshot): d for d in reflect.policy_decisions(view, policy, table)}
    # B1 options (no fill = 0, withheld out): 10,20,40,-5,-15,-60,0,0,0,0 -> -10/10
    win = by[("m-a#1", B1)]
    assert (win.row, win.horizon, win.realized, win.stratum) == (0, "eod", 20.0, "win")
    assert win.board_mean == pytest.approx(-1.0)
    assert win.board_mean_h == pytest.approx((20.0 - 15.0 + 0.0) / 3)
    assert win.best == (0, "expiry", 40.0) and win.regret == pytest.approx(20.0)
    loss = by[("m-a#2", B1)]
    assert (loss.row, loss.realized, loss.stratum) == (1, -5.0, "loss")
    assert loss.board_mean_h == pytest.approx((10.0 - 5.0 + 0.0) / 3)
    assert loss.regret == pytest.approx(45.0)
    # B2 options: 5,7,-3,-8,2,1 -> 4/6 > 0: a skip there would have won
    skip = by[("m-a#1", B2)]
    assert skip.stratum == "skip_would_win" and skip.board_mean == pytest.approx(4 / 6)
    assert skip.best == (0, "eod", 7.0) and skip.regret == pytest.approx(7.0)
    held = by[("m-a#2", B2)]
    assert held.stratum == "unevaluable" and held.realized is None and held.regret is None
    m_b = next(p for p in view.policies if p.name == "m-b")
    (only,) = reflect.policy_decisions(view, m_b, table)
    assert only.stratum == "skip_right" and only.regret == pytest.approx(40.0)


def test_aggregate_behavior_and_universe(tmp_path: Path) -> None:
    view, table = _view_and_table(make_run(tmp_path))
    pack, _ = reflect.build_pack(view, table, samples_per_arm=8)
    agg = _dossier(pack, "m-a")["aggregate"]
    assert (agg["train_boards"], agg["decided"], agg["entered"], agg["entry_rate"]) == \
        (2, 4, 3, 0.75)
    assert agg["horizon_mix"] == {"eod": 1, "hold:5": 1, "intraday": 1}
    assert agg["direction_mix"] == {"bearish": 2, "bullish": 1}
    assert agg["structure_mix"] == {"call_credit": 1, "put_credit": 1, "put_debit": 1}
    assert agg["row_position"] == {"row0_share": 0.333, "uniform_row0_share": 0.333,
                                   "mean_relative_position": 0.333}
    out = agg["outcomes"]
    assert (out["evaluated_entries"], out["no_fill_entries"], out["withheld_entries"]) == \
        (2, 0, 1)
    assert (out["net_total"], out["net_mean"], out["win_rate"]) == (15.0, 7.5, 0.5)
    assert out["pick_vs_board_mean_same_horizon"] == pytest.approx(
        round((20 - 5 / 3) + (-5 - 5 / 3), 1))
    assert out["random_entry_mean_on_skipped_boards"] == 0.7
    assert out["regret_mean"] == pytest.approx(round((20 + 7 + 45) / 3, 1))
    assert agg["strata"] == {"win": 1, "loss": 1, "skip_would_win": 1, "skip_right": 0,
                             "unevaluable": 1}
    assert agg["pnl_by_structure_horizon"] == {
        "call_credit|intraday": {"n": 1, "net": -5.0, "mean": -5.0, "win_rate": 0.0},
        "put_credit|eod": {"n": 1, "net": 20.0, "mean": 20.0, "win_rate": 1.0}}
    assert agg["entry_rate_by_repeat"] == {"m-a#1": 0.5, "m-a#2": 1.0}
    m_b = _dossier(pack, "m-b")
    assert m_b["aggregate"]["failed_receipts"] == 1 and m_b["aggregate"]["decided"] == 1
    assert m_b["policy_sentence"] == M_B_PROMPT and not m_b["default_sentence"]
    assert _dossier(pack, "m-a")["default_sentence"]
    # expiry options: a1 +40, a2 -60 realized, a3 no fill, b1..b3 withheld -> 3 of 6
    assert list(pack)[:2] == ["schema", "caveat"]  # the header, before any statistic
    assert "withheld" in pack["caveat"] and "50% of the expiry options" in pack["caveat"]
    assert "short-dated" in pack["caveat"]
    uni = pack["universe"]
    assert (uni["boards"], uni["sessions"]) == (2, 2)
    assert uni["random_row_baseline_mean_per_board"] == pytest.approx(
        round((-1.0 + 4 / 6) / 2, 2))
    assert uni["by_structure_horizon"]["put_credit|expiry"] == {
        "realized": 1, "no_fill": 0, "withheld": 1, "mean_net": 40.0, "win_rate": 1.0}
    assert uni["by_structure_horizon"]["put_debit|intraday"] == {
        "realized": 1, "no_fill": 1, "withheld": 0, "mean_net": -3.0, "win_rate": 0.0}


def test_rendered_sample_is_the_board_as_seen_without_dates(tmp_path: Path) -> None:
    view, table = _view_and_table(make_run(tmp_path))
    pack, _ = reflect.build_pack(view, table, samples_per_arm=8)
    samples = _dossier(pack, "m-a")["samples"]
    assert [s["stratum"] for s in samples] == ["win", "loss", "skip_would_win"]
    win = samples[0]
    assert win["board"]["cols"][0] == "id" and win["board"]["rows"][0][0] == "a1"
    assert win["decision"] == {"row": 0, "horizon": "eod"}
    assert win["net_by_row"] == [[10.0, 20.0, "x", 40.0], [-5.0, -15.0, "x", -60.0],
                                 [None, None, None, None]]
    assert win["context"]["underlyings"]["U1"]["ret_5s_pct"] == "1.00"
    assert [s["id"] for d in pack["dossiers"] for s in d["samples"]] == ["d1", "d2", "d3", "d4"]


def test_stratified_sample_spreads_strata_and_budget_trims(tmp_path: Path) -> None:
    view, table = _view_and_table(make_run(tmp_path))
    policy = next(p for p in view.policies if p.name == "m-a")
    decisions = reflect.policy_decisions(view, policy, table)
    assert [d.stratum for d in reflect.stratified_sample(decisions, 2, 1)] == ["win", "loss"]
    assert len(reflect.stratified_sample(decisions, 10, 1)) == 3  # unevaluable never sampled
    assert reflect.stratified_sample(decisions, 0, 1) == []
    full, _ = reflect.build_pack(view, table, samples_per_arm=8)
    budget = reflect.estimate_tokens(full) - 1
    trimmed, meta = reflect.build_pack(view, table, samples_per_arm=8, max_tokens=budget)
    assert meta["trimmed_samples"] == 1 and meta["pack_tokens_est"] <= budget
    assert len(_dossier(trimmed, "m-a")["samples"]) == 2  # the largest dossier gave one up
    none, meta0 = reflect.build_pack(view, table, samples_per_arm=8, max_tokens=1)
    assert meta0["trimmed_samples"] == 4 and all(not d["samples"] for d in none["dossiers"])
    with pytest.raises(ValueError, match="unknown policy"):
        reflect.build_pack(view, table, only=["nope"])


# ----------------------------------------------------------------- contract

GOOD = {
    "name": "Trend Hold",
    "hypothesis": ("Bullish rows held hold:5 on aliases with positive 20-session returns beat "
                   "the random-row baseline because multi-session drift persists."),
    "evidence": "m31-base lost -22.4 per entry at hold:5 while bullish|hold:5 averaged +49.1.",
    "prompt": (f"You are a trend-following {ROLE} Prefer bullish rows when ret_20s_pct is "
               "positive and hold them hold:5; skip otherwise."),
    "expected_effect": "Entry near 40%, mostly bullish hold:5; falsified if net vs random <= 0.",
}


def test_validate_reply_accepts_a_clean_proposal() -> None:
    proposal, reasons = reflect.validate_reply(GOOD)
    assert reasons == [] and proposal is not None and proposal.prompt == GOOD["prompt"]
    an, why = reflect.validate_reply(
        {**GOOD, "prompt": f"You are an adaptive trend {ROLE} Enter bullish rows at hold:5."})
    assert an is not None, why
    # the role sentence the validator demands is the one the task text asks for
    assert f"You are a <style> {ROLE}" in reflect.REFLECT_TASK
    # dollar totals in the evidence are statistics, not years
    ok, why = reflect.validate_reply({**GOOD, "evidence": "net +2031.4 over 85 entries"})
    assert ok is not None, why


@pytest.mark.parametrize(("patch", "reason"), [
    ({"prompt": "x" * 701}, "too_long:prompt"),
    ({"prompt": GOOD["prompt"] + " Since 2026-08-01 favour calls."}, "date:prompt"),
    ({"prompt": GOOD["prompt"] + " Buy dips in August."}, "date:prompt"),
    ({"prompt": GOOD["prompt"] + " Only after session_ordinal 40."}, "date:prompt"),
    ({"hypothesis": "The rally of 2026 persists into later sessions."}, "date:hypothesis"),
    ({"evidence": "Losses clustered around Aug 12 with -40.2 net."}, "date:evidence"),
    ({"prompt": GOOD["prompt"] + " Prefer SPY rows."}, "ticker:prompt"),
    ({"evidence": "QQQ drove 12 of 20 losses."}, "ticker:evidence"),
    ({"prompt": GOOD["prompt"] + " Track the S&P 500 trend."}, "ticker:prompt"),
    ({"prompt": GOOD["prompt"] + ' Reply {"choice": null}.'}, "contract:prompt_redefines_reply"),
    ({"prompt": GOOD["prompt"] + " Answer in json."}, "contract:prompt_redefines_reply"),
    ({"prompt": GOOD["prompt"] + " Or hold:10 when calm."}, "horizon:hold:10"),
    ({"prompt": "Skip."}, "too_short:prompt"),
    ({"hypothesis": "Trends persist. Reversals do not."}, "hypothesis:not_one_sentence"),
    ({"evidence": "the base policy lost money"}, "evidence:no_statistics"),
    ({"prompt": "Prefer bullish rows when ret_20s_pct is positive; hold them hold:5."},
     "contract:role_framing"),
    ({"prompt": f"You are a {ROLE} Prefer bullish rows; hold them hold:5."},
     "contract:role_framing"),  # the style descriptor is required
    ({"prompt": f"Trend first. You are a trend {ROLE} Prefer bullish rows at hold:5."},
     "contract:role_framing"),  # the framing must open the prompt
    ({"prompt": f"You are a trend {ROLE} Skip."}, "contract:no_rule_after_role"),
    ({"prompt": None}, "missing:prompt"),
    ({"name": ""}, "missing:name"),
])
def test_validate_reply_rejects_contract_breaks(patch: dict[str, Any], reason: str) -> None:
    proposal, reasons = reflect.validate_reply({**GOOD, **patch},
                                               tickers=frozenset({"QQQ"}))
    assert proposal is None and any(r.startswith(reason) for r in reasons), reasons


def test_similarity_flags_near_identical_prompts_only() -> None:
    trend = ("You are a trend-following paper-trading policy choosing ONE defined-risk option "
             "spread board row, or skipping. Read the context: trade only in the direction of "
             "an underlying whose ret_5s_pct and ret_20s_pct agree (both positive -> bullish "
             "rows; both negative -> bearish rows); skip when they disagree or rv_20s_ann_pct "
             "is unusually high. Use hold:5 when both trends are strong, eod otherwise.")
    meanrev = ("You are a mean-reversion paper-trading policy choosing ONE defined-risk option "
               "spread board row, or skipping. Fade stretched moves: when an underlying's "
               "ret_5s_pct is strongly positive prefer its bearish rows, when strongly negative "
               "prefer its bullish rows, holding eod or hold:5; skip when no underlying is "
               "stretched.")
    tweaked = trend.replace("unusually high", "very high").replace("strong,", "clear,")
    assert reflect.similarity(trend, trend) == 1.0
    assert reflect.similarity(trend, tweaked) >= reflect.DUP_THRESHOLD
    assert reflect.similarity(trend, meanrev) < reflect.DUP_THRESHOLD
    assert reflect.duplicate_of(tweaked, {"m-trend": trend, "m-mr": meanrev}) == "m-trend"
    assert reflect.duplicate_of(meanrev, {"m-trend": trend}) is None


def test_board_task_template_keeps_the_fixed_contract() -> None:
    task = reflect.board_task_template()
    assert task.startswith(reflect.POLICY_PLACEHOLDER)
    assert '"horizon"' in task and "hold:5" in task and "max loss per trade 300" in task


def test_personas_cycle_beyond_the_seed_schools() -> None:
    names = [key for key, _ in reflect.personas(8)]
    assert names[:6] == [key for key, _ in reflect.PERSONAS]
    assert names[6:] == ["trend2", "meanrev2"]
    for bad in (0, reflect.MAX_K + 1):
        with pytest.raises(ValueError):
            reflect.personas(bad)


# -------------------------------------------------------------------- panel

P_TREND = GOOD["prompt"]
P_MEANREV = (f"You are a mean-reversion {ROLE} When an alias is stretched over 5 sessions "
             "take the opposite direction at eod.")
P_VOLPREM = (f"You are a premium-selling {ROLE} Prefer credit rows whose short strike is out "
             "of the money when realized vol is high, held hold:5.")
P_UNFRAMED = "Prefer bullish rows when ret_20s_pct is positive and hold them hold:5."


def _reply(name: str, prompt: str) -> dict[str, Any]:
    return {**GOOD, "name": name, "prompt": prompt}


class PanelTransport:
    """Scripted theorists keyed by persona and attempt; records every call."""

    SCRIPT: ClassVar[dict[str, list[Any]]] = {
        "trend": [_reply("unframed", P_UNFRAMED), _reply("trend hold", P_TREND)],
        "meanrev": [_reply("fade SPY", P_MEANREV + " Avoid SPY."), _reply("fade", P_MEANREV)],
        "volprem": [_reply("near copy", P_TREND.replace("otherwise", "else")), 500,
                    _reply("sell premium", P_VOLPREM)],
        "costmin": [_reply("copy", M_B_PROMPT)] * 3,
    }

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.seen: dict[str, int] = {}

    def __call__(self, url: str, body: bytes, headers: dict[str, str],
                 timeout: float) -> tuple[int, bytes]:
        request = json.loads(body)
        first = json.loads(request["messages"][0]["content"])
        persona = first["your_school"]["name"]
        attempt = self.seen.get(persona, 0)
        self.seen[persona] = attempt + 1
        self.calls.append({"persona": persona, "messages": request["messages"],
                           "timeout": timeout, "max_tokens": request.get("max_tokens"),
                           "effort": request.get("reasoning_effort"),
                           "model": request.get("model")})
        step = self.SCRIPT[persona][attempt]
        if isinstance(step, int):
            return step, b""
        content = "<think>weighing the dossiers</think>" + json.dumps(step)
        envelope = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 50}}
        return 200, json.dumps(envelope).encode()


def test_panel_regenerates_rejects_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_MINIMAX2", "sk-test-secret")
    transport = PanelTransport()
    pack = {"dossiers": [], "universe": {}}
    accepted, calls = reflect.run_panel(pack, k=4, transport=transport, concurrency=1,
                                        existing={"m-b": M_B_PROMPT}, timeout=99.0)
    assert [a.persona for a in accepted] == ["trend", "meanrev", "volprem"]
    assert [a.proposal.prompt for a in accepted] == [P_TREND, P_MEANREV, P_VOLPREM]
    assert [a.attempt for a in accepted] == [2, 2, 3]
    assert [a.name for a in accepted] == ["refl-trend-trend-hold", "refl-meanrev-fade",
                                          "refl-volprem-sell-premium"]
    assert len(transport.calls) == len(calls) == 2 + 2 + 3 + 3
    # the reflection call's own budget/effort reach the wire; the model does not change
    assert {(c["timeout"], c["max_tokens"], c["effort"], c["model"])
            for c in transport.calls} == {(99.0, 32000, "high", "MiniMax-M3.1-Flash-Preview")}
    assert calls[0]["response"]["reasoning_effort"] == "high"
    unframed = transport.calls[1]  # trend, attempt 2: the missing role framing is fed back
    assert [m["role"] for m in unframed["messages"]] == ["user", "assistant", "user"]
    assert "contract:role_framing" in unframed["messages"][2]["content"]
    retry = transport.calls[3]  # meanrev, attempt 2: the rejection is fed back
    assert "ticker:prompt" in retry["messages"][2]["content"]
    stats = reflect.call_stats(calls, 4, accepted)
    assert (stats["calls"], stats["parse_ok"], stats["contract_ok"], stats["accepted"]) == \
        (10, 9, 7, 3)
    assert stats["reasons"]["near_duplicate"] == 4
    assert stats["reasons"]["ticker:prompt"] == 1
    assert stats["reasons"]["contract:role_framing"] == 1
    assert stats["reasons"]["call:minimax-flash: HTTP 500"] == 1
    assert stats["first_attempt_accepted"] == 0
    assert stats["usage"] == {"prompt_tokens": 9000, "completion_tokens": 450,
                              "calls_with_usage": 9}
    assert calls[0]["response"]["content"].startswith("<think>")
    fragment = [{"name": a.name, "kind": "model", "prompt": a.proposal.prompt}
                for a in accepted]
    specs = longrun.policies_from_config(fragment, builtin=False)
    assert [s.name for s in specs] == [a.name for a in accepted]


def test_run_reflect_writes_fragment_and_transcript_read_only(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_MINIMAX2", "sk-test-secret")
    run_dir = make_run(tmp_path)
    before = {p: p.stat().st_mtime_ns for p in run_dir.rglob("*")}
    out = tmp_path / "out" / "frag.json"
    transport = PanelTransport()
    summary = reflect.run_reflect(run_dir, k=4, out=out, transport=transport,
                                  concurrency=1, quota_ok=lambda: (True, "ok"),
                                  effort="medium", max_tokens=20000)
    assert {(c["max_tokens"], c["effort"]) for c in transport.calls} == {(20000, "medium")}
    assert {p: p.stat().st_mtime_ns for p in run_dir.rglob("*")} == before
    assert summary["status"] == "ok" and summary["stats"]["accepted"] == 3
    fragment = json.loads(out.read_text())
    assert fragment["schema"] == reflect.FRAGMENT_SCHEMA
    assert [p["prompt"] for p in fragment["policies"]] == [P_TREND, P_MEANREV, P_VOLPREM]
    assert all(set(p) == {"name", "kind", "prompt"} for p in fragment["policies"])
    prov = fragment["provenance"]
    assert prov["cutoff"] == CUTOFF and prov["dossier_policies"] == ["m-a", "m-b"]
    assert prov["models"] == ["MiniMax-M3.1-Flash-Preview"]
    assert (prov["reasoning_effort"], prov["max_tokens"]) == ("medium", 20000)
    assert set(prov["per_policy"]) == {p["name"] for p in fragment["policies"]}
    dossiers = json.loads(Path(prov["dossiers"]).read_text())
    assert dossiers["meta"]["pack_sha256"] == prov["dossier_sha256"]
    transcript = Path(prov["transcript"]).read_text()
    assert len(transcript.splitlines()) == 10
    assert "sk-test-secret" not in transcript and "Authorization" not in transcript


def test_quota_refusal_and_dry_run_make_no_calls(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    from tree_options.desk.__main__ import run_cli

    def forbidden(*args: Any) -> tuple[int, bytes]:
        raise AssertionError("no model call allowed here")

    monkeypatch.setattr(reflect, "urllib_post", forbidden)
    run_dir = make_run(tmp_path)
    refused = reflect.run_reflect(run_dir, k=2, out=tmp_path / "q.json",
                                  quota_ok=lambda: (False, "left=1 planned=50"))
    assert refused["status"] == "quota_refused" and not (tmp_path / "q.json").exists()
    out = tmp_path / "dry.json"
    assert run_cli(["longrun", "reflect", "--run-dir", str(run_dir), "--k", "3",
                    "--out", str(out), "--dry-run"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "dry_run" and summary["cutoff"] == CUTOFF
    assert summary["tokens_est"]["panel_first_attempts"] == 3 * summary["tokens_est"]["per_call"]
    assert summary["decided"] == {"m-a": 4, "m-b": 1}
    assert Path(summary["dossiers"]).is_file() and not out.exists()
    assert run_cli(["longrun", "reflect", "--run-dir", str(run_dir), "--k", "0",
                    "--dry-run"]) == 2
    with pytest.raises(ValueError, match="effort"):
        reflect.run_panel({}, k=1, effort="none", transport=forbidden)
