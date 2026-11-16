"""TSMOM-SHIELD-001: the report-only TS-momentum defensive overlay stage.

Sealed ``docs/desk/TSMOM-SHIELD-001.md`` (2026-11-14, RT-3 revisions R1-R6
applied) fixes the RULE, its bars and its coupling; this module is only its
machinery: an annotation stage that runs beside
:mod:`tree_options.desk.eod_equity` and writes, per session, DEF per panel
name and per entry the would-be size -- into a NEW state dir
``<state>/overlay/tsmom-shield/``, never touching live card state, drafts
or LEDGER. The sealer stays blind to overlay state when sealing (RT-3
C6b): sizes are hypothetical, written to separate state, and the bare book
never reads them. Nothing here imports the seal path, and the seal path
never imports this module (a test enforces both directions).

Rule (one config, no grid)::

    DEF_N(t) = sign( close_N(t) / close_N(t-60) - 1 )   # 60 NYSE sessions
    would-be size = 1.0  if DEF_N(t) > 0       # defensive OFF -- identical to bare
                    0.5  if DEF_N(t) < 0       # defensive ON -- half size, entry still taken
                    1.0  if DEF_N(t) = UNKNOWN # hole guard: never throttle on missing data

The hole guard is STRICTER than the burned cells and conservative: a name
missing ANY session of the 61-session window [t-60, t] is UNKNOWN and
keeps the full would-be size -- never punitive, never silent (the recorded
state says why). The SPY aggregate DEF is recorded descriptive-only and is
never an input.

Burned-era line (RT-3 R6, verbatim): "Panel sessions dated <= 2026-09-11
may feed DEF inputs only (construction warm-up); scored outcomes begin at
the first overlay-eligible card; no overlay statistic is ever computed on
pre-registration outcomes." Implemented as: the writer refuses to annotate
a session dated <= 2026-09-11 (no annotation file pretends to exist inside
the burned panel window), while every look-back that reaches back into it
reads those closes as INPUTS and nothing more.

Interim digest: report-only, at every 10 cards -- the prereg pins "the
card unit is the ENTRY (per-name card)" and rejects the sealed-DOC
reading, so the cadence implemented here is every 10th recorded ENTRY.
The digest counts DEF states only (no outcome statistic is computed before
the one-shot resolution run) and carries the coupled-pair disclosure
sentence: TSMOM-SHIELD-001 and EB-RECHECK-001 register as ONE reporting
unit, and every overlay verdict is PROVISIONAL on the premise channel.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.desk.panel import PanelLocked, read_panel_with_sha256
from tree_options.desk.sessions import Calendar
from tree_options.desk.store import atomic_write_json
from tree_options.desk.universe import PANEL_NAMES

STAGE = "tsmom-annotate"
PREREG = "TSMOM-SHIELD-001"
SCHEMA = "desk-tsmom-overlay/1"
DIGEST_SCHEMA = "desk-tsmom-overlay-digest/1"
LOOKBACK = 60  # NYSE sessions; t-60 is a fixed calendar session, never a bar offset
DEF_FULL = 1.0  # defensive OFF (and the hole-guard / DEF == 0 size: never throttled)
DEF_HALF = 0.5  # defensive ON -- house convention, pinned by the prereg (RT-3 R3)
DIGEST_EVERY = 10  # entries: "the card unit is the ENTRY (per-name card)"
MISSING_LISTED = 20  # bound the missing-session list (eod-equity's panel_holes style)
# RT-3 R6 construction/scoring line: panel sessions <= this date feed DEF
# inputs only. The first eligible rebalance (entry 2026-12-01) has its
# 60-session look-back land inside the burned panel window (which ends here).
BURNED_ERA_END = date(2026, 9, 11)
REFERENCE = "SPY"  # the panel's session clock (the same convention as eod-equity)
OVERLAY_REL = Path("overlay") / "tsmom-shield"
COUPLED_PAIR = ("TSMOM-SHIELD-001", "EB-RECHECK-001")
COUPLING_SENTENCE = (
    "No independent-evidence claim is made anywhere in this pair: both "
    "estimands descend from the same two 2022 windows of the same frozen "
    "grid on the same panel family, so a joint pass is correlated evidence "
    "about ONE premise, never two confirmations."
)
PROVISIONAL_NOTE = (
    "Interim report-only digest; no verdict is issued interim. Any overlay "
    "verdict issued before EB-RECHECK-001's first fired cell reads (or before "
    "the premise voids) is PROVISIONAL on the premise channel and carries the "
    "binding-premise label if the premise then fails."
)
_Q = Decimal("0.000001")

Panel = Mapping[str, Mapping[str, Mapping[str, Any]]]


# ------------------------------------------------------------ pure DEF rule


@dataclass(frozen=True)
class DefState:
    """DEF for one panel name at one session (no I/O)."""

    name: str
    def_: int | None  # -1 | 0 | 1, or None = UNKNOWN (hole guard)
    # "" | hole | insufficient_history | no_session_bar | not_in_panel | calendar_too_short
    reason: str
    window: tuple[date, date] | None  # [t-60, t], the guarded 61 sessions
    close_t: Decimal | None
    close_t_minus_60: Decimal | None
    missing: tuple[date, ...]  # window sessions the name has no bar for


def _def_state(
    panel: Panel, session: date, name: str, window: tuple[date, ...], lagged: date | None
) -> DefState:
    span = (window[0], window[-1]) if window else None
    if not window:
        return DefState(name, None, "calendar_too_short", None, None, None, ())
    bars = panel.get(name)
    if not bars:
        return DefState(name, None, "not_in_panel", span, None, None, ())
    d = session.isoformat()
    if d not in bars:
        return DefState(name, None, "no_session_bar", span, None, None, ())
    missing = tuple(w for w in window if w.isoformat() not in bars)
    if missing:
        # the xsmom reason split: a name whose panel starts inside the window
        # is short history, not a hole in otherwise-present history
        why = "insufficient_history" if min(bars) > window[0].isoformat() else "hole"
        return DefState(name, None, why, span, None, None, missing)
    assert lagged is not None  # window non-empty implies i >= LOOKBACK
    close_t = Decimal(str(bars[d]["close"]))
    close_60 = Decimal(str(bars[lagged.isoformat()]["close"]))
    ret = close_t / close_60 - 1
    sign = (ret > 0) - (ret < 0)
    return DefState(name, sign, "", span, close_t, close_60, ())


def def_states(
    panel: Panel,
    session: date,
    cal: Calendar,
    *,
    names: Iterable[str] = PANEL_NAMES,
) -> dict[str, DefState]:
    """DEF per panel name at ``session``: pure computation, no I/O. ``t-60``
    is a fixed NYSE session from the calendar; a name missing ANY session of
    the 61-session window [t-60, t] is UNKNOWN (the stricter hole guard)."""
    sessions = cal.sessions()
    i = cal.ordinal(session)
    window = sessions[i - LOOKBACK : i + 1] if i >= LOOKBACK else ()
    lagged = window[0] if window else None
    return {name: _def_state(panel, session, name, window, lagged) for name in sorted(names)}


def would_be_size(def_: int | None) -> float:
    """The sealed size mapping: half ONLY on DEF < 0 (monotone-reduce-only;
    DEF == 0 and UNKNOWN keep the bare size -- never throttle on missing or
    flat data)."""
    return DEF_HALF if def_ == -1 else DEF_FULL


def _fmt(x: Decimal) -> str:
    return str(x.quantize(_Q))


def state_doc(st: DefState) -> dict[str, Any]:
    """The per-name DEF state as written to the session JSON."""
    doc: dict[str, Any] = {
        "def": st.def_,
        "reason": st.reason,
        "window": [s.isoformat() for s in st.window] if st.window else None,
    }
    if st.def_ is not None and st.close_t is not None and st.close_t_minus_60 is not None:
        doc["close_t"] = str(st.close_t)
        doc["close_t_minus_60"] = str(st.close_t_minus_60)
        doc["return_60"] = _fmt(st.close_t / st.close_t_minus_60 - 1)
    else:
        doc["missing_sessions"] = [s.isoformat() for s in st.missing[:MISSING_LISTED]]
        doc["missing_n"] = len(st.missing)
    if st.name == "SPY":
        doc["descriptive_only"] = True  # the SPY aggregate is never an input
    return doc


# ----------------------------------------------------------------- the stage


@dataclass(frozen=True)
class OverlayResult:
    exit_code: int
    status: str
    detail: str = ""
    session: date | None = None
    entries_total: int = 0
    digest_boundaries: tuple[int, ...] = ()

    def line(self) -> str:
        d = self.session.isoformat() if self.session else "-"
        dig = ",".join(str(b) for b in self.digest_boundaries) or "none"
        tail = f" ({self.detail})" if self.detail else ""
        return (
            f"{STAGE} session={d} status={self.status} entries={self.entries_total} "
            f"digest={dig} exit={self.exit_code}{tail}"
        )


def _entry_doc(name: str, st: DefState | None) -> dict[str, Any]:
    st = st or DefState(name, None, "not_in_panel", None, None, None, ())
    doc: dict[str, Any] = {
        "def": st.def_,
        "reason": st.reason,
        "would_be_size": would_be_size(st.def_),
    }
    if name == "SPY":
        doc["descriptive_only"] = True
    return doc


def _read_doc(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(doc, dict):
        raise ValueError(f"{path.name}: not an overlay document")
    return doc


def _sans(doc: Mapping[str, Any]) -> dict[str, Any]:
    """A doc without its timestamp (idempotence comparison)."""
    return {k: v for k, v in doc.items() if k != "generated_at"}


def _session_docs(overlay_dir: Path) -> list[tuple[str, dict[str, Any]]]:
    """Every annotated session's doc, oldest first; a foreign or torn file in
    the overlay dir is loud, never silently skipped."""
    out: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(overlay_dir.glob("*.json")):
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError(f"{path.name}: unreadable overlay state ({exc})") from exc
        if not (isinstance(doc, dict) and isinstance(doc.get("entries"), dict)):
            raise ValueError(f"{path.name}: not an overlay session document")
        out.append((path.stem, doc))
    return out


def _recorded_entries(overlay_dir: Path) -> list[tuple[str, str, dict[str, Any]]]:
    """Every recorded entry as (session, name, entry-doc), session/name sorted."""
    rows = [
        (stem, name, entry)
        for stem, doc in _session_docs(overlay_dir)
        for name, entry in doc["entries"].items()
    ]
    return sorted(rows, key=lambda r: (r[0], r[1]))


def _count_entries(overlay_dir: Path) -> int:
    return sum(len(doc["entries"]) for _stem, doc in _session_docs(overlay_dir))


def _due_boundaries(total: int, digest_dir: Path) -> list[int]:
    """Unemitted digest boundaries (every DIGEST_EVERY entries) up to total."""
    return [
        b
        for b in range(DIGEST_EVERY, total + 1, DIGEST_EVERY)
        if not (digest_dir / f"{b}.json").exists()
    ]


def _digest_doc(overlay_dir: Path, boundary: int, now: datetime) -> dict[str, Any]:
    """The interim report-only digest over the FIRST ``boundary`` recorded
    entries. Counts of DEF states only: no overlay statistic (return, delta,
    down-episode anything) is computed before the one-shot resolution run."""
    rows = _recorded_entries(overlay_dir)
    covered = rows[:boundary]
    half = sum(1 for _s, _n, e in covered if e.get("would_be_size") == DEF_HALF)
    unknown = sum(1 for _s, _n, e in covered if e.get("def") is None)
    full = len(covered) - half - unknown
    return {
        "schema": DIGEST_SCHEMA,
        "prereg": PREREG,
        "coupled_pair": list(COUPLED_PAIR),
        "disclosure": COUPLING_SENTENCE,
        "provisional": PROVISIONAL_NOTE,
        "unit": "entry",
        "entries_covered": boundary,
        "entries_total": len(rows),
        "session_span": [covered[0][0], covered[-1][0]] if covered else None,
        "sizes": {"full": full, "half": half, "unknown": unknown},
        "on_fraction": _fmt(Decimal(half) / Decimal(boundary)),
        "report_only": True,
        "no_outcome_statistic": (
            "counts of DEF states only; no overlay statistic is computed "
            "before the one-shot resolution run"
        ),
        "generated_at": now.astimezone(UTC).isoformat(timespec="seconds"),
    }


def _emit_due_digests(overlay_dir: Path, now: datetime) -> tuple[int, ...]:
    digest_dir = overlay_dir / "digest"
    emitted = []
    for boundary in _due_boundaries(_count_entries(overlay_dir), digest_dir):
        atomic_write_json(digest_dir / f"{boundary}.json", _digest_doc(overlay_dir, boundary, now))
        emitted.append(boundary)
    return tuple(emitted)


def run_overlay(
    *,
    session: date,
    cal: Calendar,
    state: Path,
    panel_path: Path,
    entries: Iterable[str] = (),
    digest: bool = False,
    now: datetime,
    lock_timeout_s: float = 300.0,
) -> OverlayResult:
    """Annotate one session: DEF per panel name, would-be sizes for the
    entries passed EXPLICITLY (the stage is blind to the card chain -- it
    never discovers entries itself), written as ONE JSON under
    ``state/overlay/tsmom-shield/`` and nowhere else. Reads the panel under
    the shared flock convention; a rerun merges entries and never rewrites
    an unchanged document byte-for-byte. ``--digest`` emits the interim
    report-only digest at every 10th recorded ENTRY."""
    if not cal.is_session(session):
        return OverlayResult(
            0, "not_a_session", f"{session.isoformat()} is not an NYSE session", session
        )
    if session <= BURNED_ERA_END:
        return OverlayResult(
            1,
            "burned_era_session",
            f"{session.isoformat()} <= {BURNED_ERA_END.isoformat()}: burned-era "
            "sessions feed DEF inputs only (construction warm-up)",
            session,
        )
    try:
        panel, panel_sha = read_panel_with_sha256(panel_path, timeout_s=lock_timeout_s)
    except PanelLocked as exc:
        return OverlayResult(3, "panel_locked", str(exc), session)
    except (OSError, ValueError) as exc:
        return OverlayResult(
            1, "panel_missing", f"{panel_path.name}: {type(exc).__name__}", session
        )
    bars = panel.get(REFERENCE) or {}
    last = date.fromisoformat(max(bars)) if bars else None
    if last is None or last < session:
        ends = last.isoformat() if last else "-"
        return OverlayResult(
            1,
            "panel_not_current",
            f"panel ends {ends}; no {REFERENCE} bar for {session.isoformat()}",
            session,
        )

    states = def_states(panel, session, cal)
    overlay_dir = state / OVERLAY_REL
    path = overlay_dir / f"{session.isoformat()}.json"
    existing = _read_doc(path)
    merged: dict[str, Any] = dict(existing.get("entries", {})) if existing else {}
    for name in dict.fromkeys(entries):  # deduped, order preserved; recomputed fresh
        merged[name] = _entry_doc(name, states.get(name))
    doc = {
        "schema": SCHEMA,
        "prereg": PREREG,
        "session": session.isoformat(),
        "generated_at": now.astimezone(UTC).isoformat(timespec="seconds"),
        "panel": {"path": str(panel_path), "sha256": panel_sha},
        "rule": {
            "def": f"sign(close_N(t)/close_N(t-{LOOKBACK}) - 1)",
            "lookback_sessions": LOOKBACK,
            "hole_guard": "missing_any_window_session",
            "sizes": {"defensive_off": DEF_FULL, "defensive_on": DEF_HALF, "unknown": DEF_FULL},
            "burned_era_end": BURNED_ERA_END.isoformat(),
            "spy_aggregate": "descriptive_only",
            "sealer_blind": (
                "would-be sizes are hypothetical; the sealer never reads "
                "overlay state when sealing (RT-3 C6b)"
            ),
        },
        "names": {name: state_doc(st) for name, st in states.items()},
        "entries": merged,
    }
    try:
        if existing is None or _sans(doc) != _sans(existing):
            atomic_write_json(path, doc)
        total = _count_entries(overlay_dir)
        boundaries = _emit_due_digests(overlay_dir, now) if digest else ()
    except (OSError, ValueError) as exc:
        return OverlayResult(1, "overlay_state_unreadable", f"{type(exc).__name__}: {exc}", session)
    return OverlayResult(
        0, "ok", session=session, entries_total=total, digest_boundaries=boundaries
    )
