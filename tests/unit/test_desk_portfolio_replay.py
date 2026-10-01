from decimal import Decimal

import pytest

from tree_options.desk.portfolio_replay import simulate


def row(day: str, exit_day: str, name: str, loss: int, pnl: int) -> dict[str, object]:
    return {
        "signal": "xsmom_top3",
        "structure": "call_debit",
        "name": name,
        "decision": day,
        "entry": day,
        "exit": exit_day,
        "expiry": "2025-05-16",
        "max_loss": loss,
        "pnl": pnl,
    }


def report(*rows: dict[str, object]) -> dict[str, object]:
    return {
        "schema": "desk-historical-replay/1",
        "by_variant": {"xsmom_top3/call_debit": {}},
        "rows": list(rows),
    }


def test_open_risk_reservation_blocks_overlap() -> None:
    result = simulate(
        report(
            row("2025-03-04", "2025-03-07", "AAA", 200, 40),
            row("2025-03-05", "2025-03-08", "BBB", 200, 20),
            row("2025-03-10", "2025-03-11", "CCC", 200, -50),
        ),
        max_open_loss=Decimal("300"),
    )
    cell = result["variants"]["xsmom_top3/call_debit"]
    assert cell["admitted"] == 2
    assert cell["skipped"] == {"trade_cap": 0, "open_cap": 1, "capital": 0}
    assert cell["peak_open_loss_reserved"] == "200"
    assert cell["closed_pnl"] == "-10"


def test_same_day_exit_does_not_free_risk_for_entry() -> None:
    result = simulate(
        report(
            row("2025-03-04", "2025-03-06", "AAA", 200, 40),
            row("2025-03-06", "2025-03-10", "BBB", 200, 20),
        ),
        max_open_loss=Decimal("300"),
    )
    assert result["variants"]["xsmom_top3/call_debit"]["skipped"]["open_cap"] == 1


def test_trade_cap_is_rechecked_and_daily_stop_is_not_claimed() -> None:
    result = simulate(report(row("2025-03-04", "2025-03-07", "AAA", 301, 10)))
    assert result["variants"]["xsmom_top3/call_debit"]["skipped"]["trade_cap"] == 1
    assert "daily loss stops cannot be tested" in " ".join(result["limitations"])


def test_malformed_rows_and_limits_are_rejected() -> None:
    with pytest.raises(ValueError, match="exit must follow entry"):
        simulate(report(row("2025-03-04", "2025-03-04", "AAA", 100, 10)))
    with pytest.raises(ValueError, match="invalid portfolio risk limits"):
        simulate(report(), max_open_loss=Decimal("6000"))
    bad = row("2025-03-04", "2025-03-07", "AAA", 100, 10)
    bad["pnl"] = "bad"
    with pytest.raises(ValueError, match="pnl must be numeric"):
        simulate(report(bad))
    bad["pnl"] = 10
    bad["max_loss"] = "bad"
    with pytest.raises(ValueError, match="max_loss must be numeric"):
        simulate(report(bad))
    bad["max_loss"] = 100
    bad["pnl"] = -101
    with pytest.raises(ValueError, match="exceeds declared worst-case"):
        simulate(report(bad))
