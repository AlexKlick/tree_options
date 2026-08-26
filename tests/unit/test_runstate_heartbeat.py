"""Heartbeat classification matrix: UNKNOWN, never FAILED."""

from __future__ import annotations

from pathlib import Path

from tree_options.runstate import heartbeat as H
from tree_options.runstate.states import RunState

BOOT = "11111111-2222-3333-4444-555555555555"
OTHER_BOOT = "99999999-8888-7777-6666-555555555555"
NOW = 1_800_000_000


def _fake_proc(root: Path, pids: dict[int, int]) -> Path:
    proc = root / "proc"
    proc.mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").parent.mkdir(parents=True, exist_ok=True)
    (proc / "sys/kernel/random/boot_id").write_text(BOOT + "\n")
    for pid, ticks in pids.items():
        pdir = proc / str(pid)
        pdir.mkdir(parents=True)
        tail_fields = [str(1)] * 40
        tail_fields[19] = str(ticks)
        (pdir / "stat").write_text(f"{pid} (fakecomm) " + " ".join(tail_fields) + "\n")
    return proc


def _beat(
    *,
    state: RunState = RunState.CAPTURING,
    pid: int = 42,
    ticks: int = 100,
    boot: str = BOOT,
    at_epoch: int = NOW,
) -> H.Heartbeat:
    return H.Heartbeat(state=state, pid=pid, pid_start_ticks=ticks, boot_id=boot, at_epoch=at_epoch)


def test_fresh_beat_process_state_alive(tmp_path):
    proc = _fake_proc(tmp_path, {42: 100})
    assert (
        H.classify(_beat(), RunState.CAPTURING, now_epoch=NOW, boot_id_now=BOOT, proc_root=proc)
        is H.HeartbeatClass.ALIVE
    )


def test_silent_but_alive_is_alive_silent(tmp_path):
    proc = _fake_proc(tmp_path, {42: 100})
    result = H.classify(
        _beat(at_epoch=NOW - 10_000),
        RunState.CAPTURING,
        now_epoch=NOW,
        boot_id_now=BOOT,
        proc_root=proc,
    )
    assert result is H.HeartbeatClass.ALIVE_SILENT


def test_stale_threshold_boundary(tmp_path):
    proc = _fake_proc(tmp_path, {42: 100})
    fresh = H.classify(
        _beat(at_epoch=NOW - H.STALE_AFTER_S),
        RunState.CAPTURING,
        now_epoch=NOW,
        boot_id_now=BOOT,
        proc_root=proc,
    )
    assert fresh is H.HeartbeatClass.ALIVE
    stale = H.classify(
        _beat(at_epoch=NOW - H.STALE_AFTER_S - 1),
        RunState.CAPTURING,
        now_epoch=NOW,
        boot_id_now=BOOT,
        proc_root=proc,
    )
    # pid still alive -> SILENT (watch), not UNKNOWN
    assert stale is H.HeartbeatClass.ALIVE_SILENT


def test_dead_process_in_resumable_state_is_unknown_resumable(tmp_path):
    proc = _fake_proc(tmp_path, {})  # pid gone
    for state in (RunState.CAPTURING, RunState.INSPECTION_RUNNING, RunState.BARS_CAPTURING):
        result = H.classify(
            _beat(state=state),
            state,
            now_epoch=NOW,
            boot_id_now=BOOT,
            proc_root=proc,
        )
        assert result is H.HeartbeatClass.UNKNOWN_RESUMABLE, state


def test_dead_sealed_running_requires_reconciliation(tmp_path):
    proc = _fake_proc(tmp_path, {})
    result = H.classify(
        _beat(state=RunState.SEALED_RUNNING),
        RunState.SEALED_RUNNING,
        now_epoch=NOW,
        boot_id_now=BOOT,
        proc_root=proc,
    )
    assert result is H.HeartbeatClass.UNKNOWN_RECONCILIATION_REQUIRED


def test_boot_change_in_sealed_running_requires_reconciliation(tmp_path):
    proc = _fake_proc(tmp_path, {42: 100})
    result = H.classify(
        _beat(state=RunState.SEALED_RUNNING),
        RunState.SEALED_RUNNING,
        now_epoch=NOW,
        boot_id_now=OTHER_BOOT,
        proc_root=proc,
    )
    assert result is H.HeartbeatClass.UNKNOWN_RECONCILIATION_REQUIRED


def test_missing_heartbeat_in_process_state_is_unknown_never_failed(tmp_path):
    proc = _fake_proc(tmp_path, {})
    result = H.classify(None, RunState.CAPTURING, now_epoch=NOW, boot_id_now=BOOT, proc_root=proc)
    assert result is H.HeartbeatClass.UNKNOWN_RESUMABLE
    sealed = H.classify(
        None, RunState.SEALED_RUNNING, now_epoch=NOW, boot_id_now=BOOT, proc_root=proc
    )
    assert sealed is H.HeartbeatClass.UNKNOWN_RECONCILIATION_REQUIRED


def test_pid_reuse_is_owner_gone(tmp_path):
    proc = _fake_proc(tmp_path, {42: 555})  # same pid, different process
    result = H.classify(
        _beat(),
        RunState.CAPTURING,
        now_epoch=NOW,
        boot_id_now=BOOT,
        proc_root=proc,
    )
    assert result is H.HeartbeatClass.UNKNOWN_RESUMABLE


def test_no_process_expected_states_are_alive_without_beat(tmp_path):
    proc = _fake_proc(tmp_path, {})
    for state in (
        RunState.PLANNED,
        RunState.CAPTURE_COMPLETE,
        RunState.INSPECTED,
        RunState.AMENDMENT_PENDING_OWNER,
        RunState.AMENDMENT_READY,
        RunState.BARS_READY,
        RunState.BARS_COMPLETE,
        RunState.SEALED_PREFLIGHT_READY,
    ):
        result = H.classify(None, state, now_epoch=NOW, boot_id_now=BOOT, proc_root=proc)
        assert result is H.HeartbeatClass.ALIVE, state


def test_terminal_state_dead_process_is_dead_terminal(tmp_path):
    proc = _fake_proc(tmp_path, {})
    result = H.classify(
        _beat(state=RunState.SEALED_COMPLETE),
        RunState.SEALED_COMPLETE,
        now_epoch=NOW,
        boot_id_now=BOOT,
        proc_root=proc,
    )
    assert result is H.HeartbeatClass.DEAD_TERMINAL


def test_none_state_is_unknown(tmp_path):
    proc = _fake_proc(tmp_path, {})
    result = H.classify(None, None, now_epoch=NOW, boot_id_now=BOOT, proc_root=proc)
    assert result is H.HeartbeatClass.UNKNOWN_RESUMABLE


def test_write_and_read_roundtrip_atomic(tmp_path):
    store = tmp_path / "run"
    store.mkdir()
    H.write(store, _beat())
    loaded = H.read(store)
    assert loaded == _beat()
    assert not list(store.glob(".heartbeat.json.*.tmp"))


def test_torn_heartbeat_reads_as_absent(tmp_path):
    store = tmp_path / "run"
    store.mkdir()
    (store / H.HEARTBEAT_FILENAME).write_text("{ nope")
    assert H.read(store) is None
