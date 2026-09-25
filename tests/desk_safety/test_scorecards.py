"""Outcome quality, denominator transparency and no implicit promotion."""
from decimal import Decimal

import pytest

from tree_options.desk import scorecards, shadows
from tree_options.desk.evidence import EvidenceError, EvidenceStore
from tree_options.desk.sessions import cutoff_instant


def record(world, session=None):
    d = session or world.deadline
    return shadows.update_shadows(session=d, now=cutoff_instant(d), cal=world.cal,
        database=world.db, store_root=world.store, queue_dir=world.queues)


def test_missing_store_is_not_zero_pnl_or_healthy(world):
    doc = scorecards.build_scorecards(world.db)
    assert doc['status'] == 'not_initialized'
    assert not doc['execution_enabled']
    assert doc['families'] == []
    assert not world.db.exists()


def test_missing_deadline_remains_censored_not_a_loss_or_win(world):
    world.queue_file()
    world.chain(world.entry)
    record(world)
    card = scorecards.build_scorecards(world.db)['families'][0]
    assert card['n_episodes'] == 1
    assert card['n_resolved'] == 0 and card['n_censored'] == 1
    assert card['modeled_net_pnl_dollars'] is None
    assert card['promotion_ready'] is False
    world.chain(world.deadline)
    record(world)
    card = scorecards.build_scorecards(world.db)['families'][0]
    assert card['n_resolved'] == 1 and card['n_censored'] == 0
    assert Decimal(card['modeled_net_pnl_dollars']) == Decimal('-12.60')
    assert Decimal(card['assumed_commissions_dollars']) == Decimal('2.60')
    assert card['win_rate'] == 0


def test_open_episodes_do_not_count_as_censored(world):
    world.queue_file()
    record(world, world.entry)
    card = scorecards.build_scorecards(world.db)['families'][0]
    assert card['n_open'] == 1 and card['n_censored'] == 0


def test_asof_view_does_not_use_future_evaluation_or_marks(world):
    world.queue_file()
    world.chain(world.deadline)
    record(world)
    card = scorecards.build_scorecards(world.db, as_of=world.entry)['families'][0]
    assert card['n_open'] == 1 and card['n_resolved'] == 0


def test_vendor_conflict_disqualifies_previous_mark_without_deleting_it(world):
    world.queue_file()
    path, _doc = world.chain(world.deadline)
    record(world)
    path.with_name('AAPL.conflict.json.gz').write_bytes(b'conflict')
    record(world)
    card = scorecards.build_scorecards(world.db)['families'][0]
    assert card['n_resolved'] == 0 and card['n_censored'] == 1
    assert card['n_invalidated'] == 1
    with EvidenceStore(world.db, readonly=True) as store:
        assert len(store.all('mark')) == 1  # retained for audit, not silently rewritten


def test_tampered_store_never_returns_a_healthy_summary(world):
    world.queue_file()
    record(world)
    with EvidenceStore(world.db) as store:
        store.conn.execute("DELETE FROM objects WHERE kind='episode'")
    with pytest.raises(EvidenceError):
        scorecards.build_scorecards(world.db)


def test_count_floor_does_not_authorize_promotion(world):
    # Independent outcome records test the summary boundary, not twenty
    # duplicated calls to the production selector as a bogus sample generator.
    episodes = []
    marks = []
    horizon = world.cal.nth_after(world.entry, 30)
    for i in range(20):
        did = f'example-{i}'
        episodes.append({'deal_id': did, 'entry_session': world.cal.nth_after(world.entry, i).isoformat(),
            'exit_deadline': horizon.isoformat(), 'row': 'R1', 'tier': 'signal',
            'miner_sha256': 'a' * 64, 'playbook_sha256': 'b' * 64,
            'source_session': world.session.isoformat()})
        marks.append({'deal_id': did, 'session': horizon.isoformat(),
            'modeled_net_pnl_dollars': '1.00', 'modeled_gross_pnl_dollars': '3.60',
            'modeled_fees_dollars': '2.60'})
    card = scorecards.summarize(episodes, marks, [], as_of=horizon)[0]
    assert card['sample_floor_met'] is True
    assert card['promotion_ready'] is False
    assert 'deadline_eod_proxy_is_not_execution_evidence' in card['promotion_blockers']


def test_historical_card_does_not_use_quotes_received_after_requested_session(world):
    world.queue_file()
    later = world.cal.nth_after(world.deadline, 1)
    world.chain(world.deadline, fetched=cutoff_instant(later))
    record(world, later)
    current = scorecards.build_scorecards(world.db)['families'][0]
    historical = scorecards.build_scorecards(world.db, as_of=world.deadline)['families'][0]
    assert current['n_resolved'] == 1
    assert historical['n_resolved'] == 0 and historical['n_censored'] == 1
