"""Consume the ACTUAL miner output, not a hand-invented admission schema."""
import dataclasses
from decimal import Decimal

from tests.fixtures import desk_miner as fixture
from tree_options.desk import contracts, miner, selection


def test_consumer_accepts_actual_miner_wire_shapes(tmp_path, monkeypatch):
    cal = fixture.calendar()
    where = fixture.materialize(fixture.base(cal), tmp_path / 'world')
    fixture.env(monkeypatch, where)
    # Only test-runtime MC volume and test selection are relaxed; no sealed file
    # changes. This exercises serialization, NOT approval or strategy validity.
    config = dataclasses.replace(selection.load_config(), n_paths=1000,
        selection=selection.Selection(min_stress_fill_ev_usd=Decimal('-100000'),
                                      min_ev_per_max_loss=Decimal('-100')))
    result = miner.run_mine(session=fixture.D, now=fixture.NOW, cal=cal,
                           config=config, dry_run=True)
    assert result.exit_code == 0, result.detail
    queue = contracts.parse_queue(result.payload, cal)
    assert queue.admissible  # prevents a vacuous empty-queue integration test
    for raw in queue.admissible:
        deal = contracts.parse_deal(raw, queue, cal)
        assert deal.spec.id == deal.spec.deal_id == raw['deal_id']
        assert deal.spec.entry_date == queue.entry_session
