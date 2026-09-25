"""Contract boundary oracles: mutate one fact, never weaken the expected rule."""
from __future__ import annotations

import copy
from datetime import datetime, time
from decimal import Decimal

import pytest

from tree_options.desk.contracts import ContractError, parse_deal, parse_queue, read_json
from tree_options.trex.clock import ET


def test_real_miner_contract_roundtrips(world):
    q = parse_queue(world.queue, world.cal, expected_session=world.session)
    d = parse_deal(q.admissible[0], q, world.cal)
    assert d.spec.entry_date == world.entry
    assert d.spec.deal_id == d.spec.id == d.deal_id
    assert d.spec.max_loss() == Decimal('221.00')  # cap $2.21 x 100 x one package
    assert d.fill == Decimal('2.20')


@pytest.mark.parametrize('field,value,code', [
    ('schema', 'trex.deal/999', 'queue_schema'),
    ('entry_session', '2099-01-01', 'entry_session'),
    ('valid_until', '2020-01-01T11:30:00', 'aware_timestamp'),
    ('admissible', 1, 'queue_rows'),
])
def test_bad_envelope_rejected(world, field, value, code):
    doc = copy.deepcopy(world.queue)
    doc[field] = value
    with pytest.raises(ContractError, match=code):
        parse_queue(doc, world.cal)


@pytest.mark.parametrize('field,value', [
    ('max_loss', 221.0), ('fill', 2.2), ('limit', 2.21), ('ref_mid', 2.15),
    ('max_loss', 'NaN'), ('fill', 'Infinity'), ('quantity', True),
    ('max_loss', '220.00'), ('exit_deadline', '2099-01-01'),
    ('underlying', '../../escape'), ('deal_id', '../../escape'),
    ('entry_session', '2099-01-01'), ('kind', 'credit_vertical'),
])
def test_malformed_or_inconsistent_deal_rejected(world, field, value):
    q = parse_queue(world.queue, world.cal)
    d = copy.deepcopy(q.admissible[0])
    d[field] = value
    with pytest.raises(ContractError):
        parse_deal(d, q, world.cal)


def test_equal_looking_float_nested_strike_refused(world):
    q = parse_queue(world.queue, world.cal)
    doc = copy.deepcopy(q.admissible[0])
    doc['structure']['legs'][0]['strike'] = 100.0
    with pytest.raises(ContractError, match='money_string'):
        parse_deal(doc, q, world.cal)


def test_header_cannot_extend_the_admission_window(world):
    doc = copy.deepcopy(world.queue)
    doc['valid_until'] = datetime.combine(world.entry, time(16), ET).isoformat()
    with pytest.raises(ContractError, match='valid_until'):
        parse_queue(doc, world.cal)


def test_deadline_must_leave_one_full_session_before_last_hold(world):
    q = parse_queue(world.queue, world.cal)
    doc = copy.deepcopy(q.admissible[0])
    last_hold = world.cal.sessions()[world.cal.sessions().index(world.expiry) - 1]
    doc['exit_deadline'] = doc['structure']['exit_deadline'] = last_hold.isoformat()
    with pytest.raises(ContractError, match='deadline_safety_buffer'):
        parse_deal(doc, q, world.cal)


def test_duplicate_ids_across_buckets_refused(world):
    doc = copy.deepcopy(world.queue)
    doc['surfaced'] = copy.deepcopy(doc['admissible'])
    with pytest.raises(ContractError, match='duplicate_deal'):
        parse_queue(doc, world.cal)


@pytest.mark.parametrize('raw', [b'{"x":1,"x":2}', b'{"x":NaN}', b'[]'])
def test_json_ambiguities_fail_closed(raw):
    with pytest.raises(ContractError):
        read_json(raw)

@pytest.mark.parametrize('field,value', [
    ('exits', []), ('exits', None), ('legs', None), ('legs', 3),
    ('exits', {'take_profit': []}), ('legs', [{'right': [], 'strike': '100'}]),
])
def test_malformed_nested_shapes_fail_as_contract_errors(world, field, value):
    from tree_options.desk.contracts import parse_deal, parse_queue
    q = parse_queue(world.queue, world.cal)
    row = copy.deepcopy(world.queue['admissible'][0])
    row['structure'][field] = value
    with pytest.raises(ContractError):
        parse_deal(row, q, world.cal)
