"""The XU shadow drafter (tree_options.desk.xu_shadow, XU-SUCCESSOR-001
§6 item 2): the monthly XU-62 card draft on a synthetic panel whose ranking
oracle is recomputed independently from the raw closes.

The panel builder gives every roster name a distinct constant per-session
growth rate, so the 273-session return order is the roster order and the
top 3 is known without calling the implementation. BK and WBA carry dead
(truncated) series, exactly their real panel shape: they must surface as
gap exclusions in the draft's checks, never rank.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tree_options.desk import signals, xu_shadow

REBALANCE = date(2026, 12, 1)  # first NYSE session of December 2026
MID_MONTH = date(2026, 12, 15)
GENERATED = datetime(2026, 12, 1, 21, 30)


def _close(idx: int, k: int) -> str:
    """Session-k close of roster index ``idx``: a constant growth rate
    1 + idx/10000 per session (monotone in idx, so ranking == roster order)."""
    rate = Decimal(1) + Decimal(idx) / Decimal(10_000)
    return str((Decimal(100) * (rate**k)).quantize(Decimal("0.01")))


def _xu_panel(
    cal, end: date = REBALANCE, *, n_sessions: int = 300, hole: set[str] | None = None
) -> dict[str, dict[str, dict[str, Any]]]:
    sessions = cal.sessions()
    i = sessions.index(end)
    window = sessions[i - n_sessions + 1 : i + 1]
    assert len(window) == n_sessions
    panel: dict[str, dict[str, dict[str, Any]]] = {}
    for idx, name in enumerate(xu_shadow.XU_ROSTER):
        if name in xu_shadow.XU_DEAD_SERIES:
            days = window[: 40]  # a dead series: ends deep inside the window
        else:
            days = window
        series = {d.isoformat(): {"close": _close(idx, k)} for k, d in enumerate(days)}
        if hole and name in hole:
            series.pop(window[100].isoformat())  # an interior gap
        panel[name] = series
    return panel


def _live_top3() -> tuple[str, ...]:
    """The expected picks, BEST FIRST (roster index == growth rate)."""
    return tuple(
        n for n in reversed(xu_shadow.XU_ROSTER) if n not in xu_shadow.XU_DEAD_SERIES
    )[:3]


def _draft(panel, cal, session: date = REBALANCE):
    return xu_shadow.xu_draft(
        panel, session, cal, exit_session=date(2026, 12, 29), generated_at=GENERATED
    )


def test_roster_pin_matches_the_fetch_script_pin() -> None:
    from scripts.desk import fetch_ohlc_xu

    assert xu_shadow.XU_ROSTER == fetch_ohlc_xu.XU_ROSTER
    assert len(xu_shadow.XU_ROSTER) == 62 == xu_shadow.XU_ROSTER_N
    assert len(set(xu_shadow.XU_ROSTER)) == 62
    assert xu_shadow.XU_PANEL_SHA256_FROZEN == fetch_ohlc_xu.XU_PANEL_SHA256_FROZEN


def test_rebalance_draft_names_entries_and_gap_exclusions(static_calendar) -> None:
    panel = _xu_panel(static_calendar)
    text = _draft(panel, static_calendar)
    assert text is not None and text.isascii()
    assert text.startswith(f"# XU-SHADOW <N> - XSMOM-TOP3-XU monthly: {' + '.join(_live_top3())}")
    for name in _live_top3():  # every leg row: rank k/62 + the entry close
        assert f"| {name} | rank " in text and f"/{xu_shadow.XU_ROSTER_N}," in text
        assert f"| {panel[name][REBALANCE.isoformat()]['close']} | $2,500 LONG |" in text
    # the dead series surface in the checks with the ranking code's own
    # reason string (verbatim, never re-mapped), never as picks
    assert re.search(r"excluded: BK \(\w+\), WBA \(\w+\)", text)
    assert "never rank: the ranking code's own exclusion" in text
    assert "ranked 60/62" in text
    for dead in xu_shadow.XU_DEAD_SERIES:
        assert f"| {dead} | rank" not in text


def test_ranking_oracle_is_recomputed_not_trusted(static_calendar) -> None:
    # independent re-derivation from the raw closes (never the implementation)
    panel = _xu_panel(static_calendar)
    d = REBALANCE.isoformat()
    sessions = static_calendar.sessions()
    i = sessions.index(REBALANCE)
    start = sessions[i - signals.XSMOM_LOOKBACK].isoformat()
    scored = sorted(
        (
            (n, Decimal(panel[n][d]["close"]) / Decimal(panel[n][start]["close"]) - 1)
            for n in panel
            if start in panel[n] and d in panel[n]
        ),
        key=lambda kv: -kv[1],
    )
    assert tuple(n for n, _ in scored[:3]) == _live_top3()
    text = _draft(panel, static_calendar)
    assert text is not None and all(f"| {n} | rank {k}" in text for k, n in enumerate(_live_top3(), 1))


def test_below_30_rankable_names_is_an_explicit_no_trade(static_calendar, tmp_path) -> None:
    hole = {n for n in xu_shadow.XU_ROSTER if n not in xu_shadow.XU_DEAD_SERIES} - set(
        _live_top3()
    )
    hole = set(sorted(hole)[:32])  # 60 live - 32 gapped = 28 < 30 rankable
    panel = _xu_panel(static_calendar, hole=hole)
    text = _draft(panel, static_calendar)
    assert text is not None
    assert "## NO-TRADE (honest)" in text
    assert "ranked 28/62" in text
    # no entries are taken: no entry rows at all (the rule text alone remains)
    assert "## Entries" not in text and "| $2,500 LONG |" not in text
    # never silently absent: the NO-TRADE draft still writes a file
    path = xu_shadow.write_draft(tmp_path, REBALANCE, text)
    assert path == tmp_path / "card-drafts-xu" / "2026-12-01-xsmom-top3-xu.md"
    assert path.read_text() == text


def test_draft_carries_no_aggregate(static_calendar) -> None:
    text = _draft(_xu_panel(static_calendar), static_calendar)
    assert text is not None
    # the only dollar figure anywhere is the per-leg notional: no P&L, no sum
    assert set(re.findall(r"\$[0-9][0-9,.]*", text)) == {"$2,500"}
    for banned in ("P&L", "cumulative", "mean", "average", "total"):
        assert banned not in text.lower()


def test_mid_month_session_has_no_draft(static_calendar) -> None:
    assert _draft(_xu_panel(static_calendar), static_calendar, MID_MONTH) is None


def test_refuses_without_a_write_when_the_panel_does_not_cover(static_calendar) -> None:
    panel = _xu_panel(static_calendar)
    del panel["MU"][REBALANCE.isoformat()]  # a live name missing the session bar
    try:
        _draft(panel, static_calendar)
        raise AssertionError("expected XuCoverageError")
    except xu_shadow.XuCoverageError as exc:
        assert "MU" in str(exc)


def test_aa_determinism_same_inputs_same_bytes(static_calendar) -> None:
    panel = _xu_panel(static_calendar)
    first = _draft(panel, static_calendar)
    second = _draft(panel, static_calendar)  # identical inputs, second call
    assert first == second
    # and the underlying ranking is pure: a re-run reproduces the top 3
    res = signals.xsmom_top3(panel, REBALANCE, static_calendar, names=xu_shadow.XU_ROSTER)
    rerun = signals.xsmom_top3(panel, REBALANCE, static_calendar, names=xu_shadow.XU_ROSTER)
    assert res.top3 == rerun.top3 == _live_top3()
    assert first is not None and all(n in first for n in res.top3)
