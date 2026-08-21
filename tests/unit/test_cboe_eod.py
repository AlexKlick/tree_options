"""WS-A: Cboe Option EOD parser — row accounting, refusals, PIT walls.

Hermetic: every test runs off the committed fixture
`tests/fixtures/cboe_eod_rows.py`; the retained external sample is never
referenced.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from tests.fixtures import cboe_eod_rows as fx
from tree_options.data.cboe_eod import (
    CBOE_EOD_COLUMNS,
    CboeEodFormatError,
    CboeEodMultiUnderlyingError,
    CboeEodUnderlyingQuoteError,
    IndexUnderlyingNotLicensedError,
    next_trading_session,
    parse_cboe_eod_csv,
    publication_instant,
    validate_pit_invariants,
)
from tree_options.synth_options.generate import contract_id_of

STRIKE_440 = Decimal("440.00")
CID_440C = contract_id_of("SPY", date(2023, 9, 15), "C", STRIKE_440)
CID_440P = contract_id_of("SPY", date(2023, 9, 15), "P", STRIKE_440)
CID_275C = contract_id_of("SPY", date(2023, 9, 15), "C", Decimal("275.00"))
CID_720C = contract_id_of("SPY", date(2023, 11, 17), "C", Decimal("720.00"))
CID_445C = contract_id_of("SPY", date(2023, 9, 15), "C", Decimal("445.00"))
CID_200P = contract_id_of("SPY", date(2023, 11, 17), "P", Decimal("200.00"))
CID_4425C = contract_id_of("SPY", date(2023, 9, 15), "C", Decimal("442.50"))
CID_440C_DEC = contract_id_of("SPY", date(2023, 12, 15), "C", STRIKE_440)
CID_440P_DEC = contract_id_of("SPY", date(2023, 12, 15), "P", STRIKE_440)


@pytest.fixture()
def spy_csv(tmp_path: Path) -> Path:
    return fx.write_csv(tmp_path / "spy.csv", fx.SPY_MAIN_ROWS)


@pytest.fixture()
def result(spy_csv: Path):
    return parse_cboe_eod_csv(spy_csv)


# ---- file shape ------------------------------------------------------------


def test_header_drift_refuses(tmp_path: Path) -> None:
    path = fx.write_csv(
        tmp_path / "drift.csv", fx.SPY_MAIN_ROWS, header=fx.HEADER.replace("vwap", "vwap_x")
    )
    with pytest.raises(CboeEodFormatError):
        parse_cboe_eod_csv(path)


def test_column_contract_is_34(tmp_path: Path, spy_csv: Path) -> None:
    assert len(CBOE_EOD_COLUMNS) == 34
    assert spy_csv.read_text().splitlines()[0].count(",") == 33


def test_empty_file_header_only(tmp_path: Path) -> None:
    path = fx.write_csv(tmp_path / "empty.csv", ())
    result = parse_cboe_eod_csv(path)
    assert result.day_files == {}
    assert result.stats.rows_total == 0
    assert any("no sessions" in i for i in result.issues)


# ---- accounting buckets ------------------------------------------------------


def test_stats_buckets_and_identity(result) -> None:
    assert result.stats.rows_total == 13
    assert result.stats.rows_mapped == 9
    assert result.stats.duplicate_rows == 1
    assert result.stats.zero_greeks_rows == 2
    assert result.stats.zero_bid_rows == 1
    assert result.stats.early_close_sessions == 1
    assert result.stats.nonstandard_delivery_rows == 1
    # every row accounted: mapped + refused-with-a-stat; nothing uncounted
    assert result.stats.rows_total == (
        result.stats.rows_mapped
        + result.stats.duplicate_rows
        + result.stats.zero_greeks_rows
        + result.stats.nonstandard_delivery_rows
    )


def test_day_files_sessions_and_entry_counts(result) -> None:
    assert set(result.day_files) == {
        date(2023, 8, 25),
        date(2023, 8, 28),
        date(2023, 8, 29),
        date(2023, 11, 24),
    }
    assert result.underlying_security_id == "SPY"
    assert [e.contract_id for e in result.day_files[date(2023, 8, 25)].entries] == [
        CID_440C,
        CID_440P,
        CID_720C,
    ]
    assert len(result.day_files[date(2023, 8, 28)].entries) == 3
    assert len(result.day_files[date(2023, 8, 29)].entries) == 1
    assert len(result.day_files[date(2023, 11, 24)].entries) == 2


def test_normal_row_mapping_and_decimal_exactness(result) -> None:
    entry = next(
        e for e in result.day_files[date(2023, 8, 25)].entries if e.contract_id == CID_440C
    )
    assert entry.open_interest == 5678
    assert entry.same_day_volume == 1234
    assert entry.abs_delta == Decimal("0.5123")
    assert entry.quote_condition == "regular"
    assert str(entry.quote_1545.bid) == "2.0100"  # 4dp preserved, not float
    assert str(entry.quote_1545.ask) == "2.0300"
    assert entry.quote_1545.bid_size == 10
    assert entry.quote_1545.ask_size == 10
    assert str(entry.quote_eod.bid) == "1.9500"
    assert entry.quote_eod.ask_size == 15
    put = next(e for e in result.day_files[date(2023, 8, 25)].entries if e.contract_id == CID_440P)
    assert put.abs_delta == Decimal("0.4876")  # abs() of -0.4876


def test_file_level_underlying_prefers_1545_pair(result) -> None:
    file = result.day_files[date(2023, 8, 25)]
    assert str(file.underlying_bid) == "440.7800"
    assert str(file.underlying_ask) == "440.8000"
    assert file.underlying_20d_median_dollar_volume == Decimal("0")


def test_snapshot_timestamps_and_receipt_wall(result) -> None:
    file = result.day_files[date(2023, 8, 25)]
    assert file.received_at == datetime(2023, 8, 28, 13, 0, tzinfo=UTC)
    entry = file.entries[0]
    assert entry.quote_1545.exchange_timestamp == datetime(2023, 8, 25, 19, 45, tzinfo=UTC)
    assert entry.quote_eod.exchange_timestamp == datetime(2023, 8, 25, 20, 0, tzinfo=UTC)
    # every snapshot already satisfies exchange <= received
    validate_pit_invariants(result.day_files)


def test_t_plus_1_wall_friday_publishes_monday() -> None:
    assert next_trading_session(date(2023, 8, 25)) == date(2023, 8, 28)
    assert next_trading_session(date(2023, 11, 24)) == date(2023, 11, 27)
    assert publication_instant(date(2023, 8, 25)) == datetime(2023, 8, 28, 13, 0, tzinfo=UTC)
    # EST after the DST end: 09:00 America/New_York == 14:00 UTC
    assert publication_instant(date(2023, 11, 24)) == datetime(2023, 11, 27, 14, 0, tzinfo=UTC)


def test_source_sha_pins_bytes(tmp_path: Path) -> None:
    path = fx.write_csv(tmp_path / "a.csv", fx.SPY_MAIN_ROWS)
    result_a = parse_cboe_eod_csv(path)
    assert result_a.source_sha256 == sha256(path.read_bytes()).hexdigest()
    other = fx.write_csv(tmp_path / "b.csv", fx.SPY_MAIN_ROWS[:1])
    assert parse_cboe_eod_csv(other).source_sha256 != result_a.source_sha256


# ---- odd rows ---------------------------------------------------------------


def test_zero_greeks_counted_master_kept_entries_excluded(result) -> None:
    ids = {e.contract_id for f in result.day_files.values() for e in f.entries}
    assert CID_275C not in ids  # license-flattened deep-ITM call
    assert CID_200P not in ids  # delta "-0.0000" (negative zero)
    master_ids = {c.contract_id for c in result.contracts}
    assert {CID_275C, CID_200P} <= master_ids  # still mapped identities
    assert any("zero-greeks" in i for i in result.issues)


def test_zero_bid_row_mapped_and_counted(result) -> None:
    entry = next(
        e for e in result.day_files[date(2023, 8, 25)].entries if e.contract_id == CID_720C
    )
    assert entry.quote_1545.bid == 0
    assert entry.quote_eod.bid == 0
    assert result.stats.zero_bid_rows == 1
    assert any("zero-bid" in i for i in result.issues)


def test_duplicate_contract_refused_never_overwritten(result) -> None:
    assert result.stats.duplicate_rows == 1
    assert len(result.day_files[date(2023, 8, 25)].entries) == 3  # not 4
    assert any("duplicate contract row" in i and CID_440C in i for i in result.issues)


def test_nonstandard_delivery_recorded_and_refused(result) -> None:
    assert result.stats.nonstandard_delivery_rows == 1
    assert any("SD1" in i and CID_445C in i for i in result.issues)
    assert CID_445C not in {c.contract_id for c in result.contracts}
    assert CID_445C not in {e.contract_id for f in result.day_files.values() for e in f.entries}


def test_empty_delivery_code_is_the_norm(spy_csv: Path) -> None:
    result = parse_cboe_eod_csv(spy_csv)
    # 11 of 13 rows carry the trailing empty field; one carries SD1
    assert result.stats.nonstandard_delivery_rows == 1
    assert result.stats.rows_mapped == 9


def test_early_close_session_shape(result) -> None:
    file = result.day_files[date(2023, 11, 24)]
    assert all(e.quote_1545 is None for e in file.entries)
    assert file.snapshot_count() == len(file.entries)
    assert file.entries[0].quote_eod.exchange_timestamp == datetime(
        2023, 11, 24, 18, 0, tzinfo=UTC
    )  # 13:00 ET early-close instant
    assert str(file.underlying_bid) == "438.0000"  # EOD pair (no 15:45 that day)


def test_malformed_row_refused_with_issue(tmp_path: Path) -> None:
    path = fx.write_csv(tmp_path / "m.csv", (*fx.SPY_MAIN_ROWS, fx.SPY_MALFORMED_33_FIELDS))
    result = parse_cboe_eod_csv(path)
    assert result.stats.rows_total == 14
    assert result.stats.rows_mapped == 9  # the malformed row mapped nothing
    assert any("expected 34" in i for i in result.issues)


def test_underlying_pair_drift_audited(result) -> None:
    file = result.day_files[date(2023, 8, 29)]
    assert str(file.underlying_bid) == "440.7800"  # 15:45 pair kept
    assert any("drift" in i and "2023-08-29" in i for i in result.issues)


# ---- variant + selection refusals ---------------------------------------------


def test_no_cgi_index_refusal_names_symbol(tmp_path: Path) -> None:
    no_cgi = fx.write_csv(tmp_path / "spx_no_cgi.csv", fx.SPX_NO_CGI_ROWS)
    with pytest.raises(IndexUnderlyingNotLicensedError, match=r"\^SPX"):
        parse_cboe_eod_csv(no_cgi, variant="no_cgi")


def test_cgi_variant_accepts_the_same_index_underlying(tmp_path: Path) -> None:
    cgi = fx.write_csv(tmp_path / "spx_cgi.csv", fx.SPX_CGI_ROWS)
    result = parse_cboe_eod_csv(cgi, variant="cgi_or_historical")
    assert result.underlying_security_id == "^SPX"
    assert result.stats.rows_mapped == 2
    assert str(result.day_files[date(2023, 8, 25)].underlying_bid) == "4413.3200"


def test_no_cgi_variant_fine_for_equities(spy_csv: Path) -> None:
    result = parse_cboe_eod_csv(spy_csv, variant="no_cgi")
    assert result.stats.rows_mapped == 9


def test_zero_underlying_quotes_refused_in_any_variant(tmp_path: Path) -> None:
    zeros = fx.write_csv(tmp_path / "zeros.csv", fx.SPX_NO_CGI_ROWS)
    with pytest.raises(CboeEodUnderlyingQuoteError, match="degenerate"):
        parse_cboe_eod_csv(zeros, variant="cgi_or_historical")


def test_multi_underlying_file_requires_selection(tmp_path: Path) -> None:
    multi = fx.write_csv(tmp_path / "multi.csv", fx.MULTI_UNDERLYING_ROWS)
    with pytest.raises(CboeEodMultiUnderlyingError) as exc:
        parse_cboe_eod_csv(multi)
    assert "^SPX" in str(exc.value) and "SPY" in str(exc.value)


def test_selection_accounts_unselected_rows(tmp_path: Path) -> None:
    multi = fx.write_csv(tmp_path / "multi.csv", fx.MULTI_UNDERLYING_ROWS)
    result = parse_cboe_eod_csv(multi, underlying="SPY")
    assert result.underlying_security_id == "SPY"
    assert result.stats.rows_total == 2  # both rows counted
    assert result.stats.rows_mapped == 1  # only the SPY row mapped
    assert any("unselected underlying ^SPX: 1 rows" in i for i in result.issues)
