"""Board universe v3: OTM contract selection, width pairing, listing gates.

Oracles are hand-laid fixtures with hand-computed answers (no call back into
the code under test to produce an expected value), plus a pin of the frozen
20260927-v1 vintage: its boards, v2 prompts, packets and outcome rows must
hash exactly as the pre-change code (main 229b238) produced them.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from statistics import NormalDist
from typing import Any

import pytest

from tree_options.desk import board_universe as bu
from tree_options.desk import intraday_action_graph as iag
from tree_options.desk import lab, outcomes
from tree_options.trex.clock import session_calendar

# September 2026 is EDT: 13:59Z = 09:59 ET, fresh for the 10:00 ET board.
D0, D1, D2 = date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)
A = "O:SPY261016C00500000"
B = "O:SPY261016C00501000"
C = "O:SPY261016C00505000"
PRICES = {A: "4", B: "3.5", C: "2"}


def _bar(day: date, hour: int, minute: int, price: str) -> dict[str, Any]:
    stamp = datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)
    return {"t": int(stamp.timestamp() * 1000), "c": price, "v": 1}


def _raw(prices: dict[str, str], days: tuple[date, ...] = (D1,), **extra: Any) -> dict[str, Any]:
    rules = "candidate_pairing" in extra or "listing" in extra
    schema = "desk-option-minute-bars/2" if rules else "desk-option-minute-bars/1"
    return {"schema": schema, **extra, "contracts": {
        ticker: {"ticker": ticker, "timespan": "minute",
                 "results": [_bar(day, 13, 59, price) for day in days]}
        for ticker, price in prices.items()}}


def _legs(candidates: list[dict[str, Any]]) -> set[tuple[str, str, str, str]]:
    return {(c["structure"], c["long"], c["short"], c["observed_premium"]) for c in candidates}


# ------------------------------------------------------------------ pairing


def test_no_pairing_key_keeps_the_v1_adjacent_pairs() -> None:
    got = iag.decision_packet(_raw(PRICES), D1, "10:00")["candidates"]
    # adjacent strikes only: 500/501 (width 1) and 501/505 (width 4)
    assert _legs(got) == {("call_debit", A, B, "0.5"), ("call_credit", B, A, "0.5"),
                          ("call_debit", B, C, "1.5"), ("call_credit", C, B, "1.5")}


def test_width_pairing_pairs_every_listed_width() -> None:
    raw = _raw(PRICES, candidate_pairing={"rule": "widths", "widths": ["1", "5"]})
    got = iag.decision_packet(raw, D1, "10:00")["candidates"]
    # 500/501 and 500/505; 501/505 is width 4 (not listed); the 5-wide credit
    # collects 2.00 -> max loss exactly 300, inside the cap
    assert _legs(got) == {("call_debit", A, B, "0.5"), ("call_credit", B, A, "0.5"),
                          ("call_debit", A, C, "2"), ("call_credit", C, A, "2")}
    assert {c["max_loss_proxy"] for c in got if c["short"] == A and c["long"] == C} == {"300"}
    only4 = _raw(PRICES, candidate_pairing={"rule": "widths", "widths": ["4"]})
    assert _legs(iag.decision_packet(only4, D1, "10:00")["candidates"]) == {
        ("call_debit", B, C, "1.5"), ("call_credit", C, B, "1.5")}


def test_listing_gates_each_board_day_on_both_board_paths() -> None:
    raw = _raw(PRICES, (D1, D2), candidate_pairing={"rule": "widths", "widths": ["1", "4", "5"]},
               listing={A: {"from": "2026-09-24", "until": "2026-09-24"},
                        B: {"from": "2026-09-25", "until": None},
                        C: {"from": "2026-09-24"}})
    day1 = {("call_debit", A, C, "2"), ("call_credit", C, A, "2")}  # A and C listed
    day2 = {("call_debit", B, C, "1.5"), ("call_credit", C, B, "1.5")}  # B and C listed
    assert _legs(iag.decision_packet(raw, D1, "10:00")["candidates"]) == day1
    assert _legs(iag.decision_packet(raw, D2, "10:00")["candidates"]) == day2
    index = outcomes.prepare_index(raw)
    assert _legs(outcomes.board_candidates(index, D1, "10:00")) == day1
    assert _legs(outcomes.board_candidates(index, D2, "10:00")) == day2


@pytest.mark.parametrize("extra", [
    {"candidate_pairing": {"rule": "adjacent"}},
    {"candidate_pairing": {"rule": "widths", "widths": []}},
    {"candidate_pairing": {"rule": "widths", "widths": ["0"]}},
    {"listing": {A: {"from": "2026-09-24"}}},  # must name every contract
    {"listing": {A: {"from": "2026-09-24", "until": "2026-09-23"},
                 B: {"from": "2026-09-24"}, C: {"from": "2026-09-24"}}},
])
def test_malformed_candidate_rules_are_refused(extra: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        iag.decision_packet(_raw(PRICES, **extra), D1, "10:00")


def test_candidate_rules_need_the_v2_schema_and_v2_needs_no_rules() -> None:
    # a /1 bundle is what pre-v3 readers accept: it must never carry rules
    ruled = {**_raw(PRICES, candidate_pairing={"rule": "widths", "widths": ["1"]}),
             "schema": "desk-option-minute-bars/1"}
    with pytest.raises(ValueError, match="require"):
        iag.decision_packet(ruled, D1, "10:00")
    plain_v2 = {**_raw(PRICES), "schema": "desk-option-minute-bars/2"}
    assert _legs(iag.decision_packet(plain_v2, D1, "10:00")["candidates"]) == _legs(
        iag.decision_packet(_raw(PRICES), D1, "10:00")["candidates"])
    with pytest.raises(ValueError):
        iag.decision_packet({**_raw(PRICES), "schema": "desk-option-minute-bars/3"}, D1, "10:00")


def test_parity_spot_uses_listed_pairs_only() -> None:
    k500c, k500p = "O:SPY261016C00500000", "O:SPY261016P00500000"
    k510c, k510p = "O:SPY261016C00510000", "O:SPY261016P00510000"
    prices = {k500c: "10", k500p: "5", k510c: "8", k510p: "3"}  # C - P + K: 505 and 515
    at = iag._instant(D2, "10:00")
    both = outcomes.prepare_index(_raw(prices, (D2,)))
    assert outcomes.spot_asof(both, at) == {"SPY": Decimal("510")}  # median(505, 515)
    gated = outcomes.prepare_index(_raw(prices, (D2,), listing={
        k500c: {"from": "2026-09-24"}, k500p: {"from": "2026-09-24"},
        k510c: {"from": "2026-09-24", "until": "2026-09-24"}, k510p: {"from": "2026-09-24"}}))
    assert outcomes.spot_asof(gated, at) == {"SPY": Decimal("505")}


# ------------------------------------------------------------------ iv context


def test_iv_context_is_the_prior_session_close_and_absent_without_it() -> None:
    iv = {"closes": {"SPY": {"2026-09-23": "16.7", "2026-09-24": "17.2", "2026-09-25": "30"}}}
    index = outcomes.prepare_index(_raw(PRICES, (D1, D2), iv_context=iv))
    day1 = lab.board_context(index, D1, "10:00")
    day2 = lab.board_context(index, D2, "10:00")
    assert day1.public["underlyings"]["U1"]["iv30_prev_close_pct"] == "16.70"
    assert day2.public["underlyings"]["U1"]["iv30_prev_close_pct"] == "17.20"  # never 09-25's
    prompt = lab.board_prompt_v2([], day1)[0]["content"]
    assert "30-day implied-vol index (iv30_prev_close_pct)" in prompt

    plain = outcomes.prepare_index(_raw(PRICES, (D1, D2)))
    context = lab.board_context(plain, D1, "10:00")
    assert "iv30_prev_close_pct" not in context.public["underlyings"]["U1"]
    text = json.loads(lab.board_prompt_v2([], context)[0]["content"])["task"]
    assert "and 20-session realized vol. Choose a holding horizon" in text
    assert "implied" not in text


def test_iv_context_refuses_a_nonpositive_close() -> None:
    with pytest.raises(ValueError):
        outcomes.prepare_index(_raw(PRICES, iv_context={"closes": {"SPY": {"2026-09-23": "0"}}}))


# ------------------------------------------------------------------ selection rule


def test_monthly_expiry_moves_a_holiday_friday_to_the_prior_session() -> None:
    sessions = session_calendar().sessions()
    assert bu.third_friday(2026, 6) == date(2026, 6, 19)  # Juneteenth, a market holiday
    assert bu.monthly_expiry(2026, 6, sessions) == date(2026, 6, 18)
    assert bu.monthly_expiry(2026, 7, sessions) == date(2026, 7, 17)


def test_short_strike_target_hits_the_requested_delta() -> None:
    # hand: z = N^-1(0.8) = 0.841621; sigma sqrt(T) = 0.1; sigma^2 T / 2 = 0.005
    # put 100 exp(0.005 - 0.0841621) = 92.389; call 100 exp(0.0891621) = 109.326
    put = bu.short_strike_target(Decimal(100), Decimal("0.2"), Decimal("0.25"), Decimal("0.2"), "P")
    call = bu.short_strike_target(Decimal(100), Decimal("0.2"), Decimal("0.25"), Decimal("0.2"), "C")
    assert round(float(put), 3) == 92.389
    assert round(float(call), 3) == 109.326

    def delta(strike: float, right: str) -> float:  # Black-Scholes, r = 0, from the definition
        d1 = (math.log(100 / strike) + 0.2 ** 2 * 0.25 / 2) / (0.2 * math.sqrt(0.25))
        return NormalDist().cdf(d1) - (1 if right == "P" else 0)

    assert delta(float(put), "P") == pytest.approx(-0.20, abs=1e-9)
    assert delta(float(call), "C") == pytest.approx(0.20, abs=1e-9)
    with pytest.raises(ValueError):
        bu.short_strike_target(Decimal(100), Decimal("0.2"), Decimal("0.25"), Decimal("0.5"), "P")


def test_snap_ties_go_further_out_of_the_money_and_wings_need_the_exact_width() -> None:
    assert bu.snap([Decimal(95), Decimal(105)], Decimal(100), "P") == Decimal(95)
    assert bu.snap([Decimal(95), Decimal(105)], Decimal(100), "C") == Decimal(105)
    listed = [Decimal(k) for k in (90, 91, 92, 95, 97, 98, 99, 100, 102)]
    widths = (Decimal(1), Decimal(2), Decimal(5))
    # puts: 96 unlisted, 95 and 92 listed; calls: 98 99 102 (100 is width 3: not asked)
    assert bu.wings(listed, Decimal(97), "P", widths) == [Decimal(92), Decimal(95)]
    assert bu.wings(listed, Decimal(97), "C", widths) == [Decimal(98), Decimal(99), Decimal(102)]


def test_served_sessions_dte_and_months() -> None:
    days = [date(2026, 5, 26), date(2026, 6, 4), date(2026, 6, 5), date(2026, 6, 8)]
    served = bu.served_sessions([date(2026, 5, 8), date(2026, 6, 5)], days, date(2026, 6, 8))
    assert served == {date(2026, 5, 8): days[:3], date(2026, 6, 5): days[3:]}
    # 2026-07-17: 52, 43, 42 DTE -> median_low 43; a 63-DTE session is off-board
    assert bu.median_dte(date(2026, 7, 17), days[:3]) == 43
    assert bu.median_dte(date(2026, 7, 17), [date(2026, 5, 15)]) is None
    assert bu.candidate_months(date(2026, 11, 30), date(2026, 12, 2)) == [
        (2026, 11), (2026, 12), (2027, 1), (2027, 2), (2027, 3)]


def _master() -> dict[str, Any]:
    rows = [{"ticker": f"O:SPY260717{r}{k * 1000:08d}", "shares_per_contract": 100}
            for k in range(88, 113) for r in ("C", "P")]
    # a 10-share deliverable at 94.5 (nearer the 94.437 put target) and an
    # adjusted ticker: neither may ever be picked
    rows += [{"ticker": "O:SPY260717P00094500", "shares_per_contract": 10},
             {"ticker": "O:SPY1260717P00094000"}]
    return {"underlying_ticker": "SPY", "pages": [{"results": rows[:30]}, {"results": rows[30:]}]}


def test_select_reference_picks_delta_shorts_and_exact_wings() -> None:
    # one served session 2026-06-01: 07-17 is 46 DTE (06-18 is unlisted here)
    # hand: T = 46/365; sigma sqrt(T) = 0.0710004; sigma^2 T / 2 = 0.0025205
    # put  100 exp(0.0025205 - 0.841621 * 0.0710004) = 94.437 -> 94; wings 93 92 89
    # call 100 exp(0.0025205 + 0.841621 * 0.0710004) = 106.426 -> 106; wings 107 108 111
    picked = bu.select_reference(_master(), "SPY", Decimal(100), Decimal("0.2"),
                                 [date(2026, 6, 1)], session_calendar().sessions(),
                                 (Decimal("0.2"),), (Decimal(1), Decimal(2), Decimal(5)))
    got = {(p["right"], p["strike"], p["role"]) for p in picked}
    assert got == {("P", "94", "short"), ("P", "93", "wing"), ("P", "92", "wing"),
                   ("P", "89", "wing"), ("C", "106", "short"), ("C", "107", "wing"),
                   ("C", "108", "wing"), ("C", "111", "wing")}
    assert {p["dte_ref"] for p in picked} == {46}
    with pytest.raises(ValueError):
        bu.master_chains({"underlying_ticker": "QQQ", "pages": []}, "SPY")


# ------------------------------------------------------------------ bundle assembly


def _selection(widths: Any) -> dict[str, Any]:
    return {"schema": bu.SELECTION_SCHEMA, "vintage": "t", "window_start": "2026-09-23",
            "window_end": "2026-09-25", "rule": {"widths": widths}, "contracts": {
                A: {"listed_from": "2026-09-24", "listed_until": None},
                B: {"listed_from": "2026-09-25", "listed_until": "2026-09-25"},
                C: {"listed_from": "2026-09-24"}}}


def test_assemble_drops_bars_before_listing_and_records_gaps() -> None:
    series = {A: [_bar(D0, 20, 0, "4"),   # 16:00 ET 09-23: before the listing, dropped
                  _bar(D1, 3, 30, "4"),   # 23:30 ET 09-23 (a UTC 09-24 stamp): dropped
                  _bar(D1, 4, 30, "4"),   # 00:30 ET 09-24: kept (cutoff 04:00Z)
                  _bar(D1, 13, 59, "4")],
              B: [_bar(D1, 13, 59, "3.5")]}  # only before its listing: empty
    bundle = bu.assemble_bundle(_selection(["1", "5"]), series, None, selection_sha256="x",
                                captured_at="now", wire_requests=0, sources={})
    assert [bar["t"] for bar in bundle["contracts"][A]["results"]] == [
        series[A][2]["t"], series[A][3]["t"]]
    assert bundle["bars_dropped_before_listing"] == 3
    assert bundle["missing_tickers"] == [C] and bundle["empty_after_listing"] == [B]
    assert bundle["listing"] == {A: {"from": "2026-09-24", "until": None}}
    assert bundle["candidate_pairing"] == {"rule": "widths", "widths": ["1", "5"]}
    assert bundle["schema"] == "desk-option-minute-bars/2"  # pre-v3 readers refuse it
    universe = iag.bundle_contracts(bundle, bundle["contracts"])
    assert universe.widths == frozenset({Decimal(1), Decimal(5)})
    adjacent = bu.assemble_bundle(_selection("adjacent"), series, None, selection_sha256="x",
                                  captured_at="now", wire_requests=0, sources={})
    assert "candidate_pairing" not in adjacent


@pytest.mark.parametrize("body", [
    {"status": "NOT_AUTHORIZED", "ticker": A, "results": []},
    {"status": "OK", "ticker": B, "results": []},
    {"status": "OK", "ticker": A, "results": [], "next_url": "https://api.polygon.io/x"},
    {"status": "OK", "ticker": A, "results": [{"t": 1, "c": 1, "v": 0}]},
    {"status": "OK", "ticker": A, "resultsCount": 2, "results": [{"t": 1, "c": 1, "v": 1}]},
])
def test_verify_body_refuses_unusable_series(body: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        bu.verify_body(A, body)


# ------------------------------------------------------------------ v1 vintage pin

V1_SHA256 = "37de77aa629f30edca74777befecc6d25287ba8c7d9a02a3cbdcf0efd277965f"
V1_REL = Path("evaluations/intraday-graph/20260927-v1/minute-bars-4mo-expanded.json")


def _v1_bundle() -> Path | None:
    from tree_options.desk import paths

    options = [os.environ.get("TREX_V1_PIN_BUNDLE", ""), str(paths.store_root() / V1_REL),
               str(Path.home() / "documents/tree_options/artifacts/desk-store" / V1_REL)]
    return next((Path(p) for p in options if p and Path(p).is_file()), None)


def test_the_frozen_v1_vintage_is_byte_identical_under_the_new_rules() -> None:
    """Digests produced by the pre-change code (main 229b238) on the frozen bundle."""
    path = _v1_bundle()
    if path is None:
        pytest.skip("the 20260927-v1 bundle is not on this host")
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == V1_SHA256
    raw = json.loads(data)
    index = outcomes.prepare_index(raw)
    assert index.iv == {} and getattr(index.contracts, "widths", None) is None
    boards, prompts, count = hashlib.sha256(), hashlib.sha256(), 0
    for day, clock, at in index.timeline:
        candidates = outcomes.board_candidates(index, day, clock)
        count += len(candidates)
        boards.update(json.dumps(candidates, sort_keys=True, separators=(",", ":")).encode())
        if candidates:
            context = lab.board_context(index, day, clock)
            rows = lab.board_rows_v2({"as_of": at.isoformat(), "candidates": candidates}, context)
            prompts.update(json.dumps(lab.board_prompt_v2(rows, context), sort_keys=True).encode())
    assert count == 10298
    assert boards.hexdigest() == "b27d05a9ad473c72027ee56094f8704aa25af7bc54b0c0e3d44aed39075cbb62"
    assert prompts.hexdigest() == "d9ef9c320f262fddc544e73a6d82cdba4eb3bde6093ed819a7a9617a39f57e3e"
    table, rows_seen = hashlib.sha256(), 0
    last = index.sessions[1].isoformat()
    for row in outcomes.outcome_table(index, costs=outcomes.CostModel(), leg_sync_minutes=2):
        if row["snapshot"][2:12] > last:
            break
        table.update(json.dumps(row, separators=(",", ":")).encode() + b"\n")
        rows_seen += 1
    assert rows_seen == 861
    assert table.hexdigest() == "b28a5467aff8864c6eb5e57e21f2fe1624002c14dee126382d238163ddd21a61"
    packets = [iag.decision_packet(raw, d, c)["candidates"]
               for d, c in [(index.sessions[0], "10:00"), (index.sessions[40], "13:00")]]
    assert hashlib.sha256(json.dumps(packets, sort_keys=True).encode()).hexdigest() == \
        "34f2c50c682ecb2095032b8151d2e4747552fceae83848da7456a7fb6c864e14"
