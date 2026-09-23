"""Draft forward cards in the research lane's card format.

The template follows CRON-paper-engine.md step 6 and the sealed cards in
artifacts/paper-trades/ (title, seal line, Rule, Entries table, Exit
policy). A draft is NEVER a card: sealing (numbering, the seal time before
the next session's data exists, the LEDGER.md row + sha256) stays a human
or agent step. ASCII only.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from tree_options.desk.signals import (
    HOLD_SESSIONS,
    PeadEvent,
    XsmomResult,
)
from tree_options.desk.universe import NO_OPTIONS_EXPRESSION

NOTIONAL = "$2,500 LONG"  # per leg, CRON-paper-engine.md step 6


def _pct(x: Decimal) -> str:
    return f"{x * 100:+.2f}%"


def _exit_label(exit_session: date | None) -> str:
    if exit_session is None:
        return f"session+{HOLD_SESSIONS} (beyond the calendar horizon: fix before sealing)"
    return exit_session.isoformat()


def _draft_banner(generated_at: datetime, session: date, slug: str, exit_label: str) -> list[str]:
    return [
        f"DRAFT generated {generated_at.isoformat()} by the desk eod-equity job - NOT sealed.",
        "To seal: review, number the card, stamp the seal time (before any data for the",
        f"session after {session.isoformat()} exists), save it as",
        f"artifacts/paper-trades/{exit_label}-{slug}.md and add its LEDGER.md row + sha256",
        "(CRON-paper-engine.md step 6). Never take a name already held on an open card.",
    ]


def _next_reports(names: list[str], earnings: Mapping[str, list[str]], session: date) -> str:
    parts = []
    for n in names:
        upcoming = sorted(r for r in earnings.get(n, []) if r > session.isoformat())
        parts.append(f"{n} {upcoming[0] if upcoming else 'none listed'}")
    return ", ".join(parts)


def xsmom_draft(
    res: XsmomResult,
    *,
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]],
    earnings: Mapping[str, list[str]],
    exit_session: date | None,
    generated_at: datetime,
) -> str:
    d = res.session.isoformat()
    exit_label = _exit_label(exit_session)
    names = list(res.top3)
    lines = [
        f"# Card <N> - XSMOM-TOP3 monthly: {' + '.join(names)} "
        f"(entry {d} close -> exit {exit_label} close)",
        "",
        *_draft_banner(generated_at, res.session, "xsmom-top3", exit_label),
        "",
        "## Rule (sealed)",
        "XSMOM-TOP3 monthly: on the first trading day of a month, rank the 36",
        "tradables (the panel minus SPY) by trailing return over 273 sessions;",
        f"LONG the top 3, {NOTIONAL} each, hold {HOLD_SESSIONS} sessions. Baseline:",
        "PROTOCOL-XSMOM.md (46 months, in-sample upper bound; XU-XSMOM.md marks",
        "forward expectations down). Kill-switches per RESEARCH-LEDGER.md.",
        "Ranking convention: close(t)/close(t-273)-1, the computation behind every",
        "PROTOCOL-XSMOM.md row. The rule text's close(t-21)/close(t-273)-1 reading",
        f"picks {', '.join(res.top3_skip21)}"
        + (
            " (same names)."
            if res.conventions_agree
            else " (DIFFERENT names: decide before sealing)."
        ),
        "",
        "## Entries (sealed)",
        f"| name | signal at {d} | entry ({d} close) | notional | exit |",
        "|---|---|---|---|---|",
    ]
    for rank, (name, score) in enumerate(res.ranked[: len(names)], start=1):
        entry = str(panel[name][d]["close"])
        lines.append(
            f"| {name} | rank {rank}/{res.n_ranked}, 273-session return {_pct(score)} "
            f"| {entry} | {NOTIONAL} | {exit_label} close |"
        )
    lines += [
        "",
        "## Exit policy (sealed with entry - variant xsmom-monthly)",
        f"xsmom-monthly: exit at the {HOLD_SESSIONS}th-session close after entry "
        f"({exit_label}). Parameters frozen at seal.",
        "",
        "## Draft checks (delete before sealing)",
        f"- ranked {res.n_ranked}/36; excluded: "
        + (", ".join(f"{n} ({why})" for n, why in sorted(res.excluded.items())) or "none"),
        *(
            [
                f"- DATA GAP: {', '.join(res.data_gaps)} excluded for a missing session inside the"
                " 273-session window; repair the panel and re-rank before sealing if one of them"
                " could have made the top 3"
            ]
            if res.data_gaps
            else []
        ),
        "- no options expression on the desk: "
        + (", ".join(n for n in names if n in NO_OPTIONS_EXPRESSION) or "none")
        + " (the equity card is unaffected)",
        f"- next report dates (earnings-calendar.json): {_next_reports(names, earnings, res.session)}",
        "",
    ]
    return "\n".join(lines)


def pead_draft(
    event: PeadEvent,
    *,
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]],
    exit_session: date | None,
    generated_at: datetime,
) -> str:
    assert event.move is not None and event.prior_session is not None
    d = event.session
    session = date.fromisoformat(d)
    exit_label = _exit_label(exit_session)
    slug = f"pead-{event.name.lower()}"
    entry = str(panel[event.name][d]["close"])
    prior = str(panel[event.name][event.prior_session]["close"])
    lines = [
        f"# Card <N> - PEAD-BIGSURPRISE: {event.name} (entry {d} close -> exit {exit_label} close)",
        "",
        *_draft_banner(generated_at, session, slug, exit_label),
        "",
        "## Rule (sealed)",
        "PEAD-BIGSURPRISE: a name whose first session after an earnings report",
        "date (earnings-calendar.json; reports land after hours) is today, with a",
        "report-session move close(first post-report session)/close(prior)-1 of",
        "at least +1.5% (POSITIVE surprises only: PEAD-SIGN.md, +97 USD/card on",
        f"beats vs +2 on misses); LONG {NOTIONAL} at today's close, hold",
        f"{HOLD_SESSIONS} sessions. Baseline: PROTOCOL-PEAD.md (in-sample upper bound).",
        "",
        "## Entries (sealed)",
        f"| name | signal at {d} | entry ({d} close) | notional | exit |",
        "|---|---|---|---|---|",
        f"| {event.name} | report {event.report_date}, move {_pct(event.move)} "
        f"({event.prior_session} close {prior} -> {d} close {entry}) | {entry} "
        f"| {NOTIONAL} | {exit_label} close |",
        "",
        "## Exit policy (sealed with entry - variant pead-20)",
        f"pead-20: exit at the {HOLD_SESSIONS}th-session close after entry ({exit_label}).",
        "Parameters frozen at seal.",
        "",
        "## Draft checks (delete before sealing)",
        "- confirm the report date and its after-hours timing at the source",
        "",
    ]
    return "\n".join(lines)
