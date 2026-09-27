from datetime import UTC, date, datetime, timedelta

import pytest

from tree_options.desk.intraday_action_graph import (
    decision_packet,
    replay,
    schedule_for,
    windows,
)

LOW = "O:SPY261016C00500000"
HIGH = "O:SPY261016C00505000"


def _bar(day: date, hour: int, minute: int, price: str) -> dict:
    stamp = datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)
    return {"t": int(stamp.timestamp()*1000), "c": price, "v": 1}


def _bundle(day: date, *, observed: bool = True) -> dict:
    low = [_bar(day, 13, 59, "4"), _bar(day, 14, 1, "4.1"),
           _bar(day, 14, 45, "4.4")]
    high = [_bar(day, 13, 59, "2"), _bar(day, 14, 1, "2.1"),
            _bar(day, 14, 45, "2.3")]
    if not observed:
        low = low[1:]
        high = high[1:]
    return {"schema": "desk-option-minute-bars/1", "contracts": {
        LOW: {"ticker": LOW, "timespan": "minute", "results": low},
        HIGH: {"ticker": HIGH, "timespan": "minute", "results": high}}}


def test_no_lookahead_and_graph_edges() -> None:
    day = date(2026, 9, 25)
    missing = replay(_bundle(day, observed=False), [day])
    assert not any(edge["from"] == f"s:{day}T10:00" and edge["kind"] == "available_as_of"
                   for edge in missing["edges"])
    initial = replay(_bundle(day), [day])
    candidates = [n for n in initial["nodes"] if n["kind"] == "potential_trade"]
    assert len(candidates) == 4
    sid = f"s:{day}T10:00"
    debit = next(c for c in candidates if c["structure"] == "call_debit"
                 and any(edge["from"] == sid and edge["to"] == c["id"]
                         for edge in initial["edges"]))
    selected = replay(_bundle(day), [day], {sid: debit["id"]})
    assert selected["entered"] == 1
    assert selected["closed_capital_proxy"] == "5010.0"
    assert any(edge["kind"] == "chosen_as" for edge in selected["edges"])
    assert selected["execution_authorized"] is False
    packet = decision_packet(_bundle(day), day, "10:00")
    assert packet["candidates"]
    assert "4.4" not in str(packet)  # later mark is withheld from the decision
    assert "pnl" not in str(packet)
    assert replay(_bundle(day), [day], policy="call_debit")["entered"] >= 1


def test_stale_choice_and_three_month_windows() -> None:
    day = date(2026, 9, 25)
    result = replay(_bundle(day), [day], {f"s:{day}T10:00": "future-unknown"})
    assert result["entered"] == 0
    assert result["actions"][0]["reason"] == "unknown_or_stale_candidate"
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(260)]
    spans = windows(days, stride_sessions=30)
    assert len(spans) >= 4
    assert all((end-start).days >= 80 for start, end in spans)
    with pytest.raises(ValueError):
        replay(_bundle(day), [day], {"s:2026-09-24T10:00": None})
    assert len(schedule_for(date(2026, 11, 27))) == 5


def test_combined_open_loss_cap_with_missing_exit_marks() -> None:
    day = date(2026, 9, 25)
    contracts = {}
    for index, clock_hour in enumerate((14, 14, 15, 16, 17, 17)):
        minute = (0, 45, 30, 15, 0, 45)[index]
        before = datetime(day.year, day.month, day.day, clock_hour, minute, tzinfo=UTC) - timedelta(minutes=1)
        symbol = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")[index]
        for strike, price in ((500, "4"), (505, "2")):
            ticker = f"O:{symbol}261016C{strike*1000:08d}"
            contracts[ticker] = {"ticker": ticker, "timespan": "minute", "results": [
                _bar(day, before.hour, before.minute, price),
                _bar(day, clock_hour, minute+1, price),
            ]}
    result = replay({"schema": "desk-option-minute-bars/1", "contracts": contracts},
                    [day], policy="call_credit")
    assert result["entered"] == 5
    assert result["peak_open_loss_reserved"] == "1500"
    assert result["open_at_end"] == 5
    assert any(a["reason"] == "combined_open_loss_cap" for a in result["actions"])
