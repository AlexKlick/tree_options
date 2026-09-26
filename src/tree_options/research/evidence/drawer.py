"""Evidence drawer builder for the Research Lab.

Handoff §7 (Scientific study mode): "Finding / Qualification / Evidence
drawer. The evidence drawer: hypotheses, estimand, exact strategy/data
versions, cohort membership, registered versus exploratory labels,
diagnostics, robustness analyses, source artifacts and reproduction
command."

Handoff §8 (Missing metrics carry a reason): "Missing metrics carry a
reason (unpriced_fills, missing_daily_nav, no_benchmark_overlap,
insufficient_origins, unsupported_valuation, etc.). Never serialize
unknown monetary values as zero."

This module shapes one ``EvidenceEnvelope`` per (candidate_id, session)
point. The drawer reads from the desk evidence store via
``read_only_evidence.open_read_only_evidence`` and applies the R2-03
knowledge-cutoff pairing (``EvidenceStore.all_at('quality')`` filtered by
``cutoff_instant``) so the envelope only references observations that
were already recorded by the requested cutoff.

Per ``evidence_kind`` the envelope shape differs:
    - sealed_campaign   → registration/miner/playbook shas +
                            ``operator_rulings_2026_09_24``
    - shadow_proxy      → desk evidence ``mark`` audit chain (sha-bounded)
    - synthetic_backtest→ fixture sha + LedgerBook conservation proof
    - paper_execution / broker_paper → "out of scope for RL-1"
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from pathlib import Path
from sqlite3 import OperationalError as sqlite3_OperationalError
from typing import Any

from tree_options.desk.evidence import EvidenceError
from tree_options.desk.sessions import cutoff_instant
from tree_options.research.comparison.missingness import reason_broker_paper
from tree_options.research.contracts import (
    EvidenceEnvelope,
    ResearchCandidate,
    ResearchEvidenceKind,
    ResearchRegistration,
)
from tree_options.research.evidence.read_only_evidence import (
    open_read_only_evidence,
)

#: Session-attributable dashboard command used by the SPA to link the
#: drawer to the operator's daily evidence viewer. The drawer itself
#: runs in-process and does NOT spawn a subprocess.
_DASHBOARD_COMMAND_TEMPLATE = (
    "python -m tree_options.desk shadows --session {session} --as-of {as_of}"
)


def _hash_source_artifacts(paths: tuple[Path, ...]) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for p in paths:
        try:
            data = p.read_bytes()
        except OSError:
            continue
        out.append((str(p), hashlib.sha256(data).hexdigest()))
    return tuple(out)


def evidence_for_point(
    candidate: ResearchCandidate,
    *,
    session: date | None = None,
    knowledge_cutoff: datetime | None = None,
    database: Path | None = None,
) -> EvidenceEnvelope:
    """Build one ``EvidenceEnvelope`` for the (candidate, session) point.

    The ``knowledge_cutoff`` (R2-03 separation) bounds the audit envelope
    to observations recorded by that instant. ``None`` means "all
    recorded observations" (the desk scorecards default for
    ``as_of_requested=None``).
    """
    cutoff_instant_value = (
        cutoff_instant(knowledge_cutoff.date()) if knowledge_cutoff is not None else None
    )

    # EVIDENCE-KIND ROUTING
    if isinstance(candidate.evidence_kind, ResearchEvidenceKind):
        if candidate.evidence_kind in (
            ResearchEvidenceKind.PAPER_EXECUTION,
            ResearchEvidenceKind.BROKER_PAPER,
        ):
            return _broker_paper_envelope(candidate, session)

        if candidate.evidence_kind is ResearchEvidenceKind.SEALED_CAMPAIGN:
            return _sealed_envelope(candidate, session,
                                    cutoff_instant_value=cutoff_instant_value)
    else:
        # Unknown / future evidence_kind — safe default is "out of scope",
        # not a fabricated shape.
        return _broker_paper_envelope(candidate, session)

    # shadow_proxy + synthetic_backtest use the desk evidence store.
    # When the store doesn't exist yet (no live desk fire), open_read_only_evidence
    # yields None — callers must treat that as "no audit envelopes yet".
    quality_pairs: list[tuple[dict[str, Any], Any]] = []
    marks: list[tuple[dict[str, Any], Any]] = []
    mark_read_error: str | None = None
    try:
        with open_read_only_evidence(database=database) as store:
            if store is None:
                mark_read_error = "store_not_initialized"
            else:
                if cutoff_instant_value:
                    quality_pairs = list(store.all_at("quality"))
                    marks = list(store.all_at("mark"))
                else:
                    quality_pairs = [(doc, None) for doc in store.all("quality")]
                    marks = [(doc, None) for doc in store.all("mark")]
    except (EvidenceError, FileNotFoundError, OSError, sqlite3_OperationalError) as exc:
        quality_pairs = []
        marks = []
        mark_read_error = str(exc)

    return _shadow_or_synthetic_envelope(
        candidate, session,
        quality_pairs=quality_pairs,
        marks=marks,
        cutoff_instant_value=cutoff_instant_value,
        mark_read_error=mark_read_error,
    )


def _sealed_envelope(
    candidate: ResearchCandidate,
    session: date | None,
    *,
    cutoff_instant_value: datetime | None,
) -> EvidenceEnvelope:
    artifact_paths = tuple(
        Path(p) for p in [
            f"artifacts/campaign-2026-09/{candidate.family}/sealed-round.json",
        ]
        if Path(p).exists()
    )
    if (source_dir := Path(f"artifacts/campaign-2026-09/{candidate.family}")).exists():
        artifact_paths = (*artifact_paths, source_dir / "sealed-round.json")
    return EvidenceEnvelope(
        candidate_id=candidate.id,
        point_session=session,
        hypothesis=f"Sealed-round verdict for {candidate.family} ({candidate.version})",
        estimand="campaign disposition as filed at sealed-round freeze",
        exact_versions={
            "strategy": candidate.family,
            "data": "sealed-round-v1",
            "miner": candidate.artifact_hashes.get("sealed-round.calibration_v3_sha256", "?"),
            "playbook": candidate.artifact_hashes.get("sealed-round.round1_selection_sha256", "?"),
        },
        cohort_membership=(candidate.family,),
        registered_or_exploratory=candidate.registration,
        diagnostics={
            "disposition": candidate.disposition.value,
            "plot_funded_account": candidate.plot_funded_account,
            "warnings": list(candidate.warnings),
            "as_of_cutoff": cutoff_instant_value.isoformat() if cutoff_instant_value else None,
        },
        robustness=(f"sealed_round.json sha: {candidate.artifact_hashes.get('sealed-round.json', '?')}",),
        source_artifacts=_hash_source_artifacts(artifact_paths),
        reproduction_command=(
            f"python scripts/campaign/{candidate.family}_sealed_run.py "
            f"--trial <id-from-{candidate.family}>"
        ),
        warnings=(candidate.ineligibility_reason,) if candidate.ineligibility_reason else (),
    )


def _shadow_or_synthetic_envelope(
    candidate: ResearchCandidate,
    session: date | None,
    *,
    quality_pairs,
    marks,
    cutoff_instant_value: datetime | None,
    mark_read_error: str | None,
) -> EvidenceEnvelope:
    quality_audit = [
        {"doc": doc, "at": at.isoformat() if at else None}
        for doc, at in quality_pairs
    ]
    if cutoff_instant_value is not None:
        quality_audit = [
            q for q in quality_audit
            if q["at"] and q["at"] <= cutoff_instant_value.isoformat()
        ]
    mark_audit = [
        {"doc": doc, "at": at.isoformat() if at else None}
        for doc, at in marks
    ]
    if cutoff_instant_value is not None:
        mark_audit = [
            m for m in mark_audit
            if m["at"] and m["at"] <= cutoff_instant_value.isoformat()
        ]

    if candidate.evidence_kind is ResearchEvidenceKind.SYNTHETIC_BACKTEST:
        reproduction_command = (
            "python -m tree_options.backtest.equity --scenario synthetic_v1 "
            "--fixtures data/backtest/fixtures/<fixture>.json"
        )
    else:
        reproduction_command = _DASHBOARD_COMMAND_TEMPLATE.format(
            session=session.isoformat() if session else "<session>",
            as_of=(cutoff_instant_value.date().isoformat()
                   if cutoff_instant_value else "all_recorded_observations"),
        )

    warnings: list[str] = list(candidate.warnings)
    if mark_read_error:
        warnings.append(f"research.mark_read_error:{mark_read_error}")

    return EvidenceEnvelope(
        candidate_id=candidate.id,
        point_session=session,
        hypothesis=(
            f"{candidate.evidence_kind.value} mark for {candidate.family} "
            f"on {session.isoformat() if session else '(session-agnostic)'}"
        ),
        estimand=("synthetic/v1 backtest trade outcome"
                  if candidate.evidence_kind is ResearchEvidenceKind.SYNTHETIC_BACKTEST
                  else "EOD-deadline shadow mark"),
        exact_versions={
            "strategy": candidate.family,
            "data": candidate.evidence_kind.value,
            "miner": "n/a",
            "playbook": "n/a",
        },
        cohort_membership=(candidate.family,),
        registered_or_exploratory=candidate.registration,
        diagnostics={
            "as_of_cutoff": cutoff_instant_value.isoformat() if cutoff_instant_value else None,
            "quality_event_count": len(quality_audit),
            "mark_event_count": len(mark_audit),
            "marks_window": _window_for(mark_audit),
        },
        robustness=(
            ("no wallet curve for retrospective_backfill",)
            if candidate.registration is ResearchRegistration.RETROSPECTIVE_BACKFILL
            else ()
        ),
        source_artifacts=(),  # desk evidence is content-addressed internally
        reproduction_command=reproduction_command,
        warnings=tuple(warnings),
    )


def _broker_paper_envelope(candidate: ResearchCandidate,
                           session: date | None) -> EvidenceEnvelope:
    reason = reason_broker_paper()
    return EvidenceEnvelope(
        candidate_id=candidate.id,
        point_session=session,
        hypothesis="Out of scope for RL-1 (E5 broker-paper requires operator approval).",
        estimand="n/a",
        exact_versions={"strategy": candidate.family, "data": "n/a",
                        "miner": "n/a", "playbook": "n/a"},
        cohort_membership=(),
        registered_or_exploratory=candidate.registration,
        diagnostics={"reason_code": reason.code},
        robustness=(),
        source_artifacts=(),
        reproduction_command="(none — out of scope for RL-1)",
        warnings=(reason.code,),
    )


def _window_for(mark_audit: list[dict[str, Any]]) -> dict[str, str] | None:
    if not mark_audit:
        return None
    times = sorted(m["at"] for m in mark_audit if m["at"])
    if not times:
        return None
    return {"first": times[0], "last": times[-1]}


__all__ = ["evidence_for_point"]
