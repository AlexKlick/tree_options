"""Store: create/open/transition/status, manifest pin + resume refusal."""

from __future__ import annotations

import contextlib
import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from tree_options.runstate import (
    RunIdentity,
    RunState,
    RunStore,
    StoreExistsError,
    UnknownRunError,
    compute_run_id,
)
from tree_options.runstate.errors import (
    IllegalTransitionError,
    JournalCorruptError,
    ManifestMismatchError,
    RunIdRefusedError,
    StoreIdMismatchError,
)

BOOT = "11111111-2222-3333-4444-555555555555"
T0 = 1_800_000_000

# Round-1 review migration (2026-08-23): pytest's tmp_path lives under
# /tmp on this host; the runstate root refusal (RunStore.create/open)
# makes that path unusable for run-state stores. The scratch root
# therefore lives under the repo's gitignored artifacts/runstate-tests/,
# cleaned up at session end. Each per-test root is a unique subdir.
RUNSTATE_TESTS_ROOT = Path(__file__).resolve().parents[2] / "artifacts" / "runstate-tests"


@pytest.fixture()
def root() -> Path:
    RUNSTATE_TESTS_ROOT.mkdir(parents=True, exist_ok=True)
    scratch = RUNSTATE_TESTS_ROOT / f"test-{uuid4().hex}"
    scratch.mkdir(parents=True)
    return scratch


@pytest.fixture()
def store(root: Path) -> RunStore:
    return RunStore.create(root, _identity(root), now_epoch=T0)


def _identity(
    root: Path,
    *,
    run_id: str | None = None,
    run_nonce: str | None = None,
) -> RunIdentity:
    del root  # retained in the helper signature for existing call sites
    canonical = compute_run_id(
        campaign="m4-test-run",
        protocol_hash="a" * 64,
        code_sha="b" * 40,
        provider="massive-polygon-free/1",
        capture_version="m4b-capture/1",
        universe_manifest_sha256="c" * 64,
        args_hash="d" * 64,
        started_epoch=T0,
        run_nonce=run_nonce,
    )
    return RunIdentity(
        run_id=run_id or canonical,
        campaign="m4-test-run",
        protocol_hash="a" * 64,
        code_sha="b" * 40,
        provider="massive-polygon-free/1",
        capture_version="m4b-capture/1",
        universe_manifest_sha256="c" * 64,
        boot_id=BOOT,
        pid=123,
        pid_start_ticks=99,
        started_epoch=T0,
        args_hash="d" * 64,
        run_nonce=run_nonce,
    )


def _mark(store: RunStore, to: RunState, *, epoch: int, reason: str = "step") -> None:
    store.transition(to, reason=reason, now_epoch=epoch, actor_pid=123, actor_boot_id=BOOT)


def test_create_writes_identity_genesis_and_projection(root):
    store = RunStore.create(root, _identity(root), now_epoch=T0)
    assert store.state is RunState.PLANNED
    run_json = json.loads((root / store.identity.run_id / "run.json").read_text())
    assert run_json["campaign"] == "m4-test-run"
    projection = json.loads((root / store.identity.run_id / "current.json").read_text())
    assert projection["state"] == "PLANNED"


def test_create_refuses_over_existing_store(root):
    RunStore.create(root, _identity(root), now_epoch=T0)
    with pytest.raises(StoreExistsError):
        RunStore.create(root, _identity(root), now_epoch=T0 + 5)


def test_open_missing_run_refused(root):
    with pytest.raises(UnknownRunError):
        RunStore.open(root, "no-such-run")


def test_open_refuses_store_dir_named_like_another_run(root):
    """Round-3 review fix (2026-08-23, finding 3): open() binds the REQUESTED
    run id to the store's embedded identity.

    A valid run-A store placed under directory run-B used to open cleanly —
    the bars-era joins then used the EMBEDDED identity while runner output
    used the requested id (one execution, two identities). The directory
    name and identity.run_id are one fact; a mismatch is misfiled evidence."""
    store = RunStore.create(root, _identity(root), now_epoch=T0)
    # an ordinary second store in the same root keeps opening (regression)
    other = _identity(root, run_nonce="other-logical-run")
    RunStore.create(root, other, now_epoch=T0)
    # the attack: run-A's store directory renamed to run-B
    misfiled = "m4-misfiled-run-20260823-beefcafe"
    (root / store.identity.run_id).rename(root / misfiled)
    with pytest.raises(StoreIdMismatchError) as excinfo:
        RunStore.open(root, misfiled)
    assert excinfo.value.code == "STORE_ID_MISMATCH"
    # the store whose directory MATCHES its identity still opens
    reopened = RunStore.open(root, other.run_id)
    assert reopened.identity.run_id == other.run_id
    assert reopened.state is RunState.PLANNED


def test_full_happy_path_transitions(root):
    store = RunStore.create(root, _identity(root), now_epoch=T0)
    for i, state in enumerate(
        (
            RunState.CAPTURING,
            RunState.CAPTURE_COMPLETE,
            RunState.INSPECTION_RUNNING,
            RunState.INSPECTED,
            RunState.AMENDMENT_PENDING_OWNER,
        ),
        start=1,
    ):
        _mark(store, state, epoch=T0 + i)
    assert store.state is RunState.AMENDMENT_PENDING_OWNER


def test_illegal_transition_refused_and_leaves_no_trace(root, store):
    _mark(store, RunState.CAPTURING, epoch=T0 + 1)
    with pytest.raises(IllegalTransitionError):
        _mark(store, RunState.BARS_CAPTURING, epoch=T0 + 2)
    assert store.state is RunState.CAPTURING
    view_records = (store.dir / "journal.jsonl").read_text().count("\n")
    assert view_records == 2  # GENESIS + CAPTURING only


def test_transition_to_unknown_refused(root, store):
    _mark(store, RunState.CAPTURING, epoch=T0 + 1)
    with pytest.raises(IllegalTransitionError):
        _mark(store, RunState.UNKNOWN, epoch=T0 + 2)


def test_reopen_replays_to_same_state(root, store):
    _mark(store, RunState.CAPTURING, epoch=T0 + 1)
    _mark(store, RunState.CAPTURE_COMPLETE, epoch=T0 + 2)
    reopened = RunStore.open(root, store.identity.run_id)
    assert reopened.state is RunState.CAPTURE_COMPLETE
    assert reopened.status(now_epoch=T0 + 3, boot_id_now=BOOT).seq == 3


def test_manifest_pin_and_validate_resume(root, store):
    _mark(store, RunState.CAPTURING, epoch=T0 + 1)
    store.pin_manifest("e" * 64, now_epoch=T0 + 2, actor_pid=123, actor_boot_id=BOOT)
    _mark(store, RunState.CAPTURE_COMPLETE, epoch=T0 + 3)
    store.validate_resume("e" * 64)
    with pytest.raises(ManifestMismatchError):
        store.validate_resume("f" * 64)


def test_validate_resume_without_pin_refused(root, store):
    with pytest.raises(ManifestMismatchError):
        store.validate_resume("e" * 64)


def test_status_reports_failure_reason(root, store):
    _mark(store, RunState.CAPTURING, epoch=T0 + 1)
    _mark(store, RunState.FAILED, epoch=T0 + 2, reason="budget exhausted")
    status = store.status(now_epoch=T0 + 3, boot_id_now=BOOT)
    assert status.state is RunState.FAILED
    assert status.failure_reason == "budget exhausted"


def test_run_id_deterministic_in_inputs():
    kwargs = dict(
        campaign="m4-coverage-era",
        protocol_hash="a" * 64,
        code_sha="b" * 40,
        provider="massive-polygon-free/1",
        capture_version="m4b-capture/1",
        universe_manifest_sha256="c" * 64,
        args_hash="d" * 64,
        started_epoch=T0,
        run_nonce=None,
    )
    first = compute_run_id(**kwargs)
    assert first == compute_run_id(**kwargs)
    assert first != compute_run_id(**{**kwargs, "code_sha": "z" * 40})
    assert first.startswith("m4-coverage-era-")


# ---- external PR #13 audit: canonical logical run identity --------------------------


def test_create_refuses_noncanonical_run_id_before_filesystem_mutation(root: Path) -> None:
    """An operator-supplied id cannot split one logical run across stores."""
    before = tuple(root.iterdir())
    identity = _identity(root, run_id="m4-test-run-20260823-deadbeef")

    with pytest.raises(RunIdRefusedError, match="canonical"):
        RunStore.create(root, identity, now_epoch=T0)

    assert tuple(root.iterdir()) == before


def test_run_id_core_names_provider_capture_version_and_owner_nonce() -> None:
    base = dict(
        campaign="m4-coverage-era",
        protocol_hash="a" * 64,
        code_sha="b" * 40,
        provider="massive-polygon-free/1",
        capture_version="m4b-capture/1",
        universe_manifest_sha256="c" * 64,
        args_hash="d" * 64,
        started_epoch=T0,
        run_nonce=None,
    )
    canonical = compute_run_id(**base)

    assert canonical != compute_run_id(**{**base, "provider": "cboe-datashop/1"})
    assert canonical != compute_run_id(**{**base, "capture_version": "m4b-capture/2"})
    assert canonical != compute_run_id(**{**base, "run_nonce": "owner-approved-rerun-2"})


def test_reboot_and_pid_change_do_not_change_canonical_run_id() -> None:
    kwargs = dict(
        campaign="m4-coverage-era",
        protocol_hash="a" * 64,
        code_sha="b" * 40,
        provider="massive-polygon-free/1",
        capture_version="m4b-capture/1",
        universe_manifest_sha256="c" * 64,
        args_hash="d" * 64,
        started_epoch=T0,
        run_nonce=None,
    )
    first_boot = compute_run_id(**kwargs)
    # boot id, pid, and pid-start ticks are deliberately not accepted by the
    # logical-id function: a reboot resumes the same store.
    second_boot = compute_run_id(**kwargs)
    assert first_boot == second_boot


def test_open_revalidates_canonical_id_against_stored_core(root: Path) -> None:
    store = RunStore.create(root, _identity(root), now_epoch=T0)
    run_path = store.dir / "run.json"
    stored = json.loads(run_path.read_text(encoding="utf-8"))
    stored["provider"] = "foreign-provider/1"
    run_path.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(RunIdRefusedError, match="canonical"):
        RunStore.open(root, store.identity.run_id)


# ---- round-8 (finding 4): the /tmp refusal must cover the JOURNAL NAME too --------------
#
# Round-8 review fix (2026-08-24): store.py validates the run DIRECTORY
# (root resolution), but journal.py then followed journal.jsonl BY NAME on
# both the read (replay) and the append's plain open(..., "a"). An honest
# durable run dir whose journal.jsonl is a symlink to a copied chain under
# /tmp opened and transitioned cleanly: authority was appended under /tmp
# and a reboot erased a returned-success transition. Both journal opens are
# now O_NOFOLLOW against the store dir held as a REAL directory fd; ELOOP
# is the module's JournalCorruptError (the runstate mirror of the seal/bars
# ledger-name rule — the same RunStateError family, no new class).


def test_journal_name_symlinked_to_a_tmp_chain_refused(root: Path) -> None:
    """Fixture: an honest store under the durable root, its journal.jsonl
    replaced by a symlink to a VALID copied chain under /tmp. Pre-fix open()
    reads the chain through the link and transition() appends the record to
    the /tmp file; post-fix the open itself refuses (JournalCorruptError
    naming the symlink) and nothing is appended under /tmp."""
    store = RunStore.create(root, _identity(root), now_epoch=T0)
    journal = store.dir / "journal.jsonl"
    honest = journal.read_bytes()  # the GENESIS chain, valid
    tmp_dir = Path("/tmp") / f"pr-a-journal-f4-{uuid4().hex}"
    tmp_dir.mkdir()
    tmp_journal = tmp_dir / "journal.jsonl"
    tmp_journal.write_bytes(honest)  # a valid copied chain under /tmp
    journal.unlink()
    journal.symlink_to(tmp_journal)
    try:
        with pytest.raises(JournalCorruptError, match=r"journal\.jsonl") as excinfo:
            reopened = RunStore.open(root, store.identity.run_id)  # pre-fix: reads through the link
            reopened.transition(  # pre-fix: appends authority under /tmp
                RunState.CAPTURING,
                reason="would-be /tmp authority",
                now_epoch=T0 + 1,
                actor_pid=123,
                actor_boot_id=BOOT,
            )
        assert excinfo.value.code == "JOURNAL_CORRUPT"
        assert "symlink" in str(excinfo.value), "the refusal says the journal name is a symlink"
        assert tmp_journal.read_bytes() == honest, "no authority record may be appended under /tmp"
        assert journal.is_symlink(), "the fixture link is untouched by the refusal"
    finally:
        # never leave a symlink under the durable root, never leave /tmp state
        with contextlib.suppress(OSError):
            journal.unlink()
        shutil.rmtree(tmp_dir, ignore_errors=True)
