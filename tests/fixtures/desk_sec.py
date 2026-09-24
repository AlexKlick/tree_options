"""Synthetic SEC EDGAR payloads for the desk events tests.

NOT captured: DESK_SEC_UA (the contact User-Agent SEC requires) was unset
in the build environment, so no request went to sec.gov. The shapes follow
SEC's documented EDGAR APIs:

* ``https://www.sec.gov/files/company_tickers.json``:
  ``{"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}``;
* ``https://data.sec.gov/submissions/CIK##########.json``: entity fields
  plus ``filings.recent``, parallel arrays (``accessionNumber``,
  ``filingDate``, ``acceptanceDateTime`` "2026-07-30T16:30:12.000Z",
  ``form``, ``items`` "2.02,9.01", ...) and ``filings.files``, older pages
  (``{"name", "filingCount", "filingFrom", "filingTo"}``) whose own JSON
  holds the same arrays at top level.

Every CIK, accession number and time here is invented.
"""

from __future__ import annotations

import json
from typing import Any


def tickers_payload() -> bytes:
    rows = [
        (320193, "AAPL", "Apple Inc."),
        (19617, "JPM", "JPMorgan Chase & Co."),
        (34088, "XOM", "Exxon Mobil Corp"),
        (1067983, "BRK-B", "Berkshire Hathaway Inc"),
        (909832, "COST", "Costco Wholesale Corp"),
    ]
    return json.dumps(
        {str(i): {"cik_str": c, "ticker": t, "title": n} for i, (c, t, n) in enumerate(rows)}
    ).encode()


def filing_arrays(filings: list[tuple[str, str, str]]) -> dict[str, list[Any]]:
    """(form, items, acceptanceDateTime) rows as EDGAR's parallel arrays."""
    return {
        "accessionNumber": [f"0000000000-26-{i:06d}" for i in range(len(filings))],
        "filingDate": [acc[:10] for _f, _i, acc in filings],
        "reportDate": [acc[:10] for _f, _i, acc in filings],
        "acceptanceDateTime": [acc for _f, _i, acc in filings],
        "form": [f for f, _i, _a in filings],
        "items": [i for _f, i, _a in filings],
        "primaryDocument": ["doc.htm" for _ in filings],
    }


def submissions_payload(
    filings: list[tuple[str, str, str]],
    *,
    files: list[dict[str, Any]] | None = None,
    name: str = "Apple Inc.",
) -> bytes:
    return json.dumps(
        {
            "cik": "320193",
            "name": name,
            "tickers": ["AAPL"],
            "filings": {"recent": filing_arrays(filings), "files": files or []},
        }
    ).encode()


def page_payload(filings: list[tuple[str, str, str]]) -> bytes:
    return json.dumps(filing_arrays(filings)).encode()


AAPL_FILINGS: list[tuple[str, str, str]] = [
    ("8-K", "2.02,9.01", "2026-07-30T16:30:12.000Z"),  # after the close
    ("10-Q", "", "2026-08-01T06:01:44.000Z"),
    ("8-K", "5.07", "2026-02-25T16:05:00.000Z"),  # a vote result, not earnings
    ("8-K", "2.02,9.01", "2026-04-30T07:00:05.000Z"),  # before the open
    ("8-K", "2.02", "2026-01-29T12:00:00.000Z"),  # during the session
    ("8-K", "2.02,9.01", "2025-11-06T16:00:00.000Z"),  # EST, exactly 16:00
    ("8-K", "2.02,9.01", "2020-10-29T16:30:00.000Z"),  # before 2021: ignored
]
