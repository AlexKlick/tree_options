"""Draft-card text: the "next report dates" check line. The fixture is a
hand-built XsmomResult plus a one-bar panel — never produced by calling
the signals module — so the rendered line is asserted against a written-
out oracle, including the report-after-next date when one is listed."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tree_options.desk import card_drafts
from tree_options.desk.signals import XsmomResult

SESSION = date(2026, 10, 1)
EXIT = date(2026, 10, 29)
GENERATED = datetime(2026, 10, 1, 18, 0)


def _res(top3: tuple[str, ...]) -> XsmomResult:
    return XsmomResult(
        session=SESSION,
        is_rebalance_day=True,
        fires=True,
        top3=top3,
        ranked=tuple((n, Decimal("0.10") - i * Decimal("0.01")) for i, n in enumerate(top3)),
        scores={n: Decimal("0.10") for n in top3},
        top3_skip21=top3,
        scores_skip21={n: Decimal("0.09") for n in top3},
        conventions_agree=True,
        excluded={},
        n_ranked=36,
        no_options_expression=(),
        data_gaps=(),
    )


def _panel(top3: tuple[str, ...]) -> dict[str, dict[str, dict[str, Any]]]:
    d = SESSION.isoformat()
    return {n: {d: {"close": "100.00"}} for n in top3}


def test_next_reports_line_carries_the_after_next_date() -> None:
    top3 = ("AAA", "BBB", "DDD")
    earnings = {
        # past dates dropped, the third future date capped away: next then after-next
        "AAA": ["2026-06-11", "2026-10-20", "2027-01-28", "2027-05-13"],
        "BBB": ["2026-11-05"],  # next only: rendered exactly as before
        "DDD": [],  # nothing listed
    }
    draft = card_drafts.xsmom_draft(
        _res(top3),
        panel=_panel(top3),
        earnings=earnings,
        exit_session=EXIT,
        generated_at=GENERATED,
    )
    line = next(ln for ln in draft.splitlines() if ln.startswith("- next report dates"))
    assert line == (
        "- next report dates (earnings-calendar.json): "
        "AAA 2026-10-20 then 2027-01-28, BBB 2026-11-05, DDD none listed"
    )
    assert draft.isascii()


def test_next_reports_line_with_one_date_is_unchanged() -> None:
    # no after-next anywhere: the line keeps its exact single-date shape
    top3 = ("BBB",)
    draft = card_drafts.xsmom_draft(
        _res(top3),
        panel=_panel(top3),
        earnings={"BBB": ["2026-11-05"]},
        exit_session=EXIT,
        generated_at=GENERATED,
    )
    line = next(ln for ln in draft.splitlines() if ln.startswith("- next report dates"))
    assert line == "- next report dates (earnings-calendar.json): BBB 2026-11-05"
