"""DESK-BT-001 runner tests: seal enforcement, no-lookahead, the sealed
arithmetic (haircut, ranks, moneyness on the forward, exit floor), the
placebo draw and path identity, the calendar correction, and the statistics
(DSR, clustered paired Student t) against hand-computed values.

The synthetic corpus (bt001_corpus) provides the end-to-end surface; small
hand-built fixtures pin the exact arithmetic. Nothing here reads or writes
the serving checkout's artifacts."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import NormalDist

import pytest

from tree_options.desk import bt001, bt001_corpus, ivhist
from tree_options.desk.bt001_corpus import WeekdayCalendar, day_offset, weekdays
from tree_options.desk.ivhist import RateSource
from tree_options.desk.sessions import first_session_after
from tree_options.trex.clock import session_calendar

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "docs" / "desk"
PROTOCOL_CALENDAR = REPO / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
CORPUS_WINDOW = (date(2024, 6, 1), date(2025, 3, 31))


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> bt001_corpus.SyntheticWorld:
    return bt001_corpus.synthetic_world(tmp_path_factory.mktemp("bt001-world"))


def _run_corpus(
    w: bt001_corpus.SyntheticWorld, out: Path, **overrides: object
) -> dict[str, object]:
    inputs = w.inputs(docs=DOCS, repo=REPO, code_commit="test", now=NOW)
    return bt001.run(
        inputs,
        out_dir=out,
        store_pins=w.store_pins,
        slice_pins=w.slice_pins,
        scored=False,
        **overrides,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- seals


class TestSealEnforcement:
    def test_sealed_documents_match_the_runner_pins(self) -> None:
        recorded = bt001.verify_seals(DOCS, REPO)
        assert recorded["docs/desk/DESK-BT-001.md"] == bt001.SPEC_SHA256
        assert recorded["docs/desk/DESK-BT-001-AMENDMENT-1.md"] == bt001.AMENDMENT_1_SHA256
        assert recorded["docs/desk/DESK-BT-001-clarifications.md"] == bt001.CLARIFICATIONS_SHA256

    @pytest.mark.parametrize(
        "name",
        ["DESK-BT-001.md", "DESK-BT-001-AMENDMENT-1.md", "DESK-BT-001-clarifications.md"],
    )
    def test_edited_sealed_document_is_refused(self, tmp_path: Path, name: str) -> None:
        docs = tmp_path / "desk"
        docs.mkdir()
        for doc in (
            "DESK-BT-001.md",
            "DESK-BT-001-AMENDMENT-1.md",
            "DESK-BT-001-clarifications.md",
        ):
            (docs / doc).write_bytes((DOCS / doc).read_bytes())
        text = (docs / name).read_text(encoding="utf-8")
        (docs / name).write_text(text + "\n<!-- edited after seal -->\n", encoding="utf-8")
        with pytest.raises(bt001.Refused, match="is not the sealed"):
            bt001.verify_seals(docs, REPO)

    def test_missing_sealed_document_is_refused(self, tmp_path: Path) -> None:
        docs = tmp_path / "empty-desk"
        docs.mkdir()
        with pytest.raises(bt001.Refused, match="sealed document missing"):
            bt001.verify_seals(docs, REPO)

    def test_moved_calendar_is_refused(self, tmp_path: Path) -> None:
        fake = tmp_path / "calendar.json"
        fake.write_text("[]", encoding="utf-8")
        with pytest.raises(bt001.Refused, match="run calendar"):
            bt001.verify_seals(DOCS, REPO, calendar_path=fake)

    def test_universe_roster_count_is_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bt001, "XSMOM_TRADABLES", bt001.XSMOM_TRADABLES[:10])
        with pytest.raises(bt001.Refused, match="not the sealed 36"):
            bt001.verify_seals(DOCS, REPO)


# ---------------------------------------------------------------- the chain-store pins (A6)


class TestWindowPins:
    def test_wrong_first_twenty_refused(self, world: bt001_corpus.SyntheticWorld) -> None:
        shifted = (
            *world.store_pins.first_twenty[:-1],
            day_offset(world.store_pins.first_twenty[-1], 3),
        )
        pins = bt001.StorePins(
            first_twenty=shifted, manifest_sha256=world.store_pins.manifest_sha256
        )
        with pytest.raises(bt001.Refused, match="not the pinned list"):
            bt001.first_twenty_sessions(world.store, pins)

    def test_edited_manifest_refused(self, tmp_path: Path) -> None:
        w = bt001_corpus.synthetic_world(tmp_path)
        session = w.store_pins.first_twenty[5]
        path = w.store / "manifest" / f"{session.isoformat()}.json"
        path.write_text(path.read_text() + "\n", encoding="utf-8")
        with pytest.raises(bt001.Refused, match="manifests"):
            bt001.first_twenty_sessions(w.store, w.store_pins)

    def test_too_few_recorded_sessions_refused(self, tmp_path: Path) -> None:
        empty = tmp_path / "store"
        (empty / "chains").mkdir(parents=True)
        with pytest.raises(bt001.Refused, match="window is not open"):
            bt001.first_twenty_sessions(empty, bt001.SEALED_STORE_PINS)

    def test_sealed_pins_match_the_clarified_dates(self) -> None:
        assert bt001.FIRST_TWENTY[0] == date(2026, 9, 22)
        assert bt001.FIRST_TWENTY[-1] == date(2026, 10, 20)
        assert date(2026, 9, 23) not in bt001.FIRST_TWENTY  # permanently missing
        assert len(bt001.FIRST_TWENTY) == 20
        assert bt001.EARLIEST_RUN_DATE == date(2026, 10, 21)  # the one-session shift


# ---------------------------------------------------------------- slice pins (A4)


class TestSlicePins:
    def test_panel_history_edit_refused_growth_tolerated(
        self, world: bt001_corpus.SyntheticWorld, tmp_path: Path
    ) -> None:
        panel = json.loads(json.dumps(world.panel))
        cutoff = world.slice_pins.panel_cutoff
        victim = next(d for d in sorted(panel["AAPL"]) if d <= cutoff)
        original = dict(panel["AAPL"][victim])
        panel["AAPL"][victim]["close"] = 1.0  # a re-adjusted historical row
        assert bt001.panel_slice_sha256(panel, cutoff) != world.slice_pins.panel_sha256
        # nightly growth past the cutoff changes nothing the runner consumes
        panel["AAPL"][victim] = original
        grown = day_offset(date.fromisoformat(cutoff), 400).isoformat()
        panel["AAPL"][grown] = {"open": 1.0, "close": 1.0}
        assert bt001.panel_slice_sha256(panel, cutoff) == world.slice_pins.panel_sha256

    def test_dtb3_history_edit_refused(self, tmp_path: Path) -> None:
        w = bt001_corpus.synthetic_world(tmp_path)
        path = w.store / "indices" / "DTB3.csv"
        rows = path.read_text().splitlines()
        rows.append("2023-06-02,,,,9.9")  # a new observation inside the consumed slice
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        assert bt001.dtb3_slice_sha256(path, w.slice_pins.dtb3_cutoff) != w.slice_pins.dtb3_sha256

    def test_earnings_history_edit_refused(self, world: bt001_corpus.SyntheticWorld) -> None:
        earnings = {k: list(v) for k, v in world.earnings.items()}
        cutoff = world.slice_pins.earnings_cutoff
        name = next(n for n in earnings if any(d <= cutoff for d in earnings[n]))
        earnings[name].insert(0, "2024-06-30")  # a historical report appears
        assert bt001.earnings_slice_sha256(earnings, cutoff) != world.slice_pins.earnings_sha256

    def test_run_refuses_a_drifted_panel_slice(
        self, world: bt001_corpus.SyntheticWorld, tmp_path: Path
    ) -> None:
        panel = json.loads(json.dumps(world.panel))
        victim = next(d for d in sorted(panel["AAPL"]) if d <= world.slice_pins.panel_cutoff)
        panel["AAPL"][victim]["close"] = 1.0  # a re-adjusted historical row
        inputs = world.inputs(docs=DOCS, repo=REPO, code_commit="t", now=NOW)
        inputs.panel = panel
        with pytest.raises(bt001.Refused, match="panel consumed-slice"):
            bt001.run(
                inputs,
                out_dir=tmp_path / "out",
                store_pins=world.store_pins,
                slice_pins=world.slice_pins,
                scored=False,
            )


# ---------------------------------------------------------------- no lookahead


class TestNoLookahead:
    def test_signals_depend_only_on_panel_rows_through_their_session(
        self, world: bt001_corpus.SyntheticWorld
    ) -> None:
        cut = date(2024, 11, 15)
        truncated = {
            name: {d: row for d, row in bars.items() if date.fromisoformat(d) <= cut}
            for name, bars in world.panel.items()
        }
        full, _ = bt001.enumerate_entries(
            world.panel, world.earnings, world.cal, window=CORPUS_WINDOW
        )
        trunc, _ = bt001.enumerate_entries(
            truncated, world.earnings, world.cal, window=CORPUS_WINDOW
        )
        assert trunc == [e for e in full if e.session <= cut]

    def test_entry_is_priced_at_the_next_session_after_the_signal(
        self, world: bt001_corpus.SyntheticWorld, tmp_path: Path
    ) -> None:
        result = _run_corpus(world, tmp_path / "out")
        trades = result["trades"]
        assert trades
        assert all(date.fromisoformat(t["e0"]) > date.fromisoformat(t["decision"]) for t in trades)
        assert all(date.fromisoformat(t["exit"]) >= date.fromisoformat(t["e0"]) for t in trades)

    def test_rate_is_read_as_of_the_entry_session(self) -> None:
        e0 = date(2025, 5, 30)
        expiry = bt001_corpus.third_friday(2025, 7)
        bars = [
            ivhist.OptionBar(expiry, "C", 100.0, 5.0),
            ivhist.OptionBar(expiry, "P", 100.0, 5.0),
        ]
        choice, status = bt001.select_structure(bars, e0=e0, spot=100.0, rate=0.01)
        assert status == "EVALUABLE" and choice is not None
        assert choice.forward == pytest.approx(100.0 * math.exp(0.01 * choice.dte / 365.0))
        # rate_on itself never reaches forward in time (ivhist semantics)
        rates = RateSource(label="t", obs=((date(2025, 1, 1), 0.01), (date(2025, 6, 1), 0.20)))
        assert rates.rate_on(e0) == 0.01
        assert rates.rate_on(date(2025, 6, 2)) == 0.20

    def test_equity_card_refuses_rows_past_the_amendment_cutoff(
        self, world: bt001_corpus.SyntheticWorld
    ) -> None:
        with pytest.raises(bt001.Refused, match="consumption cutoff"):
            bt001._equity_return(
                world.panel, "AAPL", date(2025, 5, 1), date(2025, 6, 2), cutoff="2025-04-30"
            )

    def test_equity_card_arithmetic(self, world: bt001_corpus.SyntheticWorld) -> None:
        rows = world.panel["AAPL"]
        d1, d2 = sorted(rows)[10], sorted(rows)[11]
        o, c = float(rows[d1]["open"]), float(rows[d2]["close"])
        assert bt001._equity_return(
            world.panel,
            "AAPL",
            date.fromisoformat(d1),
            date.fromisoformat(d2),
            cutoff="2099-12-31",
        ) == pytest.approx(c / o - 1.0 - bt001.EQUITY_COST)


# ---------------------------------------------------------------- the calendar correction


class TestCalendarCorrection:
    def test_2025_01_09_is_not_a_session_of_the_run_calendar(self) -> None:
        assert not session_calendar().is_session(date(2025, 1, 9))

    def test_the_sealed_protocol_calendar_still_contains_the_phantom(self) -> None:
        doc = json.loads(PROTOCOL_CALENDAR.read_text(encoding="utf-8"))
        sessions = doc["sessions"] if isinstance(doc, dict) else doc
        dates = {date.fromisoformat(s if isinstance(s, str) else s["date"]) for s in sessions}
        assert date(2025, 1, 9) in dates

    def test_the_session_after_2025_01_08_skips_the_phantom(self) -> None:
        assert first_session_after(date(2025, 1, 8), session_calendar()) == date(2025, 1, 10)

    def test_the_correction_only_removes_sessions(self) -> None:
        doc = json.loads(PROTOCOL_CALENDAR.read_text(encoding="utf-8"))
        sessions = doc["sessions"] if isinstance(doc, dict) else doc
        protocol = {date.fromisoformat(s if isinstance(s, str) else s["date"]) for s in sessions}
        # the trex calendar runs to 2028; compare inside the protocol's coverage
        covered = {d for d in session_calendar().sessions() if d <= max(protocol)}
        assert covered <= protocol  # removal only, never a new session
        assert protocol - covered == {date(2025, 1, 9)}  # exactly the phantom


# ---------------------------------------------------------------- haircut


def _write_mini_chain(
    store: Path,
    session: date,
    name: str,
    rows: list[tuple[str, date, float, str, float, float]],
    spot: float = 100.0,
) -> None:
    """rows: (full OCC symbol, expiry, strike, right, bid, ask)."""
    folder = store / "chains" / session.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema": "desk-chain/1",
        "header": {
            "session": session.isoformat(),
            "underlying": name,
            "underlying_quote": {"close": spot},
        },
        "columns": {
            "occ": [occ for occ, *_ in rows],
            "exp": [exp.isoformat() for _occ, exp, *_ in rows],
            "strike": [k for _occ, _exp, k, *_ in rows],
            "right": [right for _occ, _exp, _k, right, *_ in rows],
            "bid": [bid for _occ, _exp, _k, _right, bid, _ask in rows],
            "ask": [ask for _occ, _exp, _k, _right, _bid, ask in rows],
        },
    }
    (folder / f"{name}.json.gz").write_bytes(gzip.compress(json.dumps(doc).encode()))


def _occ(name: str, expiry: date, right: str, strike: float) -> str:
    return f"{name}{expiry.strftime('%y%m%d')}{right}{int(strike * 1000):08d}"


def _banded_sessions(cal: WeekdayCalendar, monthly: date) -> list[date]:
    return [s for s in cal.sessions() if 30 <= (monthly - s).days <= 60]


def _haircut_store(tmp_path: Path) -> tuple[Path, WeekdayCalendar, list[date]]:
    """A dense bar-name (QQQ) whose chains mix qualifying rows with every
    kind of junk row the sealed filter must exclude."""
    cal = WeekdayCalendar(weekdays(date(2025, 1, 1), date(2025, 4, 30)))
    monthly = bt001_corpus.third_friday(2025, 3)  # 2025-03-21
    non_monthly = date(2025, 3, 28)  # the Friday AFTER the third Friday
    sessions = _banded_sessions(cal, monthly)[:20]
    assert len(sessions) == 20
    store = tmp_path / "store"
    for s in sessions:
        _write_mini_chain(
            store,
            s,
            "QQQ",
            [
                (_occ("QQQ", monthly, "C", 100.0), monthly, 100.0, "C", 99.0, 101.0),
                (_occ("QQQ", monthly, "P", 100.0), monthly, 100.0, "P", 49.5, 50.5),
                (_occ("QQQ", monthly, "P", 105.0), monthly, 105.0, "P", 49.5, 50.5),
                (_occ("QQQ", monthly, "C", 95.0), monthly, 95.0, "C", 0.0, 50.0),  # bid 0
                (_occ("QQQ", monthly, "P", 95.0), monthly, 95.0, "P", 60.0, 50.0),  # ask < bid
                (_occ("QQQ", non_monthly, "C", 100.0), non_monthly, 100.0, "C", 99.0, 101.0),
                (_occ("QQQ", monthly, "C", 200.0), monthly, 200.0, "C", 99.0, 101.0),  # far strike
                ("B1QQQ" + monthly.strftime("%y%m%d") + "C00100000", monthly, 100.0, "C", 1.0, 2.0),
            ],
        )
    return store, cal, sessions


class TestHaircut:
    def test_median_over_qualifying_contract_sessions_only(self, tmp_path: Path) -> None:
        store, cal, sessions = _haircut_store(tmp_path)
        rates = RateSource(label="t", obs=((date(2025, 1, 1), 0.04),))
        haircuts = bt001.measure_haircuts(store, sessions, cal, rates)
        # three qualifying rows per session (two 0.01 half-spreads at 100 and
        # one at 105), 60 contract-sessions in all; every junk row is excluded
        assert haircuts.counts["QQQ"] == 60
        assert haircuts.h["QQQ"] == pytest.approx(0.01)
        assert "QQQ" not in haircuts.pooled_names

    def test_pooled_fallback_below_50_contract_sessions(self, tmp_path: Path) -> None:
        store, cal, sessions = _haircut_store(tmp_path)
        monthly = bt001_corpus.third_friday(2025, 3)
        for s in sessions[:15]:  # a bar-name with only 15 qualifying sessions
            _write_mini_chain(
                store,
                s,
                "SPY",
                [
                    (_occ("SPY", monthly, "C", 100.0), monthly, 100.0, "C", 47.0, 53.0),
                ],
            )
        rates = RateSource(label="t", obs=((date(2025, 1, 1), 0.04),))
        haircuts = bt001.measure_haircuts(store, sessions, cal, rates)
        assert haircuts.counts["SPY"] == 15
        assert "SPY" in haircuts.pooled_names
        pool = sorted([0.01] * 60 + [0.06] * 15)
        expected = pool[37]  # 75 values: the middle one
        assert haircuts.pooled_value == pytest.approx(expected)
        assert haircuts.h["SPY"] == pytest.approx(haircuts.pooled_value)
        assert haircuts.pooled_pool_n == 75

    def test_nonstandard_root_rows_never_qualify(self, tmp_path: Path) -> None:
        store, cal, sessions = _haircut_store(tmp_path)
        monthly = bt001_corpus.third_friday(2025, 3)
        for s in sessions:
            # three standard rows at half-spread 1/3, plus B1VDA-style adjunct
            # junk at half-spread ~0.95: if any junk row counted, h moves
            _write_mini_chain(
                store,
                s,
                "NVDA",
                [
                    (
                        "B1VDA" + monthly.strftime("%y%m%d") + "C00100000",
                        monthly,
                        100.0,
                        "C",
                        1.0,
                        39.0,
                    ),
                    (_occ("NVDA", monthly, "C", 100.0), monthly, 100.0, "C", 1.0, 2.0),
                    (_occ("NVDA", monthly, "P", 100.0), monthly, 100.0, "P", 1.0, 2.0),
                    (_occ("NVDA", monthly, "P", 105.0), monthly, 105.0, "P", 1.0, 2.0),
                ],
            )
        rates = RateSource(label="t", obs=((date(2025, 1, 1), 0.04),))
        haircuts = bt001.measure_haircuts(store, sessions, cal, rates)
        assert haircuts.counts["NVDA"] == 60  # only the standard rows count
        assert haircuts.h["NVDA"] == pytest.approx(1 / 3)

    def test_forward_uses_the_chain_session_spot_and_rate(self, tmp_path: Path) -> None:
        cal = WeekdayCalendar(weekdays(date(2025, 1, 1), date(2025, 4, 30)))
        monthly = bt001_corpus.third_friday(2025, 3)
        s = _banded_sessions(cal, monthly)[10]
        dte = (monthly - s).days
        store = tmp_path / "store"
        _write_mini_chain(
            store,
            s,
            "QQQ",
            [
                (_occ("QQQ", monthly, "C", 100.0), monthly, 100.0, "C", 99.0, 101.0),
                (_occ("QQQ", monthly, "C", 115.0), monthly, 115.0, "C", 1.0, 1.4),
            ],
        )
        f_hi = 100.0 * math.exp(0.90 * dte / 365.0)
        assert abs(math.log(115.0 / f_hi)) <= 0.10  # in band only at the high rate
        assert abs(math.log(100.0 / f_hi)) > 0.10
        hi = bt001.measure_haircuts(
            store, [s], cal, RateSource(label="hi", obs=((date(2025, 1, 1), 0.90),))
        )
        lo = bt001.measure_haircuts(
            store, [s], cal, RateSource(label="lo", obs=((date(2025, 1, 1), 0.04),))
        )
        assert hi.counts["QQQ"] == 1  # the 115 strike, half-spread (1.4-1.0)/2.4
        assert hi.h["QQQ"] == pytest.approx(0.4 / 2.4)
        assert lo.counts["QQQ"] == 1  # the 115 strike falls out at the low rate
        assert lo.h["QQQ"] == pytest.approx(2.0 / 200.0)


# ---------------------------------------------------------------- structures


def _bars(expiry: date, strikes: list[float], rights: str = "CP") -> list[ivhist.OptionBar]:
    out = []
    for k in strikes:
        for right in rights:
            out.append(ivhist.OptionBar(expiry, right, k, 5.0 + k / 100.0))
    return out


FEB18 = date(2025, 2, 18)  # a Tuesday with both Mar 21 and Apr 18 in 30..60 DTE


class TestStructureSelection:
    def test_latest_banded_monthly_wins(self) -> None:
        near, far = bt001_corpus.third_friday(2025, 3), bt001_corpus.third_friday(2025, 4)
        assert 30 <= (near - FEB18).days <= 60 and 30 <= (far - FEB18).days <= 60
        bars = _bars(near, [100.0]) + _bars(far, [100.0])
        choice, status = bt001.select_structure(bars, e0=FEB18, spot=100.0, rate=0.04)
        assert status == "EVALUABLE" and choice is not None
        assert choice.expiry == far

    def test_moneyness_is_tested_on_the_forward_not_the_spot(self) -> None:
        expiry = bt001_corpus.third_friday(2025, 4)
        dte = (expiry - FEB18).days
        rate, spot, strike = 1.20, 100.0, 102.0
        f = spot * math.exp(rate * dte / 365.0)
        assert abs(strike / spot - 1.0) <= 0.03  # the replay scaffold would accept
        assert abs(math.log(strike / f)) > 0.03  # the seal refuses
        choice, status = bt001.select_structure(
            _bars(expiry, [strike]), e0=FEB18, spot=spot, rate=rate
        )
        assert status == "moneyness_out" and choice is None

    def test_strike_ranks_count_distinct_strikes_not_per_right(self) -> None:
        expiry = bt001_corpus.third_friday(2025, 4)
        calls = [90.0, 95.0, 100.0, 105.0, 110.0]
        # 92 below K0 and 103/104 above K0 are put-only: the DISTINCT ladder
        # is 90 92 95 100 103 104 105 110, the per-right call ladder is
        # 90 95 100 105 110 -- the two pick different S2 short strikes
        puts = [90.0, 92.0, 95.0, 100.0, 103.0, 104.0, 105.0, 110.0]
        bars = _bars(expiry, calls, "C") + _bars(expiry, puts, "P")
        choice, status = bt001.select_structure(bars, e0=FEB18, spot=100.0, rate=0.0)
        assert status == "EVALUABLE" and choice is not None
        assert choice.k0 == 100.0
        assert choice.strikes == (90.0, 92.0, 95.0, 100.0, 103.0, 104.0, 105.0, 110.0)
        legs = bt001.structure_legs("S2", choice)
        assert legs is not None
        # two DISTINCT-ladder ranks up lands on put-only 104 (the per-right
        # call ladder would say 105); the leg rule then refuses it at pricing
        assert legs[1] == ("C", 104.0, -1)
        assert bt001.structure_legs("S3", choice) == (("P", 95.0, -1), ("P", 90.0, 1))

    def test_selected_rank_without_the_legs_right_is_not_evaluable(self) -> None:
        expiry = bt001_corpus.third_friday(2025, 4)
        cal = WeekdayCalendar(weekdays(date(2025, 1, 1), date(2025, 6, 30)))
        bars = _bars(expiry, [90.0, 100.0], "C") + _bars(expiry, [90.0, 100.0, 103.0, 105.0], "P")
        choice, status = bt001.select_structure(bars, e0=FEB18, spot=100.0, rate=0.0)
        assert status == "EVALUABLE" and choice is not None
        assert choice.k0 == 100.0
        assert choice.strikes == (90.0, 100.0, 103.0, 105.0)
        # two ranks up is 105, which has no call bar: the leg rule refuses
        assert bt001.structure_legs("S2", choice) == (("C", 100.0, 1), ("C", 105.0, -1))
        scan = ivhist.CacheScan(
            source="fixture",
            options={"AAA": {FEB18: bars}},
            spot={"AAA": {FEB18: 100.0}},
        )
        priced, pstatus = bt001.price_trade(
            "AAA", FEB18, "S2", scan, cal, RateSource.constant(0.0), {"AAA": 0.02}
        )
        assert priced is None
        assert pstatus == "missing_leg_bar"

    def test_exit_walks_back_to_the_last_session_with_dte_at_least_7(self) -> None:
        cal = WeekdayCalendar(weekdays(date(2025, 1, 1), date(2025, 8, 29)))
        sessions = cal.sessions()
        near, far = bt001_corpus.third_friday(2025, 2), bt001_corpus.third_friday(2025, 4)
        e0 = next(s for s in sessions if (near - s).days == 32)
        i = cal.ordinal(e0)
        # far expiry: E0 + 20 sessions keeps DTE >= 7 -> the 20-session hold binds
        assert bt001.exit_session(e0, far, cal) == sessions[i + 20]
        # near expiry: DTE at E0+20 drops under 7 -> walk back to DTE >= 7
        limit = bt001.exit_session(e0, near, cal)
        assert limit is not None and limit < sessions[i + 20]
        assert (near - limit).days >= 7
        assert (near - sessions[cal.ordinal(limit) + 1]).days < 7

    def test_fill_prices_apply_the_haircut_by_side(self) -> None:
        legs = (("C", 100.0, 1),)
        marks = {("C", 100.0): 5.0}
        assert bt001._cash(legs, marks, 0.02, entry=True) == pytest.approx(
            -(5.0 * 1.02 * 100 + 0.65)
        )
        assert bt001._cash(legs, marks, 0.02, entry=False) == pytest.approx(5.0 * 0.98 * 100 - 0.65)

    def test_max_loss_shapes(self, world: bt001_corpus.SyntheticWorld) -> None:
        e0 = date(2024, 6, 4)
        haircuts = {n: 0.02 for n in world.bar_names}
        s1, st1 = bt001.price_trade("AAPL", e0, "S1", world.scan, world.cal, world.rates, haircuts)
        s2, st2 = bt001.price_trade("AAPL", e0, "S2", world.scan, world.cal, world.rates, haircuts)
        s3, st3 = bt001.price_trade("AAPL", e0, "S3", world.scan, world.cal, world.rates, haircuts)
        assert (st1, st2, st3) == ("EVALUABLE",) * 3
        assert s1 is not None and s2 is not None and s3 is not None
        # corpus prices at E0: call 5.5, exit 6.05; S1 debit incl. commissions
        assert s1.max_loss_base == pytest.approx(5.5 * 1.02 * 100 + 2 * 0.65)
        assert s1.pnl_base == pytest.approx(-5.5 * 1.02 * 100 - 0.65 + 6.05 * 0.98 * 100 - 0.65)
        # S2: the debit paid including all four commissions. The ladder is
        # 85..115, K0=100 sits at index 3, two DISTINCT-strike ranks up is
        # 110 (entry call 1.2), not 105
        assert s2.legs == (("C", 100.0, 1), ("C", 110.0, -1))
        assert s2.max_loss_base == pytest.approx(5.5 * 1.02 * 100 - 1.2 * 0.98 * 100 + 4 * 0.65)
        # S3: width - credit received + all four commissions
        credit = 2.6 * 0.98 * 100 - 0.5 * 1.02 * 100  # sell P95, buy P85
        assert s3.max_loss_base == pytest.approx(10.0 * 100 - credit + 4 * 0.65)

    def test_stress_doubles_the_haircut(self, world: bt001_corpus.SyntheticWorld) -> None:
        priced, status = bt001.price_trade(
            "AAPL", date(2024, 6, 4), "S1", world.scan, world.cal, world.rates, {"AAPL": 0.02}
        )
        assert status == "EVALUABLE" and priced is not None
        assert priced.pnl_base == pytest.approx(-5.5 * 1.02 * 100 - 0.65 + 6.05 * 0.98 * 100 - 0.65)
        r_stress = (-5.5 * 1.04 * 100 - 0.65 + 6.05 * 0.96 * 100 - 0.65) / (
            5.5 * 1.04 * 100 + 2 * 0.65
        )
        assert priced.r_stress == pytest.approx(r_stress)
        assert priced.r_stress != pytest.approx(priced.r_base)


# ---------------------------------------------------------------- placebo


class TestPlacebo:
    def test_draw_is_the_hash_index_of_the_sorted_candidates(self) -> None:
        e0 = date(2026, 8, 4)
        candidates = ["QQQ", "AMD", "SPY", "HD"]
        token = f"DESK-BT-001-A1|{e0.isoformat()}|NVDA|S2"
        want = int(hashlib.sha256(token.encode()).hexdigest()[:16], 16) % 4
        assert bt001.draw_placebo(e0, "NVDA", "S2", candidates) == sorted(candidates)[want]

    def test_the_structure_token_changes_the_draw(self) -> None:
        e0 = date(2026, 8, 4)
        candidates = ["QQQ", "AMD", "SPY", "HD"]
        draws = {s: bt001.draw_placebo(e0, "NVDA", s, candidates) for s in bt001.STRUCTURES}
        assert len(set(draws.values())) > 1  # hash-sensitive to S1/S2/S3 (A2)

    def test_placebo_priced_on_the_identical_path(
        self, world: bt001_corpus.SyntheticWorld, tmp_path: Path
    ) -> None:
        result = _run_corpus(world, tmp_path / "out")
        pair = next(p for p in result["placebo_pairs"] if p["status"] == "PAIRED")
        signal = next(
            t
            for t in result["trades"]
            if t["name"] == pair["name"]
            and t["structure"] == pair["structure"]
            and t["e0"] == pair["e0"]
        )
        placebo, status = bt001.price_trade(
            pair["placebo"],
            date.fromisoformat(pair["e0"]),
            pair["structure"],
            world.scan,
            world.cal,
            world.rates,
            {n: bt001_corpus.HAIRCUT_HALFSPREAD for n in world.bar_names},
        )
        assert status == "EVALUABLE" and placebo is not None
        assert pair["d"] == pytest.approx(signal["r_base"] - placebo.r_base)

    def test_no_candidate_drops_the_pair_and_counts_it(
        self,
        world: bt001_corpus.SyntheticWorld,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # every (name, E0) that is not itself an entry prices as
        # non-evaluable -> no candidates anywhere; the signal trades stay
        # evaluable and every pair is dropped and counted
        from tree_options.desk.sessions import first_session_after as next_session

        entries, _ = bt001.enumerate_entries(
            world.panel, world.earnings, world.cal, window=CORPUS_WINDOW
        )
        entry_keys = {(e.name, next_session(e.session, world.cal)) for e in entries}
        real = bt001.price_trade

        def picky(name: str, e0: date, structure: str, scan, cal, rates, haircuts):
            if (name, e0) in entry_keys:
                return real(name, e0, structure, scan, cal, rates, haircuts)
            return None, "forced_not_evaluable"

        monkeypatch.setattr(bt001, "price_trade", picky)
        result = _run_corpus(world, tmp_path / "out")
        assert result["trades"], "the signal names still price"
        assert all(v > 0 for v in result["placebo_no_candidate"].values())
        assert result["placebo_pairs"]
        assert all(p["status"] == "no_candidate" for p in result["placebo_pairs"])


# ---------------------------------------------------------------- statistics


class TestStatistics:
    def test_student_t_sf_against_an_independent_oracle(self) -> None:
        def pdf(x: float, nu: int) -> float:
            c = math.exp(math.lgamma((nu + 1) / 2) - math.lgamma(nu / 2))
            return c / math.sqrt(nu * math.pi) * (1 + x * x / nu) ** (-(nu + 1) / 2)

        def sf_quad(x: float, nu: int) -> float:
            upper, n = 800.0, 400_000  # far enough for the nu=3 heavy tail
            h = (upper - x) / n
            total = pdf(x, nu) + pdf(upper, nu)
            for i in range(1, n):
                total += pdf(x + i * h, nu) * (4 if i % 2 else 2)
            return total * h / 3

        for t, nu in ((2.0, 10), (1.0, 9), (3.0, 25), (0.5, 3)):
            assert bt001.student_t_sf_one_sided(t, nu) == pytest.approx(sf_quad(t, nu), abs=2e-8)
        assert bt001.student_t_sf_one_sided(2.0, 10) == pytest.approx(0.0366940174, abs=2e-10)
        assert bt001.student_t_sf_one_sided(1.0, 9) == pytest.approx(0.1717181981, abs=2e-10)

    def test_clustered_paired_t_hand_computed(self) -> None:
        means = [0.5, 1.0, -0.2, 0.8, 0.3, 1.2, -0.1, 0.4, 0.9, 0.55]
        diffs: list[float] = []
        clusters: list[str] = []
        for i, m in enumerate(means):
            diffs += [m, m]  # two trades per entry session, same mean
            clusters += [f"2026-{i + 1:02d}-02"] * 2
        res = bt001.clustered_paired_test(diffs, clusters)
        assert res is not None
        assert res.k == 10
        assert res.t == pytest.approx(3.701410375984, abs=1e-9)
        assert res.p == pytest.approx(0.002454913911, abs=1e-9)

    def test_cluster_below_ten_is_not_evaluable(self) -> None:
        res = bt001.clustered_paired_test([0.5] * 18, [f"d{i // 2}" for i in range(18)])
        assert res is not None and res.k == 9

    def test_dsr_hand_computed(self) -> None:
        returns = [
            0.02,
            -0.01,
            0.05,
            0.03,
            -0.02,
            0.04,
            0.01,
            -0.005,
            0.025,
            0.015,
            0.03,
            -0.02,
            0.01,
            0.04,
            -0.01,
            0.02,
            0.05,
            -0.03,
            0.012,
            0.008,
        ]
        assert bt001.deflated_sharpe(returns, 0.02) == pytest.approx(0.937123921107, abs=1e-9)
        m = bt001.sharpe_and_moments(returns)
        assert m["SR"] == pytest.approx(0.564394854930, abs=1e-9)
        # N = 6 always: the pinned SR0
        nd, gamma = NormalDist(), bt001.EULER_GAMMA
        sr0 = math.sqrt(0.02) * (
            (1 - gamma) * nd.inv_cdf(1 - 1 / 6) + gamma * nd.inv_cdf(1 - 1 / (6 * math.e))
        )
        assert sr0 == pytest.approx(0.183867673516, abs=1e-9)

    def test_dsr_falls_as_the_cross_trial_variance_rises(self) -> None:
        returns = [
            0.05,
            0.01,
            0.04,
            0.02,
            0.03,
            0.05,
            0.0,
            0.02,
            0.03,
            0.04,
            0.01,
            0.05,
            0.02,
            0.03,
            0.04,
            0.0,
            0.02,
            0.05,
            0.03,
            0.01,
        ]
        hi = bt001.deflated_sharpe(returns, 0.10)
        lo = bt001.deflated_sharpe(returns, 0.001)
        assert hi is not None and lo is not None and hi < lo


# ---------------------------------------------------------------- verdicts


def _variant_rows(structure: str, n: int, r: float, signal: str = "xsmom_top3") -> list[dict]:
    return [
        {
            "signal": signal,
            "name": f"N{i % 7}",
            "decision": f"2026-{i % 12 + 1:02d}-{i % 12 + 2:02d}",
            "e0": f"2026-{i % 12 + 1:02d}-{i % 12 + 3:02d}",  # 12 clusters
            "structure": structure,
            # vary the returns: a constant r would give sd = 0 and no SR
            "r_base": r + (i % 5) * 0.01 - 0.02,
            "r_stress": (r + (i % 5) * 0.01 - 0.02) / 2,
        }
        for i in range(n)
    ]


class TestVariantVerdicts:
    @staticmethod
    def _table(rows: list[dict], d: float, equity: dict | None = None) -> dict:
        pairs = [
            {
                "signal": r["signal"],
                "structure": r["structure"],
                "e0": r["e0"],
                "name": r["name"],
                "d": d,
                "status": "PAIRED",
            }
            for r in rows
        ]
        return bt001._variant_table(rows, pairs, equity or {})

    def test_below_twenty_trades_is_not_evaluable(self) -> None:
        rows = _variant_rows("S1", 19, 0.1)
        cell = self._table(rows, 0.1)["xsmom_top3|S1"]
        assert cell["verdict"] == "NOT_EVALUABLE" and cell["reason"] == "T=19 < 20"

    def test_fewer_than_ten_clusters_is_not_evaluable(self) -> None:
        rows = [
            {
                "signal": "xsmom_top3",
                "name": f"N{i}",
                "decision": "2026-01-05",
                "e0": "2026-01-06",
                "structure": "S1",
                "r_base": 0.1,
                "r_stress": 0.05,
            }
            for i in range(25)
        ]
        cell = self._table(rows, 0.1)["xsmom_top3|S1"]
        assert cell["verdict"] == "NOT_EVALUABLE" and "k=1 < 10" in cell["reason"]

    def test_failing_placebo_test_fails_the_variant(self) -> None:
        rows = (
            _variant_rows("S1", 30, 0.10)
            + _variant_rows("S2", 30, 0.10)
            + _variant_rows("S3", 30, 0.10)
        )
        cell = self._table(rows, -0.2)["xsmom_top3|S1"]  # (b) fails, (a) passes
        assert cell["a"] is True and cell["b"] is False
        assert cell["verdict"] == "FAIL"

    def test_negative_at_stress_fails_the_variant(self) -> None:
        rows = (
            _variant_rows("S1", 30, 0.10)
            + _variant_rows("S2", 30, 0.10)
            + _variant_rows("S3", 30, 0.10)
        )
        for r in rows:
            r["r_stress"] = -0.05  # positive at h, negative at 2h
        cell = self._table(rows, 0.1)["xsmom_top3|S1"]
        assert cell["a"] is True and cell["d"] is False
        assert cell["verdict"] == "FAIL"

    def test_equity_dominated_label(self) -> None:
        rows = (
            _variant_rows("S1", 30, 0.02)
            + _variant_rows("S2", 30, 0.02)
            + _variant_rows("S3", 30, 0.02)
        )
        # a wildly better, non-degenerate equity card
        s1 = [r for r in rows if r["structure"] == "S1"]
        equity = {
            (r["signal"], r["name"], r["decision"], r["structure"]): 0.25 + (j % 7) * 0.03
            for j, r in enumerate(s1)
        }
        card = self._table(rows, 0.1, equity)["xsmom_top3|S1"]["equity_card"]
        assert card["equity-dominated"] is True


# ---------------------------------------------------------------- end to end


class TestDryRunEndToEnd:
    def test_corpus_run_self_checks_and_one_shot_refusal(
        self, world: bt001_corpus.SyntheticWorld, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        result = _run_corpus(world, out)
        assert len(bt001_corpus.self_check(result, world)) >= 10
        assert (out / "DESK-BT-001.json").exists()
        md = (out / "DESK-BT-001.md").read_text(encoding="utf-8")
        assert "MODELED" in md and "xsmom_top3" in md
        with pytest.raises(bt001.Refused, match="one scored run"):
            _run_corpus(world, out)

    def test_the_stamp_carries_the_provenance_pins(
        self, world: bt001_corpus.SyntheticWorld, tmp_path: Path
    ) -> None:
        result = _run_corpus(world, tmp_path / "out")
        stamp = result["stamp"]
        assert stamp["scan"]["max_bar_session"]
        assert stamp["first_twenty_chain_sessions"] == [
            d.isoformat() for d in world.store_pins.first_twenty
        ]
        assert stamp["seals_sha256"]["docs/desk/DESK-BT-001.md"] == bt001.SPEC_SHA256
        assert stamp["inputs_sha256"]["ohlc-panel.consumed_slice"]

    def test_run_date_before_the_twentieth_session_is_refused(
        self, world: bt001_corpus.SyntheticWorld, tmp_path: Path
    ) -> None:
        inputs = world.inputs(
            docs=DOCS, repo=REPO, code_commit="t", now=datetime(2025, 4, 2, tzinfo=UTC)
        )
        with pytest.raises(bt001.Refused, match="not after the 20th recorded"):
            bt001.run(
                inputs,
                out_dir=tmp_path / "out",
                store_pins=world.store_pins,
                slice_pins=world.slice_pins,
                scored=False,
            )
