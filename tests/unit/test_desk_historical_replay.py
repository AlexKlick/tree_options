from __future__ import annotations

from datetime import date

from tree_options.desk import historical_replay as replay
from tree_options.desk import ivhist


class Calendar:
    def sessions(self) -> tuple[date, ...]:
        return tuple(date.fromisoformat(d) for d in (
            "2025-03-03", "2025-03-04", "2025-03-05", "2025-03-06", "2025-03-07"))


def bar(day: str, right: str, strike: float, price: float) -> ivhist.OptionBar:
    return ivhist.OptionBar(date.fromisoformat(day), right, strike, price)


def test_cross_cache_conflicts_are_dropped() -> None:
    day = date(2025, 3, 4)
    a = ivhist.CacheScan(source="a", options={"SPY": {day: [bar("2025-04-18", "C", 100, 4)]}},
                         spot={"SPY": {day: 100}})
    b = ivhist.CacheScan(source="b", options={"SPY": {day: [bar("2025-04-18", "C", 100, 5)]}},
                         spot={"SPY": {day: 101}})
    merged = replay.merge_scans((a, b))
    assert merged.options == {}
    assert merged.spot["SPY"] == {}
    assert merged.stats["cross_cache_option_conflicts"] == 1
    assert merged.stats["cross_cache_spot_conflicts"] == 1


def test_replay_uses_next_session_entry_and_records_missing_exit(monkeypatch) -> None:
    decision = date(2025, 3, 3)
    entry = date(2025, 3, 4)
    exit_day = date(2025, 3, 5)
    expiry = date(2025, 4, 18)
    rows = [ivhist.OptionBar(expiry, "C", 100, 4), ivhist.OptionBar(expiry, "P", 100, 3)]
    entry_rows = [*rows, ivhist.OptionBar(expiry, "C", 110, 2),
                  ivhist.OptionBar(expiry, "P", 110, 8)]
    scan = ivhist.CacheScan(source="fixture", options={"SPY": {decision: rows, entry: entry_rows,
                               exit_day: [ivhist.OptionBar(expiry, "C", 100, 5)]}},
                            spot={"SPY": {decision: 100, entry: 110}})
    monkeypatch.setattr(replay, "_signals_on", lambda *_args: [("xsmom_top3", "SPY")])
    spec = replay.ReplaySpec(date(2025, 3, 3), date(2025, 3, 3), ("SPY",),
                             signals=("xsmom_top3",), structures=("long_call",),
                             hold_sessions=1, max_loss=500)
    result = replay.replay(scan, {}, {}, Calendar(), spec)
    assert result["counts"]["evaluable_within_trade_cap"] == 1
    assert result["rows"][0]["entry"] == "2025-03-04"
    assert result["rows"][0]["selection_as_of"] == "2025-03-03"
    assert result["rows"][0]["legs"][0]["strike"] == 100
    assert result["rows"][0]["exit"] == "2025-03-05"
    assert result["rows"][0]["pnl"] < 100  # haircut and two commissions
    scan.options["SPY"].pop(exit_day)
    missing = replay.replay(scan, {}, {}, Calendar(), spec)
    assert missing["counts"]["missing_exit_or_entry_bar"] == 1
    assert missing["rows"] == []


def test_risk_cap_excludes_trade_without_turning_it_into_loss(monkeypatch) -> None:
    decision = date(2025, 3, 3)
    entry = date(2025, 3, 4)
    exit_day = date(2025, 3, 5)
    expiry = date(2025, 4, 18)
    decision_rows = [ivhist.OptionBar(expiry, "C", 100, 4),
                     ivhist.OptionBar(expiry, "P", 100, 3)]
    scan = ivhist.CacheScan(source="fixture", options={"SPY": {decision: decision_rows, entry: [
        ivhist.OptionBar(expiry, "C", 100, 4), ivhist.OptionBar(expiry, "P", 100, 3)],
        exit_day: [ivhist.OptionBar(expiry, "C", 100, 5)]}},
        spot={"SPY": {decision: 100}})
    monkeypatch.setattr(replay, "_signals_on", lambda *_args: [("xsmom_top3", "SPY")])
    spec = replay.ReplaySpec(date(2025, 3, 3), date(2025, 3, 3), ("SPY",),
                             structures=("long_call",), hold_sessions=1, max_loss=300)
    result = replay.replay(scan, {}, {}, Calendar(), spec)
    assert result["counts"]["over_trade_loss_cap"] == 1
    assert not result["rows"]
