"""FillEngine fail-closed tests (INV-09/10/11): same-close, quote reality, side rule.

The engine is the ONLY component allowed to mint a Fill. Every rejection is a
distinct code; nothing is silently skipped.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.fixtures.contracts import itm_call_at_expiration, standard_call
from tests.fixtures.market import (
    crossed_quote,
    execution_instant,
    fresh_quote,
    non_tradable_quote,
    over_age_quote,
    stale_quote,
    zero_size_quote,
)
from tree_options.guards.fills import FillEngine, FillRejection
from tree_options.schemas.market import (
    CrossedQuoteError,
    NonTradableConditionError,
    StaleQuoteError,
    ZeroSizeQuoteError,
)
from tree_options.schemas.trading import Order

DECISION_SESSION = date(2024, 4, 15)  # a Monday
CONTRACT_ID = "OPT-C-2024-06-21-50"


def _engine(synthetic_calendar) -> FillEngine:
    return FillEngine(synthetic_calendar)


_SEQ = [0]


def _order(
    *,
    side="buy",
    intent="open_long",
    quantity=5,
    order_type="market",
    limit_price=None,
    decision_session=DECISION_SESSION,
    contract_id=CONTRACT_ID,
):
    from tree_options.time.sessions import session_close_instant

    _SEQ[0] += 1
    return Order(
        order_id=f"ORD-{_SEQ[0]}",
        contract_id=contract_id,
        side=side,
        intent=intent,
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
        decision_at=session_close_instant(decision_session),
        decision_session=decision_session,
    )


def _next_exec(synthetic_calendar, n_sessions=1, minutes_in=60):
    ex_session = synthetic_calendar.nth_after(DECISION_SESSION, n_sessions)
    return ex_session, execution_instant(synthetic_calendar.session_open(ex_session), minutes_in)


class TestSameCloseRule:
    """D5 both levels: instant ordering AND session-ordinal ordering."""

    def test_same_session_160001_rejected(self, synthetic_calendar):
        """16:00:01 on the decision session passes instant ordering but is
        still a same-session execution — the ordinal level must catch it."""
        engine = _engine(synthetic_calendar)
        from tree_options.time.sessions import session_close_instant

        exec_at = session_close_instant(DECISION_SESSION) + timedelta(seconds=1)
        with pytest.raises(FillRejection) as ei:
            engine.execute(
                _order(),
                fresh_quote(execution_at=exec_at),
                standard_call(),
                execution_session=DECISION_SESSION,
                execution_at=exec_at,
            )
        assert ei.value.code == "SAME_SESSION_EXECUTION"

    def test_next_session_close_instant_rejected(self, synthetic_calendar):
        """Execution exactly AT the decision instant on the next session
        passes ordinal ordering but fails strict instant ordering."""
        engine = _engine(synthetic_calendar)
        from tree_options.time.sessions import session_close_instant

        ex_session = synthetic_calendar.nth_after(DECISION_SESSION, 1)
        exec_at = session_close_instant(DECISION_SESSION)  # == decision_at
        with pytest.raises(FillRejection) as ei:
            engine.execute(
                _order(),
                fresh_quote(execution_at=exec_at),
                standard_call(),
                execution_session=ex_session,
                execution_at=exec_at,
            )
        assert ei.value.code == "SAME_SESSION_EXECUTION"

    def test_next_session_intraday_fills(self, synthetic_calendar):
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        fill = engine.execute(
            _order(),
            fresh_quote(bid="1.00", ask="1.10", execution_at=exec_at),
            standard_call(),
            execution_session=ex_session,
            execution_at=exec_at,
        )
        assert fill.side == "buy"
        assert fill.price == Decimal("1.10")  # long entry at the ask
        assert fill.quantity == 5


class TestQuoteReality:
    def test_crossed_quote_rejected(self, synthetic_calendar):
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        with pytest.raises(CrossedQuoteError):
            engine.execute(
                _order(),
                crossed_quote(exec_at),
                standard_call(),
                execution_session=ex_session,
                execution_at=exec_at,
            )

    def test_zero_size_quote_rejected(self, synthetic_calendar):
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        with pytest.raises(ZeroSizeQuoteError):
            engine.execute(
                _order(),
                zero_size_quote(exec_at),
                standard_call(),
                execution_session=ex_session,
                execution_at=exec_at,
            )

    def test_stale_quote_rejected(self, synthetic_calendar):
        """Quote received after the execution instant: physically impossible."""
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        with pytest.raises(StaleQuoteError):
            engine.execute(
                _order(),
                stale_quote(exec_at),
                standard_call(),
                execution_session=ex_session,
                execution_at=exec_at,
            )

    def test_over_age_quote_rejected(self, synthetic_calendar):
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        with pytest.raises(StaleQuoteError):
            engine.execute(
                _order(),
                over_age_quote(exec_at),
                standard_call(),
                execution_session=ex_session,
                execution_at=exec_at,
            )

    def test_non_tradable_condition_rejected(self, synthetic_calendar):
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        with pytest.raises(NonTradableConditionError):
            engine.execute(
                _order(),
                non_tradable_quote(exec_at),
                standard_call(),
                execution_session=ex_session,
                execution_at=exec_at,
            )


class TestContractLifecycle:
    def test_fill_on_non_session_rejected(self, synthetic_calendar):
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        saturday = ex_session + timedelta(days=(5 - ex_session.weekday()) % 7 or 7)
        with pytest.raises(FillRejection) as ei:
            engine.execute(
                _order(),
                fresh_quote(execution_at=exec_at),
                standard_call(),
                execution_session=saturday,
                execution_at=exec_at,
            )
        assert ei.value.code == "SESSION_NOT_IN_CALENDAR"

    def test_fill_before_listing_rejected(self, synthetic_calendar):
        engine = _engine(synthetic_calendar)
        contract = standard_call(listing_start=date(2024, 6, 1))
        ex_session, exec_at = _next_exec(synthetic_calendar)
        with pytest.raises(FillRejection) as ei:
            engine.execute(
                _order(),
                fresh_quote(execution_at=exec_at),
                contract,
                execution_session=ex_session,
                execution_at=exec_at,
            )
        assert ei.value.code == "CONTRACT_NOT_LISTED"

    def test_fill_after_expiration_rejected_itm_fixture(self, synthetic_calendar):
        """Fixture 5: ITM call is tradable ON expiration, dead the next session."""
        engine = _engine(synthetic_calendar)
        contract, expiration, following = itm_call_at_expiration()
        assert contract.exists_on(expiration)
        assert contract.expired_on(following)
        assert not contract.exists_on(following)
        assert synthetic_calendar.ordinal(following) == synthetic_calendar.ordinal(expiration) + 1
        exec_at = execution_instant(synthetic_calendar.session_open(following))
        itm_id = "OPT-C-2024-04-19-50"
        with pytest.raises(FillRejection) as ei:
            engine.execute(
                _order(decision_session=expiration, contract_id=itm_id),
                fresh_quote(contract_id=itm_id, execution_at=exec_at),
                contract,
                execution_session=following,
                execution_at=exec_at,
            )
        assert ei.value.code in {"CONTRACT_NOT_LISTED", "CONTRACT_EXPIRED"}

    def test_execution_instant_outside_session_rejected(self, synthetic_calendar):
        """A fill stamped 10:00 UTC but labeled the NEXT session is a lie."""
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        mislabeled_at = exec_at + timedelta(days=1)
        with pytest.raises(FillRejection) as ei:
            engine.execute(
                _order(),
                fresh_quote(execution_at=mislabeled_at),
                standard_call(),
                execution_session=ex_session,
                execution_at=mislabeled_at,
            )
        assert ei.value.code == "EXECUTION_INSTANT_MISMATCH"

    def test_quote_contract_mismatch_rejected(self, synthetic_calendar):
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        wrong = fresh_quote(contract_id="OPT-C-9999-01-01-1", execution_at=exec_at)
        with pytest.raises(FillRejection) as ei:
            engine.execute(
                _order(),
                wrong,
                standard_call(),
                execution_session=ex_session,
                execution_at=exec_at,
            )
        assert ei.value.code == "CONTRACT_MISMATCH"


class TestSideRuleAndImprovement:
    def test_buy_fills_at_ask_sell_fills_at_bid(self, synthetic_calendar):
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        q = fresh_quote(bid="1.00", ask="1.10", execution_at=exec_at)
        buy = engine.execute(
            _order(), q, standard_call(), execution_session=ex_session, execution_at=exec_at
        )
        assert buy.price == Decimal("1.10")
        sell_order = _order(side="sell", intent="close_long")
        sell = engine.execute(
            sell_order, q, standard_call(), execution_session=ex_session, execution_at=exec_at
        )
        assert sell.price == Decimal("1.00")

    @pytest.mark.parametrize("fraction", ["0.5", "1.0"])
    def test_improvement_moves_toward_midpoint_never_past(self, synthetic_calendar, fraction):
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        q = fresh_quote(bid="1.00", ask="1.10", execution_at=exec_at)
        f = Decimal(fraction)
        buy = engine.execute(
            _order(),
            q,
            standard_call(),
            execution_session=ex_session,
            execution_at=exec_at,
            fraction_to_midpoint_f=f,
        )
        sell = engine.execute(
            _order(side="sell", intent="close_long"),
            q,
            standard_call(),
            execution_session=ex_session,
            execution_at=exec_at,
            fraction_to_midpoint_f=f,
        )
        mid = (Decimal("1.00") + Decimal("1.10")) / 2
        assert Decimal("1.10") >= buy.price >= mid
        assert mid >= sell.price >= Decimal("1.00")

    @pytest.mark.parametrize("fraction", ["0.5", "0.25"])
    def test_improvement_on_half_tick_mid_rounds_conservatively(self, synthetic_calendar, fraction):
        """A one-cent market puts the midpoint on a HALF tick (1.005): the
        buy price must round UP and the sell DOWN — quantization may only
        hurt the taker (INV-11; mutant M124 inverts the buy rounding and
        fills at the bid)."""
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        q = fresh_quote(bid="1.00", ask="1.01", execution_at=exec_at)
        f = Decimal(fraction)
        buy = engine.execute(
            _order(),
            q,
            standard_call(),
            execution_session=ex_session,
            execution_at=exec_at,
            fraction_to_midpoint_f=f,
        )
        sell = engine.execute(
            _order(side="sell", intent="close_long"),
            q,
            standard_call(),
            execution_session=ex_session,
            execution_at=exec_at,
            fraction_to_midpoint_f=f,
        )
        assert buy.price == Decimal("1.01"), "buy rounds the half-tick UP, to the ask"
        assert sell.price == Decimal("1.00"), "sell rounds the half-tick DOWN, to the bid"

    def test_invalid_improvement_fraction_rejected(self, synthetic_calendar):
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        with pytest.raises(FillRejection) as ei:
            engine.execute(
                _order(),
                fresh_quote(execution_at=exec_at),
                standard_call(),
                execution_session=ex_session,
                execution_at=exec_at,
                fraction_to_midpoint_f=Decimal("0.9"),
            )
        assert ei.value.code == "INVALID_FRACTION_TO_MIDPOINT"


class TestPartialFillsAndLimits:
    def test_partial_fill_capped_at_quote_size(self, synthetic_calendar):
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        q = fresh_quote(bid="1.00", ask="1.10", ask_size=3, execution_at=exec_at)
        fill = engine.execute(
            _order(quantity=10),
            q,
            standard_call(),
            execution_session=ex_session,
            execution_at=exec_at,
        )
        assert fill.quantity == 3  # min(order, ask_size) — never invented size

    def test_unmarketable_buy_limit_rejected(self, synthetic_calendar):
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        q = fresh_quote(bid="1.00", ask="1.10", execution_at=exec_at)
        with pytest.raises(FillRejection) as ei:
            engine.execute(
                _order(order_type="limit", limit_price=Decimal("1.05")),
                q,
                standard_call(),
                execution_session=ex_session,
                execution_at=exec_at,
            )
        assert ei.value.code == "UNMARKETABLE_LIMIT"

    def test_marketable_buy_limit_fills(self, synthetic_calendar):
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        q = fresh_quote(bid="1.00", ask="1.10", execution_at=exec_at)
        fill = engine.execute(
            _order(order_type="limit", limit_price=Decimal("1.10")),
            q,
            standard_call(),
            execution_session=ex_session,
            execution_at=exec_at,
        )
        assert fill.price == Decimal("1.10")


class TestFees:
    def test_per_contract_fee_with_minimum(self, synthetic_calendar):
        from tree_options.ledger.fees import PerContractFeeModel

        model = PerContractFeeModel()
        assert model.order_fees(1) == Decimal("1.00")  # 0.65 < 1.00 minimum
        assert model.order_fees(2) == Decimal("1.30")
        assert model.order_fees(10) == Decimal("6.50")

    def test_fill_carries_fees(self, synthetic_calendar):
        engine = _engine(synthetic_calendar)
        ex_session, exec_at = _next_exec(synthetic_calendar)
        fill = engine.execute(
            _order(quantity=4),
            fresh_quote(execution_at=exec_at),
            standard_call(),
            execution_session=ex_session,
            execution_at=exec_at,
        )
        assert fill.fees == Decimal("2.60")  # 4 * 0.65
        # cash identity: buy of 4 @ ask 1.10 with 2.60 fees
        assert fill.signed_cash() == Decimal("-440.00")  # 4 * 1.10 * 100 multiplier
