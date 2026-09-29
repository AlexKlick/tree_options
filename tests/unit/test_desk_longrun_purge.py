"""The purged walk-forward with embargo (desk.purge + longrun.score_run):
a train decision whose exit crosses the cutoff never informs finalist
selection and is counted; the random null gets the identical purge; the
test split starts after the embargo; plan.json stays backward compatible.
Hand-computed fixtures; no network, no model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tree_options.desk import longrun, purge
from tree_options.desk.longrun import Board, OutcomeCache, PolicySpec, Protocol

# four sessions, one board each; x = put_credit, y = call_credit
#   x@hold:5   net +100, exits the NEXT day (entered on the cutoff day -> crosses it)
#   y@intraday net +10,  exits the same day
#   x@intraday 0 same day; y@hold:5 0 next day
DAYS = ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"]
ROWS = [{"id": "x", "structure": "put_credit"}, {"id": "y", "structure": "call_credit"}]
NEXT = {"2026-06-01": "2026-06-02", "2026-06-02": "2026-06-03", "2026-06-03": "2026-06-04",
        "2026-06-04": "2026-06-05"}


def boards() -> list[Board]:
    return [Board(f"s:{d}T10:00", d, "10:00", [dict(r) for r in ROWS]) for d in DAYS]


def outcome(snapshot: str, cid: str, horizon: str | None) -> dict[str, Any]:
    day = snapshot[2:12]
    exit_day = NEXT[day] if horizon == "hold:5" else day
    net = {("x", "hold:5"): 100.0, ("y", "intraday"): 10.0}.get((cid, str(horizon)), 0.0)
    return {"gross": net, "net": net, "exit_at": f"{exit_day}T19:30:00+00:00"}  # 15:30 ET


def score(protocol: Protocol) -> dict[str, Any]:
    specs = [PolicySpec("a", "rule", rule=longrun.rule_fixed_structure("put_credit", "hold:5")),
             PolicySpec("b", "rule", rule=longrun.rule_fixed_structure("call_credit",
                                                                         "intraday"))]
    arms = longrun.arms_of(specs)
    bs = boards()
    receipts = {arm.name: {b.snapshot: longrun.decide(arm, b, None) for b in bs}
                for arm in arms}
    return longrun.score_run(bs, arms, receipts, OutcomeCache(outcome), protocol)


PROTO = dict(draws=1000, random_seeds=200, cutoff="2026-06-01", metric="total",
             max_finalists=1, random_horizons=("intraday", "hold:5"))


def test_a_train_decision_exiting_after_the_cutoff_is_purged_from_selection() -> None:
    doc = score(Protocol(**PROTO))
    wf = doc["walk_forward"]
    # unpurged, a's cutoff-day trade (+100, exits 06-02) would win the tune ranking over
    # b's +10; purged it scores 0 there, so b is the single finalist
    ranking = {r["policy"]: r for r in wf["ranking"]}
    assert ranking["a"]["tune_total"] == 0.0 and ranking["b"]["tune_total"] == 10.0
    assert [f["policy"] for f in wf["finalists"]] == ["b"]
    purged = wf["purge"]
    assert purged["by_arm"]["a"] == {"train_entered": 1, "purged": 1, "estimated_exits": 0}
    assert purged["by_arm"]["b"] == {"train_entered": 1, "purged": 0, "estimated_exits": 0}
    assert purged["own_coverage"]["a"]["purged"] == 1
    # the random null gets the identical purge: the train board's options (x, y) x
    # (intraday, hold:5) = 0, 100, 10, 0 -> both hold:5 options purged -> mean 10 / 4
    assert purged["random_null"] == {"train_options": 4, "purged_options": 2}
    assert ranking["b"]["tune_diff_vs_random"] == 10.0 - 2.5  # unpurged null: 10 - 27.5
    # the whole-window standings are untouched: a keeps all four +100 trades
    standing = {r["arm"]: r for r in doc["standings"]}
    assert standing["a"]["net_total"] == 400.0
    md = longrun.digest_markdown(doc)
    assert "Purge + embargo" in md and "a 1/1" in md and "b 0/1" in md


def test_the_test_split_starts_after_the_embargo() -> None:
    default = score(Protocol(**PROTO))["walk_forward"]
    assert (default["test_sessions"], default["embargoed_sessions"]) == (3, 0)
    assert default["finalists"][0]["test"]["net_total"] == 30.0  # b: 06-02, 03, 04
    wf = score(Protocol(**PROTO, embargo_sessions=2))["walk_forward"]
    assert (wf["tune_sessions"], wf["embargoed_sessions"], wf["test_sessions"]) == (1, 1, 2)
    assert wf["embargo_sessions"] == 2
    assert wf["finalists"][0]["test"]["net_total"] == 20.0  # 06-02 embargoed


def test_missing_exits_are_estimated_from_the_horizon() -> None:
    days = DAYS
    assert purge.crosses(None, "hold:5", days[0], days[0], days) == (True, True)
    assert purge.crosses(None, "hold:1", days[0], days[1], days) == (False, True)
    assert purge.crosses(None, "intraday", days[0], days[0], days) == (False, True)
    assert purge.crosses(None, None, days[0], days[0], days) == (False, True)
    assert purge.crosses(None, "expiry", days[0], days[3], days) == (True, True)
    # an exit reported in UTC is read as its ET session date (00:30Z = 20:30 ET the day before)
    assert purge.crosses("2026-06-02T00:30:00+00:00", "eod", days[0], days[0], days) == (
        False, False)


def test_protocol_stays_backward_compatible() -> None:
    assert "embargo_sessions" not in Protocol().to_json()  # pre-embargo plan.json shape
    assert Protocol(embargo_sessions=3).to_json()["embargo_sessions"] == 3
    with pytest.raises(ValueError, match="embargo_sessions"):
        Protocol(embargo_sessions=0)
    cfg = {"protocol": {"embargo_sessions": 2}}
    assert longrun.protocol_from_config(cfg).embargo_sessions == 2
    assert longrun.protocol_from_config({}).embargo_sessions == 1


def test_the_v2_table_plugin_carries_the_exit(tmp_path: Path) -> None:
    table = tmp_path / "t.jsonl"
    table.write_text(json.dumps({"snapshot": "s:2026-06-01T10:00", "candidate_id": "x",
                                 "exit_mode": "hold:5", "status": "closed", "gross": "3",
                                 "net": "1", "exit_at": "2026-06-08T19:30:00+00:00"}) + "\n")
    ctx = longrun.PluginContext(config_dir=tmp_path)
    cache = OutcomeCache(longrun.plugin("outcome", "v2")({"table": str(table)}, ctx))
    assert cache.get("s:2026-06-01T10:00", "x", "hold:5") == (3.0, 1.0)
    assert cache.exit_at("s:2026-06-01T10:00", "x", "hold:5") == "2026-06-08T19:30:00+00:00"
    assert cache.exit_at("s:2026-06-01T10:00", "x", "expiry") is None
