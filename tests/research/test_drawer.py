"""Evidence drawer tests — RL §11 acceptance matrix rows.

Coverage:
    * Access control — research worker cannot place broker orders
      (enforced elsewhere; tested by tests/research/test_boundaries.py)
    * Chart fidelity — same envelope powers tooltip + drawer
    * Forecast semantics — see routes test (forecast endpoint returns 410)
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

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
    assert "scripts/campaign/vix_term_sealed_run.py" in env.reproduction_command


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


def test_synthetic_backtest_envelope_carries_synthetic_reproduction_cmd() -> None:
    c = _candidate(evidence=ResearchEvidenceKind.SYNTHETIC_BACKTEST)
    env = evidence_for_point(c, session=date(2026, 9, 25))
    assert "backtest.equity" in env.reproduction_command
    assert "synthetic_v1" in env.reproduction_command


def test_retrospective_registration_annotates_no_wallet_curve() -> None:
    """For shadow_proxy + synthetic_backtest evidence kinds the
    robustness field carries the "no wallet curve" annotation when the
    registration is retrospective. Sealed candidates do not need this
    annotation (the campaign verdict is already explicit)."""
    c = _candidate(evidence=ResearchEvidenceKind.SHADOW_PROXY,
                  registration=ResearchRegistration.RETROSPECTIVE_BACKFILL)
    env = evidence_for_point(c, session=date(2026, 9, 25),
                             database=__import__("pathlib").Path("/nonexistent.sqlite3"))
    assert any("no wallet curve" in r for r in env.robustness)


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


def test_envelope_with_unknown_evidence_kind_returns_broker_paper_shape() -> None:
    """Forward-compat: an evidence_kind added after this test would fall
    through to the broker-paper envelope; that's not perfect (it could
    mis-classify a real new kind) but the bug surfaces as 'out of scope',
    not as fabricated data."""
    import enum
    # Construct an enum-like value the function won't recognise:
    class _FutureKind(enum.Enum):
        FUTURE_THING = "future_thing"
    c = _candidate()
    object.__setattr__(c, "evidence_kind", _FutureKind.FUTURE_THING)
    env = evidence_for_point(c)
    # It falls into the broker-paper branch (which is the safe default).
    assert env.hypothesis.startswith("Out of scope")
