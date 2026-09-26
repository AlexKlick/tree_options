"""Default sealed-round adapter for the Research Lab catalog.

Reads an artifacts/campaign-2026-09/<scope>/sealed-round.json (plus its
per-trial JSONs in <scope>/trials/) and emits one ``ResearchCandidate``
per scope.

The adapter is PURE READ — never writes the campaign artifacts, never
opens the broker book, never opens the desk evidence chain. The only
side effect is filesystem reads.

Different scopes use different sealed-round.json shapes (jepa-filter
keys differ from term-gate which differ from vrp-cond; pead-deep-2,
exit-grid-2, and tnull have no sealed-round.json at all). The adapter
must be tolerant of missing keys — every scope-specific field is read
with ``.get()`` and treated as informational only.

Wire format contract: ADAPTER_API_VERSION=1 + ``build_candidate(...)``.
Future adapters (shadow, synthetic) implement the same surface; the
loader discovers them by filename.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from tree_options.research.contracts import (
    PLOT_FUNDED_ALLOWED,
    ResearchCandidate,
    ResearchDisposition,
    ResearchEvidenceKind,
    ResearchRegistration,
)

ADAPTER_API_VERSION = 1


# -- Helpers -----------------------------------------------------------------

_DISPOSITION_MAP: dict[str, ResearchDisposition] = {
    # Direct matches to the sealed-round `family_verdict` and per-nominee keys.
    "PASS": ResearchDisposition.PASS,
    "FAIL": ResearchDisposition.FAIL,
    "HOLD-STANDS": ResearchDisposition.HOLD_STANDS,
    "DATA-GATED-NOT-RUN": ResearchDisposition.DATA_GATED_NOT_RUN,
    "NOT_EVALUABLE": ResearchDisposition.NOT_EVALUABLE,
    "NOT_EVALUABLE-SEALED": ResearchDisposition.NOT_EVALUABLE_SEALED,
    "WITHDRAWN": ResearchDisposition.WITHDRAWN,
    "INSUFFICIENT_N": ResearchDisposition.INSUFFICIENT_N,
    "INSUFFICIENT_COVERAGE": ResearchDisposition.INSUFFICIENT_COVERAGE,
    "NOT_CANDIDATE": ResearchDisposition.NOT_CANDIDATE,
    "DESCRIPTIVE-ONLY:NO-REGIME-SIGNAL":
        ResearchDisposition.DESCRIPTIVE_ONLY_NO_REGIME_SIGNAL,
}


def _normalize_disposition(raw: str) -> ResearchDisposition | None:
    if not raw:
        return None
    return _DISPOSITION_MAP.get(raw.upper())


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate_supported_window(scope_dir: Path, sealed: dict[str, Any]) -> tuple[date | None, date | None]:
    """Window of supported trial dates for this scope.

    Looks in three places, in order:
      1. ``sealed_window`` (a date range under that key in some scopes),
      2. ``round.scope_window`` (jepa-style),
      3. ``trials/c09-*.json`` (date-range over all per-trial ``entry_session`` values).

    Returns ``(None, None)`` when nothing parseable exists.
    """
    sw = sealed.get("sealed_window")
    if isinstance(sw, dict):
        return _coerce_date(sw.get("start") or sw.get("from")), _coerce_date(sw.get("end") or sw.get("to"))
    rnd = sealed.get("round")
    if isinstance(rnd, dict):
        w = rnd.get("scope_window") or rnd.get("window")
        if isinstance(w, dict):
            return _coerce_date(w.get("start")), _coerce_date(w.get("end"))
    lo: date | None = None
    hi: date | None = None
    for trial_path in sorted((scope_dir / "trials").glob("c09-*.json")):
        try:
            tdoc = json.loads(trial_path.read_text())
        except (OSError, ValueError):
            continue
        d = _coerce_date(tdoc.get("entry_session") or tdoc.get("session"))
        if d is None:
            continue
        lo = d if lo is None or d < lo else lo
        hi = d if hi is None or d > hi else hi
    return lo, hi


def _trial_registration(scope_dir: Path) -> ResearchRegistration:
    """All sealed-round.json files record back-filled verdicts (they post-
    date the actual data window) — every trial under trials/ is, by the
    campaign's convention, ``retrospective_backfill``.
    """
    return ResearchRegistration.RETROSPECTIVE_BACKFILL


# -- Public adapter surface --------------------------------------------------


def build_candidate(scope_dir: Path, *, scope_id: str | None = None) -> ResearchCandidate:
    """Build one ``ResearchCandidate`` from a scope's sealed-round.json.

    ``scope_dir`` is the scope's directory (e.g.
    ``artifacts/campaign-2026-09/jepa-filter``); ``scope_id`` defaults to
    ``scope_dir.name``.

    The adapter never raises on malformed inputs — every field is
    ``.get()``-defaulted; missing files produce a
    ``DATA-GATED_NOT_RUN`` candidate with an explanatory ineligibility
    reason.
    """
    family = scope_id or scope_dir.name
    sealed_path = scope_dir / "sealed-round.json"
    if not sealed_path.is_file():
        return ResearchCandidate(
            id=f"{family}-v?",
            family=family,
            version="v?",
            evidence_kind=ResearchEvidenceKind.SEALED_CAMPAIGN,
            registration=ResearchRegistration.RETROSPECTIVE_BACKFILL,
            disposition=ResearchDisposition.DATA_GATED_NOT_RUN,
            plot_funded_account=False,
            supported_start=None,
            supported_end=None,
            ineligibility_reason="sealed-round.json not present (scope never sealed)",
            artifact_hashes={},
            capabilities=(),
            warnings=("research.data_gated",),
            source_url=f"sealed-round/{family}",
        )

    sealed = json.loads(sealed_path.read_text())

    # Family verdict (top-level or per-nominee verdict table).
    raw_disposition = (
        sealed.get("family_verdict")
        or _first_verdict_value(sealed.get("verdicts"))
        or sealed.get("registered_sealed_acceptance")
        or "DATA-GATED-NOT-RUN"
    )
    # Scope-O / scope-C / scope-A withdrawals override.
    if sealed.get("scope_accounting", {}).get("scope_O") == "WITHDRAWN":
        raw_disposition = "WITHDRAWN"
    disposition = _normalize_disposition(raw_disposition) or ResearchDisposition.DATA_GATED_NOT_RUN

    # Frozen-input hashes — every scope names its key set.
    frozen = sealed.get("frozen_inputs") or {}
    artifact_hashes = {
        f"sealed-round.{k}": str(v) for k, v in frozen.items() if isinstance(v, str)
    }
    # Plus the sealed-round.json itself (sha256 of bytes), which is what
    # RL §7 needs in the evidence envelope.
    try:
        artifact_hashes["sealed-round.json"] = _file_sha256(sealed_path)
    except OSError:
        pass

    supported_start, supported_end = _candidate_supported_window(scope_dir, sealed)
    version = (artifact_hashes.get("sealed-round.round1_selection_sha256")
               or artifact_hashes.get("sealed-round.json") or "v?")[:12]
    ineligibility_reason = (
        None if disposition in PLOT_FUNDED_ALLOWED
        else _humanize_non_plot(reason=raw_disposition, sealed=sealed)
    )

    return ResearchCandidate(
        id=f"{family}-{version}",
        family=family,
        version=version,
        evidence_kind=ResearchEvidenceKind.SEALED_CAMPAIGN,
        registration=_trial_registration(scope_dir),
        disposition=disposition,
        plot_funded_account=disposition in PLOT_FUNDED_ALLOWED,
        supported_start=supported_start,
        supported_end=supported_end,
        artifact_hashes=artifact_hashes,
        capabilities=_capabilities(disposition),
        ineligibility_reason=ineligibility_reason,
        warnings=_warnings(sealed,
                          supported_start=supported_start,
                          supported_end=supported_end),
        source_url=f"sealed-round/{family}",
    )


def _first_verdict_value(verdicts: Any) -> str | None:
    if not isinstance(verdicts, dict):
        return None
    for v in verdicts.values():
        if isinstance(v, dict):
            for vv in v.values():
                if isinstance(vv, str):
                    return vv
        elif isinstance(v, str):
            return v
    return None


def _capabilities(disposition: ResearchDisposition) -> tuple[str, ...]:
    """The handoff §4 capability matrix, narrowed by what a sealed-round
    candidate can actually do (no portfolio curve until PASS/HOLD-STANDS).
    """
    base = ("view_published_study",)
    if disposition in PLOT_FUNDED_ALLOWED:
        return (
            *base,
            "plot_trade_outcomes",
            "plot_funded_account",
            "rerun_historical_strategy",
        )
    if disposition is ResearchDisposition.NOT_EVALUABLE_SEALED:
        return (*base, "plot_trade_outcomes")  # trade outcomes known; curve refused
    return base  # only the published study is honest


def _humanize_non_plot(reason: str, sealed: dict[str, Any]) -> str:
    notes = sealed.get("notes") or ""
    extra = f" ({notes[:80]})" if notes else ""
    return f"disposition={reason} — see REPORT.md §7{extra}"


def _warnings(sealed: dict[str, Any], *,
            supported_start: date | None,
            supported_end: date | None) -> tuple[str, ...]:
    """Per-candidate warnings. The window-unparsed warning only fires
    when both the explicit fields AND the trials fallback failed to
    produce a usable date — not when the trials fallback succeeded."""
    out: list[str] = []
    sw = sealed.get("sealed_window")
    rnd_window = sealed.get("round", {}).get("scope_window") if isinstance(sealed.get("round"), dict) else None
    if supported_start is None or supported_end is None:
        if sw is None and rnd_window is None:
            out.append("research.window_unparsed")
    if sealed.get("panel_block_history"):
        out.append("research.supersession_recorded")
    return tuple(out)
