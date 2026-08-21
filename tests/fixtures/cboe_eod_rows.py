"""Hermetic Cboe Option EOD fixture rows (M4-A WS-A).

Handcrafted CSV lines replicating the 34-column DataShop schema exactly as
observed in the retained demo (`~/m2-evidence/cboe-sample/`, NOT referenced
by any test — a clean clone reproduces everything from this module).

Universe (SPY, four sessions):
- 2023-08-25 Fri — normal session: a mapped C/P pair at 440, a deep-ITM
  zero-greeks 275C (bid 165.7000 — the Decimal-exactness row), a zero-bid
  720C wing, a duplicate of the 440C row, a 445C row carrying a
  non-standard delivery_code, and a deep-wing 200P with delta "-0.0000".
- 2023-08-28 Mon — normal session: the 440 pair again plus a new 442.5C.
- 2023-08-29 Tue — the 440C with a >0.5% drift between the 15:45 and EOD
  underlying pairs (audited issue; file-level pair stays 15:45).
- 2023-11-24 Fri — early close: the 15:45 quartet AND the 15:45 underlying
  pair are blank on every row; EOD quotes present; delta present (rows stay
  delta-bearing). EOD timestamp is the 13:00 ET early-close instant; the
  T+1 publication wall is Monday 2023-11-27 09:00 ET.

Index files: ^SPX with real underlying quotes (cgi variant accepts) and the
same rows with zeroed underlying quotes (the no_cgi product for index
underlyings — the adapter must refuse, naming the symbol).

Every row carries exactly 34 fields; the module self-checks at import.
"""

from __future__ import annotations

from pathlib import Path

HEADER = (
    "underlying_symbol,quote_date,root,expiration,strike,option_type,open,high,low,"
    "close,trade_volume,bid_size_1545,bid_1545,ask_size_1545,ask_1545,"
    "underlying_bid_1545,underlying_ask_1545,implied_underlying_price_1545,"
    "active_underlying_price_1545,implied_volatility_1545,delta_1545,gamma_1545,"
    "theta_1545,vega_1545,rho_1545,bid_size_eod,bid_eod,ask_size_eod,ask_eod,"
    "underlying_bid_eod,underlying_ask_eod,vwap,open_interest,delivery_code"
)

# 2023-08-25 (Friday) — SPY underlying: 15:45 440.7800/440.8000, EOD
# 439.9400/439.9500 (the demo's real pairs; ~0.19% drift, under tolerance).
SPY_0825_440C = (
    "SPY,2023-08-25,SPY,2023-09-15,440.000,C,2.0200,2.0600,1.9800,2.0100,1234,"
    "10,2.0100,10,2.0300,440.7800,440.8000,440.7900,440.7900,0.1812,0.5123,"
    "0.0123,-0.0045,0.0678,0.0234,12,1.9500,15,1.9700,439.9400,439.9500,"
    "2.0080,5678,"
)
SPY_0825_440P = (
    "SPY,2023-08-25,SPY,2023-09-15,440.000,P,1.9400,1.9800,1.9000,1.9200,987,"
    "11,1.9100,9,1.9300,440.7800,440.8000,440.7900,440.7900,0.2015,-0.4876,"
    "0.0131,-0.0047,0.0701,0.0221,14,1.8600,16,1.8800,439.9400,439.9500,"
    "1.9150,4321,"
)
# Deep-ITM call with license-flattened greeks (delta/gamma/theta/vega/rho all
# zero). Counted in zero_greeks_rows, kept in the contract master, NOT an
# entry (the NOT_EVALUABLE discipline). Bid 165.7000 is the exactness row.
SPY_0825_275C_ZERO_GREEKS = (
    "SPY,2023-08-25,SPY,2023-09-15,275.000,C,163.6800,163.6800,163.1700,163.1700,9,"
    "10,165.7000,10,165.8500,440.7800,440.8000,440.7900,440.7900,0.0000,0.0000,"
    "0.0000,0.0000,0.0000,0.0000,1,164.6600,37,165.2300,439.9400,439.9500,"
    "163.5100,143,"
)
# Zero-bid wing (both present bids zero) — mapped, counted in zero_bid_rows.
SPY_0825_720C_ZERO_BID = (
    "SPY,2023-08-25,SPY,2023-11-17,720.000,C,0.0000,0.0100,0.0000,0.0000,0,"
    "0,0.0000,6847,0.0100,440.7800,440.8000,440.7900,440.7900,0.0234,0.0234,"
    "0.0000,0.0000,0.0000,0.0000,0,0.0000,307,0.0100,439.9400,439.9500,"
    "0.0000,3414,"
)
# Byte-identical repeat of SPY_0825_440C: the duplicate-contract refusal row.
SPY_0825_440C_DUPLICATE = SPY_0825_440C
# Non-standard delivery_code (trailing field non-empty): refused + counted.
SPY_0825_445C_DELIVERY = (
    "SPY,2023-08-25,SPY,2023-09-15,445.000,C,1.5000,1.5500,1.4500,1.4800,50,"
    "5,1.4700,6,1.4900,440.7800,440.8000,440.7900,440.7900,0.1712,0.4856,"
    "0.0119,-0.0044,0.0661,0.0228,7,1.4400,9,1.4600,439.9400,439.9500,"
    "1.4705,999,SD1"
)
# Deep-wing put with delta "-0.0000" (negative zero): abs() == 0, so the row
# is not delta-bearing either.
SPY_0825_200P_NEG_ZERO_DELTA = (
    "SPY,2023-08-25,SPY,2023-11-17,200.000,P,0.0000,0.0000,0.0000,0.0000,0,"
    "0,0.0000,6809,0.0100,440.7800,440.8000,440.7900,440.7900,0.0000,-0.0000,"
    "0.0000,0.0000,0.0000,0.0000,0,0.0000,102,0.0100,439.9400,439.9500,"
    "0.0000,1342,"
)

# 2023-08-28 (Monday) — underlying 15:45 441.1000/441.1200, EOD
# 440.5500/440.5600 (~0.12% drift, under tolerance).
SPY_0828_440C = (
    "SPY,2023-08-28,SPY,2023-09-15,440.000,C,2.1000,2.1400,2.0600,2.0900,1100,"
    "10,2.0700,10,2.0900,441.1000,441.1200,441.1100,441.1100,0.1901,0.5098,"
    "0.0122,-0.0044,0.0672,0.0236,11,2.0200,14,2.0400,440.5500,440.5600,"
    "2.0850,5701,"
)
SPY_0828_440P = (
    "SPY,2023-08-28,SPY,2023-09-15,440.000,P,2.0000,2.0400,1.9600,1.9900,1001,"
    "12,1.9800,8,2.0000,441.1000,441.1200,441.1100,441.1100,0.2110,-0.4901,"
    "0.0130,-0.0046,0.0695,0.0219,13,1.9300,17,1.9500,440.5500,440.5600,"
    "1.9850,4415,"
)
SPY_0828_442_5C = (
    "SPY,2023-08-28,SPY,2023-09-15,442.500,C,1.8000,1.8400,1.7600,1.7900,600,"
    "7,1.7700,7,1.7900,441.1000,441.1200,441.1100,441.1100,0.1802,0.4826,"
    "0.0121,-0.0043,0.0666,0.0232,8,1.7400,10,1.7600,440.5500,440.5600,"
    "1.7850,2345,"
)

# 2023-08-29 (Tuesday) — 15:45 mid 440.79 vs EOD mid 400.01: ~9% drift,
# audited as a material underlying-pair drift issue.
SPY_0829_440C_DRIFT = (
    "SPY,2023-08-29,SPY,2023-09-15,440.000,C,2.2000,2.2400,2.1600,2.1900,1200,"
    "9,2.1700,11,2.1900,440.7800,440.8000,440.7900,440.7900,0.1888,0.5055,"
    "0.0122,-0.0044,0.0670,0.0235,10,2.1200,13,2.1400,400.0000,400.0200,"
    "2.1850,5720,"
)

# 2023-11-24 (Friday, early close) — the 15:45 quartet AND the 15:45
# underlying pair are blank; delta survives so the rows stay delta-bearing.
SPY_1124_440C_EARLY = (
    "SPY,2023-11-24,SPY,2023-12-15,440.000,C,1.9000,1.9400,1.8600,1.8900,800,"
    ",,,,,,"
    "0.0000,438.0100,0.2100,0.5000,0.0000,0.0000,0.0000,0.0000,"
    "9,1.8600,12,1.8800,438.0000,438.0200,1.8850,3111,"
)
SPY_1124_440P_EARLY = (
    "SPY,2023-11-24,SPY,2023-12-15,440.000,P,1.8500,1.8900,1.8100,1.8400,700,"
    ",,,,,,"
    "0.0000,438.0100,0.2100,-0.4998,0.0000,0.0000,0.0000,0.0000,"
    "10,1.8100,13,1.8300,438.0000,438.0200,1.8350,2999,"
)

SPY_MAIN_ROWS: tuple[str, ...] = (
    SPY_0825_440C,
    SPY_0825_440P,
    SPY_0825_275C_ZERO_GREEKS,
    SPY_0825_720C_ZERO_BID,
    SPY_0825_440C_DUPLICATE,
    SPY_0825_445C_DELIVERY,
    SPY_0825_200P_NEG_ZERO_DELTA,
    SPY_0828_440C,
    SPY_0828_440P,
    SPY_0828_442_5C,
    SPY_0829_440C_DRIFT,
    SPY_1124_440C_EARLY,
    SPY_1124_440P_EARLY,
)

# A malformed row (33 fields — the delivery_code field dropped).
SPY_MALFORMED_33_FIELDS = (
    "SPY,2023-08-25,SPY,2023-09-15,441.000,C,1.6000,1.6500,1.5500,1.5900,60,"
    "6,1.5800,7,1.6000,440.7800,440.8000,440.7900,440.7900,0.1752,0.4901,"
    "0.0120,-0.0043,0.0660,0.0230,8,1.5500,11,1.5700,439.9400,439.9500,"
    "1.5850,777"
)

# ^SPX with REAL underlying quotes (15:45 4413.3200/4414.4900, EOD
# 4404.6300/4405.8600) — the cgi_or_historical variant accepts index files.
SPX_CGI_ROWS: tuple[str, ...] = (
    "^SPX,2023-08-25,SPX,2023-09-15,4400.000,C,30.1000,30.6000,29.8000,30.2000,900,"
    "5,30.1500,7,30.2500,4413.3200,4414.4900,4413.9050,4413.9050,0.1211,0.5011,"
    "0.0012,-0.0445,0.6612,0.0231,6,30.0000,9,30.1000,4404.6300,4405.8600,"
    "30.1500,1234,",
    "^SPX,2023-08-25,SPX,2023-09-15,4400.000,P,29.8000,30.2000,29.5000,29.9000,850,"
    "8,29.8500,6,29.9500,4413.3200,4414.4900,4413.9050,4413.9050,0.1315,-0.4985,"
    "0.0013,-0.0451,0.6720,0.0225,7,29.7000,10,29.8000,4404.6300,4405.8600,"
    "29.8750,1188,",
)

# The same index rows with zeroed underlying quotes — what the no_cgi
# subscription delivers for index underlyings (observed in the demo).
SPX_NO_CGI_ROWS: tuple[str, ...] = tuple(
    row.replace("4413.3200,4414.4900", "0.0000,0.0000").replace(
        "4404.6300,4405.8600", "0.0000,0.0000"
    )
    for row in SPX_CGI_ROWS
)

# A multi-underlying bundle (selection required / per-symbol accounting).
MULTI_UNDERLYING_ROWS: tuple[str, ...] = (SPY_0825_440C, SPX_CGI_ROWS[0])


def write_csv(path: Path, rows: tuple[str, ...], *, header: str = HEADER) -> Path:
    """Write a hermetic CSV file (header + rows, newline-terminated)."""
    path.write_text("\n".join((header, *rows)) + "\n", encoding="utf-8")
    return path


def _selfcheck() -> None:
    for name, row in sorted(globals().items()):
        if name.startswith("_") or not isinstance(row, str):
            continue
        if "," not in row or row is HEADER or "MALFORMED" in name:
            continue
        assert row.count(",") + 1 == 34, f"{name}: {row.count(',') + 1} fields"
    for rows in (SPY_MAIN_ROWS, SPX_CGI_ROWS, SPX_NO_CGI_ROWS, MULTI_UNDERLYING_ROWS):
        for row in rows:
            assert row.count(",") + 1 == 34, row[:60]
    assert SPY_MALFORMED_33_FIELDS.count(",") + 1 == 33


_selfcheck()
