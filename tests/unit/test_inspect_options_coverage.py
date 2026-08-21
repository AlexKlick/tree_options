"""WS-B: the M4 real-data coverage inspector (plan §3, tests hermetic).

The engine is validated against a handcrafted Cboe-shaped CSV fixture via
an in-test minimal parser that produces the SHARED-CONTRACT shapes
(CboeEodParseResult fields + a RealOptionOverlay-shaped contract lookup)
built from the REAL schema types (`OptionDayFile`/`OptionChainEntry`/
`OptionQuoteSnapshot`/`OptionContract`). Every asserted number is
hand-computed from the fixture rows (see the arithmetic inline).

Fixture map (15 rows, 3 sessions, 2 underlyings):
- ABC 2024-03-15 (regular): 7 contracts, expiries 03-22 (4 rows) + 04-19
  (3 rows), one zero-bid + zero-greeks row (C 150), one nonstandard
  delivery_code row (P 90, code "A").
- ABC 2024-11-29 (early close: no 1545 snapshot): 2 contracts.
- DEF 2024-03-18 (regular): 6 contracts across FIVE expiries (nearest-4
  excludes 2024-12-20), all deltas inside the 0.30-0.60 ATM band.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
import types
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT
from tree_options.schemas.options import DeliverableSpec, OptionContract
from tree_options.synth_options import contract_id_of
from tree_options.synth_options.generate import (
    OptionChainEntry,
    OptionDayFile,
    OptionQuoteSnapshot,
)

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import inspect_options_coverage as coverage  # type: ignore[import-not-found]  # scripts/ via path

FIXTURE_CSV = REPO_ROOT / "tests" / "fixtures" / "m4_cboe_eod_sample.csv"

# receipt instants (values are irrelevant to the metrics, only presence)
RECEIVED_AT = {
    date(2024, 3, 15): datetime(2024, 3, 18, 13, 0, tzinfo=UTC),
    date(2024, 3, 18): datetime(2024, 3, 19, 13, 0, tzinfo=UTC),
    date(2024, 11, 29): datetime(2024, 12, 2, 14, 0, tzinfo=UTC),
}
UNDERLYING_DV = {"ABC": Decimal("5000000.00"), "DEF": Decimal("8000000.00")}


# ---- the contract-shaped stubs (WS-A interface, in-test construction) --------


@dataclass(frozen=True)
class _Stats:
    rows_total: int
    rows_mapped: int
    duplicate_rows: int
    zero_greeks_rows: int
    zero_bid_rows: int
    early_close_sessions: int
    nonstandard_delivery_rows: int


@dataclass
class _ParseResult:
    source_path: Path
    source_sha256: str
    day_files: dict[date, OptionDayFile]
    stats: _Stats
    issues: list[str]
    # the real CboeEodParseResult also carries its parsed contract master
    contracts: tuple[OptionContract, ...] | None = None


class _StubOverlay:
    """The RealOptionOverlay surface slice the inspector consumes."""

    def __init__(self, contracts: dict[str, OptionContract]) -> None:
        self._contracts = contracts

    def contract(self, contract_id: str) -> OptionContract:
        try:
            return self._contracts[contract_id]
        except KeyError as exc:
            raise ValueError(f"unresolvable contract id: {contract_id}") from exc


def _snapshot(session: date, hour: int, minute: int, bid: str, ask: str) -> OptionQuoteSnapshot:
    return OptionQuoteSnapshot(
        exchange_timestamp=datetime(
            session.year, session.month, session.day, hour, minute, tzinfo=UTC
        ),
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=10,
        ask_size=10,
    )


def _parse_fixture(path: Path) -> tuple[_ParseResult, dict[str, OptionContract]]:
    """Minimal Cboe-shaped parse mirroring the WS-A contract on the fixture."""
    with path.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    contracts: dict[str, OptionContract] = {}
    by_session: dict[date, list[OptionChainEntry]] = {}
    underlying_bid: dict[date, Decimal] = {}
    underlying_ask: dict[date, Decimal] = {}
    zero_greeks = zero_bid = nonstandard = duplicates = 0
    early_close_sessions: set[date] = set()
    all_sessions: set[date] = set()
    seen_keys: set[tuple[str, date, str, str, str]] = set()

    for row in rows:
        session = date.fromisoformat(row["quote_date"])
        expiration = date.fromisoformat(row["expiration"])
        strike = Decimal(row["strike"])
        call_put = row["option_type"]
        underlying = row["underlying_symbol"]
        key = (underlying, session, row["expiration"], row["strike"], call_put)
        if key in seen_keys:
            duplicates += 1
        seen_keys.add(key)
        all_sessions.add(session)
        underlying_bid[session] = Decimal(row["underlying_bid_eod"])
        underlying_ask[session] = Decimal(row["underlying_ask_eod"])

        cid = contract_id_of(underlying, expiration, call_put, strike)
        has_1545 = bool(row["bid_1545"])
        delta = Decimal(row["delta_1545"]) if row["delta_1545"] else Decimal("0")
        if not row["delta_1545"]:
            zero_greeks += 1
        bid_eod, ask_eod = Decimal(row["bid_eod"]), Decimal(row["ask_eod"])
        if bid_eod == 0 and ask_eod == 0:
            zero_bid += 1
        delivery_code = row["delivery_code"]
        if delivery_code:
            nonstandard += 1

        entry = OptionChainEntry(
            contract_id=cid,
            quote_1545=(
                _snapshot(session, 19, 45, row["bid_1545"], row["ask_1545"]) if has_1545 else None
            ),
            quote_eod=_snapshot(session, 20, 0, row["bid_eod"], row["ask_eod"]),
            open_interest=int(row["open_interest"]),
            same_day_volume=int(row["trade_volume"]),
            abs_delta=delta,
            quote_condition="regular",
        )
        by_session.setdefault(session, []).append(entry)
        if cid not in contracts:
            standard = not delivery_code
            action_id = f"CA-{delivery_code}" if not standard else None
            contracts[cid] = OptionContract(
                contract_id=cid,
                option_root=row["root"],
                underlying_security_id=underlying,
                expiration=expiration,
                strike=strike,
                call_put=call_put,  # type: ignore[arg-type]
                listing_start=session,
                listing_end=expiration,
                deliverable=(
                    DeliverableSpec(shares_per_contract=Decimal("100"))
                    if standard
                    else DeliverableSpec(
                        shares_per_contract=Decimal("50"), corporate_action_id=action_id
                    )
                ),
                standard_contract_flag=standard,
                corporate_action_id=action_id,
            )

    day_files: dict[date, OptionDayFile] = {}
    for session in sorted(by_session):
        entries = tuple(sorted(by_session[session], key=lambda e: e.contract_id))
        if all(e.quote_1545 is None for e in entries):
            early_close_sessions.add(session)
        first_cid = entries[0].contract_id
        sid = contracts[first_cid].underlying_security_id
        day_files[session] = OptionDayFile(
            underlying_security_id=sid,
            session=session,
            received_at=RECEIVED_AT[session],
            underlying_bid=underlying_bid[session],
            underlying_ask=underlying_ask[session],
            underlying_20d_median_dollar_volume=UNDERLYING_DV[sid],
            entries=entries,
        )

    result = _ParseResult(
        source_path=path,
        source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        day_files=day_files,
        contracts=tuple(sorted(contracts.values(), key=lambda c: c.contract_id)),
        stats=_Stats(
            rows_total=len(rows),
            rows_mapped=len(rows),
            duplicate_rows=duplicates,
            zero_greeks_rows=zero_greeks,
            zero_bid_rows=zero_bid,
            early_close_sessions=len(early_close_sessions),
            nonstandard_delivery_rows=nonstandard,
        ),
        issues=[],
    )
    return result, contracts


@pytest.fixture(scope="module")
def parsed() -> tuple[_ParseResult, dict[str, OptionContract]]:
    return _parse_fixture(FIXTURE_CSV)


@pytest.fixture(scope="module")
def report(parsed) -> coverage.CoverageReport:
    result, contracts = parsed
    return coverage.build_coverage_report(result, _StubOverlay(contracts))


def _underlying(report: coverage.CoverageReport, sid: str) -> coverage.UnderlyingCoverage:
    match = [u for u in report.underlyings if u.underlying == sid]
    assert len(match) == 1
    return match[0]


def _day(u: coverage.UnderlyingCoverage, session: date) -> coverage.UnderlyingDay:
    match = [d for d in u.days if d.session == session]
    assert len(match) == 1
    return match[0]


# ---- parse stats echo + scope -------------------------------------------------


def test_parse_stats_are_echoed(report) -> None:  # type: ignore[no-untyped-def]
    assert report.rows_total == 15
    assert report.rows_mapped == 15
    assert report.duplicate_rows == 0
    assert report.zero_greeks_rows == 1
    assert report.zero_bid_rows == 1
    assert report.nonstandard_delivery_rows == 1
    assert report.issues_count == 0
    assert report.issues_head == ()
    assert report.source_sha256 == hashlib.sha256(FIXTURE_CSV.read_bytes()).hexdigest()
    assert report.source_path == FIXTURE_CSV
    assert report.variant == "cgi_or_historical"


def test_scope_counts(report) -> None:  # type: ignore[no-untyped-def]
    assert len(report.underlyings) == 2
    assert len(report.underlying_days) == 3
    assert report.contract_rows == 15
    assert report.distinct_contracts == 15
    assert [u.underlying for u in report.underlyings] == ["ABC", "DEF"]
    assert report.early_close_sessions == 1
    assert report.early_close_sessions_from_files == 1


def test_contracts_and_expiries_per_underlying_day(report) -> None:  # type: ignore[no-untyped-def]
    abc = _underlying(report, "ABC")
    d1 = _day(abc, date(2024, 3, 15))
    assert (d1.contracts, d1.distinct_expiries) == (7, 2)  # 03-22 + 04-19
    d2 = _day(abc, date(2024, 11, 29))
    assert (d2.contracts, d2.distinct_expiries) == (2, 1)
    deff = _underlying(report, "DEF")
    d3 = _day(deff, date(2024, 3, 18))
    assert (d3.contracts, d3.distinct_expiries) == (6, 5)  # ladder depth 5
    assert abc.contract_rows == 9
    assert deff.contract_rows == 6


def test_strike_grid_width_in_spot_units(report) -> None:  # type: ignore[no-untyped-def]
    abc = _underlying(report, "ABC")
    d1 = _day(abc, date(2024, 3, 15))
    assert d1.min_strike == Decimal("80")
    assert d1.max_strike == Decimal("150")
    assert d1.spot_mid == Decimal("100.00")  # (99.90 + 100.10) / 2
    assert d1.strike_width_ratio == Decimal(150) / Decimal(80)  # 1.875
    d2 = _day(abc, date(2024, 11, 29))
    assert d2.strike_width_ratio == Decimal(1)  # single strike
    d3 = _day(_underlying(report, "DEF"), date(2024, 3, 18))
    assert (d3.min_strike, d3.max_strike) == (Decimal("195"), Decimal("200"))
    assert d3.spot_mid == Decimal("200.00")
    assert d3.strike_width_ratio == Decimal(200) / Decimal(195)


# ---- quality fractions + spread bands ------------------------------------------


def test_zero_bid_and_zero_greeks_fractions(report) -> None:  # type: ignore[no-untyped-def]
    abc = _underlying(report, "ABC")
    assert abc.zero_bid_fraction == Decimal(1) / Decimal(9)
    assert abc.zero_greeks_fraction == Decimal(1) / Decimal(9)
    deff = _underlying(report, "DEF")
    assert deff.zero_bid_fraction == 0
    assert deff.zero_greeks_fraction == 0
    assert report.zero_bid_fraction == Decimal(1) / Decimal(15)
    assert report.zero_greeks_fraction == Decimal(1) / Decimal(15)


def test_atm_spread_median_and_p90_exact(report) -> None:  # type: ignore[no-untyped-def]
    """Hand-computed (ask-bid)/mid on the EOD snapshot, mid>0, |delta| band
    0.30-0.60 inclusive: ABC rows 1/2/5/7/8/9; DEF all six rows."""
    abc = _underlying(report, "ABC")
    atm = abc.spreads.atm
    assert atm.n == 6
    # sorted: .02, .02, .02, 0.10/2.05, 0.10/1.55, 0.10/0.55
    two_41 = Decimal("0.10") / Decimal("2.05")
    assert atm.median == (Decimal("0.02") + two_41) / 2
    assert atm.p90 == Decimal("0.10") / Decimal("0.55")  # nearest-rank ceil(.9*6)=6th
    deff = _underlying(report, "DEF")
    atm_d = deff.spreads.atm
    assert atm_d.n == 6
    # sorted: 2/51, 2/51, 2/51, 0.04, 4/77, 2/21
    assert atm_d.median == (Decimal(2) / Decimal(51) + Decimal("0.04")) / 2
    assert atm_d.p90 == Decimal("0.10") / Decimal("1.05")


def test_wings_spread_stats_exact(report) -> None:  # type: ignore[no-untyped-def]
    abc = _underlying(report, "ABC")
    wings = abc.spreads.wings
    assert wings.n == 2  # C120 (delta .10) + P80 (delta .15)
    assert (
        wings.median == (Decimal("0.10") / Decimal("0.15") + Decimal("0.06") / Decimal("0.07")) / 2
    )
    assert wings.p90 == Decimal("0.06") / Decimal("0.07")
    # an empty bucket reports n=0 with explicit None quantiles (never fabricated)
    deff = _underlying(report, "DEF")
    assert deff.spreads.wings.n == 0
    assert deff.spreads.wings.median is None
    assert deff.spreads.wings.p90 is None


def test_zero_mid_and_zero_greeks_rows_excluded_from_bands(report) -> None:  # type: ignore[no-untyped-def]
    """ABC has 9 rows but only 8 spread observations: the C150 row has
    mid=0 AND delta=0 (zero-greeks) — excluded from both buckets and
    accounted for in the zero fractions instead."""
    abc = _underlying(report, "ABC")
    assert abc.spreads.atm.n + abc.spreads.wings.n == 8
    assert abc.zero_bid_fraction == Decimal(1) / Decimal(9)
    assert _day(abc, date(2024, 3, 15)).zero_greeks_entries == 1
    deff = _underlying(report, "DEF")
    assert deff.spreads.atm.n + deff.spreads.wings.n == 6


def test_aggregate_pools_across_underlyings(report) -> None:  # type: ignore[no-untyped-def]
    assert report.spreads.atm.n == 12  # 6 + 6
    # sorted: .02 x3, 2/51 x3, 0.04, 2/41, 4/77, 2/31, 2/21, 2/11 — the
    # middle pair is (2/51, 0.04)
    assert report.spreads.atm.median == (Decimal(2) / Decimal(51) + Decimal("0.04")) / 2
    assert report.spreads.atm.p90 == Decimal("0.10") / Decimal("1.05")  # rank 11 of 12
    assert report.spreads.wings.n == 2  # ABC only
    assert report.distinct_contracts == 15


# ---- concentration -------------------------------------------------------------


def test_oi_and_volume_concentration(report) -> None:  # type: ignore[no-untyped-def]
    abc = _underlying(report, "ABC")
    c = abc.concentration
    assert (c.oi_total, c.oi_in_delta_band) == (
        1870,
        1750,
    )  # 1290+580; wings 50+60 + zero-row 10 out
    assert c.oi_in_nearest4 == 1870  # both days have <= 4 live expiries
    assert (c.vol_total, c.vol_in_delta_band, c.vol_in_nearest4) == (330, 320, 330)
    deff = _underlying(report, "DEF")
    cd = deff.concentration
    assert (cd.oi_total, cd.oi_in_delta_band) == (1840, 1840)  # every delta in band
    assert cd.oi_in_nearest4 == 1835  # 2024-12-20 expiry (OI 5) outside nearest-4
    assert (cd.vol_total, cd.vol_in_nearest4) == (530, 515)  # its 15 volume excluded
    agg = report.concentration
    assert (agg.oi_total, agg.oi_in_delta_band, agg.oi_in_nearest4) == (3710, 3590, 3705)
    assert (agg.vol_total, agg.vol_in_delta_band, agg.vol_in_nearest4) == (860, 850, 845)


def test_delivery_code_distribution(report) -> None:  # type: ignore[no-untyped-def]
    abc = _underlying(report, "ABC")
    assert (abc.delivery_standard, abc.delivery_nonstandard) == (8, 1)  # P90 code "A"
    deff = _underlying(report, "DEF")
    assert (deff.delivery_standard, deff.delivery_nonstandard) == (6, 0)
    assert (report.delivery_standard, report.delivery_nonstandard) == (14, 1)
    assert report.nonstandard_delivery_rows == 1  # parse-stats echo agrees


def test_early_close_sessions(report) -> None:  # type: ignore[no-untyped-def]
    abc = _underlying(report, "ABC")
    assert _day(abc, date(2024, 11, 29)).early_close is True  # no 1545 snapshot
    assert _day(abc, date(2024, 3, 15)).early_close is False
    assert _day(_underlying(report, "DEF"), date(2024, 3, 18)).early_close is False
    assert report.early_close_sessions_from_files == 1
    assert report.early_close_sessions == 1  # stats echo matches the files


def test_underlying_quote_sanity(parsed) -> None:  # type: ignore[no-untyped-def]
    result, contracts = parsed
    report = coverage.build_coverage_report(result, _StubOverlay(contracts))
    assert report.crossed_files == 0
    assert report.zero_bid_underlying_files == 0
    # a crossed underlying quote (bid > ask) is COUNTED, not swallowed
    session = date(2024, 3, 15)
    crossed = replace(
        result,
        day_files={
            **result.day_files,
            session: result.day_files[session].model_copy(
                update={"underlying_bid": Decimal("100.50")}
            ),
        },
    )
    crossed_report = coverage.build_coverage_report(crossed, _StubOverlay(contracts))
    assert crossed_report.crossed_files == 1


# ---- loud degradation (no silent Nones) ----------------------------------------


def test_zero_underlying_ask_refuses(parsed) -> None:  # type: ignore[no-untyped-def]
    result, contracts = parsed
    session = date(2024, 3, 15)
    zero_ask = replace(
        result,
        day_files={
            **result.day_files,
            session: result.day_files[session].model_copy(update={"underlying_ask": Decimal("0")}),
        },
    )
    with pytest.raises(ValueError, match="underlying_ask"):
        coverage.build_coverage_report(zero_ask, _StubOverlay(contracts))


def test_none_fields_refuse_loudly(parsed) -> None:  # type: ignore[no-untyped-def]
    result, contracts = parsed
    session = date(2024, 3, 15)
    entries = result.day_files[session].entries
    broken_entry = entries[0].model_copy(update={"abs_delta": None})
    broken = replace(
        result,
        day_files={
            **result.day_files,
            session: result.day_files[session].model_copy(
                update={"entries": (broken_entry, *entries[1:])}
            ),
        },
    )
    with pytest.raises(ValueError, match="abs_delta"):
        coverage.build_coverage_report(broken, _StubOverlay(contracts))
    broken_stats = replace(result, stats=replace(result.stats, rows_total=None))
    with pytest.raises(ValueError, match="rows_total"):
        coverage.build_coverage_report(broken_stats, _StubOverlay(contracts))


def test_structural_oddities_refuse(parsed) -> None:  # type: ignore[no-untyped-def]
    result, contracts = parsed
    session = date(2024, 3, 15)
    entries = result.day_files[session].entries
    mixed = result.day_files[session].model_copy(
        update={"entries": (entries[0].model_copy(update={"quote_1545": None}), *entries[1:])}
    )
    with pytest.raises(ValueError, match="mixed 1545"):
        coverage.build_coverage_report(
            replace(result, day_files={**result.day_files, session: mixed}),
            _StubOverlay(contracts),
        )
    empty = result.day_files[session].model_copy(update={"entries": ()})
    with pytest.raises(ValueError, match="no entries"):
        coverage.build_coverage_report(
            replace(result, day_files={**result.day_files, session: empty}),
            _StubOverlay(contracts),
        )


def test_unknown_contract_id_refuses(parsed) -> None:  # type: ignore[no-untyped-def]
    result, _contracts = parsed
    with pytest.raises(ValueError, match="no contract master row"):
        coverage.build_coverage_report(result, _StubOverlay({}))


# ---- output shapes --------------------------------------------------------------


def test_json_shape_and_values(report) -> None:  # type: ignore[no-untyped-def]
    payload = coverage.report_to_json(report)
    assert set(payload) == {
        "report_version",
        "variant",
        "source",
        "parse_stats",
        "issues_count",
        "issues_head",
        "aggregate",
        "per_underlying",
        "spans_earnings",
    }
    assert payload["report_version"] == "m4-coverage/1"
    assert payload["source"] == {
        "path": str(FIXTURE_CSV),
        "sha256": report.source_sha256,
    }
    assert set(payload["parse_stats"]) == {
        "rows_total",
        "rows_mapped",
        "duplicate_rows",
        "zero_greeks_rows",
        "zero_bid_rows",
        "early_close_sessions",
        "nonstandard_delivery_rows",
    }
    assert payload["parse_stats"]["rows_total"] == 15
    agg = payload["aggregate"]
    assert set(agg) == {
        "underlyings",
        "underlying_days",
        "contract_rows",
        "distinct_contracts",
        "zero_bid_fraction",
        "zero_greeks_fraction",
        "spreads",
        "concentration",
        "delivery_code_distribution",
        "early_close_sessions",
        "early_close_sessions_from_files",
        "underlying_quote_violations",
    }
    assert agg["contract_rows"] == 15
    assert agg["zero_bid_fraction"] == float(Decimal(1) / Decimal(15))
    assert agg["spreads"]["atm"]["median"] == float(
        (Decimal(2) / Decimal(51) + Decimal("0.04")) / 2
    )
    assert agg["spreads"]["wings"]["p90"] == float(Decimal("0.06") / Decimal("0.07"))
    assert agg["concentration"]["oi_nearest4_share"] == float(Decimal(3705) / Decimal(3710))
    assert agg["delivery_code_distribution"] == {"standard": 14, "nonstandard": 1}
    assert set(payload["per_underlying"]) == {"ABC", "DEF"}
    abc_day = payload["per_underlying"]["ABC"]["days"][0]
    assert set(abc_day) == {
        "session",
        "contracts",
        "distinct_expiries",
        "min_strike",
        "max_strike",
        "spot_mid",
        "strike_width_ratio",
        "zero_bid_entries",
        "zero_greeks_entries",
        "early_close",
        "delivery_standard",
        "delivery_nonstandard",
        "underlying_quote_crossed",
        "underlying_bid_zero",
    }
    assert abc_day["strike_width_ratio"] == 1.875
    assert payload["per_underlying"]["DEF"]["spreads"]["wings"]["median"] is None
    assert payload["spans_earnings"]["status"] == "NOT_EVALUABLE"
    assert payload["spans_earnings"]["value"] is None
    json.dumps(payload)  # machine-serializable as-is


def test_markdown_summary_lines(report) -> None:  # type: ignore[no-untyped-def]
    text = coverage.render_markdown(report)
    assert text.startswith(f"# Cboe EOD options coverage — {FIXTURE_CSV.name}")
    assert f"- source: `{FIXTURE_CSV}`" in text
    assert "total=15 mapped=15" in text
    assert "underlyings=2 underlying-days=3 contract-rows=15" in text
    assert "| ABC | 2024-03-15 | 7 | 2 | 80 | 150 | 1.8750 | 100.00 | 1 | 1 | no |" in text
    assert "| DEF | 2024-03-18 | 6 | 5 | 195 | 200 | 1.0256 | 200.00 | 0 | 0 | no |" in text
    assert "early-close sessions: 1" in text
    assert "standard=14 nonstandard=1" in text
    assert "spans_earnings: NOT_EVALUABLE" in text


def _install_stub_adapter(monkeypatch, with_builder: bool) -> dict[str, OptionContract]:  # type: ignore[no-untyped-def]
    """Inject WS-A-shaped stub modules for tree_options.data.{cboe_eod,
    real_overlay} — with_builder picks the build_real_overlay(result) path
    (parsed master wired in) vs the bare contracted constructor."""
    shared: dict[str, OptionContract] = {}
    cboe_stub = types.ModuleType("tree_options.data.cboe_eod")

    def parse_cboe_eod_csv(
        path: Path, *, variant: str = "cgi_or_historical", underlying: str | None = None
    ) -> _ParseResult:
        result, contracts = _parse_fixture(Path(path))
        shared.update(contracts)
        assert variant == "no_cgi"
        if underlying is not None:
            shared["__underlying_selection__"] = underlying  # type: ignore[assignment]
        return result

    cboe_stub.parse_cboe_eod_csv = parse_cboe_eod_csv  # type: ignore[attr-defined]
    real_stub = types.ModuleType("tree_options.data.real_overlay")

    class RealOptionOverlay:
        def __init__(
            self,
            day_files: dict[date, OptionDayFile],
            *,
            source_sha256: str,
            contracts: tuple[OptionContract, ...] | None = None,
        ) -> None:
            assert day_files and source_sha256
            if contracts is not None:
                shared.update({c.contract_id: c for c in contracts})

        def contract(self, contract_id: str) -> OptionContract:
            return _StubOverlay(shared).contract(contract_id)

    real_stub.RealOptionOverlay = RealOptionOverlay  # type: ignore[attr-defined]

    def build_real_overlay(result: _ParseResult) -> RealOptionOverlay:
        assert result.contracts is not None
        return RealOptionOverlay(
            result.day_files,
            source_sha256=result.source_sha256,
            contracts=result.contracts,
        )

    if with_builder:
        real_stub.build_real_overlay = build_real_overlay  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tree_options.data.cboe_eod", cboe_stub)
    monkeypatch.setitem(sys.modules, "tree_options.data.real_overlay", real_stub)
    return shared


def test_cli_end_to_end_with_stub_modules(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """main() resolves the adapter at runtime per the shared contract and
    prefers build_real_overlay (the parsed contract master path — proven by
    the truthful delivery_code distribution)."""
    _install_stub_adapter(monkeypatch, with_builder=True)
    out_json = tmp_path / "coverage.json"
    rc = coverage.main(
        ["--csv", str(FIXTURE_CSV), "--variant", "no_cgi", "--out-json", str(out_json)]
    )
    assert rc == 0
    stdout = capsys.readouterr().out
    assert stdout.startswith(f"# Cboe EOD options coverage — {FIXTURE_CSV.name}")
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["variant"] == "no_cgi"
    assert payload["aggregate"]["contract_rows"] == 15
    assert payload["aggregate"]["delivery_code_distribution"] == {"standard": 14, "nonstandard": 1}
    assert payload["per_underlying"]["DEF"]["concentration"]["vol_nearest4_share"] == float(
        Decimal(515) / Decimal(530)
    )


def test_cli_falls_back_to_the_contracted_constructor(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """Without build_real_overlay, main() still runs via the contracted
    RealOptionOverlay(day_files, *, source_sha256) constructor."""
    _install_stub_adapter(monkeypatch, with_builder=False)
    out_json = tmp_path / "coverage-fallback.json"
    rc = coverage.main(
        ["--csv", str(FIXTURE_CSV), "--variant", "no_cgi", "--out-json", str(out_json)]
    )
    assert rc == 0
    assert "Cboe EOD options coverage" in capsys.readouterr().out
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["aggregate"]["contract_rows"] == 15


def test_cli_forwards_underlying_selection(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """--underlying reaches parse_cboe_eod_csv (integration reconciliation:
    the adapter refuses a multi-underlying CSV unless one symbol is selected,
    so the CLI must be able to forward the selection)."""
    shared = _install_stub_adapter(monkeypatch, with_builder=True)
    out_json = tmp_path / "coverage-selected.json"
    rc = coverage.main(
        [
            "--csv",
            str(FIXTURE_CSV),
            "--variant",
            "no_cgi",
            "--underlying",
            "DEF",
            "--out-json",
            str(out_json),
        ]
    )
    assert rc == 0
    assert shared["__underlying_selection__"] == "DEF"
    assert "Cboe EOD options coverage" in capsys.readouterr().out


def test_percentile_helpers_nearest_rank() -> None:
    assert coverage._median([Decimal(5), Decimal(1), Decimal(3)]) == Decimal(3)
    assert coverage._median([Decimal(1), Decimal(2), Decimal(3), Decimal(10)]) == Decimal("2.5")
    assert coverage._median([]) is None
    # nearest-rank p90: the ceil(0.9*n)-th smallest value
    one_to_ten = [Decimal(i) for i in range(1, 11)]
    assert coverage._p90(one_to_ten) == Decimal(9)
    one_to_six = [Decimal(i) for i in range(1, 7)]
    assert coverage._p90(one_to_six) == Decimal(6)
    assert coverage._p90(one_to_six[::-1]) == Decimal(6)  # order-independent
    assert coverage._p90([Decimal("0.02")]) == Decimal("0.02")
    assert coverage._p90([]) is None
