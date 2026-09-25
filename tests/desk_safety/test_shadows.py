import copy
from datetime import datetime, time
from decimal import Decimal

import pytest

from tree_options.desk import shadows
from tree_options.desk.contracts import ContractError
from tree_options.desk.evidence import EvidenceError, EvidenceStore
from tree_options.desk.sessions import cutoff_instant
from tree_options.trex.clock import ET


def run(world, as_of, *, now=None, dry_run=False):
    return shadows.update_shadows(session=as_of, now=now or cutoff_instant(as_of),
        cal=world.cal, database=world.db, store_root=world.store,
        queue_dir=world.queues, dry_run=dry_run)


def test_backfill_then_exact_deadline_resolution_and_fees(world):
    world.queue_file()
    world.chain(world.entry)
    world.chain(world.deadline)
    result = run(world, world.deadline)
    assert result['episodes_created'] == 1
    assert result['marks_created'] == 2
    with EvidenceStore(world.db, readonly=True) as store:
        episode = store.all('episode')[0]
        marks = store.all('mark')
        terminal = next(m for m in marks if m['session'] == world.deadline.isoformat())
        assert Decimal(terminal['modeled_gross_pnl_dollars']) == Decimal('-10.00')
        assert Decimal(terminal['modeled_fees_dollars']) == Decimal('2.60')
        assert Decimal(terminal['modeled_net_pnl_dollars']) == Decimal('-12.60')
        assert episode['entry_model'] == 'miner_hypothetical_fill'
        before = store.verify()
    again = run(world, world.deadline)
    assert again['episodes_created'] == again['marks_created'] == 0
    with EvidenceStore(world.db, readonly=True) as store:
        assert store.verify() == before


def test_no_future_mark_even_when_future_files_exist(world):
    world.queue_file()
    world.chain(world.entry)
    world.chain(world.deadline)
    run(world, world.entry)
    with EvidenceStore(world.db, readonly=True) as store:
        assert [m['session'] for m in store.all('mark')] == [world.entry.isoformat()]


def test_missing_deadline_is_censored_not_backward_filled(world):
    world.queue_file()
    world.chain(world.entry)
    run(world, world.deadline)
    with EvidenceStore(world.db, readonly=True) as store:
        assert len(store.all('mark')) == 1
        assert any(q['code'] == 'chain_missing' and q['session'] == world.deadline.isoformat()
                   for q in store.all('quality'))
    # The delayed original observation can arrive later, without overwriting a fake resolution.
    world.chain(world.deadline)
    assert run(world, world.deadline)['marks_created'] == 1


def test_missing_current_queue_does_not_stop_existing_episode_marks(world):
    world.queue_file()
    run(world, world.entry)
    world.queues.joinpath(f'{world.session.isoformat()}.json').unlink()
    world.chain(world.deadline)
    assert run(world, world.deadline)['marks_created'] == 1


def test_fetched_after_as_of_is_not_known_yet(world):
    world.queue_file()
    after = world.cal.nth_after(world.deadline, 1)
    world.chain(world.deadline, fetched=cutoff_instant(after))
    run(world, world.deadline)
    with EvidenceStore(world.db, readonly=True) as store:
        assert store.all('mark') == []
    assert run(world, after)['marks_created'] == 1


def test_changed_queue_or_chain_is_a_conflict_not_a_rewrite(world):
    world.queue_file()
    world.chain(world.entry)
    run(world, world.entry)
    world.chain(world.entry, long=('4.00', '4.10'))
    with pytest.raises(EvidenceError, match='content_conflict'):
        run(world, world.entry)
    changed = copy.deepcopy(world.queue)
    changed['admissible'][0]['notes'] = ['changed']
    world.queue_file(changed)
    with pytest.raises(EvidenceError, match='content_conflict'):
        run(world, world.entry)


def test_no_executable_files_and_dry_run_has_no_writes(world):
    world.queue_file()
    before = sorted(p for p in world.root.rglob('*'))
    assert run(world, world.entry, dry_run=True)['episodes_created'] == 1
    assert sorted(p for p in world.root.rglob('*')) == before
    run(world, world.entry)
    assert not list(world.root.rglob('book.json'))
    assert not list(world.root.rglob('specs'))


def test_all_candidates_recorded_but_only_one_representative_per_cohort(world):
    other = copy.deepcopy(world.queue['admissible'][0])
    other['deal_id'] += '-other'
    other['structure']['id'] = other['structure']['deal_id'] = other['deal_id']
    other['status'] = 'not_selected'
    world.queue['surfaced'] = [other]
    world.queue_file()
    run(world, world.entry)
    with EvidenceStore(world.db, readonly=True) as store:
        assert len(store.all('candidate')) == 2
        assert len(store.all('episode')) == 1
        assert store.all('episode')[0]['deal_id'] == world.queue['admissible'][0]['deal_id']


def test_invalid_admissible_aborts_entire_queue_transaction(world):
    world.queue['admissible'][0]['max_loss'] = '1.00'
    world.queue_file()
    with pytest.raises(ContractError):
        run(world, world.entry)
    with EvidenceStore(world.db, readonly=True) as store:
        assert store.all('queue') == []


def test_future_session_request_refused(world):
    with pytest.raises(ContractError, match='session_not_closed'):
        run(world, world.deadline, now=world.now)


@pytest.mark.parametrize('long,short,code', [
    (('3.2', '3.1'), ('0.8', '0.9'), 'quote_book'),
    (('3.0', '3.1'), ('0', '0'), 'quote_book'),
])
def test_crossed_and_zero_books_never_become_prices(world, long, short, code):
    world.queue_file()
    world.chain(world.entry, long, short)
    run(world, world.entry)
    with EvidenceStore(world.db, readonly=True) as store:
        assert store.all('mark') == []
        assert any(q['code'] == code for q in store.all('quality'))


def test_zero_long_bid_with_positive_ask_is_a_real_adverse_side(world):
    world.queue_file()
    world.chain(world.entry, long=('0', '0.1'))
    run(world, world.entry)
    with EvidenceStore(world.db, readonly=True) as store:
        m = store.all('mark')[0]
        assert m['mid'] is None
        assert Decimal(m['realistic']) == Decimal('-0.90')


def test_no_resolution_before_close_even_on_deadline_date(world):
    world.queue_file()
    world.chain(world.deadline)
    with pytest.raises(ContractError, match='session_not_closed'):
        run(world, world.deadline, now=datetime.combine(world.deadline, time(10), ET))


def test_queue_from_a_future_decision_boundary_is_not_adopted(world):
    world.queue_file()
    result = run(world, world.session)
    assert result['episodes_created'] == 0
    with EvidenceStore(world.db, readonly=True) as store:
        assert store.all('episode') == []
    assert run(world, world.entry)['episodes_created'] == 1


def test_retrospective_backfill_is_labeled_not_passed_as_forward_registration(world):
    world.queue_file()
    run(world, world.deadline)
    with EvidenceStore(world.db, readonly=True) as store:
        assert store.all('episode')[0]['registration_timing'] == 'retrospective_backfill'
