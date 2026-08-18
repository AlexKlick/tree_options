"""Fill-engine integrity (review round 2): F6 stream selection, F7 early-close
decision, F8 off-tick quotes, F9 fraction validation, F10 multiplier schema,
F12 duplicate order execution."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.fixtures.contracts import standard_call
from tests.fixtures.market import execution_instant, fresh_quote
from tree_options.guards.fills import (
    ExecutionStress,
    FillEngine,
    FillRejection,
)
from tree_options.protocol.loader import load_protocol
from tree_options.schemas.market import NonTickQuoteError, StaleQuoteError, as_tradable
from tree_options.schemas.options import DeliverableSpec, OptionContract
from tree_options.schemas.trading import Order
from tree_options.time.sessions import session_close_instant

CONTRACT_ID = "OPT-C-2024-06-21-50"


def july_contract():
    """Same standard call, alive through the July early-close fixtures."""
    return standard_call(expiration=date(2024, 12, 20))


class TestEarlyCloseDecisionBoundary:
    """F7: decision_at must equal the CALENDAR's close (13:00 ET on 2024-07-03)."""

    def test_correct_early_close_decision_accepted(self, static_calendar):
        protocol = load_protocol()
        engine = FillEngine.from_protocol(static_calendar, protocol)
        decision_session = date(2024, 7, 3)
        exec_session = static_calendar.nth_after(decision_session, 1)
        at = execution_instant(static_calendar.session_open(exec_session))
        order = Order(
            order_id="ORD-E1",
            contract_id=CONTRACT_ID,
            side="buy",
            intent="open_long",
            quantity=1,
            decision_at=static_calendar.session_close(decision_session),  # 17:00 UTC
            decision_session=decision_session,
        )
        fill = engine.execute(
            order,
            fresh_quote(execution_at=at),
            july_contract(),
            execution_session=exec_session,
            execution_at=at,
        )
        assert fill.price == Decimal("1.10")

    def test_regular_close_on_early_close_session_rejected(self, static_calendar):
        """A 20:00 UTC decision stamp on July 3 is three hours AFTER the real
        session ended — post-close information cannot ride in."""
        protocol = load_protocol()
        engine = FillEngine.from_protocol(static_calendar, protocol)
        decision_session = date(2024, 7, 3)
        exec_session = static_calendar.nth_after(decision_session, 1)
        at = execution_instant(static_calendar.session_open(exec_session))
        order = Order(
            order_id="ORD-E2",
            contract_id=CONTRACT_ID,
            side="buy",
            intent="open_long",
            quantity=1,
            decision_at=session_close_instant(decision_session),  # WRONG: 20:00 UTC
            decision_session=decision_session,
        )
        with pytest.raises(FillRejection) as ei:
            engine.execute(
                order,
                fresh_quote(execution_at=at),
                july_contract(),
                execution_session=exec_session,
                execution_at=at,
            )
        assert ei.value.code == "DECISION_INSTANT_NOT_CLOSE"


class TestOffTickQuotes:
    def test_off_tick_ask_rejected(self, static_calendar):
        at = execution_instant(static_calendar.session_open(date(2024, 7, 5)))
        q = fresh_quote(bid="1.00", ask="1.109", execution_at=at)
        with pytest.raises(NonTickQuoteError):
            as_tradable(q, execution_at=at)

    def test_off_tick_bid_rejected(self, static_calendar):
        at = execution_instant(static_calendar.session_open(date(2024, 7, 5)))
        q = fresh_quote(bid="1.005", ask="1.10", execution_at=at)
        with pytest.raises(NonTickQuoteError):
            as_tradable(q, execution_at=at)

    def test_tick_aligned_quote_accepted(self, static_calendar):
        at = execution_instant(static_calendar.session_open(date(2024, 7, 5)))
        q = fresh_quote(bid="1.00", ask="1.10", execution_at=at)
        tq = as_tradable(q, execution_at=at)
        assert tq.ask == Decimal("1.10")


class TestQuoteStreamSelection:
    """F6: the engine selects from the stream at the effective instant — an
    older favorable quote cannot be cherry-picked when a newer adverse one
    exists; latency shifts the selection instant."""

    def _order(self, static_calendar, decision_session):
        return Order(
            order_id="ORD-Q1",
            contract_id=CONTRACT_ID,
            side="buy",
            intent="open_long",
            quantity=1,
            decision_at=static_calendar.session_close(decision_session),
            decision_session=decision_session,
        )

    def test_engine_uses_latest_quote_not_most_favorable(self, static_calendar):
        protocol = load_protocol()
        engine = FillEngine.from_protocol(static_calendar, protocol)
        decision = date(2024, 7, 3)
        exec_session = static_calendar.nth_after(decision, 1)
        at = execution_instant(static_calendar.session_open(exec_session))
        favorable_old = fresh_quote(
            bid="1.00", ask="1.02", execution_at=at, received_offset_seconds=500
        )
        adverse_new = fresh_quote(
            bid="1.00", ask="1.20", execution_at=at, received_offset_seconds=10
        )
        fill = engine.execute(
            self._order(static_calendar, decision),
            [favorable_old, adverse_new],
            july_contract(),
            execution_session=exec_session,
            execution_at=at,
        )
        assert fill.price == Decimal("1.20")  # the NEWEST quote, not the cheapest

    def test_latency_application_changes_outcome(self, static_calendar):
        """Kill-test for latency: with one 300s-old quote, +700s latency makes
        the effective instant 1000s past receipt -> stale -> rejected. If
        latency were not applied, this fill would succeed."""
        protocol = load_protocol()
        engine = FillEngine.from_protocol(static_calendar, protocol)
        decision = date(2024, 7, 3)
        exec_session = static_calendar.nth_after(decision, 1)
        at = execution_instant(static_calendar.session_open(exec_session))
        q = fresh_quote(bid="1.00", ask="1.10", execution_at=at, received_offset_seconds=300)
        with pytest.raises((StaleQuoteError, FillRejection)):
            engine.execute(
                self._order(static_calendar, decision),
                [q],
                july_contract(),
                execution_session=exec_session,
                execution_at=at,
                stress=ExecutionStress(latency_seconds=700),
            )


class TestEngineConstruction:
    def test_fill_size_fraction_validated(self, static_calendar):
        with pytest.raises(ValueError, match="fill_size_fraction"):
            FillEngine(static_calendar, fill_size_fraction=Decimal("2"))

    def test_zero_fraction_rejected(self, static_calendar):
        with pytest.raises(ValueError, match="fill_size_fraction"):
            FillEngine(static_calendar, fill_size_fraction=Decimal("0"))


class TestStandardContractMultiplier:
    def test_standard_contract_requires_multiplier_100(self):
        with pytest.raises(ValidationError, match="multiplier"):
            OptionContract(
                contract_id="OPT-C-BAD-MULT",
                option_root="OPT",
                underlying_security_id="SEC-001",
                expiration=date(2024, 6, 21),
                strike=Decimal("50.00"),
                call_put="C",
                multiplier=1,  # 100-share deliverable but multiplier 1 -> $ vs 100x lie
                listing_start=date(2024, 1, 2),
                listing_end=date(2024, 6, 21),
                deliverable=DeliverableSpec(shares_per_contract=Decimal("100")),
                standard_contract_flag=True,
            )

    def test_nonstandard_multiplier_may_differ(self):
        from tests.fixtures.contracts import split_adjusted_contract

        contract, _ = split_adjusted_contract()
        assert contract.multiplier == 100  # fixture stays valid under the new rule


class TestDuplicateOrderExecution:
    def test_same_order_cannot_mint_two_fills(self, synthetic_calendar):
        cal = synthetic_calendar
        d = next(x for x in cal.sessions() if x >= date(2024, 4, 1))
        ex = cal.nth_after(d, 1)
        at = execution_instant(cal.session_open(ex))
        engine = FillEngine.from_protocol(cal, load_protocol())
        order = Order(
            order_id="ORD-DUP",
            contract_id=CONTRACT_ID,
            side="buy",
            intent="open_long",
            quantity=1,
            decision_at=cal.session_close(d),
            decision_session=d,
        )
        q = fresh_quote(execution_at=at)
        engine.execute(order, [q], july_contract(), execution_session=ex, execution_at=at)
        with pytest.raises(FillRejection) as ei:
            engine.execute(order, [q], july_contract(), execution_session=ex, execution_at=at)
        assert ei.value.code == "DUPLICATE_ORDER_EXECUTION"

    def test_explicit_partial_sequence_allowed(self, synthetic_calendar):
        """A deliberate partial-fill continuation must say so explicitly."""
        cal = synthetic_calendar
        d = next(x for x in cal.sessions() if x >= date(2024, 4, 1))
        ex = cal.nth_after(d, 1)
        ex2 = cal.nth_after(d, 2)
        at = execution_instant(cal.session_open(ex))
        at2 = execution_instant(cal.session_open(ex2))
        engine = FillEngine.from_protocol(cal, load_protocol())
        order = Order(
            order_id="ORD-PART",
            contract_id=CONTRACT_ID,
            side="buy",
            intent="open_long",
            quantity=5,
            decision_at=cal.session_close(d),
            decision_session=d,
        )
        f1 = engine.execute(
            order,
            [fresh_quote(execution_at=at)],
            july_contract(),
            execution_session=ex,
            execution_at=at,
        )
        f2 = engine.execute(
            order,
            [fresh_quote(execution_at=at2)],
            july_contract(),
            execution_session=ex2,
            execution_at=at2,
            partial_sequence=True,
        )
        assert f1.fill_id != f2.fill_id
