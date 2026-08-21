"""Hermetic Massive (Polygon) free-tier capture fixtures (M4 WS-D2).

Raw JSON TEXT, not dicts — the same discipline as `cboe_eod_rows.py` keeps
raw CSV lines: the inspector decodes with `json.loads(..., parse_float=Decimal)`
so a strike's exactness depends on the literal token in the file, and a fixture
built from Python floats would silently prove nothing.

Two families live here:

1. `REAL_*` — verbatim slices of live captures taken 2026-08-21 (API key
   never present in these bodies; `next_url` is truncated to its cursor
   head). They exist to pin the EXACT vendor key shapes, including the
   `strike_price: 587.5` fractional case, the `status: NOT_AUTHORIZED`
   body that arrives at HTTP 200, and a `next_url` that marks an
   incomplete capture.

2. The hand-built universe every numeric assertion is computed from
   (three SPY as_ofs plus one SPX as_of, all synthetic but vendor-shaped):

   SPY, as_of 2025-03-05 / 2025-03-06 / 2025-03-07
   - C1 O:SPY250307C00560000  exp 2025-03-07 K 560 call  spc 100  root SPY
   - P1 O:SPY250307P00560000  exp 2025-03-07 K 560 put   spc 100  root SPY
   - C2 O:SPY250404C00570000  exp 2025-04-04 K 570 call  spc 100  root SPY
   - C3 O:SPY250404C00580000  exp 2025-04-04 K 580 call  spc 100  root SPY
        (present 03-05 and 03-06, GONE 03-07 while un-expired)
   - C4 O:SPY250620C00600000  exp 2025-06-20 K 600 call  spc 100 -> **80**
        at 2025-03-07 (the shares_per_contract restatement)
   - A1 O:SPY1250404C00570000 exp 2025-04-04 K 570 call  spc **80**  root
        **SPY1** (the adjusted deliverable; present 03-05/03-06, gone 03-07,
        which also retires the SPY1 root)
   - L1 O:SPY260619C00650000  exp 2026-06-19 K 650 call  spc 100  root SPY
        (LEAPS, third Friday of a quarter month)
   - P2 O:SPY250404P00570000  exp 2025-04-04 K 570 put   spc 100  root SPY
        (BORN at 2025-03-06)
   Universe sizes: 7 / 8 / 6. 2025-03-06 is delivered as a TWO-PAGE capture.

   SPX ("I:SPX"), as_of 2025-03-07 only — the multi-root collision plus the
   european exercise style:
   - X1 O:SPX250321C05800000  exp 2025-03-21 K 5800 call european root SPX
   - X2 O:SPXW250307P05700000 exp 2025-03-07 K 5700 put  european root SPXW
   - X3 O:SPXW250411C05900000 exp 2025-04-11 K 5900 call european root SPXW

   Bars (daily aggregates, `t` = ms epoch of ET midnight):
   - L1 on 2025-04-14/15/16/17 and 2025-04-21 — 2025-04-18 is the WEEKDAY
     GAP (Good Friday); 04-19/04-20 are the expected weekend skip.
   - X3 on 2025-04-08/09 (contiguous, no gap).
   - O:QQQ250404C00480000 on 2025-04-08 — no contract master owns it, so it
     is the UNMATCHED series (counted and named, never silently dropped).

No API key, no network, no absolute host paths.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

# ---- verbatim live-capture slices ---------------------------------------------

# /v3/reference/options/contracts?underlying_ticker=SPY&as_of=... (3 of 250
# results, key order as delivered). `next_url` is the real cursor form,
# truncated: it never carried the API key, and `_selfcheck` refuses the vendor
# key query parameter anywhere in this module (the needle is assembled from
# fragments so the guard never plants the very string it forbids).
REAL_CONTRACTS_PAYLOAD = """{
  "results": [
    {
      "cfi": "OCASPS",
      "contract_type": "call",
      "exercise_style": "american",
      "expiration_date": "2025-03-14",
      "primary_exchange": "BATO",
      "shares_per_contract": 100,
      "strike_price": 560,
      "ticker": "O:SPY250314C00560000",
      "underlying_ticker": "SPY"
    },
    {
      "cfi": "OCASPS",
      "contract_type": "call",
      "exercise_style": "american",
      "expiration_date": "2025-03-14",
      "primary_exchange": "BATO",
      "shares_per_contract": 100,
      "strike_price": 587.5,
      "ticker": "O:SPY250314C00587500",
      "underlying_ticker": "SPY"
    },
    {
      "cfi": "OPASPS",
      "contract_type": "put",
      "exercise_style": "american",
      "expiration_date": "2025-03-14",
      "primary_exchange": "BATO",
      "shares_per_contract": 100,
      "strike_price": 375,
      "ticker": "O:SPY250314P00375000",
      "underlying_ticker": "SPY"
    }
  ],
  "status": "OK",
  "request_id": "3caaad2cbcbce3794e9efa1cca4b3950",
  "next_url": "https://api.polygon.io/v3/reference/options/contracts?cursor=YXA9JTdCJTIySUQlMjI",
  "as_of": "2025-03-07"
}
"""

# /v2/aggs/ticker/O:SPY250314C00560000/range/1/day/... (3 of 24 bars).
REAL_BARS_PAYLOAD = """{
  "ticker": "O:SPY250314C00560000",
  "queryCount": 3,
  "resultsCount": 3,
  "adjusted": true,
  "results": [
    {"v": 25, "vw": 40.08, "o": 40.08, "c": 40.08, "h": 40.08, "l": 40.08, "t": 1738558800000, "n": 1},
    {"v": 4, "vw": 45.875, "o": 45.82, "c": 45.89, "h": 45.9, "l": 45.82, "t": 1738645200000, "n": 4},
    {"v": 1, "vw": 45.12, "o": 45.12, "c": 45.12, "h": 45.12, "l": 45.12, "t": 1738731600000, "n": 1}
  ],
  "status": "OK",
  "request_id": "5ed4150a95e896d5760d30e61f63dbb4",
  "count": 3
}
"""

# /v3/snapshot/options/SPY — HTTP **200**, body says otherwise.
NOT_AUTHORIZED_PAYLOAD = """{
  "status": "NOT_AUTHORIZED",
  "request_id": "0000000000000000000000000000dead",
  "message": "You are not entitled to this data. Please upgrade your plan at https://polygon.io/pricing"
}
"""

# ---- hand-built universe -------------------------------------------------------

AS1 = "2025-03-05"
AS2 = "2025-03-06"
AS3 = "2025-03-07"

SPY = "SPY"
SPX = "I:SPX"

# Bar timestamps: ms epoch of 00:00 America/New_York (EDT, UTC-4) on each date.
T_2025_04_08 = "1744084800000"
T_2025_04_09 = "1744171200000"
T_2025_04_14 = "1744603200000"
T_2025_04_15 = "1744689600000"
T_2025_04_16 = "1744776000000"
T_2025_04_17 = "1744862400000"
T_2025_04_21 = "1745208000000"


def contract_result(
    *,
    ticker: str,
    underlying: str,
    expiration: str,
    strike: str,
    contract_type: str,
    exercise_style: str = "american",
    shares_per_contract: str = "100",
    cfi: str | None = None,
    primary_exchange: str | None = "BATO",
    extra: str = "",
) -> str:
    """One vendor result object as raw JSON text (numbers stay bare tokens).

    `strike` / `shares_per_contract` are numeric LITERALS ("560", "587.5"),
    never Python floats — that is the whole point of a text fixture.
    """
    code = cfi if cfi is not None else ("OCASPS" if contract_type == "call" else "OPASPS")
    fields = []
    if code:
        fields.append(f'"cfi": "{code}"')
    fields.append(f'"contract_type": "{contract_type}"')
    fields.append(f'"exercise_style": "{exercise_style}"')
    fields.append(f'"expiration_date": "{expiration}"')
    if primary_exchange:
        fields.append(f'"primary_exchange": "{primary_exchange}"')
    fields.append(f'"shares_per_contract": {shares_per_contract}')
    fields.append(f'"strike_price": {strike}')
    fields.append(f'"ticker": "{ticker}"')
    fields.append(f'"underlying_ticker": "{underlying}"')
    if extra:
        fields.append(extra)
    return "{" + ", ".join(fields) + "}"


def contracts_payload(
    *,
    results: Sequence[str],
    as_of: str | None = None,
    status: str | None = "OK",
    next_url: str | None = None,
    underlying: str | None = None,
) -> str:
    """A single-response capture (optionally carrying the envelope `as_of`)."""
    parts = ['"results": [' + ", ".join(results) + "]"]
    if status is not None:
        parts.append(f'"status": "{status}"')
    if next_url is not None:
        parts.append(f'"next_url": "{next_url}"')
    if as_of is not None:
        parts.append(f'"as_of": "{as_of}"')
    if underlying is not None:
        parts.append(f'"underlying_ticker": "{underlying}"')
    return "{" + ", ".join(parts) + "}"


def paged_contracts_payload(*, pages: Sequence[str], as_of: str) -> str:
    """A multi-page capture envelope: every page but the last carries next_url."""
    return "{" + f'"as_of": "{as_of}", "pages": [' + ", ".join(pages) + "]}"


def bar(
    *,
    v: str,
    t: str,
    vw: str = "1.00",
    o: str = "1.00",
    c: str = "1.00",
    h: str = "1.00",
    low: str = "1.00",
    n: str = "1",
) -> str:
    return f'{{"v": {v}, "vw": {vw}, "o": {o}, "c": {c}, "h": {h}, "l": {low}, "t": {t}, "n": {n}}}'


def bars_payload(
    *,
    ticker: str | None,
    results: Sequence[str],
    status: str | None = "OK",
    results_count: str | None = None,
    adjusted: str = "true",
) -> str:
    parts = []
    if ticker is not None:
        parts.append(f'"ticker": "{ticker}"')
    parts.append(f'"adjusted": {adjusted}')
    if results_count is not None:
        parts.append(f'"resultsCount": {results_count}')
    parts.append('"results": [' + ", ".join(results) + "]")
    if status is not None:
        parts.append(f'"status": "{status}"')
    return "{" + ", ".join(parts) + "}"


# --- SPY contract rows ---------------------------------------------------------

C1 = contract_result(
    ticker="O:SPY250307C00560000",
    underlying=SPY,
    expiration="2025-03-07",
    strike="560",
    contract_type="call",
)
P1 = contract_result(
    ticker="O:SPY250307P00560000",
    underlying=SPY,
    expiration="2025-03-07",
    strike="560",
    contract_type="put",
)
C2 = contract_result(
    ticker="O:SPY250404C00570000",
    underlying=SPY,
    expiration="2025-04-04",
    strike="570",
    contract_type="call",
)
C3 = contract_result(
    ticker="O:SPY250404C00580000",
    underlying=SPY,
    expiration="2025-04-04",
    strike="580",
    contract_type="call",
)
C4_STANDARD = contract_result(
    ticker="O:SPY250620C00600000",
    underlying=SPY,
    expiration="2025-06-20",
    strike="600",
    contract_type="call",
)
C4_ADJUSTED = contract_result(
    ticker="O:SPY250620C00600000",
    underlying=SPY,
    expiration="2025-06-20",
    strike="600",
    contract_type="call",
    shares_per_contract="80",
)
A1_NON_STANDARD = contract_result(
    ticker="O:SPY1250404C00570000",
    underlying=SPY,
    expiration="2025-04-04",
    strike="570",
    contract_type="call",
    shares_per_contract="80",
)
L1_LEAPS = contract_result(
    ticker="O:SPY260619C00650000",
    underlying=SPY,
    expiration="2026-06-19",
    strike="650",
    contract_type="call",
)
P2_BORN = contract_result(
    ticker="O:SPY250404P00570000",
    underlying=SPY,
    expiration="2025-04-04",
    strike="570",
    contract_type="put",
)

# --- SPX contract rows (european, SPX + SPXW roots) ----------------------------

X1 = contract_result(
    ticker="O:SPX250321C05800000",
    underlying=SPX,
    expiration="2025-03-21",
    strike="5800",
    contract_type="call",
    exercise_style="european",
    primary_exchange="XCBO",
)
X2 = contract_result(
    ticker="O:SPXW250307P05700000",
    underlying=SPX,
    expiration="2025-03-07",
    strike="5700",
    contract_type="put",
    exercise_style="european",
    primary_exchange="XCBO",
)
X3 = contract_result(
    ticker="O:SPXW250411C05900000",
    underlying=SPX,
    expiration="2025-04-11",
    strike="5900",
    contract_type="call",
    exercise_style="european",
    primary_exchange="XCBO",
)

# --- capture payloads ----------------------------------------------------------

SPY_AS1_PAYLOAD = contracts_payload(
    results=(C1, P1, C2, C3, C4_STANDARD, A1_NON_STANDARD, L1_LEAPS), as_of=AS1
)
# Two-page delivery: page 0 must carry next_url, page 1 must not.
SPY_AS2_PAYLOAD = paged_contracts_payload(
    pages=(
        contracts_payload(
            results=(C1, P1, C2, C3, C4_STANDARD),
            as_of=None,
            next_url="https://api.polygon.io/v3/reference/options/contracts?cursor=PAGE2",
        ),
        contracts_payload(results=(A1_NON_STANDARD, L1_LEAPS, P2_BORN), as_of=None),
    ),
    as_of=AS2,
)
SPY_AS3_PAYLOAD = contracts_payload(results=(C1, P1, C2, C4_ADJUSTED, L1_LEAPS, P2_BORN), as_of=AS3)
SPX_AS3_PAYLOAD = contracts_payload(results=(X1, X2, X3), as_of=AS3)

# The same AS1 universe with NO envelope as_of — the filename must supply it.
SPY_AS1_BARE_PAYLOAD = contracts_payload(
    results=(C1, P1, C2, C3, C4_STANDARD, A1_NON_STANDARD, L1_LEAPS), as_of=None
)

MASTER_FILES: tuple[tuple[str, str], ...] = (
    ("spy_2025-03-05.json", SPY_AS1_PAYLOAD),
    ("spy_2025-03-06.json", SPY_AS2_PAYLOAD),
    ("spy_2025-03-07.json", SPY_AS3_PAYLOAD),
    ("spx_2025-03-07.json", SPX_AS3_PAYLOAD),
)

# --- bar payloads --------------------------------------------------------------

BARS_SPY_LEAPS_PAYLOAD = bars_payload(
    ticker="O:SPY260619C00650000",
    results_count="5",
    results=(
        bar(
            v="250",
            t=T_2025_04_14,
            vw="12.50",
            o="12.40",
            c="12.60",
            h="12.70",
            low="12.30",
            n="40",
        ),
        bar(
            v="100",
            t=T_2025_04_15,
            vw="12.10",
            o="12.05",
            c="12.15",
            h="12.20",
            low="12.00",
            n="20",
        ),
        bar(v="0", t=T_2025_04_16, vw="12.00", o="12.00", c="12.00", h="12.00", low="12.00", n="0"),
        bar(
            v="40", t=T_2025_04_17, vw="11.90", o="11.85", c="11.95", h="12.00", low="11.80", n="8"
        ),
        bar(
            v="300",
            t=T_2025_04_21,
            vw="13.00",
            o="12.90",
            c="13.10",
            h="13.20",
            low="12.80",
            n="55",
        ),
    ),
)
BARS_SPX_PAYLOAD = bars_payload(
    ticker="O:SPXW250411C05900000",
    results_count="2",
    results=(
        bar(v="7", t=T_2025_04_08, vw="21.00", o="20.90", c="21.10", h="21.20", low="20.80", n="3"),
        bar(v="9", t=T_2025_04_09, vw="22.00", o="21.90", c="22.10", h="22.20", low="21.80", n="4"),
    ),
)
# No contract master owns this ticker: the UNMATCHED series.
BARS_UNMATCHED_PAYLOAD = bars_payload(
    ticker="O:QQQ250404C00480000",
    results_count="1",
    results=(
        bar(v="3", t=T_2025_04_08, vw="4.00", o="4.00", c="4.00", h="4.00", low="4.00", n="2"),
    ),
)

BAR_FILES: tuple[tuple[str, str], ...] = (
    ("bars_spy_leaps.json", BARS_SPY_LEAPS_PAYLOAD),
    ("bars_spxw.json", BARS_SPX_PAYLOAD),
    ("bars_unmatched.json", BARS_UNMATCHED_PAYLOAD),
)

SPOT_PROXY_PAYLOAD = """{
  "SPY": {"2025-03-05": "580.00", "2025-03-06": "575.00", "2025-03-07": "570.00"},
  "I:SPX": "5750.00"
}
"""


# ---- writers -------------------------------------------------------------------


def write_json(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_masters(directory: Path, files: Sequence[tuple[str, str]] = MASTER_FILES) -> Path:
    for name, text in files:
        write_json(directory / name, text)
    return directory


def write_bars(directory: Path, files: Sequence[tuple[str, str]] = BAR_FILES) -> Path:
    for name, text in files:
        write_json(directory / name, text)
    return directory


def _selfcheck() -> None:
    import json as _json

    # Needles are assembled: the guard must not plant the strings it forbids.
    key_param = "api" + "Key"
    host_path = "/" + "home" + "/"
    for name, value in sorted(globals().items()):
        if name.startswith("_") or not isinstance(value, str):
            continue
        assert key_param not in value, f"{name} leaks the vendor key query parameter"
        assert host_path not in value, f"{name} carries an absolute host path"
    for _name, text in MASTER_FILES + BAR_FILES:
        assert isinstance(_json.loads(text), dict)
    for text in (
        REAL_CONTRACTS_PAYLOAD,
        REAL_BARS_PAYLOAD,
        NOT_AUTHORIZED_PAYLOAD,
        SPY_AS1_BARE_PAYLOAD,
        SPOT_PROXY_PAYLOAD,
    ):
        assert isinstance(_json.loads(text), dict)
    # The bar timestamps are exactly one calendar day apart where consecutive.
    assert int(T_2025_04_15) - int(T_2025_04_14) == 86_400_000
    assert int(T_2025_04_21) - int(T_2025_04_17) == 4 * 86_400_000


_selfcheck()
