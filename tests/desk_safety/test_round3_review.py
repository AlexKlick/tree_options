"""Round-3 audit regressions (R3-01/02/03), named after the audit kit so the
red->green receipts line up with its verification JSON.

R3-01  terminal entry publication is a durable boundary: a failed save on the
       OPEN/CLOSED transition must not end run() with rc 0 while the disk is
       still in the entry lane, and the working-order reference must survive
       for the retry.
R3-02  price coverage propagates: book -> view -> API -> calculators -> UI.
       An unknown cost is not an absent position and a priced-subset average
       is never silently multiplied into a whole-position number.
R3-03  a full fill during a cancel wait resolves flat; no zero-size SELL ever
       reaches the adapter; a negative remainder is a fault, not a close.
"""
import json
from datetime import datetime
from decimal import Decimal

import pytest
from tests.unit.test_trex_enter import FakeEntryIbkr, _at, _enterer, _plan
from tests.unit.test_trex_monitor import FakeIbkr, _monitor

from tree_options.trex.enter import Enterer
from tree_options.trex.plan import PutSpread, TradePlan
from tree_options.trex.state import BookState, Status
from tree_options.trex_web.app import _marks_payload
from tree_options.trex_web.portfolio import plan_realized, plan_unrealized, portfolio_rollup
from tree_options.trex_web.positions import net_positions
from tree_options.trex_web.reader import StructureView


def _disk(ent: Enterer) -> object:
    sid = ent.plan.structures[0].id
    return BookState.load(ent.run_dir / 'book.json', [sid]).structures[sid]


def _fail_first_transition(monkeypatch, status: Status) -> None:
    """Raise OSError from BookState.save_owned exactly once, on the first save
    that would publish ``status`` — i.e. the terminal publication itself."""
    raised = []
    real = BookState.save_owned

    def flaky(self, path, theirs):
        if any(st.status is status for st in self.structures.values()) and not raised:
            raised.append(1)
            raise OSError('disk full at the terminal boundary')
        return real(self, path, theirs)

    monkeypatch.setattr(BookState, 'save_owned', flaky)


# -- R3-01: terminal publication is a durable boundary -----------------------


def test_r3_terminal_entry_save_failure_does_not_report_success(world, monkeypatch):
    fake = FakeEntryIbkr()
    ent = _enterer(world.root, fake, _at(10, 0))
    ent._tick()  # BUY 5 placed
    sid = ent.plan.structures[0].id
    fake.fill_partial(ent.orders[sid], 2, '0.44')
    ent._tick()  # durable: 2 @ 0.44, ENTER_WORKING

    _fail_first_transition(monkeypatch, Status.OPEN)
    fake.fill(ent.orders[sid], 5, '0.40')  # broker: all five, cumulative 0.40
    rc = ent.run()

    disk = _disk(ent)
    assert rc == 0
    assert disk.status is Status.OPEN  # the handoff IS durable, not just reported
    assert disk.filled_qty == 5
    assert disk.entry_fill == Decimal('0.40')
    assert fake.placed == [(sid, 'BUY', 5, fake.placed[0][3])]  # no over-order retry


def test_r3_terminal_cancel_with_partial_save_failure_settles_durable(world, monkeypatch):
    fake = FakeEntryIbkr()
    ent = _enterer(world.root, fake, _at(10, 0))
    ent._tick()
    fake.fill_partial(ent.orders[ent.plan.structures[0].id], 2, '0.44')
    ent._tick()
    fake.evidence = (2, Decimal('0.44'))  # broker evidence for the popped ref
    (ent.run_dir / 'FLATTEN').write_text('')

    _fail_first_transition(monkeypatch, Status.OPEN)
    rc = ent.run()

    disk = _disk(ent)
    assert rc == 0
    assert disk.status is Status.OPEN
    assert disk.filled_qty == 2
    assert disk.entry_fill == Decimal('0.44')


def test_r3_terminal_save_failure_multiple_structures_recovers_each(world, monkeypatch):
    spread_a = _plan().structures[0]
    spread_b = PutSpread(
        id='nvda-nov', underlying='NVDA', entry_date=spread_a.entry_date,
        expiry=spread_a.expiry, long_strike='185', short_strike='150',
        quantity=5, limit_cap='0.50', exit_deadline=spread_a.exit_deadline,
    )
    plan = TradePlan(id='t', account_mode='paper', structures=[spread_a, spread_b],
                     total_debit_cap='1000', entry_window_start='09:45',
                     entry_window_end='12:00')
    fake = FakeEntryIbkr()
    real_place = fake.place_combo

    def place_and_fill(spread, side, qty, limit):
        ref = real_place(spread, side, qty, limit)
        fake.fill(ref, 5, '0.40' if spread.id == spread_a.id else '0.42')
        return ref

    fake.place_combo = place_and_fill
    run_dir = world.root / 'run'
    run_dir.mkdir(exist_ok=True)
    ent = Enterer(plan, fake, BookState([spread_a.id, spread_b.id]), run_dir,
                  clock=lambda: _at(10, 0))

    _fail_first_transition(monkeypatch, Status.OPEN)
    rc = ent.run()

    disk = BookState.load(run_dir / 'book.json', [spread_a.id, spread_b.id])
    assert rc == 0
    for spread, price in ((spread_a, '0.40'), (spread_b, '0.42')):
        st = disk.structures[spread.id]
        assert st.status is Status.OPEN
        assert st.filled_qty == 5
        assert st.entry_fill == Decimal(price)
        assert sum(1 for sid, s, _q, _l in fake.placed if sid == spread.id) == 1


def test_r3_partial_save_failure_retries_control(world, monkeypatch):
    fake = FakeEntryIbkr()
    ent = _enterer(world.root, fake, _at(10, 0))
    ent._tick()
    sid = ent.plan.structures[0].id
    fake.fill_partial(ent.orders[sid], 2, '0.44')  # fills reported, save pending
    fired = []
    real = BookState.save_owned

    def flaky(self, path, theirs):
        if any(st.filled_qty for st in self.structures.values()) and not fired:
            fired.append(1)
            raise OSError('disk full')
        return real(self, path, theirs)

    monkeypatch.setattr(BookState, 'save_owned', flaky)
    with pytest.raises(OSError):
        ent._tick()
    assert _disk(ent).filled_qty == 0  # nothing half-published

    ent._sync_book_from_disk()  # the real run() loop reloads after a failure
    fake.fill_partial(ent.orders[sid], 2, '0.44')  # broker re-reports
    ent._tick()  # retry merges the same cumulative report
    assert ent.book.structures[sid].filled_qty == 2
    assert _disk(ent).filled_qty == 2


def test_r3_exit_terminal_close_survives_a_failed_save(world, monkeypatch):
    """Exit-side terminal publication, audited separately from entry (the
    monitor has its own final save): a failed close save keeps the order
    reference and retries to a durable CLOSED."""
    fake = FakeIbkr()
    mon = _monitor(world.root, fake, world.now)
    spec = mon.plan.structures[0]
    sid = spec.id
    st = mon.book.structures[sid]
    st.to(Status.ENTER_WORKING, world.now)
    st.to(Status.OPEN, world.now)
    st.filled_qty = 5
    st.to(Status.EXIT_WORKING, world.now)
    ref = fake.place_combo(spec, 'SELL', 5, Decimal('2.00'))
    mon.orders[sid] = ref
    mon._save_book()  # production reached EXIT_WORKING through saves
    fake.fill(ref, 5, '2.00')

    _fail_first_transition(monkeypatch, Status.CLOSED)
    with pytest.raises(OSError):
        mon._drain_orders()
    assert sid in mon.orders  # the reference survives for the retry
    disk = BookState.load(mon.run_dir / 'book.json', [sid]).structures[sid]
    assert disk.status is Status.EXIT_WORKING

    mon._sync_book_from_disk()  # the monitor loop reloads before retrying
    mon._drain_orders()
    assert st.status is Status.CLOSED
    assert sid not in mon.orders
    disk = BookState.load(mon.run_dir / 'book.json', [sid]).structures[sid]
    assert disk.status is Status.CLOSED
    assert (mon.run_dir / 'events.jsonl').read_text().count('"closed"') == 1


# -- R3-03: fills during a cancel wait must never request a zero-size order --


def _working_exit(world, filled: int, price: str):
    fake = FakeIbkr()
    mon = _monitor(world.root, fake, world.now)
    spec = mon.plan.structures[0]
    sid = spec.id
    st = mon.book.structures[sid]
    st.to(Status.ENTER_WORKING, world.now)
    st.to(Status.OPEN, world.now)
    st.filled_qty = 5
    st.to(Status.EXIT_WORKING, world.now)  # production shape: a working SELL
    ref = fake.place_combo(spec, 'SELL', 5, Decimal('2.00'))
    mon.orders[sid] = ref
    # fills land while the cancel is being waited on: quantity moves, the
    # status stays working until the cancel itself confirms
    ref.trade.orderStatus.filled = filled
    ref.trade.orderStatus.avgFillPrice = float(price)
    return fake, mon, spec, sid, st


def test_r3_exit_full_fill_during_cancel_sends_no_zero_replacement(world):
    fake, mon, spec, sid, st = _working_exit(world, filled=5, price='2.00')

    mon._refresh_exit(spec, Decimal('1.90'))  # reprice: cancel, merge, replace

    assert fake.placed == [(sid, 'SELL', 5, Decimal('2.00'))]  # NO SELL 0
    assert st.open_qty == 0
    mon._drain_orders()  # the next tick resolves the flat book
    assert st.status is Status.CLOSED


def test_r3_exit_partial_fill_during_cancel_sizes_remainder_control(world):
    fake, mon, spec, sid, st = _working_exit(world, filled=3, price='2.00')

    mon._refresh_exit(spec, Decimal('1.90'))

    assert fake.placed[-1] == (sid, 'SELL', 2, Decimal('1.90'))
    assert st.open_qty == 2


def test_r3_exit_unconfirmed_cancel_places_no_second_order(world):
    fake, mon, spec, _sid, st = _working_exit(world, filled=0, price='0')

    def stubborn_cancel(r):
        fake.cancelled.append(r.trade.order.orderId)  # status stays Submitted

    fake.cancel = stubborn_cancel
    mon._refresh_exit(spec, Decimal('1.90'))

    assert len(fake.placed) == 1  # the old order keeps working, no second SELL
    assert st.status is Status.EXIT_WORKING


def test_r3_negative_remainder_is_a_reconciliation_fault(world):
    fake, mon, spec, _sid, st = _working_exit(world, filled=0, price='0')
    st.exit_filled_qty = 6  # sold more than held: book/broker disagreement

    with pytest.raises(RuntimeError, match='negative'):
        mon._place_exit(spec, -1, Decimal('1.90'))

    assert len(fake.placed) == 1  # nothing sent to the adapter
    assert 'exit_reconciliation_fault' in (mon.run_dir / 'events.jsonl').read_text()


# -- R3-02: coverage propagates through the read model -----------------------


def _view(entry='0.30', filled=5, open_qty=5, unpriced=2,
          exit_fill=None, exit_filled=0, exit_unpriced=0):
    return StructureView(
        state=Status.OPEN,
        entry_fill=Decimal(entry) if entry is not None else None,
        filled_qty=filled, open_qty=open_qty,
        exit_fill=Decimal(exit_fill) if exit_fill else None,
        exit_filled_qty=exit_filled,
        entry_cycles=0, exit_cycles=0, exit_reason=None, close_reason=None,
        touch_ts=None, updated_at=None,
        entry_unpriced_qty=unpriced, exit_unpriced_qty=exit_unpriced,
    )


class _FakePlanView:
    """plan_unrealized / portfolio_rollup take a PlanView; the helpers only
    touch structures + plan identity."""

    def __init__(self, structures, plan_id='p'):
        self.structures = structures
        self.state_present = True
        self.worst_state = None
        from types import SimpleNamespace

        self.plan = SimpleNamespace(id=plan_id, account_mode='paper',
                                    committed_at_caps='1000')


def _marks(**row):
    return {'ts': '2026-09-25T12:00:00-04:00',
            'structures': {'nvda-oct': {'qty': 5, 'entry': '0.3', 'bid': '1.90',
                                        'ask': '2.10', 'mark': '2.00', **row}},
            'total_unrealized': None}


def _specs(*sids):
    return [{'id': sid, 'underlying': 'NVDA', 'long_strike': 185.0,
             'short_strike': 150.0, 'expiry': '2026-10-16'} for sid in sids]


def test_r3_replacement_priced_average_coverage_control(world):
    """The book layer through real legacy entry methods: 2 fills arrive
    without a price, the order is cancelled, and a replacement fills the
    remaining three at 0.30 — filled 5, two unpriced, 0.30 over the priced
    three."""
    fake = FakeEntryIbkr()
    ent = _enterer(world.root, fake, _at(10, 0))
    ent._tick()  # BUY 5 (order A)
    spec = ent.plan.structures[0]
    sid = spec.id
    a = ent.orders[sid]
    a.trade.orderStatus.filled = 2  # quantity known, no average yet
    a.trade.orderStatus.avgFillPrice = 0.0
    ent._tick()
    assert ent.book.structures[sid].entry_unpriced_qty == 2

    ent._reprice(spec, Decimal('0.32'))  # cancel A, place the replacement B
    b = ent.orders[sid]
    assert b.trade.order.orderId != a.trade.order.orderId
    fake.fill(b, 3, '0.30')
    ent._tick()

    st = ent.book.structures[sid]
    assert st.filled_qty == 5
    assert st.entry_unpriced_qty == 2
    assert st.entry_fill == Decimal('0.30')  # 3 x 0.30 priced; NOT 1.50/5


def test_r3_plan_unrealized_must_not_reinflate_partial_average(world):
    view = _FakePlanView({'nvda-oct': _view()})
    # a stale mark row written while coverage was complete (total 850)
    marks = _marks(total='850.00')

    u_open, u_filled = plan_unrealized(view, marks)

    assert u_open is None  # not (2.00 - 0.30) * 5 * 100
    assert u_filled is None  # a stale complete total proves nothing now


def test_r3_portfolio_committed_is_not_a_fabricated_full_position_cost(world):
    view = _FakePlanView({'nvda-oct': _view()})

    rollup = portfolio_rollup([(view, _marks())])

    assert rollup['committed_filled'] is None  # not 0.30 * 5 * 100
    assert rollup['committed_known'] == pytest.approx(90.0)  # labelled subtotal
    assert rollup['cost_unknown'] is True
    assert rollup['unpriced_qty'] == 2


def test_r3_marks_api_preserves_unpriced_disclosure():
    payload = _marks_payload(_marks(unpriced=2))

    assert payload['structures']['nvda-oct']['unpriced'] == 2


def test_r3_unpriced_open_exposure_remains_visible():
    """Every entry price unknown: the row stays (exposure is known), with
    entry-derived numbers unknown — it must not be filtered away."""
    states = {'nvda-oct': {'open_qty': 5, 'entry_fill': None, 'filled_qty': 5,
                           'entry_unpriced_qty': 5}}

    rows = net_positions(_specs('nvda-oct'), states, None)

    assert len(rows) == 1
    row = rows[0]
    assert row['open_qty'] == 5
    assert row['committed'] is None
    assert row['max_loss'] is None
    assert row['max_gain'] is None
    assert row['avg_entry'] is None
    assert row['unpriced_qty'] == 5
    assert row['legs'][0]['structure_id'] == 'nvda-oct'


def test_r3_net_positions_cannot_label_partial_cost_as_max_loss():
    states = {'nvda-oct': {'open_qty': 5, 'entry_fill': 0.3, 'filled_qty': 5,
                           'entry_unpriced_qty': 2}}
    marks = {'nvda-oct': {'bid': 1.9, 'ask': 2.1, 'entry': 0.3, 'mark': 2.0}}

    rows = net_positions(_specs('nvda-oct'), states, marks)

    row = rows[0]
    assert row['max_loss'] is None  # not -150.0
    assert row['committed'] is None
    assert row['committed_known'] == pytest.approx(90.0)
    assert row['unpriced_qty'] == 2
    assert row['open_qty'] == 5
    assert row['legs'][0]['entry'] == 0.3  # the priced-subset average stays visible
    assert row['unrealized'] is None  # not 850.0


def test_r3_view_realized_incomplete_cost_control(world):
    view = _view(entry='0.30', exit_fill='2.00', exit_filled=5, exit_unpriced=2)

    assert view.realized_pnl is None
    realized, _partial = plan_realized(_FakePlanView({'nvda-oct': view}))
    assert realized is None


def test_r3_complete_cost_read_model_control(world):
    """Coverage complete: today's numbers are exactly today's numbers."""
    view = _FakePlanView({'nvda-oct': _view(unpriced=0)})
    marks = _marks()

    u_open, _u_filled = plan_unrealized(view, marks)
    rollup = portfolio_rollup([(view, marks)])
    row = net_positions(_specs('nvda-oct'),
                        {'nvda-oct': {'open_qty': 5, 'entry_fill': 0.3,
                                      'filled_qty': 5, 'entry_unpriced_qty': 0}},
                        {'nvda-oct': {'bid': 1.9, 'ask': 2.1, 'entry': 0.3}})[0]

    assert u_open == 850.0
    assert rollup['committed_filled'] == 150.0
    assert not rollup.get('cost_unknown')  # falsy before the fix, False after
    assert row['committed'] == 150.0
    assert row['max_loss'] == -150.0
    assert row['unrealized'] == 850.0
    assert row['avg_entry'] == 0.3


def test_r3_mixed_portfolio_discloses_unknown_alongside_known(world):
    """One incomplete + one complete structure: the whole-portfolio number
    becomes unknown, the known subtotal stays labelled, and the buckets
    disclose the unknown instead of burying it in a zero."""
    good = _FakePlanView({'good': _view(entry='0.50', unpriced=0)}, plan_id='good')
    bad = _FakePlanView({'bad': _view(entry='0.30', unpriced=2)}, plan_id='bad')
    marks_good = {'ts': '2026-09-25T12:00:00-04:00',
                  'structures': {'good': {'qty': 5, 'entry': '0.5', 'bid': '1.90',
                                          'ask': '2.10', 'mark': '2.00'}},
                  'total_unrealized': None}

    rollup = portfolio_rollup([(good, marks_good), (bad, None)])

    assert rollup['committed_filled'] is None
    assert rollup['committed_known'] == pytest.approx(250.0 + 90.0)
    assert rollup['cost_unknown'] is True
    assert rollup['unpriced_qty'] == 2

    states = {'good': {'open_qty': 5, 'entry_fill': 0.5, 'filled_qty': 5,
                       'entry_unpriced_qty': 0},
              'bad': {'open_qty': 5, 'entry_fill': 0.3, 'filled_qty': 5,
                      'entry_unpriced_qty': 2}}
    specs = [dict(_specs('good')[0], id='good'), dict(_specs('bad')[0], id='bad')]
    rows = net_positions(specs, states, None)
    assert len(rows) == 1  # same underlying: one merged exposure row
    row = rows[0]
    assert row['open_qty'] == 10  # both exposures stay visible
    assert row['committed'] is None  # one unknown leg -> whole row unknown
    assert row['committed_known'] == pytest.approx(340.0)  # 250 + 90, labelled
    assert row['unpriced_qty'] == 2
    assert len(row['legs']) == 2


# -- HTTP surface ------------------------------------------------------------


PLAN_TOML = """\
id = "putspread-test"
account_mode = "paper"
total_debit_cap = 1840.00
entry_window_start = "09:45"
entry_window_end = "12:00"

[[structures]]
id = "nvda-oct"
underlying = "NVDA"
entry_date = 2026-09-18
expiry = 2026-10-16
long_strike = 185.0
short_strike = 150.0
quantity = 5
limit_cap = 0.50
exit_deadline = 2026-10-09

[[structures]]
id = "qqq-nov"
underlying = "QQQ"
entry_date = 2026-09-18
expiry = 2026-11-20
long_strike = 600.0
short_strike = 475.0
quantity = 4
limit_cap = 2.40
exit_deadline = 2026-11-06
"""


def _http_world(tmp_path, nvda_unpriced=2):
    """Private state dir: nvda-oct carries the partial-coverage book, qqq-nov
    a complete one (the mixed case), plus a live marks file."""
    from fastapi.testclient import TestClient

    from tree_options.trex.clock import ET

    plans_root = tmp_path / 'plans'
    state_root = tmp_path / 'state'
    plans_root.mkdir(parents=True)
    run = state_root / 'putspread-test'
    run.mkdir(parents=True)
    (plans_root / 'putspread-test.toml').write_text(PLAN_TOML)

    now = datetime(2026, 9, 18, 10, 0, tzinfo=ET)
    book = BookState(['nvda-oct', 'qqq-nov'])
    nvda = book.structures['nvda-oct']
    nvda.to(Status.ENTER_WORKING, now)
    nvda.to(Status.OPEN, now)
    nvda.filled_qty = 5
    nvda.entry_fill = Decimal('0.30')
    nvda.entry_unpriced_qty = nvda_unpriced
    qqq = book.structures['qqq-nov']
    qqq.to(Status.ENTER_WORKING, now)
    qqq.to(Status.OPEN, now)
    qqq.filled_qty = 4
    qqq.entry_fill = Decimal('1.00')
    book.save(run / 'book.json')
    marks = {
        'ts': now.isoformat(),
        'spots': {'NVDA': 180.0, 'QQQ': 580.0},
        'structures': {
            'nvda-oct': {'qty': 5, 'entry': '0.3', 'bid': '1.90', 'ask': '2.10',
                         'mark': '2.00', 'unpriced': nvda_unpriced},
            'qqq-nov': {'qty': 4, 'entry': '1.0', 'bid': '5.90', 'ask': '6.10',
                        'mark': '6.00'},
        },
        'total_unrealized': None,
    }
    (run / 'marks.json').write_text(json.dumps(marks))

    from tree_options.trex_web.app import create_app

    client = TestClient(create_app(state_dir=str(state_root),
                                   plans_dir=str(plans_root),
                                   discovery_dir=str(tmp_path / 'discovery')))
    return client


def test_r3_http_plan_detail_exposes_cost_coverage_and_no_false_payoff(tmp_path):
    client = _http_world(tmp_path)

    detail = client.get('/api/plans/putspread-test').json()

    nvda = detail['structures']['nvda-oct']
    assert nvda['entry_unpriced_qty'] == 2  # coverage disclosed, not dropped
    assert nvda['entry_fill'] == 0.3
    assert 'unpriced' in detail['marks']['structures']['nvda-oct']
    # the partial average must not become a payoff/max-loss label
    assert [p['structure_id'] for p in detail['payoffs']] == ['qqq-nov']
    assert detail['book_summary']['committed'] == 400.0  # qqq only, not +150
    np_row = next(r for r in detail['net_positions']
                  if r['legs'][0]['structure_id'] == 'nvda-oct')
    assert np_row['max_loss'] is None
    assert np_row['unpriced_qty'] == 2
    assert np_row['open_qty'] == 5  # an unknown cost is not an absent position

    index = client.get('/api/plans').json()
    assert index['portfolio']['committed_filled'] is None  # mixed -> unknown
    assert index['portfolio']['committed_known'] == pytest.approx(490.0)  # 90+400
    plan = next(p for p in index['plans'] if p['id'] == 'putspread-test')
    assert plan['unrealized_open'] is None  # one incomplete poisons the plan total
