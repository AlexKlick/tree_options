"""Wave 4 lane I: sealing a pilot registration is an owner act (C3.5).

The seal writes ``registrations/<program_id>.sealed.json`` once (O_EXCL),
only for an owner, only for the exact body the owner reviewed, and only once
every control the program names is sealed. The hash oracle is computed here
from the draft file's JSON (canonical: sorted keys, compact, UTF-8) minus
the four seal fields, independently of ``registration_body_sha256``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tree_options.research.pilot.seal import apply_seal_command
from tree_options.research.pilot.store import PilotError, PilotStore
from tree_options.trex.contracts import wave3 as w
from tree_options.trex.contracts.commands import Actor

REPO = Path(__file__).resolve().parents[2]
DRAFTS = REPO / "data" / "research" / "pilot"
OWNER = "owner@example.com"
OWNERS = frozenset({OWNER})
T0 = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
BASELINE = "eq-baseline-spy-weekly"
CHALLENGER = "eq-challenger-spy-reversal"
OBSERVATION = "qsl-observation"
SEAL_FIELDS = ("status", "sealed_by", "sealed_at", "sha256")


def reviewed_sha(program_id: str) -> str:
    document = json.loads((DRAFTS / f"{program_id}.draft.json").read_text())
    for name in SEAL_FIELDS:
        document.pop(name)
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.fixture()
def store(tmp_path: Path) -> PilotStore:
    store = PilotStore(tmp_path)
    store.import_drafts(DRAFTS)
    return store


def sealed_path(store: PilotStore, program_id: str) -> Path:
    return store.root / "registrations" / f"{program_id}.sealed.json"


def actor(sub: str, ingress: str = "family") -> Actor:
    return Actor(sub=sub, ingress=ingress, request_id="req-1", request_sha256="a" * 64)  # type: ignore[arg-type]


# -- the mechanical seal -----------------------------------------------------------


def test_seal_writes_the_reviewed_body_once(store: PilotStore) -> None:
    sha = reviewed_sha(OBSERVATION)
    sealed = store.seal(OBSERVATION, reviewed_sha256=sha, sealed_by=OWNER, owners=OWNERS, at=T0)
    path = sealed_path(store, OBSERVATION)
    on_disk = json.loads(path.read_text())
    assert on_disk["status"] == "sealed"
    assert on_disk["sealed_by"] == OWNER
    assert on_disk["sealed_at"] == "2026-10-22T14:00:00Z"
    assert on_disk["sha256"] == sha
    assert sealed.sha256 == sha
    state = store.state(OBSERVATION)
    assert (state.state, state.registration) == ("sealed", sealed)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("owners", [frozenset({"other@example.com"}), frozenset()])
def test_seal_requires_an_owner(store: PilotStore, owners: frozenset[str]) -> None:
    with pytest.raises(PilotError) as refusal:
        store.seal(
            OBSERVATION,
            reviewed_sha256=reviewed_sha(OBSERVATION),
            sealed_by=OWNER,
            owners=owners,
            at=T0,
        )
    assert refusal.value.code == "sealer_not_owner"
    assert not sealed_path(store, OBSERVATION).exists()
    assert store.state(OBSERVATION).state == "draft"


def test_seal_refuses_a_body_the_owner_did_not_review(store: PilotStore) -> None:
    with pytest.raises(PilotError) as refusal:
        store.seal(OBSERVATION, reviewed_sha256="0" * 64, sealed_by=OWNER, owners=OWNERS, at=T0)
    assert refusal.value.code == "registration_sha256_mismatch"
    assert not sealed_path(store, OBSERVATION).exists()


def test_a_draft_edited_after_review_cannot_be_sealed_with_the_old_hash(
    store: PilotStore,
) -> None:
    sha = reviewed_sha(BASELINE)
    edited = json.loads((DRAFTS / f"{BASELINE}.draft.json").read_text())
    edited["budgets"]["requests_per_day"] = 21
    store.write_draft(w.PilotRegistration.model_validate(edited))
    with pytest.raises(PilotError) as refusal:
        store.seal(BASELINE, reviewed_sha256=sha, sealed_by=OWNER, owners=OWNERS, at=T0)
    assert refusal.value.code == "registration_sha256_mismatch"


def test_a_sealed_registration_is_never_sealed_or_drafted_again(store: PilotStore) -> None:
    sha = reviewed_sha(OBSERVATION)
    store.seal(OBSERVATION, reviewed_sha256=sha, sealed_by=OWNER, owners=OWNERS, at=T0)
    before = sealed_path(store, OBSERVATION).read_bytes()
    later = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)
    with pytest.raises(PilotError) as again:
        store.seal(OBSERVATION, reviewed_sha256=sha, sealed_by=OWNER, owners=OWNERS, at=later)
    assert again.value.code == "registration_already_sealed"
    draft = w.PilotRegistration.model_validate(
        json.loads((DRAFTS / f"{OBSERVATION}.draft.json").read_text())
    )
    with pytest.raises(PilotError) as redraft:
        store.write_draft(draft)
    assert redraft.value.code == "registration_already_sealed"
    assert sealed_path(store, OBSERVATION).read_bytes() == before


def test_an_existing_sealed_file_is_never_overwritten(store: PilotStore) -> None:
    path = sealed_path(store, OBSERVATION)
    path.write_bytes(b"left by someone else")
    with pytest.raises(PilotError) as refusal:
        store.seal(
            OBSERVATION,
            reviewed_sha256=reviewed_sha(OBSERVATION),
            sealed_by=OWNER,
            owners=OWNERS,
            at=T0,
        )
    assert refusal.value.code == "registration_already_sealed"
    assert path.read_bytes() == b"left by someone else"


def test_controls_are_sealed_before_the_program_that_names_them(store: PilotStore) -> None:
    with pytest.raises(PilotError) as refusal:
        store.seal(
            CHALLENGER,
            reviewed_sha256=reviewed_sha(CHALLENGER),
            sealed_by=OWNER,
            owners=OWNERS,
            at=T0,
        )
    assert refusal.value.code == f"control_not_sealed:{BASELINE}"
    assert not sealed_path(store, CHALLENGER).exists()
    store.seal(
        BASELINE, reviewed_sha256=reviewed_sha(BASELINE), sealed_by=OWNER, owners=OWNERS, at=T0
    )
    store.seal(
        CHALLENGER, reviewed_sha256=reviewed_sha(CHALLENGER), sealed_by=OWNER, owners=OWNERS, at=T0
    )
    assert store.state(CHALLENGER).state == "sealed"


def test_seal_of_an_unknown_program(tmp_path: Path) -> None:
    with pytest.raises(PilotError) as refusal:
        PilotStore(tmp_path).seal(
            BASELINE, reviewed_sha256="a" * 64, sealed_by=OWNER, owners=OWNERS, at=T0
        )
    assert refusal.value.code == "registration_missing"


# -- the command path (seal_registration args + the verified actor) ----------------


def test_seal_command_from_the_owner_is_applied(store: PilotStore) -> None:
    sha = reviewed_sha(OBSERVATION)
    args = w.SealRegistrationArgs(program_id=OBSERVATION, registration_sha256=sha)
    outcome = apply_seal_command(args, actor(OWNER), T0, root=store.root, owners=OWNERS)
    assert (outcome.status, outcome.reason) == ("applied", None)
    assert outcome.facts == {
        "program_id": OBSERVATION,
        "registration_sha256": sha,
        "sealed_by": OWNER,
        "sealed_at": "2026-10-22T14:00:00Z",
        "path": f"registrations/{OBSERVATION}.sealed.json",
    }
    assert json.loads(sealed_path(store, OBSERVATION).read_text())["sealed_by"] == OWNER


@pytest.mark.parametrize(
    ("sub", "ingress"),
    [("loopback-operator", "loopback"), ("someone@example.com", "family")],
    ids=["loopback-label", "family-non-owner"],
)
def test_seal_command_requires_an_owner_actor(store: PilotStore, sub: str, ingress: str) -> None:
    args = w.SealRegistrationArgs(
        program_id=OBSERVATION, registration_sha256=reviewed_sha(OBSERVATION)
    )
    outcome = apply_seal_command(args, actor(sub, ingress), T0, root=store.root, owners=OWNERS)
    assert (outcome.status, outcome.reason) == ("refused", "sealer_not_owner")
    assert not sealed_path(store, OBSERVATION).exists()


def test_seal_command_refusals_carry_the_store_reason(store: PilotStore) -> None:
    args = w.SealRegistrationArgs(
        program_id=CHALLENGER, registration_sha256=reviewed_sha(CHALLENGER)
    )
    outcome = apply_seal_command(args, actor(OWNER), T0, root=store.root, owners=OWNERS)
    assert (outcome.status, outcome.reason) == ("refused", f"control_not_sealed:{BASELINE}")
    wrong = w.SealRegistrationArgs(program_id=BASELINE, registration_sha256="b" * 64)
    outcome = apply_seal_command(wrong, actor(OWNER), T0, root=store.root, owners=OWNERS)
    assert (outcome.status, outcome.reason) == ("refused", "registration_sha256_mismatch")


def test_seal_command_reads_owners_and_pilot_dir_from_the_environment(
    store: PilotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TREX_PILOT_DIR", str(store.root))
    monkeypatch.setenv("TREX_OWNER_EMAILS", f" {OWNER.upper()} ,other@example.com")
    args = w.SealRegistrationArgs(program_id=BASELINE, registration_sha256=reviewed_sha(BASELINE))
    outcome = apply_seal_command(args, actor(OWNER), T0)
    assert outcome.status == "applied"
    monkeypatch.setenv("TREX_OWNER_EMAILS", "")
    args = w.SealRegistrationArgs(
        program_id=OBSERVATION, registration_sha256=reviewed_sha(OBSERVATION)
    )
    assert apply_seal_command(args, actor(OWNER), T0).reason == "sealer_not_owner"
