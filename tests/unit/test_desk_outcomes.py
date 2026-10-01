"""Desk environment v2: the per-candidate outcome engine.

Oracle: with the v1 settings (``exit_mode="intraday"``, no costs, no leg
sync) every candidate's outcome equals ``hindsight._candidate_outcome``,
itself pinned to ``iag.replay`` — the v2 engine is the v1 accounting plus
new, explicit knobs, never a re-derivation. The fixtures are hand-laid
minute bars whose every exit mode has a distinct, hand-computed answer.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_desk_hindsight import DAYS, multi_day_bundle
from tests.unit.test_desk_intraday_action_graph import HIGH, LOW, _bundle
from tree_options.desk import hindsight, outcomes
from tree_options.desk import intraday_action_graph as iag
from tree_options.desk.outcomes import (
    EXIT_MODES,
    CostModel,
    candidate_outcome,
    outcome_table,
    prepare_index,
    spot_asof,
    spot_series,
    summarize,
)

LOWX = "O:SPY260918C00500000"  # the same call pair, expiring INSIDE the data
HIGHX = "O:SPY260918C00505000"


def _b(day: date, hour: int, minute: int, price: str) -> dict[str, Any]:
    stamp = datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)
    return {"t": int(stamp.timestamp() * 1000), "c": price, "v": 1}


def _raw(contracts: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    return {
        "schema": "desk-option-minute-bars/1",
        "contracts": {
            ticker: {
                "ticker": ticker,
                "timespan": "minute",
                "results": sorted(bars, key=lambda bar: bar["t"]),
            }
            for ticker, bars in contracts.items()
        },
    }


def _pair(
    long_ticker: str, short_ticker: str, rows: list[tuple[date, int, int, str, str]]
) -> dict[str, Any]:
    low = [_b(d, h, m, lo) for d, h, m, lo, _ in rows]
    high = [_b(d, h, m, hi) for d, h, m, _, hi in rows]
    return _raw({long_ticker: low, short_ticker: high})


def _cid(raw: dict[str, Any], day: date, clock: str, structure: str) -> str:
    return next(
        c["id"]
        for c in iag.decision_packet(raw, day, clock)["candidates"]
        if c["structure"] == structure
    )


# ------------------------------------------------------------------ fixtures
# UTC stamps; September is EDT, so 13:59Z = 09:59 ET and the 10:00 ET board.
# Sep 7 2026 is Labor Day: the bundle's sessions skip it (holiday walk).
D1, D2, D3, D4, D5, D6 = (
    date(2026, 9, 4),
    date(2026, 9, 8),
    date(2026, 9, 9),
    date(2026, 9, 10),
    date(2026, 9, 11),
    date(2026, 9, 14),
)
LADDER = [D1, D2, D3, D4, D5, D6]


def ladder_bundle() -> dict[str, Any]:
    """LOW/HIGH (expiry Oct 16, past the data) over six sessions.

    D1 10:00 call_debit fills 4.1-2.1 = 2.00 at 14:01Z; later spread marks:
    D1 10:45 2.10 | D1 15:15 2.50 | D2 15:15 1.60 | D4 15:15 none (only a
    stale 14:00Z print) -> D5 10:00 5.50 (clamped) | D6 15:15 1.50."""
    return _pair(
        LOW,
        HIGH,
        [
            (D1, 13, 59, "4", "2"),
            (D1, 14, 1, "4.1", "2.1"),
            (D1, 14, 45, "4.4", "2.3"),
            (D1, 19, 15, "5.0", "2.5"),
            (D2, 19, 15, "4.0", "2.4"),
            (D3, 14, 0, "4.2", "2.2"),
            (D3, 14, 1, "4.25", "2.25"),
            (D4, 14, 0, "4.3", "2.2"),
            (D5, 14, 0, "7.5", "2.0"),
            (D6, 19, 15, "3.0", "1.5"),
        ],
    )


E1, E2, E3, E4, E5, E6, E7 = (
    date(2026, 9, 8),
    date(2026, 9, 9),
    date(2026, 9, 17),
    date(2026, 9, 18),
    date(2026, 9, 21),
    date(2026, 9, 22),
    date(2026, 9, 23),
)


def expiry_bundle() -> dict[str, Any]:
    """LOWX/HIGHX expire Sep 18 (E4), inside the data. The planted prints after
    expiry (9/1 -> spread 8) must never value a held position."""
    return _pair(
        LOWX,
        HIGHX,
        [
            (E1, 13, 59, "4", "2"),
            (E1, 14, 1, "4.1", "2.1"),
            (E2, 14, 0, "4.2", "2.2"),
            (E3, 19, 15, "4.6", "2.1"),
            (E4, 19, 0, "6.0", "3.0"),
            (E5, 19, 15, "9", "1"),
            (E6, 19, 15, "9", "1"),
            (E7, 19, 15, "9", "1"),
        ],
    )


SYNC_DAY = date(2026, 9, 24)


def entry_sync_bundle() -> dict[str, Any]:
    """First later prints: LOW 14:01Z, HIGH 14:06Z (5 min apart); LOW prints
    again at 14:07Z (1 min from HIGH)."""
    d = SYNC_DAY
    return _raw(
        {
            LOW: [
                _b(d, 13, 59, "4"),
                _b(d, 14, 1, "4.1"),
                _b(d, 14, 7, "4.2"),
                _b(d, 14, 45, "4.4"),
            ],
            HIGH: [_b(d, 13, 59, "2"), _b(d, 14, 6, "2.15"), _b(d, 14, 45, "2.3")],
        }
    )


def exit_sync_bundle() -> dict[str, Any]:
    """At the 10:45 ET clock the latest prints are LOW 14:45Z / HIGH 14:35Z
    (10 min apart); LOW also printed at 14:36Z (1 min from HIGH)."""
    d = SYNC_DAY
    return _raw(
        {
            LOW: [
                _b(d, 13, 59, "4"),
                _b(d, 14, 1, "4.1"),
                _b(d, 14, 36, "4.3"),
                _b(d, 14, 45, "4.4"),
                _b(d, 15, 30, "4.6"),
            ],
            HIGH: [
                _b(d, 13, 59, "2"),
                _b(d, 14, 1, "2.1"),
                _b(d, 14, 35, "2.2"),
                _b(d, 15, 30, "2.4"),
            ],
        }
    )


def random_bundle(seed: int, days: list[date]) -> dict[str, Any]:
    """Irregular prints on two underlyings, both rights, three strikes: a mix
    of fills, no-fills, overnight exits and never-closed trades."""
    rng = random.Random(seed)
    contracts: dict[str, list[dict[str, Any]]] = {}
    for symbol, base in (("SPY", 500), ("QQQ", 400)):
        for right in "CP":
            for strike in (base, base + 5, base + 10, base + 15):
                bars: list[dict[str, Any]] = []
                for day in days:
                    minutes = sorted(rng.sample(range(13 * 60 + 30, 20 * 60), rng.randint(5, 90)))
                    bars += [
                        _b(day, m // 60, m % 60, f"{rng.randint(20, 500) / 100:.2f}")
                        for m in minutes
                    ]
                contracts[f"O:{symbol}261016{right}{strike * 1000:08d}"] = bars
    return _raw(contracts)


#: synthetic underlyings: (first close, strike grid centre); distinctive levels
#: so a leaked spot level is detectable as a string
SPOT_BASE = {"SPY": (Decimal("611.83"), 612), "QQQ": (Decimal("527.41"), 527)}


def spot_path(symbol: str, i: int) -> tuple[Decimal, Decimal]:
    """(morning spot, closing spot) of session ``i``: prints before 16:00Z
    (12:00 ET) price off the morning spot, later prints off the close."""
    start, _ = SPOT_BASE[symbol]
    close = (start + Decimal("0.37") * i + Decimal((i * 7) % 5) / 4).quantize(Decimal("0.01"))
    return close - Decimal("0.80"), close


def _time_value(strike: Decimal, spot: Decimal) -> Decimal:
    return max(Decimal("0.05"), Decimal("1.5") - abs(strike - spot) / 4).quantize(Decimal("0.01"))


def parity_bundle(days: list[date], *, far_carry: Decimal | None = None) -> dict[str, Any]:
    """Calls and puts priced so C - P = S - K EXACTLY on the Sep 18 expiry;
    an optional far (Oct 16) expiry carries ``far_carry`` of parity error,
    which the nearest-expiry estimate must ignore. Prints every 15 min."""
    expiries = [("260918", Decimal(0))]
    if far_carry is not None:
        expiries.append(("261016", far_carry))
    contracts: dict[str, list[dict[str, Any]]] = {}
    for symbol, (_, mid) in SPOT_BASE.items():
        for code, carry in expiries:
            for strike in range(mid - 4, mid + 5, 2):
                for right in "CP":
                    bars = []
                    for i, day in enumerate(days):
                        s_open, s_close = spot_path(symbol, i)
                        for minute in range(13 * 60 + 30, 20 * 60, 15):
                            spot = s_open if minute < 16 * 60 else s_close
                            tv = _time_value(Decimal(strike), spot)
                            call = max(spot - strike, Decimal(0)) + tv + carry
                            put = max(strike - spot, Decimal(0)) + tv
                            price = call if right == "C" else put
                            bars.append(_b(day, minute // 60, minute % 60, str(price)))
                    contracts[f"O:{symbol}{code}{right}{strike * 1000:08d}"] = bars
    return _raw(contracts)


def parity_days(count: int) -> list[date]:
    from tree_options.trex.clock import session_calendar

    return [d for d in session_calendar().sessions() if d >= date(2026, 8, 3)][:count]


# ------------------------------------------------------------------ oracle


def _oracle(raw: dict[str, Any], sessions: list[date]) -> tuple[int, int]:
    """Every candidate on every board: v2 with v1 settings == hindsight."""
    index = prepare_index(raw, sessions)
    bars, _ = hindsight.parse_bundle(raw)
    checked = closed = 0
    for day in sessions:
        for clock in iag.schedule_for(day):
            for candidate in iag.decision_packet(raw, day, clock)["candidates"]:
                expected = hindsight._candidate_outcome(
                    bars, sessions, day, clock, candidate, 15 * 60
                )
                got = candidate_outcome(index, day, clock, candidate["id"])
                assert got is not None, candidate["id"]
                if expected is None:
                    assert got["status"] != "closed", (day, clock, candidate["id"])
                else:
                    assert got["status"] == "closed", (day, clock, candidate["id"])
                    assert got["gross"] == expected and got["net"] == expected
                checked += 1
                closed += expected is not None
    return checked, closed


def test_oracle_single_day_fixture_equals_hindsight() -> None:
    day = date(2026, 9, 24)
    checked, closed = _oracle(_bundle(day), [day])
    assert (checked, closed) == (4, 2)  # 10:00 closes both orientations; 10:45 cannot fill


def test_oracle_multi_day_fixture_equals_hindsight() -> None:
    checked, closed = _oracle(multi_day_bundle(*DAYS), DAYS)
    assert checked >= 12 and closed == 6


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_oracle_irregular_prints_equal_hindsight(seed: int) -> None:
    days = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)]
    checked, closed = _oracle(random_bundle(seed, days), days)
    assert closed >= 20 and checked - closed >= 20, (checked, closed)  # non-vacuous both ways


def test_oracle_ladder_and_parity_fixtures_equal_hindsight() -> None:
    assert _oracle(ladder_bundle(), LADDER)[1] >= 2
    assert _oracle(expiry_bundle(), [E1, E2, E3, E4, E5, E6, E7])[1] >= 1
    days = parity_days(3)
    assert _oracle(parity_bundle(days), days)[1] >= 50


# ------------------------------------------------------------------ exit modes


def _all_modes(raw: dict[str, Any], day: date, structure: str, **kw: Any) -> dict[str, Any]:
    index = prepare_index(raw)
    cid = _cid(raw, day, "10:00", structure)
    return {
        mode: candidate_outcome(index, day, "10:00", cid, exit_mode=mode, **kw)
        for mode in EXIT_MODES
    }


def test_exit_modes_on_the_ladder() -> None:
    got = _all_modes(ladder_bundle(), D1, "call_debit")
    table = {mode: (o["status"], o["gross"], o["exit_reason"]) for mode, o in got.items()}
    assert table == {
        "intraday": ("closed", Decimal("10"), "next_clock_mark"),
        "eod": ("closed", Decimal("50"), "eod_last_mark"),
        # Fri D1 + 1 bundle session = Tue D2 (Labor Day is absent)
        "hold:1": ("closed", Decimal("-40"), "hold_mark"),
        # D4's last clock has no fresh mark: walk forward to D5 10:00, where the
        # 5.50 mark (+350) clamps at the entry-time max gain 300
        "hold:3": ("closed", Decimal("300"), "hold_mark"),
        "hold:5": ("closed", Decimal("-50"), "hold_mark"),
        # past the data end: marked at the last synced mark, never dropped
        "hold:10": ("marked_at_end", Decimal("-50"), "target_past_data_end"),
        "expiry": ("marked_at_end", Decimal("-50"), "target_past_data_end"),
    }
    assert got["intraday"]["entry_at"] == "2026-09-04T14:01:00+00:00"
    assert got["intraday"]["exit_at"] == "2026-09-04T14:45:00+00:00"
    assert got["intraday"]["hold_minutes"] == 44
    assert got["hold:1"]["exit_at"] == "2026-09-08T19:15:00+00:00"
    assert got["hold:3"]["exit_at"] == "2026-09-11T14:00:00+00:00"
    assert got["hold:10"]["exit_at"] == "2026-09-14T19:15:00+00:00"
    assert all(o["net"] == o["gross"] for o in got.values())  # no costs -> net == gross


def test_credit_orientation_mirrors_the_debit() -> None:
    got = _all_modes(ladder_bundle(), D1, "call_credit")
    assert got["intraday"]["gross"] == Decimal("-10")
    assert got["eod"]["gross"] == Decimal("-50")
    assert got["hold:3"]["gross"] == Decimal("-300")  # clamped at max loss 300


def test_eod_walks_to_the_next_session_when_the_day_has_no_later_mark() -> None:
    raw = ladder_bundle()
    index = prepare_index(raw)
    out = candidate_outcome(
        index, D3, "10:00", _cid(raw, D3, "10:00", "call_debit"), exit_mode="eod"
    )
    assert out is not None
    assert (out["status"], out["gross"], out["exit_reason"]) == (
        "closed",
        Decimal("10"),
        "eod_next_session_mark",
    )
    assert out["exit_at"] == "2026-09-10T14:00:00+00:00"


def test_expiry_inside_the_data_caps_every_horizon() -> None:
    got = _all_modes(expiry_bundle(), E1, "call_debit")
    table = {mode: (o["status"], o["gross"], o["exit_reason"]) for mode, o in got.items()}
    assert table == {
        "intraday": ("closed", Decimal("0"), "next_clock_mark"),
        "eod": ("closed", Decimal("0"), "eod_next_session_mark"),
        "hold:1": ("closed", Decimal("50"), "hold_mark"),
        "hold:3": ("closed", Decimal("100"), "hold_mark"),  # target = the expiry day
        "hold:5": ("closed", Decimal("100"), "capped_at_expiry"),  # target after expiry
        "hold:10": ("closed", Decimal("100"), "capped_at_expiry"),  # target past the data
        "expiry": ("closed", Decimal("100"), "expiry_last_mark"),
    }
    assert got["expiry"]["exit_at"] == "2026-09-18T19:15:00+00:00"


def test_a_fill_with_no_later_mark_is_marked_at_entry_not_dropped() -> None:
    day = date(2026, 9, 24)
    raw = _pair(
        LOW,
        HIGH,
        [(day, 13, 59, "4", "2"), (day, 14, 1, "4.1", "2.1"), (day, 14, 2, "4.15", "2.15")],
    )
    index = prepare_index(raw)
    cid = _cid(raw, day, "10:00", "call_debit")
    for mode in EXIT_MODES:
        out = candidate_outcome(index, day, "10:00", cid, exit_mode=mode, costs=CostModel())
        assert out is not None
        assert (out["status"], out["gross"], out["exit_reason"]) == (
            "marked_at_end",
            Decimal("0"),
            "no_mark_after_entry",
        ), mode
        assert out["net"] == Decimal("-14.60")
        assert out["exit_at"] == out["entry_at"] == "2026-09-24T14:01:00+00:00"


def test_no_fill_statuses_and_reasons() -> None:
    day = date(2026, 9, 24)
    raw = _bundle(day)
    index = prepare_index(raw)
    for candidate in iag.decision_packet(raw, day, "10:45")["candidates"]:
        out = candidate_outcome(index, day, "10:45", candidate["id"], costs=CostModel())
        assert out == {
            "gross": None,
            "net": None,
            "entry_at": None,
            "exit_at": None,
            "hold_minutes": None,
            "status": "no_fill",
            "exit_reason": "missing_later_entry_bars",
        }
    # a fill the replay risk caps refuse: the credit's max loss 5-0.5 = 450 > 300
    refused = _pair(
        LOW,
        HIGH,
        [(day, 13, 59, "4", "2"), (day, 14, 1, "2.6", "2.1"), (day, 14, 45, "4.4", "2.3")],
    )
    out = candidate_outcome(
        prepare_index(refused), day, "10:00", _cid(refused, day, "10:00", "call_credit")
    )
    assert out is not None and (out["status"], out["exit_reason"]) == ("no_fill", "entry_risk_cap")


# ------------------------------------------------------------------ leg sync


def test_entry_sync_walks_forward_to_a_synced_pair_or_no_fill() -> None:
    raw = entry_sync_bundle()
    index = prepare_index(raw)
    cid = _cid(raw, SYNC_DAY, "10:00", "call_debit")
    loose = candidate_outcome(index, SYNC_DAY, "10:00", cid)
    synced = candidate_outcome(index, SYNC_DAY, "10:00", cid, leg_sync_minutes=2)
    strict = candidate_outcome(index, SYNC_DAY, "10:00", cid, leg_sync_minutes=0)
    assert loose is not None and synced is not None and strict is not None
    # unsynced: 4.10 (14:01) - 2.15 (14:06) = 1.95 -> mark 2.10 -> +15
    assert (loose["gross"], loose["entry_at"]) == (Decimal("15"), "2026-09-24T14:06:00+00:00")
    # synced within 2 min: LOW walks to 14:07 -> 4.20 - 2.15 = 2.05 -> +5
    assert (synced["gross"], synced["entry_at"]) == (Decimal("5"), "2026-09-24T14:07:00+00:00")
    assert (strict["status"], strict["exit_reason"]) == ("no_fill", "legs_out_of_sync")


def test_exit_sync_uses_the_most_recent_synced_pair_or_walks_forward() -> None:
    raw = exit_sync_bundle()
    index = prepare_index(raw)
    cid = _cid(raw, SYNC_DAY, "10:00", "call_debit")
    loose = candidate_outcome(index, SYNC_DAY, "10:00", cid)
    synced = candidate_outcome(index, SYNC_DAY, "10:00", cid, leg_sync_minutes=2)
    strict = candidate_outcome(index, SYNC_DAY, "10:00", cid, leg_sync_minutes=0)
    assert loose is not None and synced is not None and strict is not None
    # latest prints 4.40 (14:45) / 2.20 (14:35): 2.20 -> +20 at 10:45 ET
    assert (loose["gross"], loose["exit_at"]) == (Decimal("20"), "2026-09-24T14:45:00+00:00")
    # synced: the 14:36/14:35 pair 4.30/2.20 = 2.10 -> +10, still at 10:45 ET
    assert (synced["gross"], synced["exit_at"]) == (Decimal("10"), "2026-09-24T14:45:00+00:00")
    # no same-minute pair at 10:45: walk forward to 11:30 ET (4.60 - 2.40)
    assert (strict["gross"], strict["exit_at"]) == (Decimal("20"), "2026-09-24T15:30:00+00:00")


# ------------------------------------------------------------------ costs


def test_cost_model_round_trip_math() -> None:
    assert CostModel().round_trip() == Decimal("14.60")  # 4 x 3.00 + 4 x 0.65
    wide = CostModel(half_spread_per_share=Decimal("0.06"))
    assert wide.round_trip() == Decimal("26.60")
    assert CostModel(
        commission_per_leg=Decimal(0), half_spread_per_share=Decimal("0.001"), multiplier=100
    ).round_trip() == Decimal("0.400")
    got = _all_modes(ladder_bundle(), D1, "call_debit", costs=wide)
    assert got["intraday"]["gross"] == Decimal("10")
    assert got["intraday"]["net"] == Decimal("-16.60")
    assert all(o["net"] == o["gross"] - Decimal("26.60") for o in got.values())


# ------------------------------------------------------------------ validation


def test_arguments_are_validated() -> None:
    raw = ladder_bundle()
    index = prepare_index(raw)
    cid = _cid(raw, D1, "10:00", "call_debit")
    with pytest.raises(ValueError):
        candidate_outcome(index, D1, "10:00", cid, exit_mode="hold:2")
    with pytest.raises(ValueError):
        candidate_outcome(index, D1, "10:00", cid, leg_sync_minutes=-1)
    with pytest.raises(ValueError):
        candidate_outcome(index, D1, "10:07", cid)  # not a scheduled clock
    assert candidate_outcome(index, D1, "10:00", "not-on-this-board") is None
    with pytest.raises(ValueError):
        prepare_index(raw, [D2, D1])


# ------------------------------------------------------------------ spot


def test_spot_series_recovers_the_known_spot_by_put_call_parity() -> None:
    days = parity_days(6)
    index = prepare_index(parity_bundle(days, far_carry=Decimal("1.00")))
    series = spot_series(index)
    assert set(series) == {"SPY", "QQQ"}
    for symbol in ("SPY", "QQQ"):
        assert series[symbol] == {day: spot_path(symbol, i)[1] for i, day in enumerate(days)}
    for i, day in enumerate(days):
        morning = spot_asof(index, iag._instant(day, "10:00"))
        assert morning == {s: spot_path(s, i)[0] for s in ("SPY", "QQQ")}


def test_spot_asof_uses_only_bars_at_or_before_the_instant() -> None:
    days = parity_days(2)
    raw = parity_bundle(days)
    at = iag._instant(days[1], "10:00")
    before = spot_asof(prepare_index(raw), at)
    for ticker, body in raw["contracts"].items():
        if iag.parse_contract(ticker).right == "C":  # a wild call print 5 min AFTER the instant
            body["results"].append(_b(days[1], 14, 5, "99.99"))
            body["results"].sort(key=lambda bar: bar["t"])
    planted = prepare_index(raw)
    assert spot_asof(planted, at) == before
    assert spot_asof(planted, iag._instant(days[1], "10:05")) != before


# ------------------------------------------------------------------ table + CLI


def test_outcome_table_covers_every_candidate_and_mode() -> None:
    raw = ladder_bundle()
    index = prepare_index(raw)
    rows = list(outcome_table(index, costs=CostModel(), leg_sync_minutes=2))
    boards = [(d, c) for d in LADDER for c in iag.schedule_for(d)]
    expected = sum(len(iag.decision_packet(raw, d, c)["candidates"]) for d, c in boards)
    assert len(rows) == expected * len(EXIT_MODES) and expected >= 4
    assert {r["status"] for r in rows} <= {"closed", "marked_at_end", "no_fill"}
    assert {r["exit_mode"] for r in rows} == set(EXIT_MODES)
    json.dumps(rows)  # JSON-ready
    first = next(
        r
        for r in rows
        if r["snapshot"] == f"s:{D1}T10:00"
        and r["structure"] == "call_debit"
        and r["exit_mode"] == "hold:3"
    )
    assert first["direction"] == "bullish" and Decimal(first["gross"]) == Decimal("300")
    assert Decimal(first["net"]) == Decimal("300") - Decimal("14.60")
    assert first["status"] == "closed" and first["candidate_id"]
    summary = summarize(rows)
    assert summary["modes"]["hold:3"]["rows"] == expected
    assert set(summary["modes"]["intraday"]["by_direction"]) == {"bullish", "bearish"}


def test_outcome_table_cli_writes_rows_and_a_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tree_options.desk.__main__ import run_cli

    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(ladder_bundle()))
    out = tmp_path / "table.jsonl"
    rc = run_cli(
        [
            "outcome-table",
            "--bundle",
            str(bundle),
            "--out",
            str(out),
            "--sync",
            "2",
            "--half-spread",
            "0.06",
        ]
    )
    assert rc == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert rows and all(r["exit_mode"] in EXIT_MODES for r in rows)
    first = next(
        r
        for r in rows
        if r["snapshot"] == f"s:{D1}T10:00"
        and r["structure"] == "call_debit"
        and r["exit_mode"] == "intraday"
    )
    assert (Decimal(first["gross"]), first["net"]) == (Decimal("10"), "-16.60")
    summary = json.loads(Path(f"{out}.summary.json").read_text())
    assert summary["rows"] == len(rows)
    assert summary["sync_minutes"] == 2 and summary["round_trip_cost"] == "26.60"
    assert json.loads(capsys.readouterr().out)["rows"] == len(rows)
    assert (
        run_cli(["outcome-table", "--bundle", str(bundle), "--out", str(out), "--sync", "-3"]) == 2
    )
    assert outcomes.EXIT_MODES == (
        "intraday",
        "eod",
        "hold:1",
        "hold:3",
        "hold:5",
        "hold:10",
        "expiry",
    )
