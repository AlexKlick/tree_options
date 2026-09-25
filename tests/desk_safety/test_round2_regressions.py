"""Round-2 audit regressions: R2-01 fill custody, R2-02 cost coverage, R2-03
scorecard cutoffs. Test names mirror the audit kit so the red->green receipts
line up with the verification JSON.

The lifecycle boundary under test is Enterer.run()'s per-cycle
``_sync_book_from_disk()`` reload — a test that ticks without reloading is a
control, not a lifecycle proof (that gap is how round 1's persistence fix
passed while the real cycle still lost fills).
"""
import json
from datetime import date
from decimal import Decimal

import pytest
from tests.unit.test_trex_enter import FakeEntryIbkr, _at, _enterer, _plan
from tests.unit.test_trex_monitor import FakeIbkr, _monitor

from tree_options.desk import scorecards, shadows
from tree_options.desk.evidence import EvidenceStore
from tree_options.desk.sessions import cutoff_instant
from tree_options.trex import engine
from tree_options.trex.engine import ComboQuote
from tree_options.trex.enter import Enterer
from tree_options.trex.monitor import Monitor, compute_marks
from tree_options.trex.plan import LegStructure
from tree_options.trex.state import BookState, Status, StructureState


def _disk(ent: Enterer) -> StructureState:
    sid = ent.plan.structures[0].id
    return BookState.load(ent.run_dir / 'book.json', [sid]).structures[sid]


# -- R2-01: partial fills must be durable before the next reload ------------


def test_r2_partial_fill_is_persisted_before_next_reload(world):
    fake = FakeEntryIbkr()
    ent = _enterer(world.root, fake, _at(10, 0))
    ent._tick()  # BUY 5 placed
    sid = ent.plan.structures[0].id
    a = ent.orders[sid]

    fake.fill_partial(a, 2, '0.44')
    ent._tick()  # memory records the partial fill; disk must too
    assert _disk(ent).filled_qty == 2
    assert _disk(ent).entry_fill == Decimal('0.44')
    assert _disk(ent).entry_order_seen == 2

    ent._sync_book_from_disk()  # the real run() cycle boundary
    fake.fill_partial(a, 2, '0.44')  # broker re-reports the same cumulative state
    ent._tick()
    assert ent.book.structures[sid].filled_qty == 2  # not reverted to 0
    assert _disk(ent).filled_qty == 2


def test_r2_replacement_after_reload_buys_only_actual_remainder(world):
    fake = FakeEntryIbkr()
    ent = _enterer(world.root, fake, _at(10, 0))
    ent._tick()
    sid = ent.plan.structures[0].id
    a = ent.orders[sid]

    fake.fill_partial(a, 2, '0.44')
    ent._tick()
    ent._sync_book_from_disk()  # lose any unsaved in-memory accounting

    ent._reprice(ent.plan.structures[0], Decimal('0.32'))
    assert fake.placed[-1] == (sid, 'BUY', 3, Decimal('0.32'))
    assert ent.book.structures[sid].filled_qty == 2  # cancelled order's fills kept


def test_r2_revision_survives_real_loop_reload_with_quantity(world):
    fake = FakeEntryIbkr()
    ent = _enterer(world.root, fake, _at(10, 0))
    ent._tick()
    sid = ent.plan.structures[0].id
    a = ent.orders[sid]

    fake.fill_partial(a, 2, '0.44')
    ent._tick()
    ent._sync_book_from_disk()
    fake.fill_partial(a, 2, '0.50')  # same quantity, revised average
    ent._tick()

    disk = _disk(ent)
    assert disk.filled_qty == 2
    assert disk.entry_fill == Decimal('0.50')
    assert disk.entry_order_notional == Decimal('1.00')


def test_r2_save_failure_self_heals_from_durable_state(world, monkeypatch):
    """A failed save must not silently advance an in-memory checkpoint past
    durable state: the next cycle reloads and the broker's cumulative report
    reconciles idempotently (a duplicate fill event is acceptable)."""
    fake = FakeEntryIbkr()
    ent = _enterer(world.root, fake, _at(10, 0))
    ent._tick()
    sid = ent.plan.structures[0].id
    a = ent.orders[sid]

    calls = {'n': 0}
    orig = Enterer._save_book

    def flaky(self):
        calls['n'] += 1
        if calls['n'] == 1:  # the save that would persist the partial fill
            raise OSError('disk full')
        return orig(self)

    monkeypatch.setattr(Enterer, '_save_book', flaky)
    fake.fill_partial(a, 2, '0.44')
    with pytest.raises(OSError):
        ent._tick()
    monkeypatch.undo()

    ent._sync_book_from_disk()
    fake.fill_partial(a, 2, '0.44')  # broker re-reports
    ent._tick()
    assert ent.book.structures[sid].filled_qty == 2
    assert _disk(ent).filled_qty == 2


def test_r2_terminal_fill_after_reload_completes_durable_accounting(world):
    fake = FakeEntryIbkr()
    ent = _enterer(world.root, fake, _at(10, 0))
    ent._tick()
    sid = ent.plan.structures[0].id
    a = ent.orders[sid]

    fake.fill_partial(a, 2, '0.44')
    ent._tick()
    ent._sync_book_from_disk()
    fake.fill(a, 5, '0.47')  # terminal: cumulative 5 @ 0.47 covers all fills
    ent._tick()

    assert ent.book.structures[sid].status is Status.OPEN
    disk = _disk(ent)
    assert disk.filled_qty == 5
    assert disk.entry_fill == Decimal('0.47')


def test_r2_restart_after_partial_fill_resumes_from_durable_checkpoint(world):
    fake = FakeEntryIbkr()
    ent = _enterer(world.root, fake, _at(10, 0))
    ent._tick()
    sid = ent.plan.structures[0].id
    a = ent.orders[sid]

    fake.fill_partial(a, 2, '0.44')
    ent._tick()

    # process dies; a fresh runner on the SAME run dir loads the durable book
    # (as production main() does) and adopts the still-working order
    sids = [s.id for s in _plan().structures]
    ent2 = Enterer(_plan(), fake, BookState.load(ent.run_dir / 'book.json', sids),
                   ent.run_dir, clock=lambda: _at(10, 30))
    ent2.adopt_open_entries()
    fake.fill_partial(a, 2, '0.44')  # broker re-reports the same cumulative 2
    ent2._tick()
    assert ent2.book.structures[sid].filled_qty == 2  # not re-counted to 4

    ent2._reprice(ent2.plan.structures[0], Decimal('0.32'))
    assert fake.placed[-1] == (sid, 'BUY', 3, Decimal('0.32'))


def test_r2_exit_fill_survives_a_failed_monitor_save(world, monkeypatch):
    """The exit side's mirror of the checkpoint hazard: the monitor saves at
    the end of every drain, so the loss only appears when that save fails."""
    fake = FakeIbkr()
    mon = _monitor(world.root, fake, world.now)
    spec = mon.plan.structures[0]
    sid = spec.id
    st = mon.book.structures[sid]
    st.to(Status.ENTER_WORKING, world.now)
    st.to(Status.OPEN, world.now)
    st.filled_qty = spec.quantity
    mon._save_book()  # durable baseline: OPEN with the full position
    ref = fake.place_combo(spec, 'SELL', spec.quantity, Decimal('2'))
    mon.orders[sid] = ref

    calls = {'n': 0}
    orig = Monitor._save_book

    def flaky(self):
        calls['n'] += 1
        if calls['n'] == 1:
            raise OSError('disk full')
        return orig(self)

    monkeypatch.setattr(Monitor, '_save_book', flaky)
    fake.fill(ref, 2, '2.00')
    with pytest.raises(OSError):
        mon._drain_orders()
    monkeypatch.undo()

    mon._sync_book_from_disk()
    fake.fill(ref, 2, '2.00')  # broker re-reports
    mon._drain_orders()
    assert mon.book.structures[sid].exit_filled_qty == 2


# -- R2-02: filled quantity is not price coverage ----------------------------


def test_r2_delayed_average_with_quantity_growth_uses_total_priced_quantity(world):
    fake = FakeEntryIbkr()
    ent = _enterer(world.root, fake, _at(10, 0))
    ent._tick()
    sid = ent.plan.structures[0].id
    a = ent.orders[sid]

    a.trade.orderStatus.filled = 2  # two fills, no average reported yet
    ent._tick()
    st = ent.book.structures[sid]
    assert st.filled_qty == 2
    assert st.entry_fill is None
    assert st.entry_unpriced_qty == 2

    fake.fill_partial(a, 3, '0.50')  # growth WITH a cumulative average
    ent._tick()
    assert st.filled_qty == 3
    assert st.entry_fill == Decimal('0.50')  # 1.50 / 3 priced, never 1.50 / 1
    assert st.entry_unpriced_qty == 0


def test_r2_unknown_prior_order_cost_must_not_become_a_fabricated_average(world):
    fake = FakeEntryIbkr()
    ent = _enterer(world.root, fake, _at(10, 0))
    ent._tick()
    sid = ent.plan.structures[0].id
    a = ent.orders[sid]

    a.trade.orderStatus.filled = 2  # earlier order: 2 fills, price never arrives
    ent._tick()

    ent._reprice(ent.plan.structures[0], Decimal('0.30'))  # cancel + replace
    b = ent.orders[sid]
    fake.fill_partial(b, 2, '0.50')
    ent._tick()
    fake.fill_partial(b, 3, '0.60')  # replacement grows to 3 @ cumulative 0.60
    ent._tick()

    st = ent.book.structures[sid]
    assert st.filled_qty == 5
    assert st.entry_unpriced_qty == 2  # the earlier order's cost is still unknown
    assert st.entry_fill == Decimal('0.60')  # priced-part average over 3
    assert st.to_dict()['entry_unpriced_qty'] == 2  # explicit, persisted


def test_r2_exit_replacement_after_cancel_wait_merges_final_fills(world):
    """Enter merges a cancelled order's last fills before replacing
    (``_place_after_cancel``); the exit reprice path must not lose the fills
    that land while it waits for the cancel confirmation."""
    fake = FakeIbkr()
    mon = _monitor(world.root, fake, world.now)
    spec = mon.plan.structures[0]
    sid = spec.id
    st = mon.book.structures[sid]
    st.to(Status.ENTER_WORKING, world.now)
    st.to(Status.OPEN, world.now)
    st.filled_qty = spec.quantity
    ref = fake.place_combo(spec, 'SELL', spec.quantity, Decimal('2'))
    mon.orders[sid] = ref
    fake.fill(ref, 2, '2.00')
    mon._drain_orders()

    # fills land during the cancel wait; the broker's final state says 3
    ref.trade.orderStatus.status = 'Cancelled'
    ref.trade.orderStatus.filled = 3
    ref.trade.orderStatus.avgFillPrice = 2.0
    mon._refresh_exit(spec, Decimal('1.90'))

    assert st.exit_filled_qty == 3  # the wait-time fills are durable
    assert fake.placed[-1] == (sid, 'SELL', 2, Decimal('1.90'))  # true remainder


def test_r2_exit_delayed_average_uses_total_priced_quantity(world):
    fake = FakeIbkr()
    mon = _monitor(world.root, fake, world.now)
    spec = mon.plan.structures[0]
    sid = spec.id
    st = mon.book.structures[sid]
    st.to(Status.ENTER_WORKING, world.now)
    st.to(Status.OPEN, world.now)
    st.filled_qty = spec.quantity
    ref = fake.place_combo(spec, 'SELL', spec.quantity, Decimal('2'))
    mon.orders[sid] = ref

    ref.trade.orderStatus.filled = 2  # exit fills without an average yet
    mon._drain_orders()
    assert st.exit_filled_qty == 2
    assert st.exit_fill is None
    assert st.exit_unpriced_qty == 2

    fake.fill(ref, 3, '0.50')  # cumulative average arrives with growth
    mon._drain_orders()
    assert st.exit_fill == Decimal('0.50')  # 1.50 / 3 priced, never 1.50 / 1
    assert st.exit_unpriced_qty == 0


def test_r2_take_profit_stands_down_while_entry_cost_incomplete(world):
    spec = LegStructure.model_validate({**world.queue['admissible'][0]['structure'],
        'exits': {'touch': False, 'breach': False,
                  'take_profit': {'basis': 'gain_frac', 'value': '0.10'}}})
    st = StructureState()
    st.to(Status.ENTER_WORKING, world.now)
    st.to(Status.OPEN, world.now)
    st.filled_qty, st.entry_fill = spec.quantity, Decimal('0.60')
    st.entry_unpriced_qty = 2  # a partial average is not a take-profit basis
    assert engine._take_profit_hit(spec, st, Decimal('5.00')) is False


def test_r2_stop_ticks_stand_down_while_entry_cost_incomplete(world):
    spec = LegStructure.model_validate({**world.queue['admissible'][0]['structure'],
        'exits': {'touch': False, 'breach': False,
                  'stop_loss': {'basis': 'debit_frac', 'value': '0.50'}}})
    st = StructureState()
    st.to(Status.ENTER_WORKING, world.now)
    st.to(Status.OPEN, world.now)
    st.filled_qty, st.entry_fill = spec.quantity, Decimal('0.60')
    st.entry_unpriced_qty = 2
    quote = ComboQuote(Decimal('0.10'), Decimal('0.20'))
    assert engine._stop_ticks(spec, st, quote) == 0  # not evaluable: no count


def test_r2_marks_disclose_incomplete_entry_coverage(world):
    mon = _monitor(world.root, FakeIbkr(), world.now)
    spec = mon.plan.structures[0]
    st = mon.book.structures[spec.id]
    st.to(Status.ENTER_WORKING, world.now)
    st.to(Status.OPEN, world.now)
    st.filled_qty, st.entry_fill = 5, Decimal('0.60')
    st.entry_unpriced_qty = 2

    doc = compute_marks(mon.plan.structures, mon.book,
                        {spec.id: ComboQuote(Decimal('0.70'), Decimal('0.80'))})
    row = doc['structures'][spec.id]
    assert row['unpriced'] == 2  # the incompleteness is visible
    assert 'unrealized' not in row  # never (partial average) x (whole qty)
    assert doc['total_unrealized'] is None  # no complete quoted row: not "$0.00"


def test_r2_realized_is_unknown_while_costs_incomplete(world):
    mon = _monitor(world.root, FakeIbkr(), world.now)
    spec = mon.plan.structures[0]
    st = mon.book.structures[spec.id]
    st.to(Status.ENTER_WORKING, world.now)
    st.to(Status.OPEN, world.now)
    st.filled_qty, st.entry_fill = 5, Decimal('0.60')
    st.entry_unpriced_qty = 2
    st.exit_filled_qty, st.exit_fill = 2, Decimal('0.50')

    mon._marks_history_cycle({'structures': {}, 'ts': world.now.isoformat()})
    row = json.loads(
        (mon.run_dir / 'marks_history.jsonl').read_text().splitlines()[0]
    )['structures'][spec.id]
    assert row['realized_to_date'] is None  # not (0.50 - partial) x 2 x 100
    assert row['unpriced'] == 2


# -- R2-03: the requested knowledge cutoff is not the outcome horizon --------


def _replay(world, session: date, now: date) -> dict:
    return shadows.update_shadows(session=session, now=cutoff_instant(now),
                                   cal=world.cal, database=world.db,
                                   store_root=world.store, queue_dir=world.queues)


def test_r2_clamp_preserves_requested_knowledge_cutoff(world):
    world.queue_file()
    later = world.cal.nth_after(world.deadline, 1)
    # the deadline quote was only fetched on D+1; a replay that day evaluates D
    world.chain(world.deadline, fetched=cutoff_instant(later))
    _replay(world, world.deadline, later)

    current = scorecards.build_scorecards(world.db)['families'][0]
    assert current['n_resolved'] == 1  # the late quote is known today

    card = scorecards.build_scorecards(world.db, as_of=later)['families'][0]
    doc = scorecards.build_scorecards(world.db, as_of=later)
    assert doc['as_of_clamped_to_evaluation'] is True  # horizon stays D
    assert doc['as_of_requested'] == later.isoformat()
    assert card['n_resolved'] == 1  # D+1 knowledge includes the D+1 fetch
    assert card['n_censored'] == 0  # clamping must not censor known outcomes


def test_r2_no_evaluation_is_not_ok_with_explicit_asof(world):
    with EvidenceStore(world.db):  # initialized custody, nothing evaluated
        pass
    doc = scorecards.build_scorecards(world.db, as_of=world.session)
    assert doc['status'] == 'no_evaluation'  # a date is not an evaluation


def test_r2_same_horizon_replay_is_stable(world):
    world.queue_file()
    world.chain(world.deadline)
    _replay(world, world.deadline, world.deadline)
    default = scorecards.build_scorecards(world.db)
    explicit = scorecards.build_scorecards(world.db, as_of=world.deadline)
    assert explicit['as_of_clamped_to_evaluation'] is False
    assert explicit['families'] == default['families']
