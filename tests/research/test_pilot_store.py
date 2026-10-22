"""Wave 4 lane I: the pilot registrations store and the three repo drafts.

Contract: docs/product/WAVE3-CONTRACTS.md C3.5 (``trex-pilot-registration/1``)
and C3.7 (``registrations/<program_id>.draft.json`` / ``.sealed.json`` under
``TREX_PILOT_DIR``). Oracles here are the frozen contract models, the file
system and sha256 of file bytes, never the store's own helpers.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tree_options.research.pilot.store import PilotError, PilotStore
from tree_options.trex.contracts import wave3 as w

REPO = Path(__file__).resolve().parents[2]
DRAFTS = REPO / "data" / "research" / "pilot"
OWNER = "owner@example.com"
T0 = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
BASELINE = "eq-baseline-spy-weekly"
CHALLENGER = "eq-challenger-spy-reversal"
OBSERVATION = "qsl-observation"


def _doc(program_id: str) -> dict:
    return json.loads((DRAFTS / f"{program_id}.draft.json").read_text())


def _reg(program_id: str) -> w.PilotRegistration:
    return w.PilotRegistration.model_validate(_doc(program_id))


# -- the repo drafts ---------------------------------------------------------------


def test_repo_holds_three_unsealed_drafts_one_per_role() -> None:
    paths = sorted(DRAFTS.glob("*.json"))
    rows = [w.PilotRegistration.model_validate(json.loads(p.read_text())) for p in paths]
    assert len(rows) == 3
    assert [p.name for p in paths] == [f"{r.program_id}.draft.json" for r in rows]
    assert {r.status for r in rows} == {"draft"}
    assert all(r.sealed_by is None and r.sealed_at is None and r.sha256 is None for r in rows)
    assert sorted(r.role for r in rows) == sorted(w.PILOT_ROLES)
    assert w.pilot_roster_problems(rows) == ()


def test_observation_cites_the_existing_qsl_registration_by_its_bytes() -> None:
    observation = _reg(OBSERVATION)
    assert observation.role == "observation"
    ref = observation.existing_registration
    assert ref is not None
    cited = REPO / ref.path
    assert hashlib.sha256(cited.read_bytes()).hexdigest() == ref.sha256
    assert "QSL-20260930" in cited.read_text()
    # the existing lane keeps its own budgets: the pilot grants it nothing new
    assert observation.budgets.model_dump() == {
        "requests_per_day": 0,
        "compute_minutes_per_day": 0,
        "storage_mb": 0,
    }


def test_challenger_is_preregistered_against_the_repo_baseline() -> None:
    baseline, challenger = _reg(BASELINE), _reg(CHALLENGER)
    assert baseline.role == "baseline" and challenger.role == "challenger"
    assert [(c.program_id, c.relation) for c in challenger.controls] == [(BASELINE, "baseline")]
    assert baseline.universe == challenger.universe
    assert baseline.universe.asset_kind == "equity"
    assert baseline.decision_rule.metric == challenger.decision_rule.metric


def test_every_cited_repo_file_carries_its_true_hash_in_the_provenance() -> None:
    for program_id in (BASELINE, CHALLENGER):
        for source in _reg(program_id).data_sources:
            if source.provider != "tree_options repository":
                continue
            digest = hashlib.sha256((REPO / source.dataset).read_bytes()).hexdigest()
            assert digest in source.provenance, (program_id, source.name)


def test_unverified_feeds_are_not_claimed_realtime() -> None:
    for program_id in (OBSERVATION, BASELINE, CHALLENGER):
        for source in _reg(program_id).data_sources:
            if "not verified" in source.provenance or "delayed" in source.provider.lower():
                assert source.realtime is False, (program_id, source.name)


# -- the store ---------------------------------------------------------------------


def test_write_draft_then_read_it_back(tmp_path: Path) -> None:
    store = PilotStore(tmp_path)
    path = store.write_draft(_reg(BASELINE))
    assert path == tmp_path / "registrations" / f"{BASELINE}.draft.json"
    state = store.state(BASELINE)
    assert (state.state, state.reason) == ("draft", None)
    assert state.registration == _reg(BASELINE)
    assert w.PilotRegistration.model_validate_json(path.read_bytes()) == _reg(BASELINE)


def test_a_draft_may_be_rewritten_until_sealed(tmp_path: Path) -> None:
    store = PilotStore(tmp_path)
    store.write_draft(_reg(BASELINE))
    edited = w.PilotRegistration.model_validate({**_doc(BASELINE), "title": "Edited title"})
    store.write_draft(edited)
    assert store.state(BASELINE).registration == edited


def test_missing_registration(tmp_path: Path) -> None:
    state = PilotStore(tmp_path).state(BASELINE)
    assert (state.state, state.reason, state.registration) == (
        "missing",
        "registration_missing",
        None,
    )


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        json.dumps({**_doc(CHALLENGER)}).encode(),  # another program under this name
        json.dumps({**_doc(BASELINE), "budgets": {"requests_per_day": 9_999}}).encode(),
    ],
    ids=["garbage", "foreign-program", "invalid"],
)
def test_an_unreadable_registration_is_unavailable_never_a_draft(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "registrations" / f"{BASELINE}.draft.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    state = PilotStore(tmp_path).state(BASELINE)
    assert (state.state, state.reason, state.registration) == (
        "unavailable",
        "registration_unreadable",
        None,
    )


def test_a_sealed_document_in_the_draft_file_is_unreadable(tmp_path: Path) -> None:
    sealed = w.seal_registration(_reg(OBSERVATION), sealed_by=OWNER, owners={OWNER}, at=T0)
    path = tmp_path / "registrations" / f"{OBSERVATION}.draft.json"
    path.parent.mkdir(parents=True)
    path.write_text(sealed.model_dump_json(by_alias=True))
    assert PilotStore(tmp_path).state(OBSERVATION).reason == "registration_unreadable"


def test_a_symlinked_registration_is_unreadable(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps(_doc(BASELINE)))
    path = tmp_path / "registrations" / f"{BASELINE}.draft.json"
    path.parent.mkdir(parents=True)
    path.symlink_to(real)
    assert PilotStore(tmp_path).state(BASELINE).reason == "registration_unreadable"


def test_write_draft_refuses_a_sealed_document(tmp_path: Path) -> None:
    sealed = w.seal_registration(_reg(OBSERVATION), sealed_by=OWNER, owners={OWNER}, at=T0)
    with pytest.raises(PilotError) as refusal:
        PilotStore(tmp_path).write_draft(sealed)
    assert refusal.value.code == "registration_not_draft"
    assert not (tmp_path / "registrations").exists()


def test_import_drafts_from_the_repo(tmp_path: Path) -> None:
    store = PilotStore(tmp_path)
    assert store.import_drafts(DRAFTS) == (BASELINE, CHALLENGER, OBSERVATION)
    assert store.program_ids() == (BASELINE, CHALLENGER, OBSERVATION)
    for program_id in store.program_ids():
        assert store.state(program_id).registration == _reg(program_id)


def test_program_ids_of_an_empty_store(tmp_path: Path) -> None:
    assert PilotStore(tmp_path / "absent").program_ids() == ()


def test_program_ids_ignore_foreign_files(tmp_path: Path) -> None:
    store = PilotStore(tmp_path)
    store.write_draft(_reg(BASELINE))
    (tmp_path / "registrations" / "notes.txt").write_text("x")
    (tmp_path / "registrations" / ".tmp-abc.json").write_text("x")
    assert store.program_ids() == (BASELINE,)
