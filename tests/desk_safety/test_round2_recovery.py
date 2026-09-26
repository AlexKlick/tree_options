"""Nonempty crash/replay/backup rehearsal (round-2 audit, R2-04): a REAL
child writer is SIGKILLed at two transaction boundaries of the shadow
writer — after the first object/audit pair lands inside the still-open
transaction (must roll back to genesis), and after the commit but before
any acknowledgment (must be fully retained). Recovery, idempotent replay
and a verified backup must hold in both cases. These fence the corrective
work and seed the host-side acceptance rehearsal.
"""
import multiprocessing
import os
import signal
import time
from datetime import date, datetime
from pathlib import Path

import pytest

from tree_options.desk import shadows
from tree_options.desk.evidence import EvidenceStore
from tree_options.desk.sessions import cutoff_instant
from tree_options.time.calendar import StaticSessionCalendar

_CAL = (Path(__file__).resolve().parents[2] / 'data' / 'calendar' / 'trex'
        / 'nyse_sessions_2018_01_02_2028_12_29.json')
_CAL_SHA = _CAL.with_name('nyse_sessions_2018_01_02_2028_12_29.sha256')


def _child_after_first_put(database: str, queue_dir: str, store_root: str,
                           barrier: str, session: str, now: str) -> None:
    """Run the real writer with EvidenceStore.put wrapped: after the first
    object+audit pair is written INSIDE the open transaction, signal and
    hang — the kill lands mid-transaction."""
    from tree_options.desk.evidence import EvidenceStore

    orig = EvidenceStore.put
    fired: list[int] = []

    def put(self, kind, key, payload, at):
        result = orig(self, kind, key, payload, at)
        if result and not fired:
            fired.append(1)
            Path(barrier).write_text('first-put-in-open-transaction')
            time.sleep(600)
        return result

    EvidenceStore.put = put
    shadows.update_shadows(session=date.fromisoformat(session),
                           now=datetime.fromisoformat(now),
                           cal=StaticSessionCalendar(_CAL, _CAL_SHA),
                           database=Path(database), store_root=Path(store_root),
                           queue_dir=Path(queue_dir))


def _child_after_commit(database: str, queue_dir: str, store_root: str,
                        barrier: str, session: str, now: str) -> None:
    """Run the real writer to completion (one committed transaction), then
    signal and HANG — the kill must land after the commit, before any ack.
    Without the hang the kill races interpreter shutdown: the parent can
    SIGKILL an already-exited process and read a clean exit code (the
    4714/1 gate failure on 2026-09-25)."""
    shadows.update_shadows(session=date.fromisoformat(session),
                           now=datetime.fromisoformat(now),
                           cal=StaticSessionCalendar(_CAL, _CAL_SHA),
                           database=Path(database), store_root=Path(store_root),
                           queue_dir=Path(queue_dir))
    Path(barrier).write_text('committed-not-acked')
    time.sleep(600)


def _kill_child_at_barrier(root: Path, target, world, timeout: float = 30.0) -> None:
    root.mkdir(parents=True, exist_ok=True)
    barrier = root / 'barrier'
    ctx = multiprocessing.get_context('spawn')
    p = ctx.Process(target=target, args=(str(root / 'desk.sqlite3'),
        str(world.queues), str(world.store), str(barrier),
        world.deadline.isoformat(), cutoff_instant(world.deadline).isoformat()))
    p.start()
    deadline = time.monotonic() + timeout
    try:
        while not barrier.exists():
            if not p.is_alive():
                raise AssertionError(f'child died before the barrier (exit {p.exitcode})')
            if time.monotonic() > deadline:
                raise AssertionError('barrier timeout — child never reached it')
            time.sleep(0.05)
        os.kill(p.pid, signal.SIGKILL)
        p.join()
        assert p.exitcode == -signal.SIGKILL  # the actual kill receipt
    finally:
        if p.is_alive():
            os.kill(p.pid, signal.SIGKILL)
            p.join()


def _replay(world, database: Path) -> dict:
    return shadows.update_shadows(session=world.deadline,
        now=cutoff_instant(world.deadline), cal=world.cal, database=database,
        store_root=world.store, queue_dir=world.queues)


@pytest.mark.parametrize('mode', ['after_first_put', 'after_commit'])
def test_r2_nonempty_sigkill_replay_and_verified_backup(world, mode):
    world.queue_file()
    world.chain(world.deadline)
    root = world.root / 'crash'
    target = _child_after_first_put if mode == 'after_first_put' else _child_after_commit
    _kill_child_at_barrier(root, target, world)
    database = root / 'desk.sqlite3'

    with EvidenceStore(database, readonly=True) as store:
        crashed = store.verify()
        episodes, marks = len(store.all('episode')), len(store.all('mark'))
    if mode == 'after_first_put':
        # the open transaction rolled back: nothing partial is visible
        assert (episodes, marks) == (0, 0)
        assert crashed['events'] == 0
    else:
        # the committed transaction survived the kill intact
        assert (episodes, marks) == (1, 1)
        assert crashed['ok'] and crashed['events'] > 0

    first = _replay(world, database)
    assert first['episodes_created'] == (1 if mode == 'after_first_put' else 0)
    assert first['marks_created'] == (1 if mode == 'after_first_put' else 0)

    with EvidenceStore(database, readonly=True) as store:
        head = store.verify()
        assert head['ok']
        assert (len(store.all('episode')), len(store.all('mark'))) == (1, 1)

    second = _replay(world, database)  # idempotent: nothing re-created
    assert second['episodes_created'] == 0 and second['marks_created'] == 0
    with EvidenceStore(database, readonly=True) as store:
        assert store.verify() == head  # the audit head is stable across replays

    backup = root / 'backup.sqlite3'
    with EvidenceStore(database) as store:
        store.backup(backup)
    with EvidenceStore(backup, readonly=True) as restored:
        assert restored.verify() == head  # the verified backup matches exactly
        assert len(restored.all('mark')) == 1
