"""Lease: duplicate launch, stale adoption, PID reuse, boot change, torn owner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tree_options.runstate import lease as L
from tree_options.runstate.errors import LeaseHeldError

BOOT = "11111111-2222-3333-4444-555555555555"


def _fake_proc(root: Path, pids: dict[int, int]) -> Path:
    """Build a fake /proc: pid -> starttime ticks (field 22)."""
    proc = root / "proc"
    proc.mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").parent.mkdir(parents=True, exist_ok=True)
    (proc / "sys/kernel/random/boot_id").write_text(BOOT + "\n")
    for pid, ticks in pids.items():
        pdir = proc / str(pid)
        pdir.mkdir(parents=True)
        tail_fields = [str(1)] * 40
        tail_fields[19] = str(ticks)  # tail[19] == field 22 (starttime)
        (pdir / "stat").write_text(f"{pid} (fakecomm) " + " ".join(tail_fields) + "\n")
    return proc


def _owner(pid: int, ticks: int, *, boot_id: str = BOOT) -> L.LeaseOwner:
    return L.LeaseOwner(
        pid=pid,
        pid_start_ticks=ticks,
        boot_id=boot_id,
        started_epoch=1_700_000_000,
        argv_hash="0" * 64,
    )


@pytest.fixture()
def store_dir(tmp_path: Path) -> Path:
    d = tmp_path / "run"
    d.mkdir()
    return d


def test_acquire_creates_owner_file(store_dir, tmp_path):
    proc = _fake_proc(tmp_path, {42: 100})
    result = L.acquire(store_dir, _owner(42, 100), boot_id_now=BOOT, proc_root=proc)
    assert result is L.LeaseClassification.HELD
    owner = json.loads((store_dir / "lease/owner.json").read_text())
    assert owner["pid"] == 42


def test_duplicate_launcher_refused_while_owner_alive(store_dir, tmp_path):
    proc = _fake_proc(tmp_path, {42: 100})
    L.acquire(store_dir, _owner(42, 100), boot_id_now=BOOT, proc_root=proc)
    with pytest.raises(LeaseHeldError):
        L.acquire(store_dir, _owner(43, 101), boot_id_now=BOOT, proc_root=proc)


def test_dead_pid_classified_stale_and_adoptable(store_dir, tmp_path):
    proc = _fake_proc(tmp_path, {})  # pid 42 does not exist
    L.acquire(store_dir, _owner(42, 100), boot_id_now=BOOT, proc_root=proc)
    classification = L.classify_existing(store_dir, boot_id_now=BOOT, proc_root=proc)
    assert classification is L.LeaseClassification.STALE_DEAD_PID
    adopted = L.acquire(
        store_dir,
        _owner(77, 700),
        boot_id_now=BOOT,
        proc_root=proc,
        allow_stale_adopt=True,
    )
    assert adopted is L.LeaseClassification.STALE_DEAD_PID
    owner = json.loads((store_dir / "lease/owner.json").read_text())
    assert owner["pid"] == 77


def test_stale_lease_not_adopted_without_flag(store_dir, tmp_path):
    proc = _fake_proc(tmp_path, {})
    L.acquire(store_dir, _owner(42, 100), boot_id_now=BOOT, proc_root=proc)
    with pytest.raises(LeaseHeldError):
        L.acquire(
            store_dir,
            _owner(77, 700),
            boot_id_now=BOOT,
            proc_root=proc,
            allow_stale_adopt=False,
        )


def test_boot_change_classified_stale_even_with_alive_pid(store_dir, tmp_path):
    # The rebooted host may have a process with the same pid number; the
    # boot id mismatch must dominate, or a reused pid authorizes adoption
    # of a lease whose owner died with the previous boot.
    proc = _fake_proc(tmp_path, {42: 100})
    L.acquire(store_dir, _owner(42, 100), boot_id_now=BOOT, proc_root=proc)
    other_boot = "99999999-8888-7777-6666-555555555555"
    classification = L.classify_existing(store_dir, boot_id_now=other_boot, proc_root=proc)
    assert classification is L.LeaseClassification.STALE_BOOT_CHANGED


def test_pid_reuse_detected_by_starttime(store_dir, tmp_path):
    # Owner recorded pid 42 @ ticks 100; the LIVE pid 42 is a different
    # process (ticks 555). This is not the owner.
    proc = _fake_proc(tmp_path, {42: 555})
    L.acquire(store_dir, _owner(42, 100), boot_id_now=BOOT, proc_root=proc)
    classification = L.classify_existing(store_dir, boot_id_now=BOOT, proc_root=proc)
    assert classification is L.LeaseClassification.STALE_PID_REUSED


def test_live_owner_with_matching_starttime_is_held(store_dir, tmp_path):
    proc = _fake_proc(tmp_path, {42: 100})
    L.acquire(store_dir, _owner(42, 100), boot_id_now=BOOT, proc_root=proc)
    assert (
        L.classify_existing(store_dir, boot_id_now=BOOT, proc_root=proc)
        is L.LeaseClassification.HELD
    )


def test_torn_owner_file_classified_torn_and_adoptable(store_dir, tmp_path):
    proc = _fake_proc(tmp_path, {})
    lease_dir = store_dir / "lease"
    lease_dir.mkdir(parents=True)
    (lease_dir / "owner.json").write_text("{ not json")
    assert (
        L.classify_existing(store_dir, boot_id_now=BOOT, proc_root=proc)
        is L.LeaseClassification.TORN
    )
    adopted = L.acquire(
        store_dir,
        _owner(77, 700),
        boot_id_now=BOOT,
        proc_root=proc,
        allow_stale_adopt=True,
    )
    assert adopted is L.LeaseClassification.TORN


def test_release_only_your_own_lease(store_dir, tmp_path):
    proc = _fake_proc(tmp_path, {42: 100})
    mine = _owner(42, 100)
    L.acquire(store_dir, mine, boot_id_now=BOOT, proc_root=proc)
    # Someone else (a stale adopter path) replaced the owner file.
    other = _owner(90, 900)
    (store_dir / "lease/owner.json").write_text(
        json.dumps(json.loads(other.model_dump_json()), sort_keys=True)
    )
    assert not L.release(store_dir, mine)
    assert (store_dir / "lease/owner.json").exists()
    assert L.release(store_dir, other)
    assert not (store_dir / "lease/owner.json").exists()


def test_proc_start_ticks_parses_field22(tmp_path):
    proc = _fake_proc(tmp_path, {7: 4242})
    assert L.proc_start_ticks(7, proc) == 4242
    assert L.proc_start_ticks(8, proc) is None
