"""Catalog ingestion tests for the sealed-round adapter.

Each test seeds a tmp_path campaign layout and asserts the adapter
emits the right ``ResearchCandidate``. The adapter is the RL-1 catalog's
default; the loader-discovery layer is a separate test in
``test_adapters_loader.py``.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from tree_options.research.catalog.sealed_round import build_candidate
from tree_options.research.contracts import (
    ResearchDisposition,
    ResearchRegistration,
)


def _write(scope: Path, doc: dict) -> None:
    scope.mkdir(parents=True, exist_ok=True)
    (scope / "sealed-round.json").write_text(json.dumps(doc))


def test_passing_scope_emits_plot_eligible_candidate(tmp_path: Path) -> None:
    """A sealed-round with family_verdict=PASS produces a plot-eligible
    candidate carrying the right hashes and supported window."""
    scope = tmp_path / "vix_term"
    _write(scope, {
        "conventions_disclosed": "...",
        "frozen_inputs": {
            "round1_selection_sha256": "abcdef1234567890",
            "calibration_v3_sha256": "cafef00d",
        },
        "round": {"round2-sealed": True},
        "family_verdict": "PASS",
        "verdicts": {"a_h21": "PASS"},
        "scope_accounting": {},
        "stamp": {"sha": "stamp-1"},
        "notes": "vix_term incumbent dominates",
        "operator_rulings_2026_09_24": "carry-forward",
    })
    cand = build_candidate(scope)
    assert cand.family == "vix_term"
    assert cand.disposition is ResearchDisposition.PASS
    # RL1-06: a PASS verdict is NOT data. The sealed-round format records
    # no capital/cashflow/valuation history, so even a PASS cannot plot a
    # funded account — the verdict and the capability stay separate.
    assert cand.plot_funded_account is False
    assert cand.funded_history.value == "unavailable"
    assert cand.funded_history_reason
    assert cand.registration is ResearchRegistration.RETROSPECTIVE_BACKFILL
    assert cand.evidence_kind.value == "sealed_campaign"
    assert cand.ineligibility_reason is not None
    assert cand.source_url == "sealed-round/vix_term"
    # Hashes include both frozen-input names AND the sealed-round.json itself.
    assert "sealed-round.round1_selection_sha256" in cand.artifact_hashes
    assert "sealed-round.calibration_v3_sha256" in cand.artifact_hashes
    assert "sealed-round.json" in cand.artifact_hashes
    assert len(cand.artifact_hashes["sealed-round.json"]) == 64  # sha256 hex
    # Capabilities come from DATA support: no funded plot without a
    # reconstructable series, published study always viewable.
    assert "plot_funded_account" not in cand.capabilities
    assert "view_published_study" in cand.capabilities


def test_withdrawn_scope_emits_non_plot_candidate_with_reason(tmp_path: Path) -> None:
    scope = tmp_path / "term-gate"
    _write(scope, {
        "family_verdict": "WITHDRAWN",
        "frozen_inputs": {"calibration_v3_sha256": "abc"},
        "round": {"round2-sealed": True},
    })
    cand = build_candidate(scope)
    assert cand.disposition is ResearchDisposition.WITHDRAWN
    assert cand.plot_funded_account is False
    assert cand.ineligibility_reason is not None
    assert "WITHDRAWN" in cand.ineligibility_reason
    assert "plot_funded_account" not in cand.capabilities
    # Carries calibration sha even when not plot-eligible.
    assert "sealed-round.calibration_v3_sha256" in cand.artifact_hashes


def test_hold_stands_keeps_verdict_but_not_a_funded_plot(tmp_path: Path) -> None:
    """HOLD-STANDS remains a valid scientific disposition — and still
    cannot conjure a funded series the artifacts do not contain
    (RL1-06: verdict and data capability are separate dimensions)."""
    scope = tmp_path / "hold-20"
    _write(scope, {
        "family_verdict": "HOLD-STANDS",
        "frozen_inputs": {"calibration_v3_sha256": "h20"},
        "round": {"round2-sealed": True},
    })
    cand = build_candidate(scope)
    assert cand.disposition is ResearchDisposition.HOLD_STANDS
    assert cand.plot_funded_account is False
    assert cand.funded_history.value == "unavailable"
    assert cand.ineligibility_reason is not None


def test_scope_o_withdrawal_overrides_verdict(tmp_path: Path) -> None:
    """vrp-cond's scope-O withdrawal is documented in scope_accounting; the
    adapter must surface it as WITHDRAWN even when family_verdict disagrees."""
    scope = tmp_path / "vrp-cond"
    _write(scope, {
        "family_verdict": "PASS",
        "scope_accounting": {"scope_O": "WITHDRAWN"},
        "frozen_inputs": {"round1_selection_sha256": "v"},
        "round": {"2": True},
    })
    cand = build_candidate(scope)
    assert cand.disposition is ResearchDisposition.WITHDRAWN
    assert cand.plot_funded_account is False


def test_data_gated_when_sealed_round_missing(tmp_path: Path) -> None:
    """Scopes like tnull that were VOIDED never wrote a sealed-round.json."""
    scope = tmp_path / "tnull"
    scope.mkdir()
    cand = build_candidate(scope)
    assert cand.disposition is ResearchDisposition.DATA_GATED_NOT_RUN
    assert cand.plot_funded_account is False
    assert cand.ineligibility_reason is not None
    assert "DATA-GATED" in cand.ineligibility_reason or "sealed-round.json not present" in cand.ineligibility_reason
    # Warning flags the missing window.
    assert "research.data_gated" in cand.warnings


def test_supported_window_parsed_from_sealed_window_field(tmp_path: Path) -> None:
    scope = tmp_path / "foo"
    _write(scope, {
        "family_verdict": "PASS",
        "frozen_inputs": {},
        "sealed_window": {"start": "2024-01-02", "end": "2026-09-25"},
        "round": {},
    })
    cand = build_candidate(scope)
    from datetime import date
    assert cand.supported_start == date(2024, 1, 2)
    assert cand.supported_end == date(2026, 9, 25)


def test_supported_window_falls_back_to_trials_when_sealed_window_missing(tmp_path: Path) -> None:
    scope = tmp_path / "bar"
    scope.mkdir(parents=True)
    trials = scope / "trials"
    trials.mkdir()
    (trials / "c09-bar-001-g1.json").write_text(json.dumps({
        "entry_session": "2024-06-01", "session": "2024-06-01",
    }))
    (trials / "c09-bar-002-g1.json").write_text(json.dumps({
        "entry_session": "2024-08-15",
    }))
    _write(scope, {
        "family_verdict": "PASS",
        "frozen_inputs": {},
        "round": {},
    })
    cand = build_candidate(scope)
    from datetime import date
    assert cand.supported_start == date(2024, 6, 1)
    assert cand.supported_end == date(2024, 8, 15)
    # No window-discoverability warning when trials carry dates.
    assert "research.window_unparsed" not in cand.warnings


def test_adapter_tolerates_unknown_disposition_string(tmp_path: Path) -> None:
    """Forward-compat: a future sealed-round may carry a disposition the
    adapter doesn't know yet — fall back to DATA-GATED-NOT-RUN rather
    than crash."""
    scope = tmp_path / "future"
    _write(scope, {
        "family_verdict": "BRAND-NEW-VERDICT",
        "frozen_inputs": {},
        "round": {},
    })
    cand = build_candidate(scope)
    assert cand.disposition is ResearchDisposition.DATA_GATED_NOT_RUN
    assert cand.plot_funded_account is False


def test_structured_family_verdict_block_reduces_to_its_verdict_key(tmp_path: Path) -> None:
    """term-gate shape: ``family_verdict`` is a structured block whose
    ``verdict`` key carries the machine-readable family verdict."""
    scope = tmp_path / "term-gate"
    _write(scope, {
        "family_verdict": {
            "candidates": [],
            "registered_criterion": "family beat-or-withdraw",
            "verdict": "WITHDRAW",
            "verdict_reason": "no CANDIDATE on either rule",
        },
        "frozen_inputs": {"round1_selection_sha256": "abcdef1234567890"},
    })
    cand = build_candidate(scope)
    assert cand.disposition is ResearchDisposition.WITHDRAWN
    assert cand.plot_funded_account is False
    assert cand.ineligibility_reason is not None
    assert "WITHDRAW" in cand.ineligibility_reason


def test_non_string_verdict_never_crashes_the_adapter(tmp_path: Path) -> None:
    """A structured verdict block WITHOUT a ``verdict`` key (or a
    non-string where a string was expected) degrades to DATA-GATED —
    never AttributeError."""
    scope = tmp_path / "odd-scope"
    _write(scope, {
        "family_verdict": {"candidates": [], "registered_criterion": 7},
        "frozen_inputs": {},
    })
    cand = build_candidate(scope)
    assert cand.disposition is ResearchDisposition.DATA_GATED_NOT_RUN
    assert cand.plot_funded_account is False


def test_sealed_window_list_shape_parses(tmp_path: Path) -> None:
    """term-gate shape: ``sealed_window`` is a stats block whose
    ``window`` key is a 2-list of ISO dates; an unparseable explicit
    field must fall through, not return (None, None) early."""
    scope = tmp_path / "term-gate"
    _write(scope, {
        "family_verdict": {"verdict": "WITHDRAW"},
        "sealed_window": {"n_sessions": 479, "window": ["2024-10-01", "2026-08-28"]},
    })
    cand = build_candidate(scope)
    assert cand.disposition is ResearchDisposition.WITHDRAWN
    assert cand.supported_start == date(2024, 10, 1)
    assert cand.supported_end == date(2026, 8, 28)


def test_supersession_record_emits_warning(tmp_path: Path) -> None:
    scope = tmp_path / "jepa-filter"
    _write(scope, {
        "family_verdict": "FAIL",
        "frozen_inputs": {},
        "round": {},
        "panel_block_history": ["9a81a2b panel drift repaired"],
    })
    cand = build_candidate(scope)
    assert "research.supersession_recorded" in cand.warnings
