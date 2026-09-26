"""Evidence drawer builder for the Research Lab.

Handoff §7 (Scientific study mode): "Finding / Qualification / Evidence
drawer. The evidence drawer: hypotheses, estimand, exact strategy/data
versions, cohort membership, registered versus exploratory labels,
diagnostics, robustness analyses, source artifacts and reproduction
command."

RL1-05 correction (2026-09-25 audit). The previous drawer:
    * called ``.isoformat()`` on the desk store's STRING timestamps
      (AttributeError the moment a real store had data);
    * counted the ENTIRE store — another candidate's different-session
      mark landed in this candidate's envelope;
    * let SYNTHETIC candidates borrow the desk's proxy marks;
    * reduced a knowledge cutoff to the end of its calendar DATE;
    * read sealed artifacts via CWD-relative paths and silently rehashed
      changed bytes while keeping stale candidate metadata;
    * emitted placeholder reproduction commands pointing at sealed
      campaign executors.

This drawer binds each point to its actual candidate/session/deal,
normalizes timestamps once, compares exact aware instants, keeps
evidence kinds separate, pins artifact paths to the repo root, refuses
changed-source ambiguity with a warning, and hands the operator a REAL
read-only reproduction command (``python -m tree_options.research
inspect …``) — no sealed window is ever reopened to inspect a point.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from sqlite3 import OperationalError as sqlite3_OperationalError
from typing import Any

from tree_options.desk.evidence import EvidenceError
from tree_options.research.comparison.missingness import reason_broker_paper
from tree_options.research.contracts import (
    EvidenceEnvelope,
    ResearchCandidate,
    ResearchEvidenceKind,
)
from tree_options.research.evidence.read_only_evidence import (
    open_read_only_evidence,
)

#: The repo root (drawer.py sits at src/tree_options/research/evidence/).
_REPO_ROOT = Path(__file__).resolve().parents[4]

#: Real, read-only reproduction surface (see tree_options.research.__main__).
_INSPECT_COMMAND = "python -m tree_options.research inspect --candidate {cid}"


def _normalize_instant(value: str | datetime) -> datetime:
    """Desk store timestamps are ISO strings; normalize once to aware
    datetimes (naive values are UTC by convention — never a local-wall
    ambiguity)."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


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

    ``knowledge_cutoff`` bounds the envelope to observations recorded by
    that EXACT instant (never the end of its calendar date). ``None``
    means all recorded observations.
    """
    # EVIDENCE-KIND ROUTING — kinds never borrow each other's data.
    if candidate.evidence_kind in (
        ResearchEvidenceKind.PAPER_EXECUTION,
        ResearchEvidenceKind.BROKER_PAPER,
    ):
        return _broker_paper_envelope(candidate, session)
    if candidate.evidence_kind is ResearchEvidenceKind.SEALED_CAMPAIGN:
        return _sealed_envelope(candidate, session,
                                cutoff_instant_value=knowledge_cutoff)
    if candidate.evidence_kind is ResearchEvidenceKind.SYNTHETIC_BACKTEST:
        return _synthetic_envelope(candidate, session,
                                   cutoff_instant_value=knowledge_cutoff)
    return _shadow_envelope(candidate, session,
                           cutoff_instant_value=knowledge_cutoff,
                           database=database)


def _sealed_envelope(
    candidate: ResearchCandidate,
    session: date | None,
    *,
    cutoff_instant_value: datetime | None,
) -> EvidenceEnvelope:
    scope_dir = _REPO_ROOT / "artifacts" / "campaign-2026-09" / candidate.family
    artifact_paths = tuple(p for p in (scope_dir / "sealed-round.json",)
                           if p.is_file())
    source_artifacts = _hash_source_artifacts(artifact_paths)
    warnings: list[str] = list(candidate.warnings)
    # Changed-source guard: the freshly hashed bytes must still match the
    # hash the CANDIDATE was built from — old metadata over new evidence
    # bytes is a conflict, never a silent combination (RL1-05).
    claimed = candidate.artifact_hashes.get("sealed-round.json")
    if source_artifacts and claimed is not None:
        fresh = source_artifacts[0][1]
        if fresh != claimed:
            warnings.append(
                "research.source_hash_conflict: sealed-round.json bytes no "
                "longer match the hash this candidate was cataloged from "
                f"({claimed[:12]}… vs {fresh[:12]}…); the artifact changed "
                "after cataloging — re-catalog before trusting this envelope"
            )
    elif source_artifacts and claimed is None:
        warnings.append("research.source_unbound: candidate carries no "
                        "sealed-round.json hash to bind provenance")

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
            "funded_history": candidate.funded_history.value,
            "funded_history_reason": candidate.funded_history_reason,
            "plot_funded_account": candidate.plot_funded_account,
            "warnings": list(candidate.warnings),
            "as_of_cutoff": (cutoff_instant_value.isoformat()
                             if cutoff_instant_value else None),
        },
        robustness=(f"sealed_round.json sha: {claimed or '?'}",),
        source_artifacts=source_artifacts,
        reproduction_command=_INSPECT_COMMAND.format(cid=candidate.id)
        + (f" --session {session.isoformat()}" if session else ""),
        warnings=tuple(warnings),
    )


def _shadow_envelope(
    candidate: ResearchCandidate,
    session: date | None,
    *,
    cutoff_instant_value: datetime | None,
    database: Path | None,
) -> EvidenceEnvelope:
    """Desk shadow-proxy evidence — POINT-SCOPED: only marks whose deal
    is associated with THIS candidate and whose session is the requested
    one (all associated sessions when None). A candidate without a
    declared desk association counts ZERO marks rather than borrowing
    the whole store."""
    warnings: list[str] = list(candidate.warnings)
    deal_ids = _associated_deal_ids(candidate)
    if not deal_ids:
        warnings.append(
            "research.no_desk_association: candidate declares no shadow "
            "deal (source_url); no desk marks are attributable to it")

    quality_audit: list[dict[str, Any]] = []
    mark_audit: list[dict[str, Any]] = []
    read_error: str | None = None
    try:
        with open_read_only_evidence(database=database) as store:
            if store is None:
                read_error = "store_not_initialized"
            else:
                quality_pairs: list[tuple[dict[str, Any], datetime | None]]
                mark_pairs: list[tuple[dict[str, Any], datetime | None]]
                if cutoff_instant_value is not None:
                    quality_pairs = [
                        (doc, _normalize_instant(at))
                        for doc, at in store.all_at("quality")
                        if _normalize_instant(at) <= cutoff_instant_value
                    ]
                    mark_pairs = [
                        (doc, _normalize_instant(at))
                        for doc, at in store.all_at("mark")
                        if _normalize_instant(at) <= cutoff_instant_value
                    ]
                else:
                    quality_pairs = [(doc, None)
                                     for doc in store.all("quality")]
                    mark_pairs = [(doc, None) for doc in store.all("mark")]
                quality_audit = _scoped(quality_pairs, deal_ids, session,
                                        kind="quality")
                mark_audit = _scoped(mark_pairs, deal_ids, session, kind="mark")
    except (EvidenceError, FileNotFoundError, OSError,
            sqlite3_OperationalError) as exc:
        read_error = str(exc)

    if read_error:
        warnings.append(f"research.mark_read_error:{read_error}")

    return EvidenceEnvelope(
        candidate_id=candidate.id,
        point_session=session,
        hypothesis=(
            f"shadow_proxy mark for {candidate.family} on "
            f"{session.isoformat() if session else '(session-agnostic)'}"
        ),
        estimand="EOD-deadline shadow mark (proxy valuation — never "
                 "execution evidence)",
        exact_versions={
            "strategy": candidate.family,
            "data": "desk-evidence/desk-mark/2",
            "miner": "n/a",
            "playbook": "n/a",
        },
        cohort_membership=tuple(deal_ids),
        registered_or_exploratory=candidate.registration,
        diagnostics={
            "as_of_cutoff": (cutoff_instant_value.isoformat()
                             if cutoff_instant_value else None),
            "quality_event_count": len(quality_audit),
            "mark_event_count": len(mark_audit),
            "deals": list(deal_ids),
            "marks_window": _window_for(mark_audit),
        },
        robustness=("deadline_eod_proxy valuations are not fills",),
        source_artifacts=(),  # desk evidence is content-addressed internally
        reproduction_command=_INSPECT_COMMAND.format(cid=candidate.id)
        + (f" --session {session.isoformat()}" if session else ""),
        warnings=tuple(warnings),
    )


def _associated_deal_ids(candidate: ResearchCandidate) -> tuple[str, ...]:
    """The desk deals this candidate is associated with, declared (not
    guessed) via ``source_url = "shadow/<deal_id>"``."""
    prefix = "shadow/"
    if candidate.source_url.startswith(prefix):
        return (candidate.source_url[len(prefix):],)
    return ()


def _scoped(pairs: list[tuple[dict[str, Any], datetime | None]],
            deal_ids: tuple[str, ...],
            session: date | None,
            *, kind: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for doc, at in pairs:
        # fail-closed: no declared association ⇒ NOTHING is attributable
        # (an empty deal_ids matches no document, never all of them)
        if doc.get("deal_id") not in deal_ids:
            continue
        if session is not None and doc.get("session") != session.isoformat():
            continue
        out.append({"doc": doc, "at": at.isoformat() if at is not None else None,
                    "kind": kind})
    return out


def _synthetic_envelope(
    candidate: ResearchCandidate,
    session: date | None,
    *,
    cutoff_instant_value: datetime | None,
) -> EvidenceEnvelope:
    """Synthetic fixtures never touch the desk store: their provenance
    is the sha-pinned fixture itself, permanently labeled synthetic —
    a synthetic point must not acquire desk or historical provenance."""
    return EvidenceEnvelope(
        candidate_id=candidate.id,
        point_session=session,
        hypothesis=(
            f"synthetic/v1 fixture point for {candidate.family} on "
            f"{session.isoformat() if session else '(session-agnostic)'}"
        ),
        estimand="synthetic/v1 machinery validation (not investment evidence)",
        exact_versions={
            "strategy": candidate.family,
            "data": "synthetic/v1",
            "miner": "n/a",
            "playbook": "n/a",
        },
        cohort_membership=(candidate.family,),
        registered_or_exploratory=candidate.registration,
        diagnostics={
            "as_of_cutoff": (cutoff_instant_value.isoformat()
                             if cutoff_instant_value else None),
            "fixture_hashes": {k: v for k, v in candidate.artifact_hashes.items()},
        },
        robustness=("fixture plots never become live research results",),
        source_artifacts=(),
        reproduction_command=_INSPECT_COMMAND.format(cid=candidate.id)
        + (f" --session {session.isoformat()}" if session else ""),
        warnings=tuple(candidate.warnings),
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
