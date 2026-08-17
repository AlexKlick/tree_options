"""Fill layer v2 semantics (audit §4.1/4.2/4.4/4.5).

New contract under test, red-first:
  - fraction_to_midpoint: 0.0 = executable edge, 1.0 = midpoint,
    buy = ask - f*(ask - mid), sell = bid + f*(mid - bid); integer
    half-tick arithmetic, conservative rounding.
  - Fill snapshots multiplier + deliverable shares; cash moves by
    price * qty * multiplier exactly; independent hand calculation.
  - ExecutionStress: worse fee / slippage / latency never improves any
    fill or portfolio result (exact, integer ticks).
  - Locked (bid == ask) and nonpositive quotes rejected with distinct codes.
  - Max fill quantity = floor(fill_size_fraction * displayed size) from
    the frozen protocol.
  - Contract must be listed at DECISION time as well as execution time;
    decision_at must equal the decision session close.
  - Quote selection from a stream is monotone in time (later execution
    never reaches back for an earlier quote).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.fixtures.contracts import standard_call
from tests.fixtures.market import execution_instant, fresh_quote
from tree_options.guards.fills import (
    FillEngine,
    FillRejection,
)
from tree_options.guards.fills import (
    fraction_to_midpoint as fmt_price,
)
from tree_options.schemas.market import LockedQuoteError, NonpositiveQuoteError, as_tradable
from tree_options.schemas.trading import Order
from tree_options.time.sessions import session_close_instant

DECISION = None  # filled per-test from the synthetic calendar
CONTRACT_ID = "OPT-C-2024-06-21-50"


def _sessions(synthetic_calendar):
    cal = synthetic_calendar
    d = next(x for x in cal.sessions() if __import__("datetime").date(2024, 4, 1) <= x)
    return cal, d, cal.nth_after(d, 1)


def _order(
    cal,
    decision_session,
    side="buy",
    qty=2,
    order_type="market",
    limit=None,
    contract_id=CONTRACT_ID,
):
    return Order(
        order_id="ORD-1",
        contract_id=contract_id,
        side=side,
        intent="open_long" if side == "buy" else "close_long",
        quantity=qty,
        order_type=order_type,
        limit_price=limit,
        decision_at=session_close_instant(decision_session),
        decision_session=decision_session,
    )


class TestFractionToMidpointArithmetic:
    """Integer half-tick arithmetic on a grid of quotes and fractions."""

    @pytest.mark.parametrize(
        ("bid", "ask", "f", "want_buy", "want_sell"),
        [
            ("1.00", "1.10", "0", "1.10", "1.00"),  # edge (primary)
            ("1.00", "1.10", "1", "1.05", "1.05"),  # exact midpoint
            ("1.00", "1.10", "0.5", "1.08", "1.02"),  # conservative tick rounding
            ("1.00", "1.11", "0.5", "1.09", "1.02"),  # odd spread, mid 1.055
            ("2.30", "2.32", "1", "2.31", "2.31"),
            ("0.50", "0.53", "1", "0.52", "0.51"),  # odd-cent mid: 0.515
        ],
    )
    def test_exact_prices(self, bid, ask, f, want_buy, want_sell):
        assert fmt_price(Decimal(bid), Decimal(ask), "buy", Decimal(f)) == Decimal(want_buy)
        assert fmt_price(Decimal(bid), Decimal(ask), "sell", Decimal(f)) == Decimal(want_sell)

    def test_monotone_toward_midpoint(self):
        """Increasing f moves buy down / sell up, never past the midpoint,
        never outside [bid, ask] — for the whole tick grid."""
        from decimal import Decimal as D

        for bid_cents in range(100, 300, 7):
            for spread_cents in range(1, 25, 3):
                bid = D(bid_cents) / 100
                ask = D(bid_cents + spread_cents) / 100
                mid = (bid + ask) / 2
                prev_buy, prev_sell = None, None
                for f in (D("0"), D("0.25"), D("0.5"), D("0.75"), D("1")):
                    buy = fmt_price(bid, ask, "buy", f)
                    sell = fmt_price(bid, ask, "sell", f)
                    assert bid <= sell <= mid <= buy <= ask
                    if prev_buy is not None:
                        assert buy <= prev_buy and sell >= prev_sell
                    prev_buy, prev_sell = buy, sell


class TestMultiplierSnapshot:
    def test_fill_carries_multiplier_and_deliverable(self, synthetic_calendar):
        cal, decision, ex = _sessions(synthetic_calendar)
        engine = FillEngine.from_protocol(
            cal,
            __import__("tree_options.protocol.loader", fromlist=["load_protocol"]).load_protocol(),
        )
        at = execution_instant(cal.session_open(ex))
        fill = engine.execute(
            _order(cal, decision),
            fresh_quote(bid="1.00", ask="1.10", execution_at=at),
            standard_call(),
            execution_session=ex,
            execution_at=at,
        )
        assert fill.multiplier == 100
        assert fill.deliverable_shares_per_contract == Decimal("100")
        # Independent hand calculation: 2 contracts * $1.10 * 100 + fees(2)=1.30
        assert fill.notional() == Decimal("220.00")
        assert fill.signed_cash() == Decimal("-220.00")
        assert fill.fees == Decimal("1.30")
        # Total cash move including fees, computed by hand:
        assert fill.signed_cash() - fill.fees == Decimal("-221.30")

    def test_nonstandard_deliverable_rejected_not_silent_100(self, synthetic_calendar):
        from tests.fixtures.contracts import split_adjusted_contract

        cal, decision, ex = _sessions(synthetic_calendar)
        engine = FillEngine.from_protocol(
            cal,
            __import__("tree_options.protocol.loader", fromlist=["load_protocol"]).load_protocol(),
        )
        contract, _action = split_adjusted_contract()
        at = execution_instant(cal.session_open(ex))
        with pytest.raises(FillRejection) as ei:
            engine.execute(
                _order(cal, decision, contract_id=contract.contract_id),
                fresh_quote(contract_id=contract.contract_id, execution_at=at),
                contract,
                execution_session=ex,
                execution_at=at,
            )
        assert ei.value.code == "NONSTANDARD_DELIVERABLE"


class TestExecutionStressMonotonicity:
    """Worse stress never improves a fill: exact, integer ticks, f=0 primary."""

    def test_stress_components_only_worsen(self, synthetic_calendar, protocol):
        from tree_options.guards.fills import ExecutionStress

        base = ExecutionStress.zero()
        worse = ExecutionStress(
            slippage_ticks_buy=1, slippage_ticks_sell=1, extra_fee_per_contract="0.10"
        )
        worse2 = ExecutionStress(latency_seconds=120)
        assert worse.buy_price_delta_ticks >= 0
        assert -worse.sell_price_delta_ticks >= 0
        assert worse.extra_fee_per_contract >= base.extra_fee_per_contract
        assert worse2.latency_seconds >= base.latency_seconds

    def test_round_trip_cost_monotone_under_stress_exact(self, synthetic_calendar, protocol):
        """buy+sell round trip at f=0: adding any adverse stress component can
        never reduce net cost, exactly, on an integer tick grid."""
        from tree_options.guards.fills import ExecutionStress

        cal, decision, ex = _sessions(synthetic_calendar)
        engine = FillEngine.from_protocol(cal, protocol)
        at = execution_instant(cal.session_open(ex))
        ex2 = cal.nth_after(ex, 1)
        at2 = execution_instant(cal.session_open(ex2))

        def net(stress):
            q1 = fresh_quote(
                bid="1.20", ask="1.26", execution_at=at + timedelta(seconds=stress.latency_seconds)
            )
            buy = engine.execute(
                _order(cal, decision),
                q1,
                standard_call(),
                execution_session=ex,
                execution_at=at + timedelta(seconds=stress.latency_seconds),
                stress=stress,
            )
            q2 = fresh_quote(
                bid="1.20", ask="1.26", execution_at=at2 + timedelta(seconds=stress.latency_seconds)
            )
            sell = engine.execute(
                _order(cal, ex, side="sell"),
                q2,
                standard_call(),
                execution_session=ex2,
                execution_at=at2 + timedelta(seconds=stress.latency_seconds),
                stress=stress,
            )
            return (buy.price - sell.price) * buy.quantity * buy.multiplier + buy.fees + sell.fees

        base_cost = net(ExecutionStress.zero())
        assert net(ExecutionStress(slippage_ticks_buy=2)) >= base_cost
        assert net(ExecutionStress(slippage_ticks_sell=2)) >= base_cost
        assert net(ExecutionStress(slippage_ticks_buy=1, slippage_ticks_sell=3)) >= base_cost
        assert net(ExecutionStress(extra_fee_per_contract="0.25")) > base_cost
        # Latency either leaves the fill unchanged or rejects it (staleness) —
        # it can never produce a cheaper fill.
        try:
            assert net(ExecutionStress(latency_seconds=600)) >= base_cost
        except FillRejection as exc:
            assert (
                exc.code in {"STALE_QUOTE", "EXECUTION_INSTANT_MISMATCH"}
                or "stale" in str(exc).lower()
            )

    def test_quote_stream_selection_monotone_in_time(self, synthetic_calendar):
        from tree_options.schemas.market import select_quote

        cal, _decision, ex = _sessions(synthetic_calendar)
        at = execution_instant(cal.session_open(ex))
        q1 = fresh_quote(bid="1.00", ask="1.10", execution_at=at, received_offset_seconds=600)
        q2 = fresh_quote(bid="0.98", ask="1.08", execution_at=at, received_offset_seconds=60)
        q3 = fresh_quote(bid="0.95", ask="1.05", execution_at=at, received_offset_seconds=10)
        chosen_early = select_quote([q1, q2, q3], at - timedelta(seconds=300))
        chosen_late = select_quote([q1, q2, q3], at)
        # Eligibility: a selection may NEVER return a quote received after
        # its own execution instant (this is what kills reach-back mutants).
        assert chosen_early.quote.received_timestamp <= at - timedelta(seconds=300)
        assert chosen_late.quote.received_timestamp <= at
        # A later execution instant never reaches back for an earlier quote.
        assert chosen_late.quote.received_timestamp >= chosen_early.quote.received_timestamp
        assert chosen_late.quote == q3


class TestQuoteRealityV2:
    def test_locked_quote_rejected_distinct_code(self, synthetic_calendar, protocol):
        cal, _decision, ex = _sessions(synthetic_calendar)
        at = execution_instant(cal.session_open(ex))
        locked = fresh_quote(bid="1.05", ask="1.05", execution_at=at)
        with pytest.raises(LockedQuoteError):
            as_tradable(locked, execution_at=at, reject_locked=True)

    def test_nonpositive_quote_rejected_distinct_code(self, synthetic_calendar):
        cal, _decision, ex = _sessions(synthetic_calendar)
        at = execution_instant(cal.session_open(ex))
        q = fresh_quote(bid="0.00", ask="1.10", execution_at=at)
        with pytest.raises(NonpositiveQuoteError):
            as_tradable(q, execution_at=at)

    def test_engine_uses_protocol_age_and_locked_policy(self, synthetic_calendar, protocol):
        cal, _decision, _ex = _sessions(synthetic_calendar)
        engine = FillEngine.from_protocol(cal, protocol)
        assert engine.max_quote_age_seconds == protocol.fills.max_quote_age_seconds
        assert engine.reject_locked_quotes is True
        assert engine.fill_size_fraction == Decimal("1.0")


class TestDisplayedSizeFraction:
    def test_fill_capped_at_fraction_of_displayed(self, synthetic_calendar, protocol):
        cal, decision, ex = _sessions(synthetic_calendar)
        engine = FillEngine.from_protocol(cal, protocol)
        engine.fill_size_fraction = Decimal("0.5")
        at = execution_instant(cal.session_open(ex))
        q = fresh_quote(bid="1.00", ask="1.10", ask_size=10, execution_at=at)
        fill = engine.execute(
            _order(cal, decision, qty=9), q, standard_call(), execution_session=ex, execution_at=at
        )
        assert fill.quantity == 5  # floor(0.5 * 10), not 9 or 10


class TestDecisionTimeCoherence:
    def test_contract_must_exist_at_decision_time(self, synthetic_calendar, protocol):
        """A contract listed AFTER the decision session is unknowable at the
        decision, even if it exists by execution."""
        cal, decision, ex = _sessions(synthetic_calendar)
        engine = FillEngine.from_protocol(cal, protocol)

        contract = standard_call(listing_start=ex)  # listed on execution day only
        at = execution_instant(cal.session_open(ex))
        with pytest.raises(FillRejection) as ei:
            engine.execute(
                _order(cal, decision),
                fresh_quote(execution_at=at),
                contract,
                execution_session=ex,
                execution_at=at,
            )
        assert ei.value.code == "CONTRACT_UNKNOWN_AT_DECISION"

    def test_decision_at_must_be_session_close(self, synthetic_calendar, protocol):
        cal, decision, ex = _sessions(synthetic_calendar)
        engine = FillEngine.from_protocol(cal, protocol)
        at = execution_instant(cal.session_open(ex))
        bad = _order(cal, decision).model_copy(
            update={"decision_at": session_close_instant(decision) - timedelta(seconds=1)}
        )
        with pytest.raises(FillRejection) as ei:
            engine.execute(
                bad,
                fresh_quote(execution_at=at),
                standard_call(),
                execution_session=ex,
                execution_at=at,
            )
        assert ei.value.code == "DECISION_INSTANT_NOT_CLOSE"

    def test_limit_fills_at_better_executable_quote(self, synthetic_calendar, protocol):
        """Marketable buy limit fills at the executable ask, not at the limit,
        when the executable is better than the limit."""
        cal, decision, ex = _sessions(synthetic_calendar)
        engine = FillEngine.from_protocol(cal, protocol)
        at = execution_instant(cal.session_open(ex))
        q = fresh_quote(bid="1.00", ask="1.10", execution_at=at)
        fill = engine.execute(
            _order(cal, decision, order_type="limit", limit=Decimal("1.15")),
            q,
            standard_call(),
            execution_session=ex,
            execution_at=at,
        )
        assert fill.price == Decimal("1.10")  # executable, not the limit
