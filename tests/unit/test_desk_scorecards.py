"""Desk D7 scorecards: aggregation and the promotion/pause/retire rules.

Episodes are written directly as desk-shadow/1 documents (the shadows
module's own tests cover how they are produced); every expected figure is
computed by hand here.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tree_options.desk import scorecards, shadows

D = Decimal


def _entry_of(week: str) -> str:
    """The Thursday of an ISO week key, the session the scorecards cluster on."""
    year, wn = week.split("-W")
    from datetime import date

    return date.fromisocalendar(int(year), int(wn), 4).isoformat()


def _episode(
    deal_id: str,
    *,
    row: str = "R1",
    basis: str = "xsmom",
    week: str = "2025-W11",
    pnl: str = "100.00",
    ev: str | None = "20.00",
    slippage: str | None = "-10.00",
    stress: str | None = "-5.00",
    fallback: bool = False,
    resolved: bool = True,
) -> dict[str, Any]:
    return {
        "schema": "desk-shadow/1",
        "deal_id": deal_id,
        "row": row,
        "row_title": "row",
        "tier": "signal-validated",
        "underlying": "AAPL",
        "kind": "debit_vertical",
        "quantity": 1,
        "legs": [],
        "entry_session": _entry_of(week),
        "exit_deadline": "2025-04-14",
        "fill": "2.20",
        "width": "5.00",
        "max_loss": "220.00",
        "decision": (
            {"basis": basis, "ev": ev, "ev_stress_fill": stress, "ev_per_max_loss": "0.09"}
            if ev is not None or stress is not None
            else None
        ),
        "mine_status": "surfaced",
        "mine_reasons": [],
        "marks": [],
        "resolution": (
            {
                "session": "2025-04-14",
                "deadline": "2025-04-14",
                "fallback": fallback,
                "mid": None,
                "realistic": "3.20",
                "pnl_dollars": pnl,
                "decision_ev": ev,
                "pnl_minus_ev_dollars": None,
                "first_mark_realistic": "2.10",
                "slippage_first_mark_dollars": slippage,
            }
            if resolved
            else None
        ),
        "state": "resolved" if resolved else "open",
    }


def _write(tmp_path: Path, *episodes: dict[str, Any]) -> Path:
    eps = tmp_path / "shadows" / "episodes"
    eps.mkdir(parents=True, exist_ok=True)
    for ep in episodes:
        (eps / f"{ep['deal_id']}.json").write_text(json.dumps(ep))
    return tmp_path / "shadows"


def _views(*docs: dict[str, Any]) -> list[scorecards.EpisodeView]:
    out = []
    for d in docs:
        v = scorecards.episode_view(shadows.episode_from_doc(d))
        assert v is not None
        out.append(v)
    return out


def test_card_math_on_a_small_family(tmp_path: Path) -> None:
    state = _write(
        tmp_path,
        _episode("a", pnl="100.00", week="2025-W11"),
        _episode("b", pnl="-60.00", week="2025-W11"),
        _episode("c", pnl="40.00", week="2025-W12"),
        _episode("open", resolved=False),
    )
    doc = scorecards.build_scorecards(state)
    assert doc["episodes_total"] == 4 and doc["resolved_total"] == 3
    card = doc["families"]["row:R1"]
    assert card["n_resolved"] == 3
    assert D(card["pnl"]["total_dollars"]) == D("80.00")
    assert card["pnl"]["win_rate"] == pytest.approx(2 / 3)
    assert card["pnl"]["n_weeks"] == 2
    # weekly means: W11 +20, W12 +40; t = 30 / (sd / sqrt(2))
    sd = math.sqrt(((20.0 - 30.0) ** 2 + (40.0 - 30.0) ** 2) / 1)
    assert card["pnl"]["weekly_t_stat"] == pytest.approx(30.0 / (sd / math.sqrt(2)))
    assert card["prediction"]["n_with_ev"] == 3
    assert D(card["prediction"]["mean_decision_ev_dollars"]) == D("20.00")
    # per-episode pnl - ev: 80, -80, 20 -> mean 20/3
    assert D(card["prediction"]["mean_realized_minus_ev_dollars"]) == D(
        repr(20.0 / 3.0)
    )
    # the literals are the PLAN's numbers (>= 20 resolved promotes, a
    # trailing 6-week tail, the -$1,000 pause drawdown), pinned on purpose
    # so a change to any of them must show up here
    assert card["rules"] == {
        "promotion_ready": False,
        "min_resolved": 20,
        "retire": False,
        "pause": False,
        "trailing_weeks": 6,
        "pause_cumulative_dollars": "-1000",
    }


def test_retire_when_the_first_twenty_average_non_positive() -> None:
    losing = _views(*[_episode(f"l{i}", pnl="-50.00") for i in range(20)])
    card = scorecards.family_card(losing)
    assert card["rules"]["retire"] is True
    assert card["rules"]["promotion_ready"] is False
    winning = _views(*[_episode(f"w{i}", pnl="50.00") for i in range(20)])
    card2 = scorecards.family_card(winning)
    assert card2["rules"]["retire"] is False
    assert card2["rules"]["promotion_ready"] is True


def test_pause_needs_both_a_negative_tail_and_a_1000_drawdown() -> None:
    deep = _views(
        *[_episode(f"p{i}", pnl="-50.00", week=f"2025-W{11 + i // 4}") for i in range(24)]
    )
    card = scorecards.family_card(deep)
    # trailing 6 weeks all -50 and cumulative -1200 <= -1000: pause fires
    # (and the first-20 mean is negative, so retire fires too)
    assert card["rules"]["pause"] is True and card["rules"]["retire"] is True
    shallow = _views(*[_episode(f"q{i}", pnl="-50.00", week="2025-W11") for i in range(8)])
    card2 = scorecards.family_card(shallow)
    # cumulative -400 > -1000: no pause despite the negative tail
    assert card2["rules"]["pause"] is False


def test_input_slices_and_fallback_counts(tmp_path: Path) -> None:
    state = _write(
        tmp_path,
        _episode("a", basis="xsmom", pnl="100.00"),
        _episode("b", basis="pead", pnl="-100.00", fallback=True),
    )
    doc = scorecards.build_scorecards(state)
    assert set(doc["inputs"]) == {"input:xsmom", "input:pead"}
    assert doc["inputs"]["input:pead"]["pnl"]["fallback_resolutions"] == 1
    assert doc["inputs"]["input:xsmom"]["pnl"]["fallback_resolutions"] == 0


def test_write_scorecards_lays_out_the_cards(tmp_path: Path) -> None:
    state = _write(tmp_path, _episode("a"), _episode("b", row="R3", basis="pead"))
    doc = scorecards.write_scorecards(state)
    out = state / "scorecards"
    names = sorted(p.name for p in out.glob("*.json"))
    assert names == [
        "input:pead.json",
        "input:xsmom.json",
        "row:R1.json",
        "row:R3.json",
        "summary.json",
    ]
    again = scorecards.load_summary(out)
    assert again["resolved_total"] == doc["resolved_total"] == 2
