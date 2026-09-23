"""Series extents: P&L charts anchor at zero; LEVEL charts (equity, stock
prices) fit their data (M8 flash review: a ~$12 equity move on a $1M
account drew as a flat line pinned to the top of a $0-based axis)."""

from __future__ import annotations

import pytest

from tree_options.trex.series import level_extent, y_extent
from tree_options.trex_web.stats import equity_series

EQUITY = [(0, 1_000_276.02), (60_000, 1_000_270.16), (120_000, 1_000_282.16)]


class TestLevelExtent:
    def test_fits_the_data_with_padding_not_zero(self) -> None:
        lo, hi = level_extent(EQUITY)
        assert 1_000_260 < lo < 1_000_270.16
        assert 1_000_282.16 < hi < 1_000_292
        # the move fills most of the axis (padding ~8% per side)
        assert (1_000_282.16 - 1_000_270.16) / (hi - lo) > 0.8

    def test_flat_series_gets_a_visible_band_around_the_value(self) -> None:
        lo, hi = level_extent([(0, 690.0), (1, 690.0)])
        assert lo < 690.0 < hi
        assert hi - lo >= 1.0

    def test_flat_band_scales_with_the_level(self) -> None:
        lo, hi = level_extent([(0, 1_000_000.0), (1, 1_000_000.0)])
        assert hi - lo >= 10.0  # a cent-level wiggle never fills the chart

    def test_pnl_extent_still_includes_zero(self) -> None:
        assert y_extent([(0, 5.0), (1, 9.0)])[0] == 0.0


class TestEquitySeries:
    def test_equity_axis_is_not_zero_based(self) -> None:
        records = [
            {"ts": "2026-09-22T18:28:00-04:00", "net_liquidation": 1_000_276.02},
            {"ts": "2026-09-22T19:44:00-04:00", "net_liquidation": 1_000_270.16},
        ]
        series = equity_series(records)
        assert series is not None
        assert series["y_lo"] > 1_000_000
        assert series["y_lo"] < 1_000_270.16 < 1_000_276.02 < series["y_hi"]

    def test_symbol_bars_axis_fits_prices(self) -> None:
        pytest.importorskip("fastapi")
        from tree_options.trex_web.app import _bars_series

        series = _bars_series([{"t": 1, "c": 688.0}, {"t": 2, "c": 695.5}])
        assert series is not None
        assert 680 < series["y_lo"] < 688.0 and 695.5 < series["y_hi"] < 705
