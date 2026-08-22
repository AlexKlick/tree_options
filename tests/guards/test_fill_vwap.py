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


class TestReviewHardening:
    """Pins from the independent Codex review round (gpt-5.6-sol): each
    test names the concrete scenario the review showed was possible."""

    def _exec_ctx(self, synthetic_calendar, n_sessions=1, minutes_in=60):
        ex_session = synthetic_calendar.nth_after(DECISION_SESSION, n_sessions)
        return (
            ex_session,
            execution_instant(synthetic_calendar.session_open(ex_session), minutes_in),
        )

    def test_mislabeled_bar_session_refuses(self, synthetic_calendar):
        """P0-1: a bar LABELED a session its exchange stamp does not close
        must refuse — the label must not ride a foreign stamp into a fill."""
        ex_session, exec_at = self._exec_ctx(synthetic_calendar)
        bar = _bar(session=DECISION_SESSION, execution_at=None)
        # relabel the session but keep the decision-session stamp
        mislabeled = bar.model_copy(update={"session": date(2024, 6, 3)})
        engine = _engine(synthetic_calendar)
        with pytest.raises(FillRejection) as exc:
            engine.execute(
                _order(),
                mislabeled,
                standard_call(),
                execution_session=ex_session,
                execution_at=exec_at,
            )
        assert exc.value.code == "BAR_SESSION_STAMP_MISMATCH"

    def test_bar_session_not_in_calendar_refuses(self, synthetic_calendar):
        ex_session, exec_at = self._exec_ctx(synthetic_calendar)
        bar = _bar(session=DECISION_SESSION, execution_at=None)
        bogus = bar.model_copy(
            update={
                "session": date(2024, 4, 14),  # a Sunday, not a session
                "exchange_timestamp": session_close_instant(date(2024, 4, 14)),
                "received_timestamp": session_close_instant(date(2024, 4, 14)),
            }
        )
        engine = _engine(synthetic_calendar)
        with pytest.raises(FillRejection) as exc:
            engine.execute(
                _order(),
                bogus,
                standard_call(),
                execution_session=ex_session,
                execution_at=exec_at,
            )
        assert exc.value.code == "BAR_SESSION_NOT_IN_CALENDAR"

    def test_partial_sequence_cannot_reuse_vwap_bar_capacity(self, synthetic_calendar):
        """P0-2: volume 4 caps TOTAL fills at 4 — a partial-sequence second
        execution against the same bar must find the capacity exhausted."""
        from tests.fixtures.contracts import standard_call as _sc

        ex_session, exec_at = self._exec_ctx(synthetic_calendar)
        bar = _bar(session=DECISION_SESSION, volume=4, execution_at=None)
        engine = _engine(synthetic_calendar)
        order = _order(quantity=8)
        first = engine.execute(
            order, bar, _sc(), execution_session=ex_session, execution_at=exec_at
        )
        assert first.quantity == 4
        with pytest.raises(FillRejection) as exc:
            engine.execute(
                order,
                bar,
                _sc(),
                execution_session=ex_session,
                execution_at=exec_at,
                partial_sequence=True,
            )
        assert exc.value.code == "NO_LIQUIDITY"

    def test_second_order_cannot_reuse_vwap_bar_capacity(self, synthetic_calendar):
        """P0-2 cross-order: a different order against the same bar also
        draws from the same cumulative participation budget."""
        ex_session, exec_at = self._exec_ctx(synthetic_calendar)
        bar = _bar(session=DECISION_SESSION, volume=4, execution_at=None)
        engine = _engine(synthetic_calendar)
        first = engine.execute(
            _order(quantity=3),
            bar,
            standard_call(),
            execution_session=ex_session,
            execution_at=exec_at,
        )
        assert first.quantity == 3
        second = engine.execute(
            _order(quantity=9),
            bar,
            standard_call(),
            execution_session=ex_session,
            execution_at=exec_at,
        )
        assert second.quantity == 1  # 4 observed - 3 consumed

    def test_float_vwap_refused_at_the_boundary(self):
        """P0-6: a float price must not be silently Decimal()-ed into the
        event — exactness lost upstream refuses at the schema. (Fresh
        construction: model_copy(update=...) skips validators by design.)"""
        from pydantic import ValidationError

        good = _bar(session=DECISION_SESSION, execution_at=None)
        fields = good.model_dump()
        fields["vwap"] = 0.1 + 0.2  # a float, post-validation
        with pytest.raises(ValidationError, match="got float"):
            VwapQuoteEvent.model_validate(fields)

    def test_boolean_volume_refused(self):
        from pydantic import ValidationError

        good = _bar(session=DECISION_SESSION, execution_at=None)
        fields = good.model_dump()
        fields["volume"] = True
        with pytest.raises(ValidationError):
            VwapQuoteEvent.model_validate(fields)

    def test_cross_kind_tie_refuses_deterministically(self, synthetic_calendar):
        """P1-8: identical (received, exchange) across kinds is ambiguous —
        stream order must not decide the fill price."""
        from tests.fixtures.market import fresh_quote

        ex_session, exec_at = self._exec_ctx(synthetic_calendar)
        bar = _bar(session=DECISION_SESSION, execution_at=None)
        q = fresh_quote(execution_at=exec_at, bid="1.30", ask="1.32")
        tied_quote = q.model_copy(
            update={
                "received_timestamp": bar.received_timestamp,
                "exchange_timestamp": bar.exchange_timestamp,
            }
        )
        engine = _engine(synthetic_calendar)
        with pytest.raises(ValueError, match="ambiguous cross-kind"):
            engine.execute(
                _order(),
                [bar, tied_quote],
                standard_call(),
                execution_session=ex_session,
                execution_at=exec_at,
            )


class TestRoundTwoHardening:
    """Pins from Codex round 2 (gpt-5.6-sol): recency, type gates, leaks."""

    def test_week_old_coherent_bar_refuses(self, synthetic_calendar):
        """R2 P0-1: coherence alone let a T+1-published bar fill five
        sessions later. The bar must be the session IMMEDIATELY before the
        execution session — an older bar's VWAP fabricates liquidity the
        intervening zero-volume sessions deny."""
        ex_session = synthetic_calendar.nth_after(DECISION_SESSION, 3)
        exec_at = execution_instant(synthetic_calendar.session_open(ex_session), 60)
        bar = _bar(session=DECISION_SESSION, execution_at=None)
        engine = _engine(synthetic_calendar)
        with pytest.raises(FillRejection) as exc:
            engine.execute(
                _order(),
                bar,
                standard_call(),
                execution_session=ex_session,
                execution_at=exec_at,
            )
        assert exc.value.code == "BAR_NOT_MOST_RECENT"

    def test_previous_session_bar_fills(self, synthetic_calendar):
        """The positive case: execution session N consumes session N-1's
        bar (the massive lane's own T+1 rhythm)."""
        ex_session = synthetic_calendar.nth_after(DECISION_SESSION, 1)
        prev = synthetic_calendar.nth_after(DECISION_SESSION, 0)  # decision itself
        bar = _bar(session=prev, execution_at=None)
        engine = _engine(synthetic_calendar)
        fill = engine.execute(
            _order(),
            bar,
            standard_call(),
            execution_session=ex_session,
            execution_at=execution_instant(synthetic_calendar.session_open(ex_session), 60),
        )
        assert fill.price == Decimal("1.24")
