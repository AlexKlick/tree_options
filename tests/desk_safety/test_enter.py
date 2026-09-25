"""Admission previews cannot migrate into the executable lane."""
from datetime import datetime, time

import pytest

from tree_options.desk import enter
from tree_options.desk.contracts import ContractError
from tree_options.desk.evidence import EvidenceStore
from tree_options.trex.clock import ET


def run(world, **kwargs):
    return enter.run_enter(now=kwargs.pop('now', world.now), cal=world.cal,
        database=world.db, queue_dir=world.queues, run_dir=world.root / 'execution', **kwargs)


def test_default_preview_records_blockers_but_never_spec_or_book(world):
    world.queue_file()
    doc = run(world)
    assert doc['admitted'] == 0 and doc['execution_enabled'] is False
    assert doc['previewed'] == 1
    row = doc['decisions'][0]
    assert {'shadow_only_build', 'selection_rule_not_activated',
            'broker_risk_snapshot_not_available'} <= set(row['blockers'])
    assert not list(world.root.rglob('specs'))
    assert not list(world.root.rglob('book.json'))
    with EvidenceStore(world.db, readonly=True) as store:
        assert len(store.all('admission_preview')) == 1
    run(world)
    with EvidenceStore(world.db, readonly=True) as store:
        assert len(store.all('admission_preview')) == 1


@pytest.mark.parametrize('flag', ['HALT', 'AUTO_OFF'])
def test_operator_flags_are_additional_blockers(world, flag):
    world.queue_file()
    rd = world.root / 'execution'
    rd.mkdir()
    (rd / flag).touch()
    assert flag.lower() in run(world)['decisions'][0]['blockers']


def test_armed_switch_is_explicitly_unsupported_even_with_dummy_book(world):
    world.queue_file()
    rd = world.root / 'execution'
    rd.mkdir()
    (rd / 'book.json').write_text('{}')
    with pytest.raises(ContractError, match='execution_not_implemented'):
        run(world, shadow=False)
    assert not world.db.exists()


@pytest.mark.parametrize('hour,minute', [(9, 49), (11, 30), (12, 0)])
def test_closed_entry_window_writes_no_preview(world, hour, minute):
    world.queue_file()
    now = datetime.combine(world.entry, time(hour, minute), ET)
    assert run(world, now=now)['status'] == 'outside_entry_window'
    assert not world.db.exists()


def test_dry_run_no_persistent_side_effects(world):
    world.queue_file()
    before = sorted(world.root.rglob('*'))
    assert run(world, dry_run=True)['previewed'] == 1
    assert sorted(world.root.rglob('*')) == before


def test_seal_or_identity_tampering_cannot_be_admitted(world):
    world.queue['admissible'][0]['structure']['deal_id'] = 'forged'
    world.queue_file()
    with pytest.raises(ContractError):
        run(world)
    assert not world.db.exists()


def test_explicit_old_session_cannot_bypass_real_entry_date(world):
    world.queue_file()
    with pytest.raises(ContractError, match='session_not_current'):
        run(world, session=world.cal.sessions()[world.cal.ordinal(world.session) - 1])


def test_research_directory_env_is_not_execution_directory(world, monkeypatch):
    monkeypatch.setenv('DESK_PAPER_DIR', str(world.root / 'research'))
    monkeypatch.setenv('HOME', str(world.root))
    assert enter.execution_directory() != world.root / 'research'
    monkeypatch.setenv('TREX_DESK_RUN_DIR', str(world.root / 'explicit-run'))
    assert enter.execution_directory() == world.root / 'explicit-run'
