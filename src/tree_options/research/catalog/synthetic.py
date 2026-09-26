"""Synthetic fixture candidates for the Research Lab catalog.

RL-1's vertical slice (audit §6 Phase B): one benchmark and two strategy
versions with a COMPLETE funded history — executions, marks and sessions
— proving the comparison machinery end-to-end. The fixture is
sha-pinned, permanently labeled synthetic, and is machinery validation:
its numbers are INVENTED and must never be presented as investment
evidence (the label travels on every candidate, envelope, and wire
payload that touches it).

``funded_history`` is RECONSTRUCTED here — that is the point: the
capability comes from the data actually present (executions + marks +
declared sessions), not from a verdict.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from tree_options.research.contracts import (
    FundedHistorySupport,
    ResearchCandidate,
    ResearchDisposition,
    ResearchEvidenceKind,
    ResearchRegistration,
)

ADAPTER_API_VERSION = 1

_REPO_ROOT = Path(__file__).resolve().parents[4]
_FIXTURE = _REPO_ROOT / "data" / "research" / "fixtures" / "synthetic-v1.json"

_SYNTHETIC_WARNING = "research.synthetic_v1_machinery_validation"


def fixture_path() -> Path:
    return _FIXTURE


def fixture_sha256() -> str:
    return hashlib.sha256(_FIXTURE.read_bytes()).hexdigest()


def load_fixture() -> dict[str, Any] | None:
    """The frozen fixture document, or None when absent (hermetic
    environments that did not ship data/)."""
    if not _FIXTURE.is_file():
        return None
    return json.loads(_FIXTURE.read_text())


def build_synthetic_candidates() -> list[ResearchCandidate]:
    """One ResearchCandidate per fixture series — plot-eligible because
    the fixture IS a complete funded history, synthetic because the
    numbers are invented."""
    doc = load_fixture()
    if doc is None:
        return []
    sessions = [date.fromisoformat(s) for s in doc.get("sessions", [])]
    sha = fixture_sha256()
    out: list[ResearchCandidate] = []
    for cid, spec in sorted(doc.get("candidates", {}).items()):
        label = str(spec.get("label", cid))
        out.append(ResearchCandidate(
            id=cid,
            family=cid,
            version="v1",
            evidence_kind=ResearchEvidenceKind.SYNTHETIC_BACKTEST,
            # A fixture is not a registered study; the synthetic warning
            # keeps that from being misread as study provenance.
            registration=ResearchRegistration.BEFORE_ENTRY_WINDOW_END,
            disposition=ResearchDisposition.PASS,
            plot_funded_account=True,
            supported_start=sessions[0] if sessions else None,
            supported_end=sessions[-1] if sessions else None,
            funded_history=FundedHistorySupport.RECONSTRUCTED,
            funded_history_reason=None,
            artifact_hashes={"synthetic-fixture.json": sha},
            capabilities=(
                "view_published_study",
                "plot_trade_outcomes",
                "plot_funded_account",
            ),
            ineligibility_reason=None,
            data_completeness={"sessions": len(sessions)},
            warnings=(_SYNTHETIC_WARNING,),
            source_url=f"synthetic/{cid}",
        ))
        assert label  # label is informational; ids are the join key
    return out


__all__ = ["build_synthetic_candidates", "fixture_path", "fixture_sha256",
           "load_fixture"]
