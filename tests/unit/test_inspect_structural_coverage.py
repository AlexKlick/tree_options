"""WS-D2: the Massive free-tier STRUCTURAL coverage inspector (hermetic).

Every number asserted here is hand-computed from
`tests/fixtures/massive_structural_sample.py` — no network, no API key, no
absolute host paths, and no dependency on WS-D1's client module landing
(the inspector consumes captured payloads; it only PROBES that module for
provenance, which is exercised with stubs).

Hand-computed map (fixture docstring carries the roster):

SPY 2025-03-05 — 7 contracts, strikes {560,570,580,600,650} (depth 5),
  expiries {03-07, 04-04, 06-20, 2026-06-19} (4). DTE 2,2,30,30,30,107,471
  -> dte_0_7=2, dte_30_60=3, dte_61_180=1, dte_gt_365=1. One expiry inside
  the protocol 30-60 band (04-04); two quarterly (2025-06-20, 2026-06-19);
  one LEAPS. spc {100: 6, 80: 1}; roots {SPY: 6, SPY1: 1} -> MULTI-ROOT.
SPY 2025-03-06 — 8 contracts (CID_P2 born), DTE 1,1,29,29,29,29,106,470 ->
  dte_0_7=2, dte_8_29=4, dte_61_180=1, dte_gt_365=1 and ZERO expiries in
  the 30-60 band. Delivered as a TWO-PAGE capture.
SPY 2025-03-07 — 6 contracts: CID_C3 and CID_A1 vanish while un-expired (2 active
  delistings, which also retires the SPY1 root) and CID_C4 is restated from
  100 to 80 shares_per_contract. DTE 0,0,28,28,105,469.
I:SPX 2025-03-07 — 3 european contracts across roots SPX + SPXW (the
  M4-A collision), DTE 0/14/35, one expiry in the band, one quarterly.

Bars — CID_L1 on 04-14/15/16/17 + 04-21 (volumes 250/100/0/40/300: total 690,
median 100, nearest-rank p90 300, one zero-volume bar, three bars at or
above min_same_day_volume=100) with 2025-04-18 the WEEKDAY GAP and
04-19/04-20 the expected weekend skip; X3 on 04-08/09 (volumes 7/9); and
one unmatched QQQ series that no master owns.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT
from tests.fixtures import massive_structural_sample as fx

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import inspect_structural_coverage as structural  # type: ignore[import-not-found]  # scripts/

AS1 = date(2025, 3, 5)
AS2 = date(2025, 3, 6)
AS3 = date(2025, 3, 7)
CID_C1 = "O:SPY250307C00560000"
CID_C3 = "O:SPY250404C00580000"
CID_C4 = "O:SPY250620C00600000"
CID_A1 = "O:SPY1250404C00570000"
CID_L1 = "O:SPY260619C00650000"
CID_P2 = "O:SPY250404P00570000"


@pytest.fixture(scope="module")
def captures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("massive-captures")
    fx.write_masters(root / "masters")
    fx.write_bars(root / "bars")
    fx.write_json(root / "spot.json", fx.SPOT_PROXY_PAYLOAD)
    return root


@pytest.fixture(scope="module")
def masters(captures: Path) -> tuple[structural.ContractMaster, ...]:
    return structural.load_contract_masters(captures / "masters")


@pytest.fixture(scope="module")
def report(captures: Path, masters: tuple[structural.ContractMaster, ...]):  # type: ignore[no-untyped-def]
    return structural.build_structural_report(
        masters, bars=structural.load_bar_series(captures / "bars")
    )


def _underlying(report, sid: str):  # type: ignore[no-untyped-def]
    match = [u for u in report.underlyings if u.underlying == sid]
    assert len(match) == 1
    return match[0]


def _slice(structure, as_of: date):  # type: ignore[no-untyped-def]
    match = [s for s in structure.slices if s.as_of == as_of]
    assert len(match) == 1
    return match[0]


def _parse(text: str, *, source: str = "capture.json", as_of: date | None = None):  # type: ignore[no-untyped-def]
    return structural.parse_contract_master(
        structural.decode_payload(text, source=source), source=source, as_of=as_of
    )


# ---- the live vendor shapes ---------------------------------------------------


def test_real_capture_slice_keeps_exact_decimal_strikes() -> None:
    """The verbatim live slice parses, and `strike_price: 587.5` survives as a
    Decimal built from the raw token (never a float)."""
    master = _parse(fx.REAL_CONTRACTS_PAYLOAD, source="real_contracts.json")
    assert master.underlying == "SPY"
    assert master.as_of == AS3  # from the capture envelope, not the vendor body
    assert len(master.contracts) == 3
    strikes = [c.strike for c in master.contracts]
    assert strikes == [Decimal("560"), Decimal("587.5"), Decimal("375")]
    assert str(strikes[1]) == "587.5"
    fractional = master.contracts[1]
    assert fractional.ticker == "O:SPY250314C00587500"
    assert fractional.root == "SPY"
    assert fractional.expiration == date(2025, 3, 14)
    assert fractional.cfi == "OCASPS"
    assert fractional.primary_exchange == "BATO"
    assert fractional.shares_per_contract == Decimal("100")
    # The last page still carried next_url: the universe is UNDER-counted.
    assert master.capture_complete is False
    assert master.unknown_result_keys == ()


def test_not_authorized_body_refuses_even_at_http_200() -> None:
    payload = structural.decode_payload(fx.NOT_AUTHORIZED_PAYLOAD, source="snapshot.json")
    with pytest.raises(structural.StructuralCoverageError, match="NOT_AUTHORIZED"):
        structural.require_ok_status(payload, where="snapshot.json")
    with pytest.raises(structural.StructuralCoverageError, match="not entitled"):
        structural.parse_bar_series(payload, source="snapshot.json", ticker=CID_C1)


def test_real_bar_slice_sessions_volume_and_exact_vwap() -> None:
    series = structural.parse_bar_series(
        structural.decode_payload(fx.REAL_BARS_PAYLOAD, source="real_bars.json"),
        source="real_bars.json",
    )
    assert series.ticker == "O:SPY250314C00560000"
    assert series.root == "SPY"
    assert series.adjusted is True
    assert [b.session for b in series.bars] == [
        date(2025, 2, 3),
        date(2025, 2, 4),
        date(2025, 2, 5),
    ]
    assert [b.volume for b in series.bars] == [Decimal(25), Decimal(4), Decimal(1)]
    assert series.bars[1].vwap == Decimal("45.875")  # exact vendor text
    assert [b.transactions for b in series.bars] == [1, 4, 1]
    volume = structural._volume_stats(series.bars)
    assert (volume.total, volume.median, volume.p90) == (Decimal(30), Decimal(4), Decimal(25))


def test_occ_ticker_parsing_and_cross_check_refusals() -> None:
    occ = structural.parse_occ_ticker("O:SPXW250411C05900000")
    assert (occ.root, occ.expiration, occ.call_put, occ.strike) == (
        "SPXW",
        date(2025, 4, 11),
        "C",
        Decimal("5900"),
    )
    assert structural.parse_occ_ticker(CID_A1).root == "SPY1"
    assert structural.parse_occ_ticker("O:SPY250314C00587500").strike == Decimal("587.5")
    with pytest.raises(structural.StructuralCoverageError, match="not an OCC option ticker"):
        structural.parse_occ_ticker("SPY250314C00587500")

    def _one(**kwargs: str):  # type: ignore[no-untyped-def]
        row = structural.decode_payload(fx.contract_result(**kwargs), source="row")
        return structural.parse_contract(row, where="row")

    with pytest.raises(structural.StructuralCoverageError, match="encodes strike"):
        _one(
            ticker=CID_C1,
            underlying="SPY",
            expiration="2025-03-07",
            strike="561",
            contract_type="call",
        )
    with pytest.raises(structural.StructuralCoverageError, match="encodes expiry"):
        _one(
            ticker=CID_C1,
            underlying="SPY",
            expiration="2025-03-14",
            strike="560",
            contract_type="call",
        )
    with pytest.raises(structural.StructuralCoverageError, match="encodes C"):
        _one(
            ticker=CID_C1,
            underlying="SPY",
            expiration="2025-03-07",
            strike="560",
            contract_type="put",
        )


# ---- universe, ladder, tenors -------------------------------------------------


def test_universe_sizes_and_strike_ladders(report) -> None:  # type: ignore[no-untyped-def]
    spy = _underlying(report, "SPY")
    assert spy.as_ofs == (AS1, AS2, AS3)
    assert [s.universe_size for s in spy.slices] == [7, 8, 6]
    first = _slice(spy, AS1)
    assert (first.strikes.depth, first.strikes.min_strike, first.strikes.max_strike) == (
        5,
        Decimal(560),
        Decimal(650),
    )
    assert first.strikes.span == Decimal(90)
    # No spot proxy supplied on this report: spot units stay NOT_EVALUABLE.
    assert first.strikes.spot is None
    assert first.strikes.span_in_spot_units is None
    assert _slice(spy, AS3).strikes.depth == 4  # C3's 580 leaves with it
    spx = _underlying(report, "I:SPX")
    grid = _slice(spx, AS3).strikes
    assert (grid.depth, grid.min_strike, grid.max_strike, grid.span) == (
        3,
        Decimal(5700),
        Decimal(5900),
        Decimal(200),
    )
    assert report.contract_rows == 24  # 7 + 8 + 6 + 3
    assert report.distinct_contracts == 11  # 8 SPY + 3 SPX
    assert report.masters == 4
    assert report.as_ofs == (AS1, AS2, AS3)


def test_tenor_histogram_and_protocol_band(report) -> None:  # type: ignore[no-untyped-def]
    spy = _underlying(report, "SPY")
    first = _slice(spy, AS1).tenors
    assert dict(first.buckets) == {
        "dte_0_7": 2,
        "dte_8_29": 0,
        "dte_30_60": 3,
        "dte_61_180": 1,
        "dte_181_365": 0,
        "dte_gt_365": 1,
    }
    assert (first.nearest_days, first.farthest_days) == (2, 471)
    assert first.band_expiries == 1  # only 2025-04-04 sits at 30 DTE
    assert first.quarterly_expiries == 2  # 2025-06-20 and 2026-06-19
    assert first.leaps_expiries == 1
    second = _slice(spy, AS2).tenors
    assert dict(second.buckets) == {
        "dte_0_7": 2,
        "dte_8_29": 4,
        "dte_30_60": 0,
        "dte_61_180": 1,
        "dte_181_365": 0,
        "dte_gt_365": 1,
    }
    assert second.band_expiries == 0  # one day later the whole band is empty
    spx = _slice(_underlying(report, "I:SPX"), AS3).tenors
    assert dict(spx.buckets) == {
        "dte_0_7": 1,
        "dte_8_29": 1,
        "dte_30_60": 1,
        "dte_61_180": 0,
        "dte_181_365": 0,
        "dte_gt_365": 0,
    }
    assert (spx.nearest_days, spx.farthest_days, spx.band_expiries) == (0, 35, 1)
    assert spx.quarterly_expiries == 1  # 2025-03-21, third Friday of March


def test_exercise_style_and_shares_per_contract_distribution(report) -> None:  # type: ignore[no-untyped-def]
    spy_first = _slice(_underlying(report, "SPY"), AS1)
    assert spy_first.exercise_style_counts == (("american", 7),)
    assert spy_first.shares_per_contract_counts == (("80", 1), ("100", 6))
    assert spy_first.non_standard == (CID_A1,)
    assert spy_first.contract_type_counts == (("call", 6), ("put", 1))
    assert spy_first.cfi_counts == (("OCASPS", 6), ("OPASPS", 1))
    spy_last = _slice(_underlying(report, "SPY"), AS3)
    assert spy_last.shares_per_contract_counts == (("80", 1), ("100", 5))
    assert spy_last.non_standard == (CID_C4,)  # CID_A1 is gone; CID_C4 was restated
    spx = _slice(_underlying(report, "I:SPX"), AS3)
    assert spx.exercise_style_counts == (("european", 3),)
    assert spx.shares_per_contract_counts == (("100", 3),)
    assert spx.non_standard == ()
    assert spx.primary_exchange_counts == (("XCBO", 3),)
    assert report.exercise_style_counts == (("american", 21), ("european", 3))
    assert report.shares_per_contract_counts == (("80", 3), ("100", 21))
    assert report.non_standard_rows == 3


def test_adjustment_timeline_is_first_class(report) -> None:  # type: ignore[no-untyped-def]
    spy = _underlying(report, "SPY")
    kinds = [(e.kind, e.as_of, e.ticker or e.root, e.before, e.after) for e in spy.adjustments]
    assert kinds == [
        ("shares_per_contract_change", AS3, CID_C4, "100", "80"),
        ("root_disappeared", AS3, "SPY1", "SPY1", None),
    ]
    assert spy.adjustments[0].previous_as_of == AS2
    assert _underlying(report, "I:SPX").adjustments == ()
    assert report.adjustment_events == 2
    # Contracts that vanish while still un-expired are the same risk seen
    # from the other side, and are surfaced separately.
    assert spy.active_delistings == ((AS3, CID_A1), (AS3, CID_C3))
    assert report.active_delistings == 2


def test_contract_lifecycle_and_births_deaths(report) -> None:  # type: ignore[no-untyped-def]
    spy = _underlying(report, "SPY")
    lifecycle = spy.lifecycle
    assert (lifecycle.as_ofs, lifecycle.distinct_contracts, lifecycle.full_span) == (3, 8, 5)
    entries = {e.ticker: e for e in lifecycle.entries}
    assert (entries[CID_C1].first_seen, entries[CID_C1].last_seen, entries[CID_C1].as_of_count) == (
        AS1,
        AS3,
        3,
    )
    assert (entries[CID_C3].first_seen, entries[CID_C3].last_seen, entries[CID_C3].as_of_count) == (
        AS1,
        AS2,
        2,
    )
    assert (entries[CID_P2].first_seen, entries[CID_P2].last_seen, entries[CID_P2].as_of_count) == (
        AS2,
        AS3,
        2,
    )
    assert entries[CID_A1].last_seen == AS2
    first, second, third = spy.slices
    # The baseline as_of has no predecessor, so births/deaths are null, never 0.
    assert (first.births, first.deaths) == (None, None)
    assert (second.births, second.deaths, second.born) == (1, 0, (CID_P2,))
    assert (third.births, third.deaths) == (0, 2)
    assert third.died_active == (CID_A1, CID_C3)
    assert third.died_expired == ()
    assert _underlying(report, "I:SPX").lifecycle.full_span == 3


def test_multi_root_underlyings_surface_loudly(report) -> None:  # type: ignore[no-untyped-def]
    spy = _underlying(report, "SPY")
    assert spy.roots == ("SPY", "SPY1")
    assert spy.multi_root is True
    assert _slice(spy, AS1).root_counts == (("SPY", 6), ("SPY1", 1))
    assert _slice(spy, AS3).root_counts == (("SPY", 6),)
    assert _slice(spy, AS3).multi_root is False  # SPY1 retired on the last as_of
    spx = _underlying(report, "I:SPX")
    assert spx.roots == ("SPX", "SPXW")
    assert _slice(spx, AS3).root_counts == (("SPX", 1), ("SPXW", 2))
    assert report.multi_root_underlyings == ("I:SPX", "SPY")


# ---- bars, volume, implied session calendar -----------------------------------


def test_bars_volume_and_implied_session_calendar(report) -> None:  # type: ignore[no-untyped-def]
    spy_bars = _underlying(report, "SPY").bars
    assert spy_bars is not None
    assert spy_bars.series == 1
    assert spy_bars.tickers == (CID_L1,)
    volume = spy_bars.volume
    assert volume.bars == 5
    assert volume.total == Decimal(690)  # 250 + 100 + 0 + 40 + 300
    assert (volume.minimum, volume.median, volume.p90, volume.maximum) == (
        Decimal(0),
        Decimal(100),
        Decimal(300),
        Decimal(300),
    )
    assert volume.zero_volume_bars == 1
    assert volume.bars_at_or_above_min == 3  # 100, 250, 300 vs min_same_day_volume=100
    calendar = spy_bars.calendar
    assert calendar.sessions == (
        date(2025, 4, 14),
        date(2025, 4, 15),
        date(2025, 4, 16),
        date(2025, 4, 17),
        date(2025, 4, 21),
    )
    assert (calendar.first, calendar.last, calendar.calendar_days_spanned) == (
        date(2025, 4, 14),
        date(2025, 4, 21),
        8,
    )
    assert calendar.weekday_gaps == (date(2025, 4, 18),)  # candidate market holiday
    assert calendar.weekend_days_skipped == 2  # 04-19 Sat, 04-20 Sun
    spx_bars = _underlying(report, "I:SPX").bars
    assert spx_bars is not None
    # The SPXW series is attributed to I:SPX through the underlying's ROOT set.
    assert spx_bars.tickers == ("O:SPXW250411C05900000",)
    assert spx_bars.volume.total == Decimal(16)
    assert spx_bars.calendar.weekday_gaps == ()


def test_aggregate_bars_and_unmatched_series(report) -> None:  # type: ignore[no-untyped-def]
    assert report.unmatched_bar_tickers == ("O:QQQ250404C00480000",)
    assert report.bars is not None
    assert report.bars.series == 2  # the unmatched series is excluded, not merged
    assert report.bars.volume.total == Decimal(706)  # 690 + 16, QQQ's 3 excluded
    assert report.bars.volume.median == Decimal(40)  # [0,7,9,40,100,250,300]
    assert report.bars.volume.p90 == Decimal(300)
    calendar = report.bars.calendar
    assert len(calendar.sessions) == 7
    assert calendar.calendar_days_spanned == 14  # 2025-04-08 .. 2025-04-21
    assert calendar.weekday_gaps == (date(2025, 4, 10), date(2025, 4, 11), date(2025, 4, 18))
    assert calendar.weekend_days_skipped == 4


# ---- fail-loud discipline ------------------------------------------------------


def test_missing_or_null_required_fields_refuse_loudly() -> None:
    good = json.loads(fx.C1, parse_float=Decimal)
    for key in structural.REQUIRED_CONTRACT_KEYS:
        row = {k: v for k, v in good.items() if k != key}
        with pytest.raises(structural.StructuralCoverageError, match="missing required field"):
            structural.parse_contract(row, where="row")
        nulled = dict(good, **{key: None})
        with pytest.raises(structural.StructuralCoverageError, match="is null"):
            structural.parse_contract(nulled, where="row")
    # cfi / primary_exchange are DESCRIPTIVE: recorded, never fatal.
    bare = fx.contract_result(
        ticker=CID_C1,
        underlying="SPY",
        expiration="2025-03-07",
        strike="560",
        contract_type="call",
        cfi="",
        primary_exchange=None,
    )
    master = _parse(fx.contracts_payload(results=(bare,), as_of="2025-03-05"))
    assert master.contracts[0].cfi is None
    assert _slice_of(master).cfi_counts == (("(missing)", 1),)
    assert _slice_of(master).primary_exchange_counts == (("(missing)", 1),)


def _slice_of(master):  # type: ignore[no-untyped-def]
    return structural.build_structural_report((master,)).underlyings[0].slices[0]


def test_capture_and_envelope_refusals(captures: Path, tmp_path: Path) -> None:
    # as_of is not in the vendor body: no envelope key and no dated filename.
    with pytest.raises(structural.StructuralCoverageError, match="cannot determine as_of"):
        _parse(fx.SPY_AS1_BARE_PAYLOAD)
    # ... but a dated filename supplies it.
    named = tmp_path / "from-name"
    fx.write_json(named / "spy_2025-03-05.json", fx.SPY_AS1_BARE_PAYLOAD)
    (from_name,) = structural.load_contract_masters(named)
    assert (from_name.underlying, from_name.as_of) == ("SPY", AS1)
    ambiguous = tmp_path / "ambiguous"
    fx.write_json(ambiguous / "spy_2025-03-05_to_2025-03-07.json", fx.SPY_AS1_BARE_PAYLOAD)
    with pytest.raises(structural.StructuralCoverageError, match="ambiguous as_of"):
        structural.load_contract_masters(ambiguous)

    with pytest.raises(structural.StructuralCoverageError, match="duplicate ticker"):
        _parse(fx.contracts_payload(results=(fx.C1, fx.C1), as_of="2025-03-05"))
    with pytest.raises(structural.StructuralCoverageError, match="empty"):
        _parse(fx.contracts_payload(results=(), as_of="2025-03-05"))
    with pytest.raises(structural.StructuralCoverageError, match="underlyings in one master"):
        _parse(fx.contracts_payload(results=(fx.C1, fx.X1), as_of="2025-03-05"))
    with pytest.raises(structural.StructuralCoverageError, match="before as_of"):
        _parse(fx.contracts_payload(results=(fx.C1,), as_of="2025-04-01"))
    with pytest.raises(structural.StructuralCoverageError, match="broken chain"):
        _parse(
            fx.paged_contracts_payload(
                pages=(
                    fx.contracts_payload(results=(fx.C1,)),
                    fx.contracts_payload(results=(fx.C2,)),
                ),
                as_of="2025-03-05",
            )
        )
    duplicate = _parse(fx.SPY_AS1_PAYLOAD, source="a.json")
    with pytest.raises(structural.StructuralCoverageError, match="one master per"):
        structural.build_structural_report((duplicate, _parse(fx.SPY_AS1_PAYLOAD, source="b.json")))
    with pytest.raises(structural.StructuralCoverageError, match="nothing to inspect"):
        structural.build_structural_report(())
    assert structural.load_contract_masters(captures / "masters")  # dir path still works
    empty = tmp_path / "empty-dir"
    empty.mkdir()
    with pytest.raises(structural.StructuralCoverageError, match=r"no \*\.json files"):
        structural.load_contract_masters(empty)
    with pytest.raises(structural.StructuralCoverageError, match="not a file or directory"):
        structural.load_contract_masters(tmp_path / "nope.json")


def test_float_input_refuses_rather_than_coercing() -> None:
    """A payload decoded WITHOUT parse_float=Decimal has already lost the raw
    text — the inspector refuses instead of laundering a float into a strike."""
    floaty = json.loads(fx.REAL_CONTRACTS_PAYLOAD)["results"][1]
    assert isinstance(floaty["strike_price"], float)
    with pytest.raises(structural.StructuralCoverageError, match="refusing to coerce a float"):
        structural.parse_contract(floaty, where="row")


def test_bar_integrity_refusals() -> None:
    good = fx.bar(v="1", t=fx.T_2025_04_14)
    later = fx.bar(v="2", t=fx.T_2025_04_15)

    def _series(text: str):  # type: ignore[no-untyped-def]
        return structural.parse_bar_series(
            structural.decode_payload(text, source="bars.json"), source="bars.json"
        )

    with pytest.raises(structural.StructuralCoverageError, match="truncated capture"):
        _series(fx.bars_payload(ticker=CID_L1, results=(good, later), results_count="3"))
    with pytest.raises(structural.StructuralCoverageError, match="not strictly after"):
        _series(fx.bars_payload(ticker=CID_L1, results=(later, good)))
    with pytest.raises(structural.StructuralCoverageError, match="no silent empty coverage"):
        _series(fx.bars_payload(ticker=CID_L1, results=()))
    with pytest.raises(structural.StructuralCoverageError, match="missing required field 'v'"):
        _series(fx.bars_payload(ticker=CID_L1, results=('{"t": 1744603200000, "n": 1}',)))
    with pytest.raises(structural.StructuralCoverageError, match="cannot determine the option"):
        _series(fx.bars_payload(ticker=None, results=(good,)))
    # 09:00 America/New_York, not the session's ET midnight.
    noon = fx.bar(v="1", t=str(int(fx.T_2025_04_14) + 9 * 3_600_000))
    with pytest.raises(structural.StructuralCoverageError, match="ET midnight"):
        _series(fx.bars_payload(ticker=CID_L1, results=(noon,)))
    with pytest.raises(structural.StructuralCoverageError, match="not a whole second"):
        structural.bar_session(1744603200001, where="bar")


def test_incomplete_capture_and_schema_drift_are_reported() -> None:
    drifting = fx.contract_result(
        ticker=CID_C1,
        underlying="SPY",
        expiration="2025-03-07",
        strike="560",
        contract_type="call",
        extra='"correction": 1',
    )
    master = _parse(
        fx.contracts_payload(
            results=(drifting,), as_of="2025-03-05", next_url="https://api.polygon.io/next"
        )
    )
    assert master.unknown_result_keys == ("correction",)
    assert master.capture_complete is False
    report = structural.build_structural_report((master,))
    assert report.incomplete_captures == ("capture.json",)
    assert report.unknown_result_keys == ("correction",)
    markdown = structural.render_markdown(report)
    assert "INCOMPLETE CAPTURES (1)" in markdown
    assert "SCHEMA DRIFT: unexpected result keys correction" in markdown


# ---- capability, JSON, markdown, CLI ------------------------------------------


def test_capability_block_names_the_blocked_filter_inputs() -> None:
    inputs = structural.CAPABILITY["protocol_filter_inputs"]
    assert inputs["dte_min/dte_max"].startswith("SATISFIABLE")
    assert inputs["standard_deliverable_only"].startswith("SATISFIABLE")
    assert inputs["min_same_day_volume"].startswith("SATISFIABLE")
    for blocked in (
        "abs_delta_min/abs_delta_max",
        "min_open_interest",
        "max_spread_fraction_of_midpoint",
        "min_underlying_20d_median_dollar_volume",
        "exclude_earnings_spanning_hold",
    ):
        assert inputs[blocked].startswith("BLOCKED"), blocked
    assert set(structural.CAPABILITY["unavailable"]) == {
        "bid_ask_quotes",
        "greeks_delta",
        "open_interest",
    }
    assert any("candidate filter" in lane for lane in structural.CAPABILITY["blocked_lanes"])
    assert any("INV-11" in lane for lane in structural.CAPABILITY["blocked_lanes"])


def test_json_shape_and_stable_top_level_keys(report) -> None:  # type: ignore[no-untyped-def]
    payload = structural.report_to_json(report)
    assert list(payload) == [
        "report_version",
        "capability",
        "sources",
        "aggregate",
        "per_underlying",
        "not_evaluable",
    ]
    assert payload["report_version"] == "m4-structural/1"
    aggregate = payload["aggregate"]
    assert aggregate["contract_rows"] == 24
    assert aggregate["shares_per_contract"] == {"80": 3, "100": 21}
    assert aggregate["exercise_style"] == {"american": 21, "european": 3}
    assert aggregate["multi_root_underlyings"] == ["I:SPX", "SPY"]
    assert aggregate["adjustment_events"] == 2
    assert aggregate["unmatched_bar_tickers"] == ["O:QQQ250404C00480000"]
    spy = payload["per_underlying"]["SPY"]
    assert spy["as_ofs"] == ["2025-03-05", "2025-03-06", "2025-03-07"]
    assert spy["lifecycle"]["contracts"][CID_C3] == {
        "first_seen": "2025-03-05",
        "last_seen": "2025-03-06",
        "as_of_count": 2,
    }
    first = spy["as_of_slices"][0]
    # Strikes travel as exponent-free STRINGS so the ladder round-trips exactly.
    assert first["strikes"]["min_strike"] == "560"
    assert first["strikes"]["max_strike"] == "650"
    assert first["strikes"]["span"] == "90"
    assert first["strikes"]["spot_units_status"] == "NOT_EVALUABLE"
    assert first["strikes"]["span_in_spot_units"] is None
    assert first["tenors"]["band_bucket"] == "dte_30_60"
    assert first["births"] is None and first["deaths"] is None
    assert spy["as_of_slices"][2]["died_active"] == [CID_A1, CID_C3]
    assert spy["adjustment_timeline"][0]["kind"] == "shares_per_contract_change"
    calendar = spy["bars"]["implied_session_calendar"]
    assert calendar["weekday_gaps"] == ["2025-04-18"]
    assert calendar["weekend_days_skipped"] == 2
    assert "CANDIDATE market holiday" in calendar["weekday_gap_note"]
    assert payload["not_evaluable"]["open_interest"]["status"] == "NOT_EVALUABLE"
    assert payload["not_evaluable"]["strike_grid_in_spot_units"]["status"] == "NOT_EVALUABLE"
    assert payload["sources"]["adapter"]["module"] == "tree_options.data.massive_options"
    json.dumps(payload)  # the artifact must be serialisable as-is


def test_markdown_summary_lines(report) -> None:  # type: ignore[no-untyped-def]
    markdown = structural.render_markdown(report)
    assert markdown.startswith("# Massive free-tier structural coverage — m4-structural/1")
    assert "MULTI-ROOT UNDERLYINGS (M4-A found SPX/SPXW collisions): I:SPX, SPY" in markdown
    assert "- UNAVAILABLE `open_interest`:" in markdown
    assert "- BLOCKED LANE: M3 candidate filter" in markdown
    assert (
        "| 2025-03-05 | 7 | 5 | 560 | 650 | NOT_EVALUABLE | 4 | 2 | 3 | 1 | 2 | yes | - | - |"
        in (markdown)
    )
    assert "2025-03-06 -> 2025-03-07 shares_per_contract_change" in markdown
    assert "2025-03-06 -> 2025-03-07 root_disappeared: root SPY1 SPY1 -> (absent)" in markdown
    assert f"NON_STANDARD deliverable (shares_per_contract != 100): {CID_A1}" in markdown
    assert "NO expiry inside the protocol band 30-60 DTE" in markdown
    assert "WEEKDAY GAPS (1): 2025-04-18" in markdown
    assert "ACTIVE DELISTINGS (contract vanished while un-expired)" in markdown
    assert "bar series excluded (no contract-master owner): O:QQQ250404C00480000" in markdown


def test_cli_end_to_end_with_spot_proxy(captures: Path, tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    out_json = tmp_path / "structural.json"
    rc = structural.main(
        [
            "--contracts-json",
            str(captures / "masters"),
            "--bars-json",
            str(captures / "bars"),
            "--spot-json",
            str(captures / "spot.json"),
            "--out-json",
            str(out_json),
        ]
    )
    assert rc == 0
    stdout = capsys.readouterr().out
    assert stdout.startswith("# Massive free-tier structural coverage")
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["sources"]["spot_proxy_supplied"] is True
    grid = payload["per_underlying"]["SPY"]["as_of_slices"][0]["strikes"]
    assert grid["spot_units_status"] == "EVALUATED"
    assert grid["spot_proxy"] == "580"
    assert grid["min_in_spot_units"] == "0.965517"  # 560 / 580, quantized to 6dp
    assert grid["max_in_spot_units"] == "1.12069"  # 650 / 580
    assert grid["span_in_spot_units"] == "0.155172"  # 90 / 580
    # The flat per-underlying form applies to every as_of.
    spx_grid = payload["per_underlying"]["I:SPX"]["as_of_slices"][0]["strikes"]
    assert spx_grid["spot_proxy"] == "5750"
    assert spx_grid["span_in_spot_units"] == "0.034783"  # 200 / 5750
    assert payload["not_evaluable"]["strike_grid_in_spot_units"]["status"] == "EVALUATED"
    assert payload["not_evaluable"]["abs_delta"]["status"] == "NOT_EVALUABLE"


def test_adapter_probe_records_provenance_without_importing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """WS-D1's client is never imported by the engine; the CLI only records
    whether it exists, so this inspector works before and after it lands."""
    assert structural.adapter_status("tree_options.data.__ws_d1_not_here__") == "ABSENT"
    stub = types.ModuleType("stub_massive")
    stub.MASSIVE_SCHEMA_VERSION = "m4-massive/1"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "stub_massive", stub)
    assert structural.adapter_status("stub_massive") == "PRESENT (m4-massive/1)"
    bare = types.ModuleType("stub_bare")
    monkeypatch.setitem(sys.modules, "stub_bare", bare)
    assert structural.adapter_status("stub_bare") == "PRESENT"


def test_percentile_helpers_nearest_rank() -> None:
    assert structural._median([Decimal(5), Decimal(1), Decimal(3)]) == Decimal(3)
    assert structural._median([Decimal(1), Decimal(2), Decimal(3), Decimal(10)]) == Decimal("2.5")
    assert structural._median([]) is None
    one_to_ten = [Decimal(i) for i in range(1, 11)]
    assert structural._p90(one_to_ten) == Decimal(9)
    assert structural._p90(one_to_ten[::-1]) == Decimal(9)  # order-independent
    assert structural._p90([Decimal(1)]) == Decimal(1)
    assert structural._p90([]) is None


def test_lane_files_carry_no_secret_and_no_host_paths() -> None:
    """This lane never reads, logs or stores the API key, and the committed
    fixtures/inspector must not contain the vendor key parameter, the key
    env-var name, or an absolute host path. The needles are assembled from
    fragments so this guard does not plant the strings it forbids."""
    key_param = "api" + "Key"
    key_env = "POLYGON" + "_API_KEY"
    host_path = "/" + "home" + "/"
    for relative in (
        Path("tests") / "fixtures" / "massive_structural_sample.py",
        Path("scripts") / "inspect_structural_coverage.py",
        Path("tests") / "unit" / "test_inspect_structural_coverage.py",
    ):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert key_param not in source, relative
        assert key_env not in source, relative
        assert host_path not in source, relative
