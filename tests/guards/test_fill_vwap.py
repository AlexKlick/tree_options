"""G3 (protocol 0.2.0): vwap-quote fills — the second quote kind's engine.

Pins the ratified semantics: a vwap bar fills AT the session VWAP rounded
conservatively to the tick (buy up, sell down), participation-capped by the
bar's observed volume; a zero-volume session is unfillable; an unpublished
bar is future data; the midpoint fraction is a two-sided concept and
refuses; the two-sided path is byte-identical in behavior (same engine,
same order checks, mixed streams select by received instant).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from tests.fixtures.contracts import standard_call
from tests.fixtures.market import execution_instant
from tree_options.guards.fills import FillEngine, FillRejection
from tree_options.schemas.market import (
    StaleQuoteError,
    VwapQuoteEvent,
    ZeroVolumeVwapError,
    as_tradable_vwap,
    conservative_tick,
)
from tree_options.schemas.trading import Order
from tree_options.time.sessions import session_close_instant

DECISION_SESSION = date(2024, 4, 15)
CONTRACT_ID = "OPT-C-2024-06-21-50"
ET = session_close_instant(DECISION_SESSION).tzinfo


def _bar(
    *,
    session: date,
    vwap: str = "1.2375",
    volume: int = 400,
    execution_at: datetime,
    received_at: datetime | None = None,
    condition: str = "regular",
) -> VwapQuoteEvent:
    """A published bar: exchange stamp = the bar session's close, receipt
    at the T+1 wall (09:00 ET next day) by default — the massive lane's
    own convention."""
    exchange = session_close_instant(session)
    received = received_at if received_at is not None else exchange
    return VwapQuoteEvent(
        contract_id=CONTRACT_ID,
        session=session,
        exchange_timestamp=exchange,
        received_timestamp=received,
        vwap=Decimal(vwap),
        volume=volume,
        trade_count=37,
        quote_condition=condition,
        source="massive-derived-free/1",
    )


def _engine(synthetic_calendar, **kwargs) -> FillEngine:
    return FillEngine(synthetic_calendar, **kwargs)


_SEQ = [0]


def _order(*, side="buy", quantity=5, order_type="market", limit_price=None):
    _SEQ[0] += 1
    return Order(
        order_id=f"VORD-{_SEQ[0]}",
        contract_id=CONTRACT_ID,
        side=side,
        intent="open_long" if side == "buy" else "close_long",
        quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
        decision_at=session_close_instant(DECISION_SESSION),
        decision_session=DECISION_SESSION,
    )


class TestVwapDoor:
    def test_published_bar_passes_and_carries_facts(self):
        bar = _bar(session=DECISION_SESSION, execution_at=None, received_at=None)
        tq = as_tradable_vwap(bar, execution_at=bar.received_timestamp)
        assert tq.vwap == Decimal("1.2375")
        assert tq.volume == 400

    def test_unpublished_bar_is_stale(self):
        bar = _bar(session=DECISION_SESSION, execution_at=None)
        with pytest.raises(StaleQuoteError) as exc:
            as_tradable_vwap(bar, execution_at=bar.received_timestamp - timedelta(seconds=1))
        assert "unpublished" in str(exc.value)

    def test_zero_volume_unfillable(self):
        bar = _bar(session=DECISION_SESSION, volume=0, execution_at=None)
        with pytest.raises(ZeroVolumeVwapError):
            as_tradable_vwap(bar, execution_at=bar.received_timestamp)

    def test_non_tradable_condition_refuses(self):
        bar = _bar(session=DECISION_SESSION, condition="halted", execution_at=None)
        with pytest.raises(Exception) as exc:
            as_tradable_vwap(bar, execution_at=bar.received_timestamp)
        assert "not tradable" in str(exc.value)

    def test_no_age_in_seconds_rule(self):
        """A daily bar is not stale at 901 seconds: publication is the only
        time gate for this kind (protocol 0.2.0 fills.vwap)."""
        bar = _bar(session=DECISION_SESSION, execution_at=None)
        late = bar.received_timestamp + timedelta(hours=6)
        as_tradable_vwap(bar, execution_at=late)  # no raise


class TestConservativeTick:
    def test_buy_rounds_up(self):
        assert conservative_tick(Decimal("1.2375"), "buy") == Decimal("1.24")

    def test_sell_rounds_down(self):
        assert conservative_tick(Decimal("1.2375"), "sell") == Decimal("1.23")

    def test_on_tick_price_unchanged(self):
        assert conservative_tick(Decimal("1.24"), "buy") == Decimal("1.24")
        assert conservative_tick(Decimal("1.24"), "sell") == Decimal("1.24")

    def test_bad_side_refuses(self):
        with pytest.raises(ValueError, match="buy or sell"):
            conservative_tick(Decimal("1.24"), "hold")


class TestVwapFills:
    def test_buy_fills_at_vwap_rounded_up(self, synthetic_calendar):
        """1.2375 VWAP -> buy at 1.24: quantization hurts the taker."""
        ex_session = synthetic_calendar.nth_after(DECISION_SESSION, 1)
        # The bar is the DECISION session's own bar, received at its close;
        # executing next session uses it (publication satisfied).
        bar = _bar(session=DECISION_SESSION, execution_at=None)
        engine = _engine(synthetic_calendar)
        fill = engine.execute(
            _order(side="buy"),
            bar,
            standard_call(),
            execution_session=ex_session,
            execution_at=execution_instant(synthetic_calendar.session_open(ex_session), 60),
        )
        assert fill.price == Decimal("1.24")

    def test_sell_fills_at_vwap_rounded_down(self, synthetic_calendar):
        ex_session = synthetic_calendar.nth_after(DECISION_SESSION, 1)
        bar = _bar(session=DECISION_SESSION, execution_at=None)
        engine = _engine(synthetic_calendar)
        fill = engine.execute(
            _order(side="sell"),
            bar,
            standard_call(),
            execution_session=ex_session,
            execution_at=execution_instant(synthetic_calendar.session_open(ex_session), 60),
        )
        assert fill.price == Decimal("1.23")

    def test_participation_cap_bars_beyond_volume(self, synthetic_calendar):
        """fill_size_fraction * volume is a hard cap on quantity."""
        ex_session = synthetic_calendar.nth_after(DECISION_SESSION, 1)
        bar = _bar(session=DECISION_SESSION, volume=4, execution_at=None)
        engine = _engine(synthetic_calendar, fill_size_fraction=Decimal("0.5"))
        fill = engine.execute(
            _order(quantity=5),
            bar,
            standard_call(),
            execution_session=ex_session,
            execution_at=execution_instant(synthetic_calendar.session_open(ex_session), 60),
        )
        assert fill.quantity == 2  # floor(0.5 * 4)

    def test_volume_one_cannot_fill_two(self, synthetic_calendar):
        ex_session = synthetic_calendar.nth_after(DECISION_SESSION, 1)
        bar = _bar(session=DECISION_SESSION, volume=1, execution_at=None)
        engine = _engine(synthetic_calendar)
        fill = engine.execute(
            _order(quantity=2),
            bar,
            standard_call(),
            execution_session=ex_session,
            execution_at=execution_instant(synthetic_calendar.session_open(ex_session), 60),
        )
        assert fill.quantity == 1

    def test_zero_volume_session_unfillable_not_fabricated(self, synthetic_calendar):
        ex_session = synthetic_calendar.nth_after(DECISION_SESSION, 1)
        bar = _bar(session=DECISION_SESSION, volume=0, execution_at=None)
        engine = _engine(synthetic_calendar)
        with pytest.raises(ZeroVolumeVwapError):
            engine.execute(
                _order(),
                bar,
                standard_call(),
                execution_session=ex_session,
                execution_at=execution_instant(synthetic_calendar.session_open(ex_session), 60),
            )

    def test_unpublished_bar_refuses_as_future_data(self, synthetic_calendar):
        """The bar publishes at the T+1 receipt wall; an execution instant
        BEFORE the wall may not consume it (lookahead refusal). The engine
        refuses at SELECTION — an unpublished bar is not even eligible —
        which is stronger than the door's own check (pinned directly in
        TestVwapDoor above)."""
        ex_session = synthetic_calendar.nth_after(DECISION_SESSION, 1)
        wall = session_close_instant(ex_session) + timedelta(hours=17)
        bar = _bar(session=DECISION_SESSION, execution_at=None, received_at=wall)
        engine = _engine(synthetic_calendar)
        with pytest.raises(StaleQuoteError) as exc:
            engine.execute(
                _order(),
                bar,
                standard_call(),
                execution_session=ex_session,
                execution_at=execution_instant(synthetic_calendar.session_open(ex_session), 60),
            )
        assert "no quote received" in str(exc.value)

    def test_midpoint_fraction_refuses_on_vwap_kind(self, synthetic_calendar):
        ex_session = synthetic_calendar.nth_after(DECISION_SESSION, 1)
        bar = _bar(session=DECISION_SESSION, execution_at=None)
        engine = _engine(synthetic_calendar)
        with pytest.raises(FillRejection) as exc:
            engine.execute(
                _order(),
                bar,
                standard_call(),
                execution_session=ex_session,
                execution_at=execution_instant(synthetic_calendar.session_open(ex_session), 60),
                fraction_to_midpoint_f=Decimal("0.5"),
            )
        assert exc.value.code == "INVALID_FRACTION_TO_MIDPOINT"

    def test_unmarketable_buy_limit_refuses(self, synthetic_calendar):
        ex_session = synthetic_calendar.nth_after(DECISION_SESSION, 1)
        bar = _bar(session=DECISION_SESSION, execution_at=None)
        engine = _engine(synthetic_calendar)
        with pytest.raises(FillRejection) as exc:
            engine.execute(
                _order(order_type="limit", limit_price=Decimal("1.20")),
                bar,
                standard_call(),
                execution_session=ex_session,
                execution_at=execution_instant(synthetic_calendar.session_open(ex_session), 60),
            )
        assert exc.value.code == "UNMARKETABLE_LIMIT"

    def test_slippage_worsens_vwap_price_monotonely(self, synthetic_calendar):
        from tree_options.guards.fills import ExecutionStress

        ex_session = synthetic_calendar.nth_after(DECISION_SESSION, 1)
        bar = _bar(session=DECISION_SESSION, execution_at=None)
        engine = _engine(synthetic_calendar)
        base = engine.execute(
            _order(),
            bar,
            standard_call(),
            execution_session=ex_session,
            execution_at=execution_instant(synthetic_calendar.session_open(ex_session), 60),
        )
        stressed = engine.execute(
            _order(),
            bar,
            standard_call(),
            execution_session=ex_session,
            execution_at=execution_instant(synthetic_calendar.session_open(ex_session), 60),
            stress=ExecutionStress(slippage_ticks_buy=2),
        )
        assert stressed.price == base.price + Decimal("0.02")

    def test_mixed_stream_selects_latest_received(self, synthetic_calendar):
        """A stream mixing kinds selects by received instant — a later
        two-sided quote supersedes an older bar, and vice versa."""
        from tests.fixtures.market import fresh_quote

        ex_session = synthetic_calendar.nth_after(DECISION_SESSION, 1)
        exec_at = execution_instant(synthetic_calendar.session_open(ex_session), 60)
        bar = _bar(session=DECISION_SESSION, execution_at=None)
        # Two-sided quote received 5s before exec (fresh), bar received
        # earlier (at decision close): the quote wins selection.
        q = fresh_quote(execution_at=exec_at, bid="1.30", ask="1.32")
        assert q.received_timestamp > bar.received_timestamp
        engine = _engine(synthetic_calendar)
        fill = engine.execute(
            _order(),
            [bar, q],
            standard_call(),
            execution_session=ex_session,
            execution_at=exec_at,
        )
        assert fill.price == Decimal("1.32")  # ask edge: the two-sided path
