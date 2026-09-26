"""Comparison engine tests — RL §11 acceptance matrix rows that the
engine covers.

Coverage:
    * Dataset scope — synthetic, paper, sealed, broker-paper are distinct
      evidence kinds; the engine refuses broker-paper with a reason
    * Selection integrity — retrospective candidates get no wallet curve
    * Sample floor — sample_floor_met is set only when sample_size >=
      SAMPLE_FLOOR
    * Missingness (priced-subset never prices whole) — covered via the
      funded accounting + the paired-diff null rendering (see test_pair.py)

The funded-account economic oracles (NAV vs cash, flows vs gain, fee
application, ledger conservation) moved to ``test_funded.py`` when the
2026-09-25 audit showed this file's contribution oracle ($10,520)
repeated the implementation's flawed identity instead of checking
economics (correct NAV: $10,510).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from tree_options.research.comparison.engine import (
    SAMPLE_FLOOR,
    run_comparison,
)
from tree_options.research.contracts import (
    PLOT_FUNDED_ALLOWED,
    ComparisonSpec,
    ResearchCandidate,
    ResearchDisposition,
    ResearchEvidenceKind,
    ResearchRegistration,
)


def _candidate(*, family="vix_term", disposition=ResearchDisposition.PASS,
               registration=ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
               evidence=ResearchEvidenceKind.SEALED_CAMPAIGN,
               supported_start=date(2024, 1, 1),
               supported_end=date(2026, 9, 25),
               id=None) -> ResearchCandidate:
    cid = id or f"{family}-v2"
    return ResearchCandidate(
        id=cid,
        family=family,
        version="v2",
        evidence_kind=evidence,
        registration=registration,
        disposition=disposition,
        plot_funded_account=disposition in PLOT_FUNDED_ALLOWED,
        supported_start=supported_start,
        supported_end=supported_end,
    )


def _spec(candidates=("vix_term-v2", "hold-20-v2"),
          starting_capital=Decimal("10000")) -> ComparisonSpec:
    return ComparisonSpec(
        candidate_ids=candidates,
        starting_capital=starting_capital,
        common_start=date(2024, 1, 2),
        common_end=date(2026, 9, 25),
    )


# -- engine tests (acceptance matrix) --------------------------------------


def test_engine_refuses_broker_paper_with_reason() -> None:
    c = _candidate(evidence=ResearchEvidenceKind.BROKER_PAPER,
                  disposition=ResearchDisposition.PASS)
    spec = _spec()
    res = run_comparison(spec, (c,), baseline=_candidate(family="bh"))
    assert res.candidates[0].rejection_reason is not None
    assert "broker_paper" in res.candidates[0].rejection_reason or \
           "research.broker_paper" == res.candidates[0].rejection_reason


def test_engine_no_longer_refuses_retrospective_wholesale() -> None:
    """RL1-06: registration is a study-provenance LABEL the summary
    carries, not a plotting veto — a retrospective candidate with
    reconstructable data plots; one without data is rejected for the
    MISSING DATA, with the verdict named separately."""
    with_data = _candidate(registration=ResearchRegistration.RETROSPECTIVE_BACKFILL)
    res = run_comparison(_spec(), (with_data,))
    assert res.candidates[0].rejection_reason is None  # data speaks
    assert res.candidates[0].candidate.registration.value == "retrospective_backfill"

    no_data = _candidate(registration=ResearchRegistration.RETROSPECTIVE_BACKFILL,
                         disposition=ResearchDisposition.WITHDRAWN)
    res2 = run_comparison(_spec(), (no_data,))
    assert res2.candidates[0].rejection_reason is not None
    assert "no funded history" in res2.candidates[0].rejection_reason
    assert "WITHDRAWN" in res2.candidates[0].rejection_reason


def test_engine_marks_ineligible_disposition_with_reason() -> None:
    c = _candidate(disposition=ResearchDisposition.WITHDRAWN)
    spec = _spec()
    res = run_comparison(spec, (c,))
    assert res.candidates[0].rejection_reason is not None
    assert "WITHDRAWN" in res.candidates[0].rejection_reason


def test_engine_zero_sample_size_is_below_sample_floor() -> None:
    c = _candidate()  # SEALED_CAMPAIGN with no executions yet (adapter stub)
    spec = _spec()
    res = run_comparison(spec, (c,))
    assert res.candidates[0].sample_size == 0
    assert res.candidates[0].sample_floor == SAMPLE_FLOOR
    assert res.candidates[0].sample_floor_met is False


def test_engine_sealed_candidate_returns_zero_fills_until_adapter_filled() -> None:
    """The sealed adapter stub returns []; the engine surfaces this as
    zero-row summary with no fabricated wallet curve (RL §11 missingness
    + handoff §4 'unsupported candidates show reasons instead of
    invented series')."""
    c = _candidate()
    spec = _spec()
    res = run_comparison(spec, (c,))
    assert res.candidates[0].rows_by_date == {}
    assert res.candidates[0].drawdown == {}
    assert res.candidates[0].final_ending_value is None
    assert res.candidates[0].rejection_reason is None  # eligibility ok
    assert res.candidates[0].sample_size == 0


def test_engine_paired_diff_only_for_plot_eligible_candidates() -> None:
    """Ineligible candidates never enter the paired diff."""
    eligible = _candidate(family="vix_term")
    ineligible = _candidate(family="term-gate",
                            disposition=ResearchDisposition.WITHDRAWN)
    baseline = _candidate(family="bh",
                         disposition=ResearchDisposition.HOLD_STANDS)
    spec = _spec(candidates=(eligible.id, ineligible.id))
    res = run_comparison(spec, (eligible, ineligible), baseline=baseline)
    assert eligible.id in res.paired_diff
    assert ineligible.id not in res.paired_diff
