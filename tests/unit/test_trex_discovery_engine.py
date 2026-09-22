"""C6: the discovery scan engine (pure — chains in, ranked candidates out).

Debit vocabulary per the plan's D1 decision: buy long_strike, sell
short_strike, pay debit. Rules reuse the protocol filter's tri-state
constants (PASS/FAIL/NOT_EVALUABLE/NOT_APPLICABLE). Missing inputs are
never fabricated into a number.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tree_options.candidates.filters import NOT_APPLICABLE, NOT_EVALUABLE
from tree_options.trex.discovery.config import ScanConfig
from tree_options.trex.discovery.engine import (
    Candidate,
    ChainRow,
    ScanInput,
    SpotContext,
    row_mid,
    scan,
    select_top,
)

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 22, 15, 55, tzinfo=ET)


def _row(strike: float, bid: float | None, ask: float | None, delta: float | None = None) -> ChainRow:
    return ChainRow(strike=strike, bid=bid, ask=ask, delta=delta, ts=NOW)


def _chain() -> list[ChainRow]:
    """A small put chain: puts are worth more at higher strikes."""
    return [
        _row(140.0, 0.10, 0.14),
        _row(145.0, 0.16, 0.20, -0.12),
        _row(150.0, 0.40, 0.46, -0.18),
        _row(155.0, 0.90, 1.00, -0.27),
        _row(160.0, 1.70, 1.85, -0.36),
    ]


def _input(rows: list[ChainRow] | None = None, dte: int = 24) -> ScanInput:
    return ScanInput(underlying="NVDA", expiry="20261016", dte=dte, rows=_chain() if rows is None else rows)


def _cfg(**overrides: object) -> ScanConfig:
    fields = {"widths": [5.0, 10.0], "max_candidates_per_underlying": 5}
    fields.update(overrides)  # type: ignore[arg-type]
    return ScanConfig(underlyings=["NVDA"], **fields)  # type: ignore[arg-type]


def _find(result, short: float) -> Candidate:
    for c in result.candidates + result.rejected:
        if c.short_strike == short:
            return c
    raise AssertionError(f"no candidate with short strike {short}")


class TestTargetSelection:
    def test_premium_mode_generates_all_valid_pairs_ranked_by_yield(self) -> None:
        result = scan(_input(), _cfg(target_mode="premium"), None, NOW)
        # greeks present: only |delta| 0.27 (155) sits inside the band
        assert {c.short_strike for c in result.candidates} == {155.0}
        yields = [c.yield_ratio for c in result.candidates]
        assert yields == sorted(yields, reverse=True)

    def test_delta_mode_picks_the_nearest_band_delta(self) -> None:
        result = scan(_input(), _cfg(target_mode="delta"), None, NOW)
        # |delta| 0.30 target: 0.27 (155) is nearer than 0.36 (160)
        assert [c.short_strike for c in result.candidates] == [155.0]

    def test_otm_mode_uses_spot_fraction(self) -> None:
        spot = SpotContext(symbol="NVDA", spot=165.0, source="cboe_eod")
        result = scan(_input(), _cfg(target_mode="otm"), spot, NOW)
        # 165 * (1 - 0.06) = 155.1 -> nearest chain strike 155 (in band)
        assert {c.short_strike for c in result.candidates} == {155.0}

    def test_auto_degrades_to_premium_when_no_greeks_no_spot(self) -> None:
        rows = [_row(r.strike, r.bid, r.ask, None) for r in _chain()]
        result = scan(_input(rows), _cfg(target_mode="auto"), None, NOW)
        assert result.effective_target_mode == "premium"
        assert any("no greeks" in n for n in result.notes)

    def test_auto_degrades_to_otm_when_spot_present(self) -> None:
        rows = [_row(r.strike, r.bid, r.ask, None) for r in _chain()]
        result = scan(
            _input(rows),
            _cfg(target_mode="auto"),
            SpotContext(symbol="NVDA", spot=165.0, source="cboe_eod"),
            NOW,
        )
        assert result.effective_target_mode == "otm"
        assert {c.short_strike for c in result.candidates} == {155.0}

    def test_explicit_delta_without_greeks_is_never_silent(self) -> None:
        rows = [_row(r.strike, r.bid, r.ask, None) for r in _chain()]
        result = scan(_input(rows), _cfg(target_mode="delta"), None, NOW)
        assert result.candidates == []
        assert result.rejected
        assert all(
            r.status == NOT_EVALUABLE and "greeks" in r.detail
            for c in result.rejected
            for r in c.rules
            if r.rule == "delta"
        )


class TestRules:
    def test_missing_leg_quote_is_not_evaluable(self) -> None:
        rows = [
            _row(140.0, None, 0.14),  # short leg has no bid
            _row(145.0, 0.16, 0.20),
            _row(150.0, 0.40, 0.46),
        ]
        result = scan(_input(rows), _cfg(target_mode="premium"), None, NOW)
        c = _find(result, 140.0)
        assert c.accepted is False
        assert any(r.status == NOT_EVALUABLE and r.rule == "quotes" for r in c.rules)
        assert c.debit_mid is None

    def test_zero_bid_is_no_market_and_never_ranked(self) -> None:
        rows = [r if r.strike != 150.0 else _row(150.0, 0.0, 0.02) for r in _chain()]
        result = scan(_input(rows), _cfg(target_mode="premium"), None, NOW)
        c = _find(result, 150.0)
        assert any(r.status == NOT_EVALUABLE and "no market" in r.detail for r in c.rules)
        assert c.accepted is False

    def test_wide_leg_spread_fails_liquidity(self) -> None:
        rows = [r if r.strike != 155.0 else _row(155.0, 0.60, 1.40) for r in _chain()]
        result = scan(_input(rows), _cfg(target_mode="premium"), None, NOW)
        c = _find(result, 155.0)
        assert any(r.rule == "leg_spread" and r.status != NOT_EVALUABLE for r in c.rules)
        assert c.accepted is False

    def test_debit_below_floor_fails_min_debit(self) -> None:
        cfg = _cfg(target_mode="premium", min_debit=5.0)  # impossible floor
        result = scan(_input(), cfg, None, NOW)
        assert result.candidates == []
        assert any(
            r.rule == "min_debit" for c in result.rejected for r in c.rules
        )

    def test_dte_window_rule(self) -> None:
        result = scan(_input(dte=10), _cfg(), None, NOW)
        assert result.candidates == []
        assert all(
            any(r.rule == "dte" for r in c.rules) for c in result.rejected
        )

    def test_delta_rule_not_applicable_without_greeks(self) -> None:
        rows = [_row(r.strike, r.bid, r.ask, None) for r in _chain()]
        result = scan(_input(rows), _cfg(target_mode="premium"), None, NOW)
        assert result.greeks_available is False
        assert result.candidates
        assert any(
            r.rule == "delta" and r.status == NOT_APPLICABLE
            for c in result.candidates
            for r in c.rules
        )

    def test_delta_rule_passes_inside_band_with_greeks(self) -> None:
        # 155 put has |delta| 0.27, inside default band 0.20-0.45
        result = scan(_input(), _cfg(target_mode="premium"), None, NOW)
        c = _find(result, 155.0)
        assert any(r.rule == "delta" and r.status == "PASS" for r in c.rules)


class TestRanking:
    def test_ranked_by_yield_ratio_descending(self) -> None:
        result = scan(_input(), _cfg(target_mode="premium"), None, NOW)
        yields = [c.yield_ratio for c in result.candidates]
        assert yields == sorted(yields, reverse=True)
        assert result.candidates[0].rank == 1

    def test_per_underlying_cap(self) -> None:
        rows = [_row(r.strike, r.bid, r.ask, None) for r in _chain()]
        result = scan(
            _input(rows), _cfg(target_mode="premium", max_candidates_per_underlying=2), None, NOW
        )
        assert len(result.candidates) == 2
        assert len(result.rejected) >= 1  # overflow disclosed, not dropped

    def test_total_cap_across_scans(self) -> None:
        rows = [_row(r.strike, r.bid, r.ask, None) for r in _chain()]
        a = scan(_input(rows), _cfg(target_mode="premium"), None, NOW)
        b = scan(_input(rows), _cfg(target_mode="premium"), None, NOW)
        b.underlying = "QQQ"
        top = select_top([a, b], _cfg(target_mode="premium", max_candidates_total=4))
        assert sum(len(r.candidates) for r in top) == 4
        # demotion is disclosed, never silent
        assert sum(len(r.rejected) for r in top) >= 2


class TestDataQuality:
    def test_greeks_available_false_when_all_deltas_none(self) -> None:
        rows = [_row(r.strike, r.bid, r.ask, None) for r in _chain()]
        result = scan(_input(rows), _cfg(target_mode="premium"), None, NOW)
        assert result.greeks_available is False

    def test_notes_name_delayed_data(self) -> None:
        result = scan(_input(), _cfg(), None, NOW)
        assert any("delayed" in n for n in result.notes)

    def test_quote_counts(self) -> None:
        rows = [r if r.strike != 145.0 else _row(145.0, None, None) for r in _chain()]
        result = scan(_input(rows), _cfg(), None, NOW)
        assert result.rows_quoted == len(rows) - 1
        assert result.rows_unquoted == 1


class TestHonesty:
    def test_rejected_candidates_carry_reasons_and_no_fabricated_moneys(self) -> None:
        rows = [
            _row(140.0, None, None),
            _row(145.0, 0.16, 0.20),
            _row(150.0, 0.40, 0.46),
        ]
        result = scan(_input(rows), _cfg(target_mode="premium"), None, NOW)
        c = _find(result, 140.0)
        assert c.reasons
        assert c.debit_mid is None and c.max_profit is None and c.max_loss is None

    def test_accepted_candidates_carry_full_money_terms(self) -> None:
        result = scan(_input(), _cfg(target_mode="premium"), None, NOW)
        top = result.candidates[0]
        assert top.debit_mid is not None
        assert top.debit_mid > 0
        assert top.max_profit == pytest.approx((top.width - top.debit_mid) * 100)
        assert top.max_loss == pytest.approx(top.debit_mid * 100)
        assert top.long_strike - top.short_strike == top.width

    def test_debit_mid_matches_leg_mids(self) -> None:
        result = scan(_input(), _cfg(target_mode="premium"), None, NOW)
        c = result.candidates[0]
        long_mid = row_mid(
            next(r for r in _chain() if r.strike == c.long_strike)
        )
        short_mid = row_mid(next(r for r in _chain() if r.strike == c.short_strike))
        assert c.debit_mid == pytest.approx(long_mid - short_mid)  # type: ignore[arg-type]
