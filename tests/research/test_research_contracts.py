"""Round-trip and shape checks for the research contracts.

Money crosses the JSON boundary as a Decimal-rendered string;
``to_dict`` must preserve that (mirror the desk's
``Valuation.as_dict`` discipline).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

from tree_options.research.contracts import (
    PLOT_FUNDED_ALLOWED,
    ComparisonSpec,
    EvidenceEnvelope,
    ResearchCandidate,
    ResearchDisposition,
    ResearchEvidenceKind,
    ResearchRegistration,
    ResearchRun,
    ResearchRunStatus,
)


def test_candidate_to_dict_round_trips_cleanly() -> None:
    c = ResearchCandidate(
        id="vix_term-v2",
        family="vix_term",
        version="v2",
        evidence_kind=ResearchEvidenceKind.SEALED_CAMPAIGN,
        registration=ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
        disposition=ResearchDisposition.PASS,
        plot_funded_account=True,
        supported_start=date(2024, 1, 2),
        supported_end=date(2026, 9, 25),
        artifact_hashes={"selection": "abc123"},
        capabilities=("view_published_study", "plot_trade_outcomes"),
        warnings=(),
        source_url="sealed-round/vix_term",
    )
    d = c.to_dict()
    assert d["id"] == "vix_term-v2"
    assert d["evidence_kind"] == "sealed_campaign"
    assert d["registration"] == "before_entry_window_end"
    assert d["disposition"] == "PASS"
    assert d["plot_funded_account"] is True
    assert d["ineligibility_reason"] is None
    # JSON-safety: every contract field survives a round-trip.
    rt = json.loads(json.dumps(d))
    assert rt == d
    assert set(rt.keys()) == set(d.keys())


def test_candidate_with_hold_stands_is_plot_eligible() -> None:
    """HOLD-STANDS is on the PLOT_FUNDED_ALLOWED list per the campaign
    sealed-round disposition table (vix_term stays dominant)."""
    assert ResearchDisposition.HOLD_STANDS in PLOT_FUNDED_ALLOWED


def test_candidate_with_withdrawn_is_not_plot_eligible() -> None:
    assert ResearchDisposition.WITHDRAWN not in PLOT_FUNDED_ALLOWED
    assert ResearchDisposition.DATA_GATED_NOT_RUN not in PLOT_FUNDED_ALLOWED
    assert ResearchDisposition.NOT_EVALUABLE not in PLOT_FUNDED_ALLOWED


def test_comparison_spec_money_renders_as_string() -> None:
    spec = ComparisonSpec(
        candidate_ids=("vix_term-v2", "hold-20-v2"),
        starting_capital=Decimal("10000.00"),
        common_start=date(2024, 1, 2),
        common_end=date(2026, 9, 25),
        contribution_per_period=Decimal("500.00"),
        benchmark_candidate_id="hold-20-v2",
        knowledge_cutoff=datetime(2026, 9, 25, 20, 15, tzinfo=UTC),
    )
    d = spec.to_dict()
    assert d["starting_capital"] == "10000.00"
    assert d["contribution_per_period"] == "500.00"
    assert d["knowledge_cutoff"] == "2026-09-25T20:15:00+00:00"
    assert d["currency"] == "USD"
    assert d["cost_model_kind"] == "five_bp_fixed"
    assert d["idle_cash_policy"] == "cash_yields_zero"
    assert d["rebalancing"] == "none"
    assert d["position_sizing"] == "integer"
    assert d["collateral"] == "none"
    assert d["borrowing"] == "none"
    # Round-trip through json: Decimal-as-string survives exactly.
    rt = json.loads(json.dumps(d))
    assert rt["starting_capital"] == "10000.00"
    assert float(rt["starting_capital"]) == 10000.0


def test_enums_have_expected_string_values() -> None:
    # These string values are part of the wire contract — do NOT rename
    # without updating web/src/lib/types.ts in lockstep.
    assert ResearchEvidenceKind.SEALED_CAMPAIGN.value == "sealed_campaign"
    assert ResearchRegistration.RETROSPECTIVE_BACKFILL.value == "retrospective_backfill"
    assert ResearchDisposition.NOT_EVALUABLE_SEALED.value == "NOT_EVALUABLE-SEALED"


def test_research_run_to_dict() -> None:
    spec = ComparisonSpec(
        candidate_ids=("vix_term-v2",),
        starting_capital=Decimal("0"),
        common_start=None,
        common_end=None,
    )
    run = ResearchRun(
        id="abc",
        spec=spec,
        spec_hash="deadbeef",
        started_at=datetime(2026, 9, 25, tzinfo=UTC),
        status=ResearchRunStatus.QUEUED,
    )
    d = run.to_dict()
    assert d["id"] == "abc"
    assert d["status"] == "queued"
    assert d["result_id"] is None


def test_evidence_envelope_to_dict_carries_source_artifacts_as_pairs() -> None:
    env = EvidenceEnvelope(
        candidate_id="vix_term-v2",
        point_session=date(2026, 9, 25),
        hypothesis="signed-roll-vol targeting",
        estimand="per-trade ROI",
        exact_versions={"strategy": "vix_term", "data": "synthetic_v1",
                        "miner": "v1", "playbook": "v2"},
        cohort_membership=("tnull", "vrp-cond"),
        registered_or_exploratory=ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
        source_artifacts=(("artifacts/campaign-2026-09/vix_term/sealed-round.json", "abc"),),
        reproduction_command="python scripts/campaign/vix_term_sealed_run.py --trial <id>",
    )
    d = env.to_dict()
    assert d["exact_versions"]["strategy"] == "vix_term"
    assert d["source_artifacts"][0] == {"path": "artifacts/campaign-2026-09/vix_term/sealed-round.json",
                                         "sha256": "abc"}
    assert d["registered_or_exploratory"] == "before_entry_window_end"
