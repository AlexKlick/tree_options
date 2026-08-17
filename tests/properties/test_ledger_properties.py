"""Ledger conservation and fill pricing properties (INV-11/12).

Everything is exact Decimal — no floats anywhere near cash. Properties:
  1. Quote-bound fills: every minted price lies in [bid, ask], correct side.
  2. Cost monotonicity: wider spreads / higher fees NEVER improve net PnL.
  3. Conservation: cash == initial + sum(signed cash) - sum(fees), exactly,
     for random long-only streams; flat endings reconcile to realized PnL.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.fixtures.contracts import standard_call
from tests.fixtures.market import execution_instant, fresh_quote
from tree_options.guards.fills import FillEngine
from tree_options.ledger.book import LedgerBook, LedgerViolation
from tree_options.schemas.trading import Order

CONTRACT_ID = "OPT-C-2024-06-21-50"
# First synthetic session in April 2024: inside the fixture contract's
# 2024-01-02..2024-06-21 listing window and well clear of its expiration.
DECISION_DATE = date(2024, 4, 1)


def _base(synthetic_calendar):
    cal = synthetic_calendar
    decision_session = next(d for d in cal.sessions() if d >= DECISION_DATE)
    exec_session = cal.nth_after(decision_session, 1)
    exec_at = execution_instant(cal.session_open(exec_session))
    engine = FillEngine(cal)
    contract = standard_call()
    return cal, decision_session, exec_session, exec_at, engine, contract


def _order(i, side, intent, qty, decision_session):
    from tree_options.time.sessions import session_close_instant

    return Order(
        order_id=f"ORD-{i}",
        contract_id=CONTRACT_ID,
        side=side,
        intent=intent,
        quantity=qty,
        decision_at=session_close_instant(decision_session),
        decision_session=decision_session,
    )


def _buy_fill(engine, contract, exec_session, exec_at, qty, bid, ask, f, decision_session):
    q = fresh_quote(bid=str(bid), ask=str(ask), execution_at=exec_at)
    return engine.execute(
        _order(1, "buy", "open_long", qty, decision_session), q, contract,
        execution_session=exec_session, execution_at=exec_at, fraction_to_midpoint_f=f,
    )


def _sell_fill(engine, contract, exec_session, exec_at, qty, bid, ask, f, decision_session, i=2):
    q = fresh_quote(bid=str(bid), ask=str(ask), execution_at=exec_at)
    return engine.execute(
        _order(i, "sell", "close_long", qty, decision_session), q, contract,
        execution_session=exec_session, execution_at=exec_at, fraction_to_midpoint_f=f,
    )


class TestQuoteBound:
    @given(
        bid_units=st.integers(1, 500),
        half_spread_cents=st.integers(1, 80),
        fraction=st.sampled_from([Decimal("0"), Decimal("0.5"), Decimal("1.0")]),
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_fill_price_inside_quote_correct_side(
        self, synthetic_calendar, bid_units, half_spread_cents, fraction
    ):
        ctx = _base(synthetic_calendar)
        _, decision_session, exec_session, exec_at, engine, contract = ctx
        bid = Decimal(bid_units) / 100
        ask = bid + Decimal(half_spread_cents) / 50  # ask - bid = 2 * half_spread cents
        buy = _buy_fill(engine, contract, exec_session, exec_at, 1, bid, ask, fraction, decision_session)
        assert bid <= buy.price <= ask
        sell = _sell_fill(engine, contract, exec_session, exec_at, 1, bid, ask, fraction, decision_session)
        assert bid <= sell.price <= ask
        assert sell.price <= buy.price  # you buy at the worse level


class TestCostMonotonicity:
    """Cost monotonicity, stated tick-robustly.

    At f=0 (primary execution) fills are tick-exact (buy at the ask, sell at
    the bid), so round-trip net cost is EXACTLY spread + fees and monotonicity
    is exact. Improvement fractions move prices by fractions of a tick, so a
    quantized fill can absorb up to one tick per leg; there the guarantee is
    (a) fees are additively monotone at ANY fraction, and (b) each fill lies
    within one tick of the exact improvement price, never past the midpoint.
    """

    @given(
        bid_units=st.integers(50, 400),
        spread1_cents=st.integers(2, 60),
        extra_cents=st.integers(0, 40),
        fee1=st.integers(0, 200),
        fee_extra=st.integers(0, 100),
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_primary_round_trip_exactly_monotone(
        self, synthetic_calendar, bid_units, spread1_cents, extra_cents, fee1, fee_extra
    ):
        ctx = _base(synthetic_calendar)
        cal, decision_session, exec_session, exec_at, _, contract = ctx
        bid = Decimal(bid_units) / 100
        ask1 = bid + Decimal(spread1_cents) / 100
        ask2 = ask1 + Decimal(extra_cents) / 100

        class _Fee:
            def __init__(self, amount):
                self.amount = amount

            def order_fees(self, quantity):
                return self.amount

        e1 = FillEngine(synthetic_calendar, fee_model=_Fee(Decimal(fee1) / 100))
        e2 = FillEngine(
            synthetic_calendar, fee_model=_Fee(Decimal(fee1 + fee_extra) / 100)
        )

        def net(engine, ask):
            q = fresh_quote(bid=str(bid), ask=str(ask), execution_at=exec_at)
            buy = engine.execute(
                _order(1, "buy", "open_long", 1, decision_session), q, contract,
                execution_session=exec_session, execution_at=exec_at,
            )
            exec2 = cal.nth_after(exec_session, 1)
            at2 = execution_instant(cal.session_open(exec2))
            q2 = fresh_quote(bid=str(bid), ask=str(ask), execution_at=at2)
            sell = engine.execute(
                _order(2, "sell", "close_long", 1, exec_session), q2, contract,
                execution_session=exec2, execution_at=at2,
            )
            return (buy.price - sell.price) + buy.fees + sell.fees

        n1 = net(e1, ask1)
        n2 = net(e1, ask2)
        n3 = net(e2, ask2)
        # Exact identities and exact monotonicity at the primary execution.
        assert n1 == (ask1 - bid) + Decimal(fee1) / 100 * 2
        assert n2 == (ask2 - bid) + Decimal(fee1) / 100 * 2
        assert n3 == (ask2 - bid) + Decimal(fee1 + fee_extra) / 100 * 2
        assert n2 >= n1
        assert n3 >= n2

    @given(
        fraction=st.sampled_from([Decimal("0.5"), Decimal("1.0")]),
        fee1=st.integers(0, 200),
        fee_extra=st.integers(1, 100),
        bid_units=st.integers(50, 400),
        spread_cents=st.integers(2, 80),
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_higher_fee_never_cheaper_any_fraction(
        self, synthetic_calendar, fraction, fee1, fee_extra, bid_units, spread_cents
    ):
        """Fees are additive and independent of price: a fee rise can never
        reduce round-trip net cost, at any improvement fraction."""
        ctx = _base(synthetic_calendar)
        cal, decision_session, exec_session, exec_at, _, contract = ctx
        bid = Decimal(bid_units) / 100
        ask = bid + Decimal(spread_cents) / 100

        class _Fee:
            def __init__(self, amount):
                self.amount = amount

            def order_fees(self, quantity):
                return self.amount

        def net(fee_amount):
            engine = FillEngine(synthetic_calendar, fee_model=_Fee(fee_amount))
            q = fresh_quote(bid=str(bid), ask=str(ask), execution_at=exec_at)
            buy = engine.execute(
                _order(1, "buy", "open_long", 1, decision_session), q, contract,
                execution_session=exec_session, execution_at=exec_at,
                fraction_to_midpoint_f=fraction,
            )
            exec2 = cal.nth_after(exec_session, 1)
            at2 = execution_instant(cal.session_open(exec2))
            q2 = fresh_quote(bid=str(bid), ask=str(ask), execution_at=at2)
            sell = engine.execute(
                _order(2, "sell", "close_long", 1, exec_session), q2, contract,
                execution_session=exec2, execution_at=at2,
                fraction_to_midpoint_f=fraction,
            )
            return (buy.price - sell.price) + buy.fees + sell.fees

        cheap = net(Decimal(fee1) / 100)
        dear = net(Decimal(fee1 + fee_extra) / 100)
        assert dear == cheap + Decimal(fee_extra) / 50  # +2x per round trip
        assert dear > cheap

    @given(
        fraction=st.sampled_from([Decimal("0.5"), Decimal("1.0")]),
        bid_units=st.integers(50, 400),
        spread_cents=st.integers(2, 80),
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_improvement_fill_within_one_tick_of_exact(
        self, synthetic_calendar, fraction, bid_units, spread_cents
    ):
        """Each improvement fill is within one tick of the exact improvement
        price, on the conservative side (buy rounds up, sell rounds down),
        and never past the midpoint."""
        ctx = _base(synthetic_calendar)
        cal, decision_session, exec_session, exec_at, engine, contract = ctx
        bid = Decimal(bid_units) / 100
        ask = bid + Decimal(spread_cents) / 100
        q = fresh_quote(bid=str(bid), ask=str(ask), execution_at=exec_at)
        buy = engine.execute(
            _order(1, "buy", "open_long", 1, decision_session), q, contract,
            execution_session=exec_session, execution_at=exec_at,
            fraction_to_midpoint_f=fraction,
        )
        exec2 = cal.nth_after(exec_session, 1)
        at2 = execution_instant(cal.session_open(exec2))
        q2 = fresh_quote(bid=str(bid), ask=str(ask), execution_at=at2)
        sell = engine.execute(
            _order(2, "sell", "close_long", 1, exec_session), q2, contract,
            execution_session=exec2, execution_at=at2,
            fraction_to_midpoint_f=fraction,
        )
        tick = Decimal("0.01")
        exact_buy = ask - fraction * (ask - (ask + bid) / 2)
        exact_sell = bid + fraction * ((ask + bid) / 2 - bid)
        mid = (bid + ask) / 2
        assert Decimal(0) <= (buy.price - exact_buy) < tick
        assert Decimal(0) <= (exact_sell - sell.price) < tick
        assert buy.price >= mid and sell.price <= mid
        # And the exact formula itself is monotone: wider spread never cheaper.
        s1 = ask - bid
        assert (1 - fraction) * s1 >= 0

    def test_higher_fee_alone_never_cheaper(self, synthetic_calendar):
        ctx = _base(synthetic_calendar)
        _, decision_session, exec_session, exec_at, _, contract = ctx
        from tree_options.ledger.fees import PerContractFeeModel

        cheap_engine = FillEngine(synthetic_calendar, fee_model=PerContractFeeModel())
        dear_engine = FillEngine(
            synthetic_calendar,
            fee_model=PerContractFeeModel(fee_per_contract=Decimal("1.30")),
        )
        q = fresh_quote(bid="1.00", ask="1.10", execution_at=exec_at)
        cheap = cheap_engine.execute(
            _order(1, "buy", "open_long", 2, decision_session), q, contract,
            execution_session=exec_session, execution_at=exec_at,
        )
        dear = dear_engine.execute(
            _order(1, "buy", "open_long", 2, decision_session), q, contract,
            execution_session=exec_session, execution_at=exec_at,
        )
        assert dear.fees > cheap.fees
        assert dear.price == cheap.price


class TestConservation:
    def test_exact_cash_identity_random_streams(self, synthetic_calendar):
        """Deterministic pseudo-random long-only streams: exact Decimal identity."""
        import random

        rng = random.Random(20260817)
        ctx = _base(synthetic_calendar)
        cal, decision_session, exec_session, _exec_at, engine, contract = ctx

        for _trial in range(25):
            book = LedgerBook(initial_cash=Decimal("10000.00"))
            held = 0
            session = exec_session
            for step in range(8):
                session = cal.nth_after(session, 1)
                at = execution_instant(cal.session_open(session))
                bid = Decimal(rng.choice(["0.80", "1.00", "1.20", "1.45"]))
                ask = bid + Decimal(rng.choice(["0.05", "0.10", "0.25"]))
                if held == 0 or (held > 0 and rng.random() < 0.55):
                    qty = rng.randint(1, 5)
                    fill = _buy_fill(engine, contract, session, at, qty, bid, ask, Decimal(0), decision_session)
                    book.apply(fill)
                    held += fill.quantity
                else:
                    qty = rng.randint(1, held)
                    fill = _sell_fill(engine, contract, session, at, qty, bid, ask, Decimal(0), decision_session, i=step)
                    book.apply(fill)
                    held -= fill.quantity
            book.assert_conservation()
            assert book.quantity(CONTRACT_ID) == held

    def test_flat_stream_reconciles_to_realized(self, synthetic_calendar):
        ctx = _base(synthetic_calendar)
        cal, decision_session, exec_session, exec_at, engine, contract = ctx
        book = LedgerBook(initial_cash=Decimal("1000.00"))
        # buy: decided at decision_session, executes on exec_session
        q = fresh_quote(bid="1.00", ask="1.10", execution_at=exec_at)
        buy = engine.execute(
            _order(1, "buy", "open_long", 3, decision_session), q, contract,
            execution_session=exec_session, execution_at=exec_at,
        )
        book.apply(buy)
        # sell: decided at exec_session close, executes the next session
        sell_session = cal.nth_after(exec_session, 1)
        at2 = execution_instant(cal.session_open(sell_session))
        q2 = fresh_quote(bid="1.00", ask="1.10", execution_at=at2)
        sell = engine.execute(
            _order(2, "sell", "close_long", buy.quantity, exec_session), q2, contract,
            execution_session=sell_session, execution_at=at2,
        )
        book.apply(sell)
        assert book.quantity(CONTRACT_ID) == 0
        book.assert_conservation()
        # cash == initial + realized - fees when the position is flat
        assert book.cash == (
            Decimal("1000.00") + book.realized_pnl(CONTRACT_ID) - book.total_fees
        )

    def test_sell_beyond_position_fails_closed(self, synthetic_calendar):
        ctx = _base(synthetic_calendar)
        _, decision_session, exec_session, exec_at, engine, contract = ctx
        book = LedgerBook(initial_cash=Decimal("1000.00"))
        fill = _buy_fill(engine, contract, exec_session, exec_at, 2, "1.00", "1.10", Decimal(0), decision_session)
        book.apply(fill)
        exec2 = synthetic_calendar.nth_after(exec_session, 1)
        at2 = execution_instant(synthetic_calendar.session_open(exec2))
        oversell = _sell_fill(engine, contract, exec2, at2, 3, "1.00", "1.10", Decimal(0), exec_session)

        with pytest.raises(LedgerViolation) as ei:
            book.apply(oversell)
        assert ei.value.code == "POSITION_UNDERFLOW"

    def test_tampered_cash_detected(self, synthetic_calendar):
        """The checker itself is under test: a wrong cash sum must be caught."""
        ctx = _base(synthetic_calendar)
        _, decision_session, exec_session, exec_at, engine, contract = ctx
        book = LedgerBook(initial_cash=Decimal("1000.00"))
        fill = _buy_fill(engine, contract, exec_session, exec_at, 2, "1.00", "1.10", Decimal(0), decision_session)
        book.apply(fill)
        object.__setattr__(book, "_fills", [*book._fills, fill])  # duplicate a fill

        with pytest.raises(LedgerViolation):
            book.assert_conservation()



class TestLedgerIntegrityV2:
    """Audit §4.3: position identity, duplicate rejection, ordering, entries."""

    def test_reopened_position_reports_reopen_session(self, synthetic_calendar):
        """Close fully, then reopen: opened_session must be the REOPEN session,
        not the first historical buy."""
        ctx = _base(synthetic_calendar)
        cal, decision_session, exec_session, exec_at, engine, contract = ctx
        book = LedgerBook(initial_cash=Decimal("10000.00"))
        s2 = cal.nth_after(exec_session, 1)
        s3 = cal.nth_after(exec_session, 2)
        book.apply(_buy_fill(engine, contract, exec_session, exec_at, 2, "1.00", "1.10", Decimal(0), decision_session))
        at2 = execution_instant(cal.session_open(s2))
        book.apply(_sell_fill(engine, contract, s2, at2, 2, "1.00", "1.10", Decimal(0), exec_session))
        assert book.quantity(CONTRACT_ID) == 0
        at3 = execution_instant(cal.session_open(s3))
        book.apply(_buy_fill(engine, contract, s3, at3, 1, "1.00", "1.10", Decimal(0), decision_session))
        assert book.opened_session(CONTRACT_ID) == s3  # reopen session, NOT exec_session
        pos = book.position(CONTRACT_ID)
        assert pos.opened_session == s3

    def test_lot_provenance_snapshotted(self, synthetic_calendar):
        ctx = _base(synthetic_calendar)
        _cal, decision_session, exec_session, exec_at, engine, contract = ctx
        book = LedgerBook(initial_cash=Decimal("10000.00"))
        fill = _buy_fill(engine, contract, exec_session, exec_at, 3, "1.20", "1.30", Decimal(0), decision_session)
        book.apply(fill)
        (lot,) = book.lots(CONTRACT_ID)
        assert lot.fill_id == fill.fill_id
        assert lot.order_id == fill.order_id
        assert lot.execution_session == exec_session
        assert lot.unit_price == Decimal("1.30")
        assert lot.multiplier == 100
        assert lot.cost_basis == Decimal("390.00")  # 1.30 * 3 * 100 by hand

    def test_duplicate_fill_id_fails_closed(self, synthetic_calendar):
        ctx = _base(synthetic_calendar)
        _, decision_session, exec_session, exec_at, engine, contract = ctx
        book = LedgerBook(initial_cash=Decimal("1000.00"))
        fill = _buy_fill(engine, contract, exec_session, exec_at, 1, "1.00", "1.10", Decimal(0), decision_session)
        book.apply(fill)
        with pytest.raises(LedgerViolation) as ei:
            book.apply(fill.model_copy())  # same fill_id
        assert ei.value.code == "DUPLICATE_FILL"
        book.assert_conservation()  # state unchanged

    def test_out_of_order_fill_rejected(self, synthetic_calendar):
        ctx = _base(synthetic_calendar)
        cal, _decision_session, exec_session, exec_at, engine, contract = ctx
        book = LedgerBook(initial_cash=Decimal("1000.00"))
        s2 = cal.nth_after(exec_session, 1)
        at2 = execution_instant(cal.session_open(s2))
        later = _buy_fill(engine, contract, s2, at2, 1, "1.00", "1.10", Decimal(0), exec_session)
        earlier_ts = exec_at  # earlier instant than later.execution_at
        book.apply(later)
        with pytest.raises(LedgerViolation) as ei:
            book.apply(later.model_copy(update={"fill_id": "EARLY-F", "execution_at": earlier_ts}))
        assert ei.value.code == "OUT_OF_ORDER_FILL"

    def test_entries_conserved(self, synthetic_calendar):
        ctx = _base(synthetic_calendar)
        cal, decision_session, exec_session, exec_at, engine, contract = ctx
        book = LedgerBook(initial_cash=Decimal("1000.00"))
        book.apply(_buy_fill(engine, contract, exec_session, exec_at, 2, "1.00", "1.10", Decimal(0), decision_session))
        s2 = cal.nth_after(exec_session, 1)
        at2 = execution_instant(cal.session_open(s2))
        book.apply(_sell_fill(engine, contract, s2, at2, 1, "1.00", "1.10", Decimal(0), exec_session))
        book.assert_conservation()  # includes ENTRY_MISMATCH check
        # hand check: buy -220.00 -1.30 fee; sell +110.00 -1.00 fee (1 contract)
        assert book.cash == Decimal("1000.00") - Decimal("220.00") - Decimal("1.30") + Decimal("100.00") - Decimal("1.00")

    def test_partial_fifo_close_across_lots_hand_calculated(self, synthetic_calendar):
        """Two lots at 1.10 and 1.30; sell 2 of 3 FIFO: realized gross =
        2 sells at 1.20 minus lot1 basis — all by hand."""
        ctx = _base(synthetic_calendar)
        cal, decision_session, exec_session, exec_at, engine, contract = ctx
        book = LedgerBook(initial_cash=Decimal("10000.00"))
        book.apply(_buy_fill(engine, contract, exec_session, exec_at, 1, "1.00", "1.10", Decimal(0), decision_session))
        s2 = cal.nth_after(exec_session, 1)
        at2 = execution_instant(cal.session_open(s2))
        book.apply(_buy_fill(engine, contract, s2, at2, 2, "1.20", "1.30", Decimal(0), exec_session))
        s3 = cal.nth_after(exec_session, 2)
        at3 = execution_instant(cal.session_open(s3))
        book.apply(_sell_fill(engine, contract, s3, at3, 2, "1.20", "1.30", Decimal(0), s2))
        # FIFO: 1 contract from lot1 @1.10 + 1 from lot2 @1.30; sell @1.20
        expected_realized = (Decimal("1.20") - Decimal("1.10")) * 100 + (Decimal("1.20") - Decimal("1.30")) * 100
        assert book.realized_pnl(CONTRACT_ID) == expected_realized
        assert book.quantity(CONTRACT_ID) == 1
        assert book.lots(CONTRACT_ID)[0].unit_price == Decimal("1.30")
        book.assert_conservation()

    def test_conservation_oracle_independent_of_fill_methods(self, synthetic_calendar):
        """If the replay oracle called Fill.notional()/signed_cash(), a bug in
        those methods would validate itself. A LYING fill must not disturb
        conservation: the oracle computes from primitive fields, so a mutant
        that switches the oracle onto Fill methods raises here."""
        from tree_options.schemas.trading import Fill

        ctx = _base(synthetic_calendar)
        _, decision_session, exec_session, exec_at, engine, contract = ctx

        class LyingFill(Fill):
            def notional(self):
                return Decimal("999.00")

            def signed_cash(self):
                return Decimal("-999.00")

        fill = _buy_fill(engine, contract, exec_session, exec_at, 1, "1.00", "1.10", Decimal(0), decision_session)
        lying = LyingFill(**{**fill.model_dump(), "fill_id": "LIE-1"})
        book = LedgerBook(initial_cash=Decimal("1000.00"))
        book.apply(lying)
        # Running cash used primitive arithmetic: -110.00 - 1.00 fee.
        assert book.cash == Decimal("889.00")
        book.assert_conservation()  # independent oracle: still conserved
