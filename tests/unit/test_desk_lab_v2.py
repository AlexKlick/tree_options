"""Desk environment v2: the stratified, context-carrying, aliased board.

v1 showed the top-12 rows by reward/risk with no market context: bullish
rows (low reward/risk in a rising tape) were hidden and direction could not
be reasoned about. v2 stratifies by structure, carries as-of context per
aliased underlying, and adds a strict horizon to the JSON contract. The
as-of discipline is proven by planting FUTURE prints and showing nothing
the model sees changes.
"""

from __future__ import annotations

import copy
import json
import math
import re
import statistics
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from typing import Any

import pytest

from tests.unit.test_desk_intraday_action_graph import _bundle
from tests.unit.test_desk_lab import UNDER, FakeTransport
from tests.unit.test_desk_outcomes import SPOT_BASE, _b, parity_bundle, parity_days, spot_path
from tree_options.desk import intraday_action_graph as iag
from tree_options.desk import lab
from tree_options.desk.outcomes import prepare_index, spot_series

CTX_DAYS = parity_days(24)
AT = 21  # the board session: 21 prior sessions exist for the 20-session context
V2_FIELDS = {"id", "structure", "direction", "underlying", "width", "observed_premium",
             "max_loss", "max_gain", "reward_risk", "dte", "short_strike_moneyness_pct",
             "long_recent_move", "short_recent_move"}


@pytest.fixture(scope="module")
def ctx_raw() -> dict[str, Any]:
    return parity_bundle(CTX_DAYS)


def _render(raw: dict[str, Any], day: Any, clock: str) -> tuple[Any, Any, Any]:
    index = prepare_index(raw)
    context = lab.board_context(index, day, clock)
    rows = lab.board_rows_v2(iag.decision_packet(raw, day, clock), context)
    return context.public, rows, lab.board_prompt_v2(rows, context)


def _pct(now: Decimal, base: Decimal) -> str:
    return str(((now / base - 1) * 100).quantize(Decimal("0.01")))


# ------------------------------------------------------------------ context


def test_board_context_values_are_as_of_and_aliased(ctx_raw: dict[str, Any]) -> None:
    day = CTX_DAYS[AT]
    index = prepare_index(ctx_raw)
    context = lab.board_context(index, day, "10:00")
    assert context.aliases == {"QQQ": "U1", "SPY": "U2"}  # sorted tickers, stable
    assert context.spot == {s: spot_path(s, AT)[0] for s in SPOT_BASE}
    assert context.public["time_of_day"] == "open"
    assert context.public["session_ordinal"] == AT + 1
    assert set(context.public["underlyings"]) == {"U1", "U2"}
    spy = context.public["underlyings"]["U2"]
    now = spot_path("SPY", AT)[0]
    assert spy["ret_1s_pct"] == _pct(now, spot_path("SPY", AT - 1)[1])
    assert spy["ret_5s_pct"] == _pct(now, spot_path("SPY", AT - 5)[1])
    assert spy["ret_20s_pct"] == _pct(now, spot_path("SPY", AT - 20)[1])
    closes = [float(spot_path("SPY", j)[1]) for j in range(AT - 21, AT)]
    logs = [math.log(b / a) for a, b in pairwise(closes)]
    assert spy["rv_20s_ann_pct"] == f"{statistics.stdev(logs) * math.sqrt(252) * 100:.1f}"
    early = lab.board_context(index, CTX_DAYS[2], "10:45")
    qqq = early.public["underlyings"]["U1"]
    assert qqq["ret_1s_pct"] is not None
    assert (qqq["ret_5s_pct"], qqq["ret_20s_pct"], qqq["rv_20s_ann_pct"]) == (None, None, None)
    assert early.public["time_of_day"] == "morning"
    assert lab.board_context(index, day, "15:15").public["time_of_day"] == "close"
    with pytest.raises(ValueError):
        lab.board_context(index, day, "10:07")


def test_future_prints_change_nothing_the_model_sees(ctx_raw: dict[str, Any]) -> None:
    day, following = CTX_DAYS[AT], CTX_DAYS[AT + 1]
    before = _render(ctx_raw, day, "10:00")
    planted = copy.deepcopy(ctx_raw)
    for ticker, body in planted["contracts"].items():
        if iag.parse_contract(ticker).right == "C":
            # same session after the 10:00 ET decision (10:05 and 15:50 ET) and
            # the next session: all would move a leaked spot/close estimate
            body["results"] += [_b(day, 14, 5, "77.77"), _b(day, 19, 50, "77.77"),
                                _b(following, 14, 5, "77.77")]
            body["results"].sort(key=lambda bar: bar["t"])
    assert _render(planted, day, "10:00") == before
    # the planted prints are not inert: the SAME-session close does move
    moved = spot_series(prepare_index(planted))["SPY"][day]
    assert moved != spot_series(prepare_index(ctx_raw))["SPY"][day]


# ------------------------------------------------------------------ rows


def _cand(structure: str, rr: str, *, short_strike: int = 500, cid: str = "") -> dict[str, Any]:
    right = "P" if structure.startswith("put") else "C"
    return {"id": cid or f"{structure}-{rr}", "structure": structure, "underlying": "SPY",
            "expiry": "2026-10-16", "long": f"O:SPY261016{right}00495000",
            "short": f"O:SPY261016{right}{short_strike * 1000:08d}", "width": "5",
            "observed_premium": "1.00", "max_loss_proxy": "100", "max_gain_proxy": "400",
            "reward_to_risk_proxy": rr, "long_recent_trade_move": "0.0100",
            "short_recent_trade_move": None, "data_kind": "last-traded-minute-close"}


AS_OF = datetime(2026, 9, 24, 14, 0, tzinfo=UTC)


def _context(spot: str = "500") -> lab.BoardContext:
    return lab.BoardContext(public={"time_of_day": "open", "session_ordinal": 1,
                                    "underlyings": {"U1": {}}},
                            aliases={"SPY": "U1"}, spot={"SPY": Decimal(spot)}, as_of=AS_OF)


def test_rows_v2_stratify_by_structure_without_reward_risk_sorting() -> None:
    # bullish rows carry the LOW reward/risk: a top-12-by-rr board hides them all
    candidates = []
    for structure in ("put_credit", "call_debit"):
        candidates += [_cand(structure, f"0.{k}0") for k in range(1, 8)]
    for structure in ("put_debit", "call_credit"):
        candidates += [_cand(structure, f"{k}.00") for k in range(1, 8)]
    packet = {"as_of": AS_OF.isoformat(), "candidates": candidates}
    v1 = lab.board_rows(packet)
    assert not any(r["structure"] in ("put_credit", "call_debit") for r in v1)
    rows = lab.board_rows_v2(packet, _context())
    assert len(rows) == 16
    by = {s: sorted(r["reward_risk"] for r in rows if r["structure"] == s)
          for s in ("put_credit", "put_debit", "call_credit", "call_debit")}
    # nearest-to-median (4): 4, then 3 and 5, then the 2/6 tie broken by id -> 2
    assert by["put_debit"] == by["call_credit"] == ["2.00", "3.00", "4.00", "5.00"]
    assert by["put_credit"] == by["call_debit"] == ["0.20", "0.30", "0.40", "0.50"]
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)  # no rr ordering either
    directions = {r["structure"]: r["direction"] for r in rows}
    assert directions == {"put_credit": "bullish", "call_debit": "bullish",
                          "put_debit": "bearish", "call_credit": "bearish"}
    assert all(set(r) == V2_FIELDS for r in rows)
    even = [_cand("put_debit", f"{k}.00") for k in range(1, 7)]
    picked = lab.board_rows_v2({"as_of": AS_OF.isoformat(), "candidates": even}, _context())
    assert sorted(r["reward_risk"] for r in picked) == ["2.00", "3.00", "4.00", "5.00"]


def test_rows_v2_fields_dte_and_moneyness() -> None:
    packet = {"as_of": AS_OF.isoformat(),
              "candidates": [_cand("call_debit", "1.50", short_strike=505, cid="a"),
                             _cand("put_credit", "0.50", short_strike=500, cid="b")]}
    rows = {r["id"]: r for r in lab.board_rows_v2(packet, _context("500"))}
    assert rows["a"] == {"id": "a", "structure": "call_debit", "direction": "bullish",
                         "underlying": "U1", "width": "5", "observed_premium": "1.00",
                         "max_loss": "100", "max_gain": "400", "reward_risk": "1.50",
                         "dte": 22, "short_strike_moneyness_pct": "1.00",
                         "long_recent_move": "0.0100", "short_recent_move": None}
    assert rows["b"]["short_strike_moneyness_pct"] == "0.00"
    no_spot = lab.BoardContext(public={}, aliases={"SPY": "U1"}, spot={}, as_of=AS_OF)
    assert all(r["short_strike_moneyness_pct"] is None
               for r in lab.board_rows_v2(packet, no_spot))
    with pytest.raises(ValueError):  # a context for another board is refused
        lab.board_rows_v2({**packet, "as_of": "2026-09-24T14:45:00+00:00"}, _context())
    with pytest.raises(ValueError):  # an unaliased underlying never falls back to its ticker
        lab.board_rows_v2(packet, lab.BoardContext(public={}, aliases={}, spot={}, as_of=AS_OF))


def test_real_board_has_both_directions_and_no_leaks(ctx_raw: dict[str, Any]) -> None:
    day = CTX_DAYS[AT]
    public, rows, prompt = _render(ctx_raw, day, "10:00")
    assert {r["direction"] for r in rows} == {"bullish", "bearish"}
    assert {r["structure"] for r in rows} == {"put_credit", "put_debit", "call_credit",
                                              "call_debit"}
    assert max(sum(r["structure"] == s for r in rows) for s in {r["structure"] for r in rows}) <= 4
    content = prompt[0]["content"]
    for leak in ("SPY", "QQQ", "O:", "T10:00", "T14:00", "+00:00"):
        assert leak not in content, leak
    assert re.search(r"(19|20)\d\d-\d\d-\d\d", content) is None  # no dates (hex ids may hold digits)
    for symbol in SPOT_BASE:  # no spot LEVEL (a level identifies ticker and era)
        for level in spot_path(symbol, AT):
            assert str(level) not in content
    body = json.loads(content)
    assert set(body) == {"task", "context", "board"}
    assert body["context"] == public and body["board"] == rows


# ------------------------------------------------------------------ prompt + parser


def test_prompt_v2_contract_and_policy_sentence() -> None:
    rows = lab.board_rows_v2({"as_of": AS_OF.isoformat(),
                              "candidates": [_cand("put_credit", "0.50", cid="r1")]}, _context())
    task = json.loads(lab.board_prompt_v2(rows, _context())[0]["content"])["task"]
    assert task.startswith(lab.POLICY_SENTENCE)
    assert '"horizon": "intraday" | "eod" | "hold:5" | "expiry"' in task
    assert '"choice": "<row id>" | null' in task and "14.60" in task
    custom = json.loads(lab.board_prompt_v2(rows, _context(), policy_prompt="Be bold.")[0]["content"])
    assert custom["task"] == task.replace(lab.POLICY_SENTENCE, "Be bold.", 1)
    leaky = [{**rows[0], "long": "O:SPY261016P00495000"}]  # extra keys are never rendered
    assert "O:SPY" not in lab.board_prompt_v2(leaky, _context())[0]["content"]


def test_parse_choice_v2_rejects_unknown_ids_and_horizons_never_repairs() -> None:
    ids = {"a1", "b2"}
    assert lab.parse_choice_v2({"choice": "a1", "horizon": "eod", "note": "ok"}, ids) == (
        "a1", "eod", "ok")
    for horizon in lab.V2_HORIZONS:
        assert lab.parse_choice_v2({"choice": "b2", "horizon": horizon}, ids) == ("b2", horizon, "")
    assert lab.parse_choice_v2({"choice": None, "horizon": "whatever", "note": "skip"}, ids) == (
        None, None, "skip")
    assert lab.parse_choice_v2({"choice": "zz", "horizon": "eod"}, ids) == (
        None, None, "unknown id rejected: zz")
    for bad in ("hold:3", "EOD", " eod", "hold:05", None, 5):
        choice, horizon, note = lab.parse_choice_v2({"choice": "a1", "horizon": bad}, ids)
        assert (choice, horizon) == (None, None), bad
        assert note.startswith("unknown horizon rejected"), bad
    for bad_id in (" a1", "A1", 1, ["a1"]):
        assert lab.parse_choice_v2({"choice": bad_id, "horizon": "eod"}, ids)[:2] == (None, None)
    long_note = lab.parse_choice_v2({"choice": "a1", "horizon": "eod", "note": "x" * 99}, ids)[2]
    assert len(long_note) == 60


# ------------------------------------------------------------------ providers


def test_minimax_flash_is_a_model_provider(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert lab.model_provider("model:minimax-flash") == "minimax-flash"
    assert lab.model_provider("gepa:minimax-flash:abc123def456") == "minimax-flash"
    assert lab.model_provider("gepa:local:abc123def456") == "local"
    assert lab.model_provider("gepa:abc123def456") == "zai"  # unchanged default
    for bad in ("model:bogus", "gepa:bogus:abc123", "model:"):
        with pytest.raises(ValueError):
            lab.model_provider(bad)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN_MINIMAX2", "test-key-material-2")
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(_bundle(datetime(2026, 9, 24).date())))
    transport = FakeTransport("first")
    document = lab.run_lab(lab.LabConfig(bundle=bundle, policy="model:minimax-flash",
                                         sessions=1, lab_root=tmp_path / "lab"),
                           windows=UNDER, transport=transport,
                           now=datetime(2026, 9, 28, 20, 0, tzinfo=UTC))
    assert document["status"] == "ok" and document["model_failures"] == 0
    assert transport.calls[0]["url"] == "https://api.minimax.io/v1/chat/completions"
    assert transport.calls[0]["body"]["model"] == "MiniMax-M3.1-Flash-Preview"
    assert all(r["provider"] == "minimax-flash" for r in document["receipts"])
