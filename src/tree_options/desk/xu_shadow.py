"""The XU-62 successor shadow book's monthly card DRAFTER (XU-SUCCESSOR-001
§6 item 2; §4.1 construction, §4.3 digest-only fence).

Pure drafting, nothing else. The shadow book is the SAME sealed construction
- ``close(t)/close(t-273)-1``, top 3, $2,500/leg, hold 20, 5bp RT, the same
seal discipline - on the 62-name frozen XU roster, which REPLACES the 36
(no union roster; PLTR/SPCX not added). This module computes the monthly
card via :func:`tree_options.desk.signals.xsmom_top3` with ``names=XU_ROSTER``
- ZERO changes to ``signals``/``eod_equity``/``card_drafts`` - and writes
ONLY ``<state>/card-drafts-xu/<D>-xsmom-top3-xu.md`` (atomic write). Sealing
stays a human/agent step (runbook, not code) into
``artifacts/paper-trades/xu-shadow/`` + ``XU-SHADOW-LEDGER.md``.

The fence (§4.3, assertable): before READ-1 no aggregate - count beyond the
roster, mean, sum, cumulative, quantile or plot - over shadow cards may be
computed, written or surfaced by any desk code path. This drafter renders
per-card entries only; it never computes P&L, sums or any statistic over
cards. The unit test ``tests/unit/test_desk_xu_fence.py`` pins that no
digest/roll-up code path touches the XU namespace.

Honesty rules (§4.1): WBA and BK stay on the roster but can never rank (dead
series -> the ranking code's own ``gap`` exclusion); months with fewer than
30 rankable names are an explicit NO-TRADE draft, never a silent absence; a
refusal (the XU panel not covering the session) writes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.desk import signals
from tree_options.desk.card_drafts import NOTIONAL
from tree_options.desk.sessions import is_first_session_of_month
from tree_options.desk.store import atomic_write_bytes

# The frozen 62-name roster (§4.1): identical literal to the one pinned in
# scripts/desk/fetch_ohlc_xu.py, both derived from ohlc-panel-xu.json at
# sha256 3753dcb8…036c0a5a. The roster is DATA (§5), not a rule: no new
# direction signal enters signals.py because of it.
XU_ROSTER: tuple[str, ...] = (
    "ABBV", "ABT", "ACN", "ADI", "AMGN", "APO", "AXP", "BA", "BK", "BLK",
    "BMY", "BRK.B", "C", "CAT", "CME", "COF", "COP", "CVX", "DD", "DE",
    "DHR", "DOW", "EMR", "F", "FCX", "GE", "GILD", "GM", "HON", "ICE",
    "ISRG", "JNJ", "KHC", "KKR", "LIN", "LMT", "MCD", "MDLZ", "MMM", "MO",
    "MRK", "MU", "NKE", "NUE", "PFE", "PLD", "PNC", "RTX", "SBUX", "SCHW",
    "SPG", "SYK", "T", "TGT", "TMO", "TXN", "UNP", "USB", "VZ", "WBA",
    "WFC", "WMT",
)
XU_ROSTER_N = len(XU_ROSTER)
XU_PANEL_SHA256_FROZEN = (
    "3753dcb862eafd67cb9635116b5cb66ed056891dd8251cb4c73ad25a036c0a5a"
)
XU_PANEL_NAME = "ohlc-panel-xu.json"

# Dead series: delisted inside the window and KEPT on the roster (the
# survivorship-honest choice, §5). They never rank - the ranking code's own
# gap exclusion drops them every month - and they are not panel-coverage
# requirements (their bars end at the delisting date).
XU_DEAD_SERIES: tuple[str, ...] = ("BK", "WBA")

# The shadow book's own namespace (§4.3): drafts here, sealed cards under
# artifacts/paper-trades/xu-shadow/. NEVER the incumbent card-drafts/ queue
# or LEDGER.md.
DRAFTS_DIRNAME = "card-drafts-xu"
DRAFT_SLUG = "xsmom-top3-xu"


class XuCoverageError(RuntimeError):
    """The XU panel does not cover the session: refuse, write nothing."""


def drafts_dir(state_root: Path) -> Path:
    return Path(state_root) / DRAFTS_DIRNAME


def draft_filename(session: date) -> str:
    return f"{session.isoformat()}-{DRAFT_SLUG}.md"


def panel_covers(panel: Mapping[str, Mapping[str, Mapping[str, Any]]], d: str) -> str | None:
    """'' when the XU panel covers session ``d``; else the refusal reason.
    Coverage = every frozen-roster name present, and a ``d`` bar for every
    live name (dead series are exempt - their truncation is the record)."""
    absent = [n for n in XU_ROSTER if not panel.get(n)]
    if absent:
        return f"{len(absent)} roster name(s) not in the panel ({', '.join(absent[:5])})"
    short = [n for n in XU_ROSTER if n not in XU_DEAD_SERIES and d not in panel[n]]
    if short:
        return f"no {d} bar for {len(short)} live name(s) ({', '.join(short[:5])}) - extend the panel first"
    return None


def _pct(x: Decimal) -> str:
    return f"{x * 100:+.2f}%"


def _exit_label(exit_session: date | None) -> str:
    if exit_session is None:
        return (
            f"session+{signals.HOLD_SESSIONS} (beyond the calendar horizon: fix before sealing)"
        )
    return exit_session.isoformat()


def _banner(generated_at: datetime, session: date, exit_label: str) -> list[str]:
    return [
        "XU-SHADOW DRAFT generated "
        f"{generated_at.isoformat()} by the xu-shadow drafter - NOT sealed.",
        "XU-62 successor universe per XU-SUCCESSOR-001 section 4.1 (the shadow book).",
        "Digest-only fence section 4.3: NO aggregate over the XU namespace may be",
        "computed, written or surfaced by any desk code path before READ-1;",
        "per-card lines only.",
        "To seal: review, number the card, stamp the seal time (before any data",
        f"for the session after {session.isoformat()} exists), save it as",
        f"artifacts/paper-trades/xu-shadow/{exit_label}-{DRAFT_SLUG}.md and add its",
        "row + the card file's sha256 to XU-SHADOW-LEDGER.md - never the",
        "incumbent LEDGER.md. Sealing or refusing (EARNINGS-BLOCK / NAME-HELD /",
        "NO-TRADE / DEFECT) is a human/agent step; a drafted-but-unsealed month",
        "with no recorded refusal is a protocol breach under the registration.",
    ]


def xu_draft(
    panel: Mapping[str, Mapping[str, Mapping[str, Any]]],
    session: date,
    cal: signals.Calendar,
    *,
    exit_session: date | None,
    generated_at: datetime,
) -> str | None:
    """The month's XU shadow card text (None on a non-rebalance session;
    raises :class:`XuCoverageError` when the panel does not cover the
    session). A month with fewer than :data:`XSMOM_MIN_RANKED` rankable names
    returns an explicit honest NO-TRADE draft - never a silent absence."""
    if not is_first_session_of_month(session, cal):
        return None
    d = session.isoformat()
    refusal = panel_covers(panel, d)
    if refusal:
        raise XuCoverageError(refusal)
    res = signals.xsmom_top3(panel, session, cal, names=XU_ROSTER)
    exit_label = _exit_label(exit_session)
    names = list(res.top3)
    lines = [
        f"# XU-SHADOW <N> - XSMOM-TOP3-XU monthly: {' + '.join(names) or 'NO-TRADE'} "
        f"(entry {d} close -> exit {exit_label} close)",
        "",
        *_banner(generated_at, session, exit_label),
        "",
        "## Rule (the sealed construction on the XU roster)",
        "XSMOM-TOP3-XU monthly: identical construction to the sealed rule - on",
        "the first trading day of a month, rank by trailing return over 273",
        "sessions, close(t)/close(t-273)-1 (the pinned convention; the skip-21",
        f"variant is record-only) - LONG the top 3, {NOTIONAL} each, hold",
        f"{signals.HOLD_SESSIONS} sessions, 5bp RT - on the {XU_ROSTER_N}-name XU roster",
        "which REPLACES the 36 (no union roster; PLTR/SPCX not added). The",
        "skip-21 record-only variant picks "
        + (", ".join(res.top3_skip21) or "nothing")
        + (" (same names)." if res.conventions_agree else " (DIFFERENT names: record only)."),
        "",
    ]
    if res.n_ranked < signals.XSMOM_MIN_RANKED:
        lines += [
            "## NO-TRADE (honest)",
            f"Only {res.n_ranked}/{XU_ROSTER_N} names rank this month"
            f" (< {signals.XSMOM_MIN_RANKED}): no entries are taken. The month is",
            "recorded here, never silently absent; log the refusal as NO-TRADE in",
            "XU-SHADOW-LEDGER.md (section 2.3 discipline; the calendar extends).",
            "",
            "## Draft checks (delete before sealing)",
            _checks(res),
            "",
        ]
        return "\n".join(lines)
    lines += [
        "## Entries (draft)",
        f"| name | signal at {d} | entry ({d} close) | notional | exit |",
        "|---|---|---|---|---|",
    ]
    for rank, (name, score) in enumerate(res.ranked[: len(names)], start=1):
        entry = str(panel[name][d]["close"])
        lines.append(
            f"| {name} | rank {rank}/{XU_ROSTER_N}, 273-session return {_pct(score)} "
            f"| {entry} | {NOTIONAL} | {exit_label} close |"
        )
    lines += [
        "",
        "## Exit policy (draft - variant xsmom-monthly)",
        f"xsmom-monthly: exit at the {signals.HOLD_SESSIONS}th-session close after entry",
        f"({exit_label}). Parameters frozen at seal.",
        "",
        "## Draft checks (delete before sealing)",
        _checks(res),
        "",
    ]
    return "\n".join(lines)


def _checks(res: Any) -> str:
    excluded = ", ".join(f"{n} ({why})" for n, why in sorted(res.excluded.items())) or "none"
    dead = ", ".join(n for n in XU_DEAD_SERIES if n in res.excluded)
    gap_note = (
        "- dead series never rank: the ranking code's own exclusion drops them every"
        f" month (kept on the roster: {dead})"
        if dead
        else ""
    )
    lines = [
        f"- ranked {res.n_ranked}/{XU_ROSTER_N}; excluded: {excluded}",
        *([gap_note] if gap_note else []),
        *(
            [
                f"- DATA GAP: {', '.join(res.data_gaps)} excluded for a missing session inside"
                " the 273-session window; repair the panel and re-rank before sealing"
            ]
            if res.data_gaps
            else []
        ),
        f"- namespace only: drafts in {DRAFTS_DIRNAME}/, seals in xu-shadow/; never the",
        "  incumbent book's card-drafts/ queue or LEDGER.md",
    ]
    return "\n".join(lines)


def write_draft(state_root: Path, session: date, text: str) -> Path:
    """Write the draft into the XU namespace ONLY (atomic, ASCII)."""
    path = drafts_dir(state_root) / draft_filename(session)
    atomic_write_bytes(path, text.encode("ascii"))
    return path
