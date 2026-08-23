"""Store: create/open/transition/status, manifest pin + resume refusal."""

from __future__ import annotations

import json
from pathlib import Path

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
    ManifestMismatchError,
)

BOOT = "11111111-2222-3333-4444-555555555555"
T0 = 1_800_000_000


def _identity(tmp_path: Path, *, run_id: str = "m4-test-run-20260823-abcdef12") -> RunIdentity:
    return RunIdentity(
        run_id=run_id,
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
    )


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "runstate"


@pytest.fixture()
def store(root: Path) -> RunStore:
    return RunStore.create(root, _identity(root), now_epoch=T0)


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
        universe_manifest_sha256="c" * 64,
        args_hash="d" * 64,
        started_epoch=T0,
    )
    first = compute_run_id(**kwargs)
    assert first == compute_run_id(**kwargs)
    assert first != compute_run_id(**{**kwargs, "code_sha": "z" * 40})
    assert first.startswith("m4-coverage-era-")
