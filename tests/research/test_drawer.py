"""Evidence drawer tests — RL §11 acceptance matrix rows.

Coverage:
    * Access control — research worker cannot place broker orders
      (enforced elsewhere; tested by tests/research/test_boundaries.py)
    * Chart fidelity — same envelope powers tooltip + drawer
    * Forecast semantics — see routes test (forecast endpoint returns 410)
"""

from __future__ import annotations

from datetime import UTC, date
from pathlib import Path

import pytest

from tree_options.research.contracts import (
    ResearchCandidate,
    ResearchDisposition,
    ResearchEvidenceKind,
    ResearchRegistration,
)
from tree_options.research.evidence.drawer import evidence_for_point


def _candidate(*, evidence=ResearchEvidenceKind.SEALED_CAMPAIGN,
               family="vix_term",
               disposition=ResearchDisposition.PASS,
               registration=ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
               version="v2") -> ResearchCandidate:
    return ResearchCandidate(
        id=f"{family}-{version}",
        family=family,
        version=version,
        evidence_kind=evidence,
        registration=registration,
        disposition=disposition,
        plot_funded_account=disposition
        in {ResearchDisposition.PASS, ResearchDisposition.HOLD_STANDS},
        supported_start=date(2024, 1, 2),
        supported_end=date(2026, 9, 25),
        artifact_hashes={"sealed-round.calibration_v3_sha256": "abc",
                         "sealed-round.round1_selection_sha256": "def",
                         "sealed-round.json": "a" * 64},
    )


def test_sealed_envelope_carries_version_shas_and_reproduction_cmd(tmp_path: Path) -> None:
    c = _candidate(family="vix_term")
    env = evidence_for_point(c, session=date(2026, 9, 25))
    assert env.candidate_id == "vix_term-v2"
    assert env.point_session == date(2026, 9, 25)
    assert env.exact_versions["strategy"] == "vix_term"
    assert env.exact_versions["miner"] == "abc"
    assert env.exact_versions["playbook"] == "def"
    # RL1-05: a REAL read-only reproduction command — never a placeholder
    # pointing at the sealed campaign executor.
    assert env.reproduction_command == (
        "python -m tree_options.research inspect --candidate vix_term-v2 "
        "--session 2026-09-25")


def test_sealed_envelope_with_no_artifact_directory_carries_no_source_paths(tmp_path: Path) -> None:
    c = _candidate(family="nonexistent_scope")
    env = evidence_for_point(c)
    # No filesystem = no source_artifacts paths; the envelope still has
    # the rest of the shape (hypothesis, estimand, versions).
    assert env.source_artifacts == ()
    assert env.exact_versions["strategy"] == "nonexistent_scope"


def test_broker_paper_envelope_returns_out_of_scope_shape(tmp_path: Path) -> None:
    c = _candidate(evidence=ResearchEvidenceKind.BROKER_PAPER)
    env = evidence_for_point(c, session=date(2026, 9, 25))
    assert env.hypothesis.startswith("Out of scope")
    assert env.estimand == "n/a"
    assert env.reproduction_command == "(none — out of scope for RL-1)"
    assert env.warnings == ("research.broker_paper",)
    assert env.exact_versions["data"] == "n/a"


def test_paper_execution_kind_returns_out_of_scope_shape(tmp_path: Path) -> None:
    c = _candidate(evidence=ResearchEvidenceKind.PAPER_EXECUTION)
    env = evidence_for_point(c)
    assert env.hypothesis.startswith("Out of scope")
    assert "research.broker_paper" in env.warnings


def test_shadow_proxy_envelope_reads_desk_evidence_with_cutoff(tmp_path: Path) -> None:
    """The shadow envelope reads from the desk evidence store; with no
    store present (no live desk at this path) it surfaces a
    research.mark_read_error warning and zero audit events."""
    c = _candidate(evidence=ResearchEvidenceKind.SHADOW_PROXY)
    env = evidence_for_point(c, session=date(2026, 9, 25),
                             database=tmp_path / "nonexistent.sqlite3")
    # No live store -> no marks, no quality; warnings carry the read error.
    assert env.diagnostics["mark_event_count"] == 0
    assert any("research.mark_read_error" in w for w in env.warnings)


def test_synthetic_envelope_never_reads_desk_evidence() -> None:
    """RL1-05: a synthetic candidate must not borrow another evidence
    kind's data — no desk marks, fixture provenance only, permanently
    labeled synthetic."""
    c = _candidate(evidence=ResearchEvidenceKind.SYNTHETIC_BACKTEST)
    env = evidence_for_point(c, session=date(2026, 9, 25),
                             database=_populated_desk_store(
                                 "SYN-DEAL", date(2026, 9, 25)))
    assert "mark_event_count" not in env.diagnostics  # never even looked
    assert env.estimand.startswith("synthetic/v1")
    assert "synthetic/v1" in env.exact_versions["data"] or \
        env.exact_versions["data"] == "synthetic/v1"
    assert env.reproduction_command.startswith(
        "python -m tree_options.research inspect")


def test_retrospective_registration_is_a_label_not_a_veto() -> None:
    """RL1-06: registration rides on the envelope as provenance; the
    shadow envelope's robustness note is about proxy semantics, not a
    'no wallet curve' suppression."""
    c = _candidate(evidence=ResearchEvidenceKind.SHADOW_PROXY,
                  registration=ResearchRegistration.RETROSPECTIVE_BACKFILL)
    env = evidence_for_point(c, session=date(2026, 9, 25),
                             database=Path("/nonexistent.sqlite3"))
    assert env.registered_or_exploratory is ResearchRegistration.RETROSPECTIVE_BACKFILL
    assert any("proxy" in r for r in env.robustness)


def test_sealed_envelope_retrospective_carries_no_wallet_curve_via_robustness() -> None:
    """Sealed campaigns carry the operator's verbatim verdict; the
    envelope's robustness field is informational, not a wallet-curve
    claim — a retrospective sealed candidate does not need the
    annotation (the campaign's verdict is already explicit)."""
    c = _candidate(registration=ResearchRegistration.RETROSPECTIVE_BACKFILL)
    env = evidence_for_point(c, session=date(2026, 9, 25))
    # Sealed envelope doesn't add the no-wallet-curve robustness note.
    assert not any("no wallet curve" in r for r in env.robustness)


def test_envelope_serializes_all_required_fields(tmp_path: Path) -> None:
    """Charts and tooltip both pull from the same envelope — the field
    set is the source of truth for both."""
    c = _candidate(family="hold-20")
    env = evidence_for_point(c, session=date(2026, 9, 25))
    d = env.to_dict()
    for required in ("candidate_id", "point_session", "hypothesis", "estimand",
                     "exact_versions", "cohort_membership",
                     "registered_or_exploratory", "diagnostics", "robustness",
                     "source_artifacts", "reproduction_command", "warnings"):
        assert required in d, f"missing required field: {required}"
    # And the registration enum value crosses as its string form.
    assert d["registered_or_exploratory"] == "before_entry_window_end"


def test_envelope_with_unknown_evidence_kind_fails_closed() -> None:
    """Forward-compat: an unrecognized evidence_kind routes to the
    shadow shape with NO desk association — zero marks counted and an
    explicit warning, never borrowed data."""
    import enum

    class _FutureKind(enum.Enum):
        FUTURE_THING = "future_thing"
    c = _candidate()
    object.__setattr__(c, "evidence_kind", _FutureKind.FUTURE_THING)
    env = evidence_for_point(c)
    assert env.diagnostics["mark_event_count"] == 0
    assert any("research.no_desk_association" in w for w in env.warnings)


# -- RL1-05: point-specific provenance boundaries -----------------------------


def _populated_desk_store(deal_id: str, session: date,
                          tmp: Path | None = None) -> Path:
    """A REAL desk EvidenceStore (the drawer must integrate with its
    actual interface — string timestamps and all). One mark for
    ``(deal_id, session)`` recorded at 12:00 UTC."""
    import tempfile
    from datetime import datetime

    from tree_options.desk.evidence import EvidenceStore
    root = tmp or Path(tempfile.mkdtemp())
    db = root / "desk.sqlite3"
    at = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    with EvidenceStore(db) as store:
        with store.atomic():
            store.put("mark", f"{deal_id}|{session.isoformat()}",
                      {"schema": "desk-mark/2", "deal_id": deal_id,
                       "session": session.isoformat(),
                       "realistic": "1.25"}, at)
    return db


def test_evidence_accepts_real_store_timestamp_type(tmp_path: Path) -> None:
    """The audit's AttributeError: the desk store's ``all_at`` returns
    STRING timestamps; the old drawer called ``.isoformat()`` on them.
    A real populated store must produce an envelope, not a crash."""
    c = _candidate(evidence=ResearchEvidenceKind.SHADOW_PROXY,
                   family="vix_term")
    object.__setattr__(c, "source_url", "shadow/DEAL-A")
    db = _populated_desk_store("DEAL-A", date(2026, 9, 25), tmp_path)
    env = evidence_for_point(c, session=date(2026, 9, 25), database=db)
    assert env.diagnostics["mark_event_count"] == 1
    assert env.diagnostics["deals"] == ["DEAL-A"]


def test_point_envelope_does_not_count_other_candidates_and_sessions(
        tmp_path: Path) -> None:
    """The audit's probe: one target mark plus ANOTHER candidate's
    different-session mark used to yield two events in this envelope.
    Filtering is by declared deal association AND requested session."""
    from datetime import datetime

    from tree_options.desk.evidence import EvidenceStore

    db_path = tmp_path / "desk.sqlite3"
    at = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    with EvidenceStore(db_path) as store:
        with store.atomic():
            store.put("mark", "DEAL-A|2026-09-25",
                      {"schema": "desk-mark/2", "deal_id": "DEAL-A",
                       "session": "2026-09-25", "realistic": "1.25"}, at)
            store.put("mark", "DEAL-B|2026-09-24",
                      {"schema": "desk-mark/2", "deal_id": "DEAL-B",
                       "session": "2026-09-24", "realistic": "0.75"}, at)

    cand_a = _candidate(evidence=ResearchEvidenceKind.SHADOW_PROXY,
                        family="vix_term")
    object.__setattr__(cand_a, "source_url", "shadow/DEAL-A")
    env = evidence_for_point(cand_a, session=date(2026, 9, 25),
                             database=db_path)
    assert env.diagnostics["mark_event_count"] == 1  # only DEAL-A @ 09-25

    env_no_session = evidence_for_point(cand_a, database=db_path)
    assert env_no_session.diagnostics["mark_event_count"] == 1  # all DEAL-A sessions


def test_exact_instant_cutoff_not_date_rounded(tmp_path: Path) -> None:
    """The audit: a 08:00 UTC cutoff on 2026-09-25 used to behave like
    end-of-day (a mark recorded at 12:00 that day was included). The
    cutoff is the EXACT instant now."""
    from datetime import datetime

    c = _candidate(evidence=ResearchEvidenceKind.SHADOW_PROXY,
                   family="vix_term")
    object.__setattr__(c, "source_url", "shadow/DEAL-A")
    db = _populated_desk_store("DEAL-A", date(2026, 9, 25), tmp_path)  # 12:00Z
    before = evidence_for_point(
        c, session=date(2026, 9, 25), database=db,
        knowledge_cutoff=datetime(2026, 9, 25, 8, 0, tzinfo=UTC))
    assert before.diagnostics["mark_event_count"] == 0  # not yet recorded
    at_instant = evidence_for_point(
        c, session=date(2026, 9, 25), database=db,
        knowledge_cutoff=datetime(2026, 9, 25, 12, 0, tzinfo=UTC))
    assert at_instant.diagnostics["mark_event_count"] == 1  # boundary inclusive


def test_changed_sealed_source_conflicts_instead_of_silent_rehash(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit: a cached candidate kept its old metadata while the
    drawer re-hashed CHANGED source bytes — old metadata over new
    evidence, silently. A mismatch is a loud warning now."""
    from tree_options.research.evidence import drawer as drawer_mod

    fam = tmp_path / "artifacts" / "campaign-2026-09" / "changed-scope"
    fam.mkdir(parents=True)
    (fam / "sealed-round.json").write_text('{"family_verdict": "PASS"}')
    monkeypatch.setattr(drawer_mod, "_REPO_ROOT", tmp_path)
    c = _candidate(family="changed-scope")
    # candidate claims a hash the file no longer matches
    assert c.artifact_hashes["sealed-round.json"] != (
        __import__("hashlib").sha256((fam / "sealed-round.json").read_bytes())
        .hexdigest())
    env = evidence_for_point(c, session=date(2026, 9, 25))
    assert any("research.source_hash_conflict" in w for w in env.warnings)
    assert env.source_artifacts  # the FRESH hash is what's reported


def test_unassociated_candidate_counts_zero_marks_not_everything(
        tmp_path: Path) -> None:
    """A shadow candidate with no declared desk association borrows
    nothing: zero marks and an explicit warning (the old drawer counted
    the whole store)."""
    c = _candidate(evidence=ResearchEvidenceKind.SHADOW_PROXY,
                   family="vix_term")
    db = _populated_desk_store("DEAL-A", date(2026, 9, 25), tmp_path)
    env = evidence_for_point(c, session=date(2026, 9, 25), database=db)
    assert env.diagnostics["mark_event_count"] == 0
    assert any("research.no_desk_association" in w for w in env.warnings)
