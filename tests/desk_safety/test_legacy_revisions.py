"""Behavioral regressions: order-local price revisions, no quantity growth.

Reuse the existing broker fakes and plans rather than inventing a second broker
interface. Test the drain directly so market-hour and trigger rules are irrelevant.
"""
import json
from decimal import Decimal

import pytest
from tests.unit.test_trex_enter import FakeEntryIbkr, _at, _enterer
from tests.unit.test_trex_monitor import FakeIbkr, _monitor

from tree_options.trex.state import BookState


@pytest.mark.parametrize('prior_qty,prior_price', [(0, '0'), (2, '1.00')])
def test_entry_revision_blends_current_order_not_whole_book(world, prior_qty, prior_price):
    ent = _enterer(world.root, FakeEntryIbkr(), world.now)
    sid = ent.plan.structures[0].id
    st = ent.book.structures[sid]
    st.filled_qty = prior_qty
    st.entry_fill = Decimal(prior_price) if prior_qty else None
    ent._merge_order_total(sid, 2, Decimal('2.00'), 'Submitted')
    ent._merge_order_total(sid, 2, Decimal('3.00'), 'Submitted')
    expected = (prior_qty * Decimal(prior_price) + Decimal('6.00')) / (prior_qty + 2)
    assert st.filled_qty == prior_qty + 2
    assert st.entry_fill == expected
    assert st.entry_order_notional == Decimal('6.00')
    ent._order_seen.clear()  # persisted checkpoints, as on restart
    ent._order_notional.clear()
    ent._merge_order_total(sid, 2, Decimal('3.00'), 'Submitted')
    assert st.entry_fill == expected
    assert st.filled_qty == prior_qty + 2
    assert ent.events_path.read_text().count('entry_fill_revised') == 1


@pytest.mark.parametrize('prior_qty,prior_price', [(0, '0'), (1, '1.00')])
def test_exit_revision_blends_current_order_not_whole_book(world, prior_qty, prior_price):
    fake = FakeIbkr()
    mon = _monitor(world.root, fake, world.now)
    spec = mon.plan.structures[0]
    sid = spec.id
    st = mon.book.structures[sid]
    st.filled_qty = spec.quantity
    st.exit_filled_qty = prior_qty
    st.exit_fill = Decimal(prior_price) if prior_qty else None
    ref = fake.place_combo(spec, 'SELL', spec.quantity - prior_qty, Decimal('2'))
    mon.orders[sid] = ref
    fake.fill(ref, 2, '2.00')
    mon._drain_orders()
    fake.fill(ref, 2, '3.00')
    assert mon._drain_orders() is True  # price-only mutations must report changed
    expected = (prior_qty * Decimal(prior_price) + Decimal('6')) / (prior_qty + 2)
    # Averaging repeating Decimals can differ by one context ULP; compare
    # far below a monetary tick, not via a binary float.
    assert abs(st.exit_fill - expected) < Decimal("1e-24")
    assert st.exit_filled_qty == prior_qty + 2
    assert st.exit_order_notional == Decimal('6.00')
    assert mon._drain_orders() is False
    # Averaging repeating Decimals can differ by one context ULP; compare
    # far below a monetary tick, not via a binary float.
    assert abs(st.exit_fill - expected) < Decimal("1e-24")
    assert (mon.run_dir / 'events.jsonl').read_text().count('exit_fill_revised') == 1


def test_entry_price_revision_survives_the_next_book_reload(world):
    """The enter loop reloads book.json every cycle (85f68da): a nonterminal
    price revision that is not persisted is wiped by that reload and then
    re-applied (duplicate events) or lost across a restart. Codex round 1,
    finding 3."""
    fake = FakeEntryIbkr()
    ent = _enterer(world.root, fake, _at(10, 0))
    ent._tick()  # BUY placed
    sid = ent.plan.structures[0].id
    a = ent.orders[sid]

    fake.fill_partial(a, 2, '0.44')
    ent._tick()
    assert ent.book.structures[sid].entry_fill == Decimal('0.44')

    fake.fill_partial(a, 2, '0.50')  # same fills, revised average
    ent._tick()

    disk = BookState.load(ent.run_dir / 'book.json', [sid]).structures[sid]
    assert disk.entry_fill == Decimal('0.50')
    assert disk.entry_order_notional == Decimal('0.50') * 2

    fake.fill_partial(a, 2, '0.50')  # broker re-reports; must stay idempotent
    ent._tick()
    events = [json.loads(line) for line in ent.events_path.read_text().splitlines()]
    assert sum(e.get('event') == 'entry_fill_revised' for e in events) == 1
