from tree_options.desk.model_game import prepare, score


def _row(decision: str, entry: str, exit_day: str, name: str, pnl: int,
         max_loss: int = 100) -> dict[str, object]:
    return {
        "decision": decision, "entry": entry, "exit": exit_day,
        "expiry": "2026-10-16", "name": name, "signal": "xsmom_top3",
        "structure": "call_debit", "max_loss": max_loss, "pnl": pnl,
        "legs": [{"right": "C", "side": 1, "strike": 100}],
    }


def test_blind_packet_omits_outcomes_and_source_identity() -> None:
    replay = {"schema": "desk-historical-replay/1", "rows": [
        _row("2025-08-01", "2025-08-04", "2025-08-11", "AAA", 20),
        _row("2025-09-02", "2025-09-03", "2025-09-10", "BBB", -30),
    ]}
    packet, hidden = prepare(replay)
    assert len(packet["training"]) == len(packet["blind_candidates"]) == 1
    assert packet["training"][0]["modeled_pnl"] == "20"
    assert "modeled_pnl" not in packet["blind_candidates"][0]
    assert "BBB" not in str(packet)
    assert score(hidden, list(hidden))["modeled_closed_pnl"] == "-30"


def test_overlap_and_invalid_selection_are_fail_closed() -> None:
    later = [_row("2025-09-02", "2025-09-03", "2025-09-10", f"B{i}",
                  -100 if i == 0 else 10, 300) for i in range(6)]
    replay = {"schema": "desk-historical-replay/1", "rows": [
        _row("2025-08-01", "2025-08-04", "2025-08-11", "AAA", 20), *later,
    ]}
    _, hidden = prepare(replay)
    result = score(hidden, list(hidden))
    assert result["admitted"] == 5
    assert result["skipped"]["open_cap"] == 1
    assert result["peak_open_loss_reserved"] == "1500"
    assert result["modeled_closed_pnl"] == "-60"
