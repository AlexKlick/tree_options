"""Point-in-time beta vs SPY from the research panel (desk/beta.py).

The oracle is numpy's least-squares slope over log returns the test
computes itself from the stored close strings (never the module's own
return series). The panel here is synthetic: deterministic returns with
a known loading, written as the panel's string closes.
"""

from __future__ import annotations

import copy
import itertools
import json
import math
import random
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from tree_options.desk import beta as desk_beta

D_AS_OF = date(2026, 9, 24)


def _sessions(cal, start: date, end: date) -> list[date]:
    return [s for s in cal.sessions() if start <= s <= end]


def _panel(cal, *, loading: float = 1.3, seed: int = 7) -> dict[str, dict[str, dict[str, str]]]:
    rng = random.Random(seed)
    days = _sessions(cal, date(2025, 6, 2), date(2026, 12, 31))
    spy, name, other = 400.0, 50.0, 80.0
    out: dict[str, dict[str, dict[str, str]]] = {"SPY": {}, "ABC": {}, "XYZ": {}}
    for d in days:
        m = rng.gauss(0.0, 0.01)
        spy *= math.exp(m)
        name *= math.exp(loading * m + rng.gauss(0.0, 0.008))
        other *= math.exp(-0.4 * m + rng.gauss(0.0, 0.01))
        for sym, px in (("SPY", spy), ("ABC", name), ("XYZ", other)):
            c = f"{px:.4f}"
            out[sym][d.isoformat()] = {"open": c, "high": c, "low": c, "close": c, "volume": "1"}
    return out


def _oracle(panel, sym: str, window_days: list[date]) -> tuple[float, int]:
    """numpy slope over the paired log returns whose two closes exist."""
    xs, ys = [], []
    for prev, cur in itertools.pairwise(window_days):
        rows = [panel[s].get(d.isoformat()) for s in ("SPY", sym) for d in (prev, cur)]
        if any(r is None for r in rows):
            continue
        sp0, sp1, nm0, nm1 = (float(r["close"]) for r in rows)  # type: ignore[index]
        xs.append(np.log(sp1 / sp0))
        ys.append(np.log(nm1 / nm0))
    slope = np.polyfit(np.array(xs), np.array(ys), 1)[0]
    return float(slope), len(xs)


def _window(cal, as_of: date, n: int = 252) -> list[date]:
    """The n+1 sessions ending at the last session <= as_of."""
    s = [d for d in cal.sessions() if d <= as_of]
    return s[-(n + 1) :]


class TestPointInTimeBeta:
    def test_matches_the_numpy_slope(self, static_calendar) -> None:
        panel = _panel(static_calendar)
        est = desk_beta.beta_from_panel(panel, "ABC", D_AS_OF, static_calendar)
        want, n = _oracle(panel, "ABC", _window(static_calendar, D_AS_OF))
        assert n == 252
        assert est.n_returns == 252
        assert est.beta is not None and isinstance(est.beta, Decimal)
        assert abs(float(est.beta) - want) < 1e-6
        assert 1.1 < want < 1.5  # the synthetic loading is 1.3
        w = _window(static_calendar, D_AS_OF)
        assert (est.window_first, est.session) == (w[1], D_AS_OF)
        assert est.reason == ""

    def test_negative_loading(self, static_calendar) -> None:
        panel = _panel(static_calendar)
        est = desk_beta.beta_from_panel(panel, "XYZ", D_AS_OF, static_calendar)
        want, _ = _oracle(panel, "XYZ", _window(static_calendar, D_AS_OF))
        assert est.beta is not None and abs(float(est.beta) - want) < 1e-6
        assert want < 0

    def test_spy_beta_is_one(self, static_calendar) -> None:
        est = desk_beta.beta_from_panel(_panel(static_calendar), "SPY", D_AS_OF, static_calendar)
        assert est.beta == Decimal("1.000000")

    def test_no_look_ahead(self, static_calendar) -> None:
        panel = _panel(static_calendar)
        before = desk_beta.beta_from_panel(panel, "ABC", D_AS_OF, static_calendar)
        poisoned = copy.deepcopy(panel)
        for sym, rows in poisoned.items():
            for day in list(rows):
                if date.fromisoformat(day) > D_AS_OF:
                    rows[day] = {k: "9999.0" for k in rows[day]}
            rows["2026-09-25"] = {"close": "0.01"}
            rows["2027-01-04"] = {"close": "123"}
            if sym == "ABC":
                for day in [d for d in rows if date.fromisoformat(d) > D_AS_OF][:3]:
                    del rows[day]
        after = desk_beta.beta_from_panel(poisoned, "ABC", D_AS_OF, static_calendar)
        assert after == before

    def test_gaps_drop_returns_and_never_stretch_the_window(self, static_calendar) -> None:
        panel = _panel(static_calendar)
        w = _window(static_calendar, D_AS_OF)
        gapped = copy.deepcopy(panel)
        # five isolated missing name closes inside the window: 2 returns each
        for i in (10, 50, 100, 150, 200):
            del gapped["ABC"][w[i].isoformat()]
        # one missing SPY close: 2 more returns
        del gapped["SPY"][w[230].isoformat()]
        est = desk_beta.beta_from_panel(gapped, "ABC", D_AS_OF, static_calendar)
        want, n = _oracle(gapped, "ABC", w)
        assert n == 252 - 12
        assert est.n_returns == n
        assert est.beta is not None and abs(float(est.beta) - want) < 1e-6
        # the window still starts where a complete window would
        assert est.window_first == w[1]

    def test_missing_first_close_of_the_window(self, static_calendar) -> None:
        panel = _panel(static_calendar)
        w = _window(static_calendar, D_AS_OF)
        del panel["ABC"][w[0].isoformat()]  # the anchor of the first return
        est = desk_beta.beta_from_panel(panel, "ABC", D_AS_OF, static_calendar)
        assert est.n_returns == 251

    def test_too_few_returns_is_unavailable(self, static_calendar) -> None:
        panel = _panel(static_calendar)
        w = _window(static_calendar, D_AS_OF)
        for d in w[1:53]:  # 52 missing closes: 53 returns lost -> 199 < 200
            del panel["ABC"][d.isoformat()]
        est = desk_beta.beta_from_panel(panel, "ABC", D_AS_OF, static_calendar)
        assert est.n_returns == 199
        assert est.beta is None
        assert "199" in est.reason

    def test_exactly_min_returns_is_available(self, static_calendar) -> None:
        panel = _panel(static_calendar)
        w = _window(static_calendar, D_AS_OF)
        for d in w[1:52]:  # 51 missing -> 52 returns lost -> 200
            del panel["ABC"][d.isoformat()]
        est = desk_beta.beta_from_panel(panel, "ABC", D_AS_OF, static_calendar)
        assert est.n_returns == 200
        assert est.beta is not None

    def test_bad_closes_count_as_missing(self, static_calendar) -> None:
        panel = _panel(static_calendar)
        w = _window(static_calendar, D_AS_OF)
        panel["ABC"][w[40].isoformat()]["close"] = "0"
        panel["ABC"][w[80].isoformat()]["close"] = "n/a"
        est = desk_beta.beta_from_panel(panel, "ABC", D_AS_OF, static_calendar)
        assert est.n_returns == 252 - 4

    def test_weekend_as_of_uses_the_last_session(self, static_calendar) -> None:
        panel = _panel(static_calendar)
        fri = desk_beta.beta_from_panel(panel, "ABC", date(2026, 9, 25), static_calendar)
        sat = desk_beta.beta_from_panel(panel, "ABC", date(2026, 9, 26), static_calendar)
        assert (sat.beta, sat.n_returns, sat.session) == (
            fri.beta,
            fri.n_returns,
            date(2026, 9, 25),
        )
        assert sat.as_of == date(2026, 9, 26)

    def test_unknown_symbol_is_unavailable(self, static_calendar) -> None:
        est = desk_beta.beta_from_panel(_panel(static_calendar), "NOPE", D_AS_OF, static_calendar)
        assert est.beta is None and est.n_returns == 0

    def test_no_benchmark_is_unavailable(self, static_calendar) -> None:
        panel = _panel(static_calendar)
        del panel["SPY"]
        est = desk_beta.beta_from_panel(panel, "ABC", D_AS_OF, static_calendar)
        assert est.beta is None

    def test_calendar_too_short_is_unavailable(self, static_calendar) -> None:
        est = desk_beta.beta_from_panel(
            _panel(static_calendar), "ABC", date(2018, 3, 1), static_calendar
        )
        assert est.beta is None


class TestLoadBetas:
    def test_reads_the_panel_file(self, static_calendar, tmp_path: Path) -> None:
        panel = _panel(static_calendar)
        path = tmp_path / "ohlc-panel.json"
        path.write_text(json.dumps(panel))
        got = desk_beta.load_betas(["ABC", "SPY"], D_AS_OF, static_calendar, panel_path=path)
        assert got["ABC"] == desk_beta.beta_from_panel(panel, "ABC", D_AS_OF, static_calendar)
        assert got["SPY"].beta == Decimal("1.000000")
        assert json.loads(path.read_text()) == panel  # read-only

    def test_default_path_is_the_paper_panel(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setenv("DESK_PAPER_DIR", str(tmp_path))
        assert desk_beta.default_panel_path() == tmp_path / "ohlc-panel.json"


@pytest.mark.parametrize("window", [0, 1])
def test_degenerate_window_is_refused(window: int, static_calendar) -> None:
    with pytest.raises(ValueError):
        desk_beta.beta_from_panel(
            _panel(static_calendar), "ABC", D_AS_OF, static_calendar, window=window
        )
