"""D3 events: the sealed macro calendar, earnings timing, ETF holding reports.

Macro calendar (``data/desk/events/macro-<Y1>-<Y2>.json`` + ``.sha256`` +
``MACRO-SEALS.md``, tracked, ``DESK_EVENTS_DIR`` overrides):

* FOMC meetings parsed from the Fed's calendar page (:data:`FOMC_URL`) by
  :func:`parse_fomc`; the decision day is the meeting's last day;
* CPI and NFP release dates: HAND-ENTERED only (bls.gov answers scripts
  with HTTP 403), each item ``{date, source, entered_by}``; an empty array
  is a declared gap (``todo``), never a default;
* monthly OpEx (third Friday, else the session before) and VIX final
  settlement (the Wednesday 30 calendar days before the next month's
  OpEx Friday, else the session before), computed on the NYSE calendar.

``seal-macro`` (operator/agent, in a worktree) rebuilds the file, carries
the hand-entered items, rewrites the sidecar and appends a MACRO-SEALS.md
row; :func:`load_macro` refuses a file whose sha256 does not match.
The weekly ``update-events`` job only CHECKS the seal and the Fed page
(``<state>/events/macro-drift.json`` when they differ); it never reseals.

Earnings timing (``<paper>/earnings-timing.json``, gitignored artifacts)::

    {name: {date: {"timing": "bmo"|"amc"|"unknown", "source": str,
                   "fetched_at": iso, "status": "confirmed"|"estimated"}}}

* estimated: Nasdaq's earnings calendar for the next HORIZON sessions
  (``time-pre-market`` / ``time-after-hours`` / ``time-not-supplied``).
  An estimate the vendor stops listing on a day it answered with rows is
  dropped; an empty answer is no evidence and drops nothing;
* confirmed: SEC EDGAR 8-K item 2.02 filings since 2021, one per ET date
  (the earliest), timed by ``acceptanceDateTime`` against 09:30 / 16:00 ET.
  EDGAR's digits are Eastern wall time despite the trailing "Z" (they
  match the filing index pages; EDGAR accepts filings 06:00-22:00 ET): a
  time outside those hours is ``unknown``, and a run where more than 10%
  fall outside is dropped whole as ``tz_suspect``. SEC requires a contact
  User-Agent: it is read ONLY from ``DESK_SEC_UA``; unset, EDGAR is
  skipped (``sec_ua_missing``). A confirmed entry is never downgraded.

The sealed ``earnings-calendar.json`` is read-only here. Readers:
:func:`upcoming_earnings` (sealed dates first; estimated dates are
``blocker_only``: they may block a trade, never trigger PEAD),
:func:`macro_events`, :func:`etf_holding_reports`.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from tree_options.desk import paths
from tree_options.desk.http import BROWSER_HEADERS, Get
from tree_options.desk.sessions import Calendar, previous_session
from tree_options.desk.store import atomic_write_bytes
from tree_options.desk.universe import PANEL_ETFS, PANEL_NAMES
from tree_options.time.expiries import minus_calendar_days
from tree_options.time.monthlies import is_monthly_expiry
from tree_options.trex.clock import ET

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings?date={day}"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_PAGE_URL = "https://data.sec.gov/submissions/{name}"
SEC_UA_ENV = "DESK_SEC_UA"

MACRO_SCHEMA = "desk-macro/1"
MACRO_FILE = "macro-2026-2027.json"
MACRO_SEALS = "MACRO-SEALS.md"
TIMING_FILE = "earnings-timing.json"
SEALED_CALENDAR = "earnings-calendar.json"
HAND_KINDS = ("cpi", "nfp")
TIMINGS = frozenset({"bmo", "amc", "unknown"})
TIMING_STATUSES = frozenset({"confirmed", "estimated"})

HORIZON = 65  # sessions of Nasdaq estimates (~3 months: covers 45-90 DTE entries)
NASDAQ_PACE_S = 1.0
SEC_PACE_S = 0.25  # SEC allows 10 requests/s; stay well under
SEC_MAX_PAGES = 10  # older submissions pages per company
EDGAR_FROM = date(2021, 1, 1)
EDGAR_OPEN = time(6, 0)
EDGAR_CLOSE = time(22, 1)  # accepts until 22:00 ET
TZ_SUSPECT_SHARE = 0.10
TIMEOUT_S = 30.0
# the Fed page served a full browser UA over HTTP/1.1 (200, 2026-09-23)
FED_HEADERS: Mapping[str, str] = {"User-Agent": BROWSER_HEADERS["User-Agent"]}

Clock = Callable[[], datetime]
Sleep = Callable[[float], None]

# Largest holdings of the sector/theme ETFs, for the "a big holding reports"
# flag. Only long-standing, obvious top weights, as published by the issuers
# (State Street SPDR XLE/XLV/XLF, iShares SOXX, VanEck SMH, Invesco QQQ) and
# known to the author on 2026-09-23. NOT machine-verified that day: the
# scripted fetch of the issuer holdings files was refused (SSGA 301, iShares
# an HTML page, Invesco 406). Re-check by hand each quarter.
ETF_HOLDINGS: Mapping[str, tuple[str, ...]] = {
    "SMH": ("NVDA", "TSM", "AVGO"),
    "SOXX": ("AVGO", "NVDA", "AMD"),
    "XLE": ("XOM", "CVX"),
    "XLV": ("LLY", "JNJ", "ABBV", "UNH"),
    "XLF": ("BRK.B", "JPM", "V", "MA"),
    "QQQ": ("NVDA", "MSFT", "AAPL", "AMZN", "AVGO", "META", "GOOGL"),
}

# names whose earnings timing is tracked: the panel's single stocks plus the
# ETF holdings above
EARNINGS_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(
        [
            *(n for n in PANEL_NAMES if n not in PANEL_ETFS),
            *(h for hs in ETF_HOLDINGS.values() for h in hs),
        ]
    )
)


class EventsError(ValueError):
    """An events input is missing, malformed or out of range (fail closed)."""


class FomcParseError(EventsError):
    """The Fed calendar page did not parse whole."""


class MacroSealError(EventsError):
    """The macro file does not match its sha256 sidecar."""


class VendorParseError(EventsError):
    """A vendor payload is not the documented shape."""


# ------------------------------------------------------------------ FOMC

_MONTHS = {
    name: i
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}
_PANEL = re.compile(r"<h4><a id=\"\d+\">(\d{4}) FOMC Meetings</a></h4>")
_ROW = re.compile(
    r"fomc-meeting__month[^\"]*\">(.*?)</div>\s*<div class=\"fomc-meeting__date[^\"]*\">(.*?)</div>",
    re.S,
)
_TWO_DAY = re.compile(r"(\d{1,2})-(\d{1,2})(\*?)")
_ONE_DAY = re.compile(r"(\d{1,2})(\*?)(?:\s*\((notation vote|unscheduled)\))?")
_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class FomcMeeting:
    start: date
    end: date  # the decision (statement) day
    sep: bool  # with a Summary of Economic Projections
    kind: str  # scheduled | notation_vote | unscheduled


def _text(cell: str) -> str:
    return " ".join(_TAG.sub(" ", cell).split())


def _month(name: str, where: str) -> int:
    try:
        return _MONTHS[name.strip().lower()]
    except KeyError:
        raise FomcParseError(f"{where}: unknown month {name!r}") from None


def _meeting(year: int, month_cell: str, date_cell: str) -> FomcMeeting:
    where = f"{year} {month_cell} {date_cell}"
    months = [_month(m, where) for m in month_cell.split("/")]
    if len(months) not in (1, 2):
        raise FomcParseError(f"{where}: bad month cell")
    try:
        two = _TWO_DAY.fullmatch(date_cell)
        if two:
            m1, m2 = months[0], months[-1]
            start = date(year, m1, int(two.group(1)))
            end = date(year + 1 if m2 < m1 else year, m2, int(two.group(2)))
            if end <= start:
                raise FomcParseError(f"{where}: meeting ends before it starts")
            return FomcMeeting(start, end, two.group(3) == "*", "scheduled")
        one = _ONE_DAY.fullmatch(date_cell)
        if one and len(months) == 1:
            d = date(year, months[0], int(one.group(1)))
            kind = (one.group(3) or "scheduled").replace(" ", "_")
            return FomcMeeting(d, d, one.group(2) == "*", kind)
    except ValueError as exc:
        raise FomcParseError(f"{where}: {exc}") from exc
    raise FomcParseError(f"{where}: unreadable date cell")


def parse_fomc(html: str) -> list[FomcMeeting]:
    """Every meeting on the Fed's calendar page, all year panels. Raises
    rather than return a partial or empty list."""
    heads = list(_PANEL.finditer(html))
    if not heads:
        raise FomcParseError("no FOMC year panels on the page")
    out: list[FomcMeeting] = []
    for k, head in enumerate(heads):
        year = int(head.group(1))
        seg = html[head.end() : heads[k + 1].start() if k + 1 < len(heads) else len(html)]
        rows = _ROW.findall(seg)
        if not rows or len(rows) != seg.count("fomc-meeting__month"):
            raise FomcParseError(f"{year}: {len(rows)} readable meeting rows")
        out.extend(_meeting(year, _text(m), _text(d)) for m, d in rows)
    return sorted(out, key=lambda m: (m.end, m.kind))


# ------------------------------------------------------- OpEx / VIX expiry


def _within(cal: Calendar, d: date) -> None:
    sessions = cal.sessions()
    if not sessions[0] <= d <= sessions[-1]:
        raise EventsError(f"{d} is outside the calendar ({sessions[0]}..{sessions[-1]})")


def _session_or_before(d: date, cal: Calendar) -> date:
    _within(cal, d)
    if cal.is_session(d):
        return d
    prev = previous_session(d, cal)
    if prev is None:
        raise EventsError(f"no session before {d}")
    return prev


def third_friday(year: int, month: int) -> date:
    for day in range(15, 22):
        d = date(year, month, day)
        if is_monthly_expiry(d):
            return d
    raise AssertionError("every month has a third Friday")


def monthly_opex(year: int, month: int, cal: Calendar) -> date:
    """The monthly option expiry: the third Friday, or the session before
    it when the exchange is closed (Juneteenth 2026-06-19 -> 06-18)."""
    return _session_or_before(third_friday(year, month), cal)


def vix_expiry(year: int, month: int, cal: Calendar) -> date:
    """VIX final settlement for ``month``: 30 calendar days before the next
    month's SPX expiry (its third Friday, or the business day before it
    when that Friday is a holiday); a holiday there moves to the session
    before (Cboe's rule)."""
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return _session_or_before(minus_calendar_days(monthly_opex(ny, nm, cal), 30), cal)


def _months(start: date, end: date) -> Iterable[tuple[int, int]]:
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


# ------------------------------------------------------------ macro file


def _check_items(kind: str, items: Any, lo: str, hi: str) -> list[dict[str, str]]:
    if not isinstance(items, list):
        raise EventsError(f"{kind}: not a list")
    out = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"date", "source", "entered_by"}:
            raise EventsError(f"{kind}: items are exactly {{date, source, entered_by}}")
        try:
            d = date.fromisoformat(item["date"]).isoformat()
        except (TypeError, ValueError):
            raise EventsError(f"{kind}: bad date {item.get('date')!r}") from None
        if d != item["date"] or not lo <= d <= hi:
            raise EventsError(f"{kind}: date {item['date']!r} not an ISO date in {lo}..{hi}")
        for key in ("source", "entered_by"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise EventsError(f"{kind} {d}: {key} is required (provenance)")
        out.append(dict(item))
    return sorted(out, key=lambda x: x["date"])


def build_macro(
    meetings: Sequence[FomcMeeting],
    cal: Calendar,
    start: date,
    end: date,
    *,
    fetched_on: date,
    cpi: Sequence[Mapping[str, str]] = (),
    nfp: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """The macro document for ``[start, end]``. FOMC from the parsed page
    (every year in range must have a scheduled meeting, else refuse),
    CPI/NFP only as handed in (validated), OpEx/VIX computed."""
    if end < start:
        raise EventsError("empty range")
    lo, hi = start.isoformat(), end.isoformat()
    fomc = [m for m in meetings if start <= m.end <= end]
    for year in range(start.year, end.year + 1):
        if not any(m.end.year == year and m.kind == "scheduled" for m in fomc):
            raise EventsError(f"no scheduled FOMC meeting for {year}: refusing a partial calendar")
    hand = {
        "cpi": _check_items("cpi", [dict(x) for x in cpi], lo, hi),
        "nfp": _check_items("nfp", [dict(x) for x in nfp], lo, hi),
    }
    opex = [monthly_opex(y, m, cal) for y, m in _months(start, end)]
    vix = [vix_expiry(y, m, cal) for y, m in _months(start, end)]
    return {
        "schema": MACRO_SCHEMA,
        "range": {"from": lo, "to": hi},
        "fomc": [
            {"start": m.start.isoformat(), "date": m.end.isoformat(), "sep": m.sep, "kind": m.kind}
            for m in fomc
        ],
        "fomc_source": {
            "url": FOMC_URL,
            "fetched": fetched_on.isoformat(),
            "parser": "tree_options.desk.events.parse_fomc",
        },
        "cpi": hand["cpi"],
        "nfp": hand["nfp"],
        "opex": [d.isoformat() for d in opex if start <= d <= end],
        "vix_expiry": [d.isoformat() for d in vix if start <= d <= end],
        "computed": {
            "opex": "third Friday (time.monthlies.is_monthly_expiry), else the session before",
            "vix_expiry": (
                "30 calendar days before the next month's opex date "
                "(time.expiries.minus_calendar_days), else the session before"
            ),
        },
        "todo": [
            f"{k}: operator/agent entry required (bls.gov refuses scripted requests, "
            "HTTP 403); add {date, source, entered_by} items, then run seal-macro"
            for k in HAND_KINDS
            if not hand[k]
        ],
    }


def encode_macro(doc: Mapping[str, Any]) -> bytes:
    return (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode("ascii")


def _check_macro(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, dict) or doc.get("schema") != MACRO_SCHEMA:
        raise EventsError(f"not a {MACRO_SCHEMA} document")
    rng = doc.get("range")
    if not isinstance(rng, dict) or not all(isinstance(rng.get(k), str) for k in ("from", "to")):
        raise EventsError("macro: bad range")
    for kind in HAND_KINDS:
        _check_items(kind, doc.get(kind), rng["from"], rng["to"])
    for key in ("fomc", "opex", "vix_expiry"):
        if not isinstance(doc.get(key), list):
            raise EventsError(f"macro: {key} is not a list")
    return doc


def seal_macro(
    doc: Mapping[str, Any], path: Path, *, basis: str, sealed_at: datetime | None = None
) -> str:
    """Write the document and its sha256 sidecar; append a seal row."""
    _check_macro(dict(doc))
    data = encode_macro(doc)
    sha = hashlib.sha256(data).hexdigest()
    atomic_write_bytes(path, data)
    atomic_write_bytes(path.with_suffix(".sha256"), f"{sha}  {path.name}\n".encode("ascii"))
    seals = path.parent / MACRO_SEALS
    if not seals.exists():
        atomic_write_bytes(seals, _SEALS_HEADER.encode("ascii"))
    at = (sealed_at or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    counts = " ".join(f"{k}={len(doc[k])}" for k in ("fomc", "cpi", "nfp", "opex", "vix_expiry"))
    with open(seals, "a") as fh:
        fh.write(f"| {at} | {path.name} | {sha} | {counts} | {basis} |\n")
    return sha


_SEALS_HEADER = """\
# Desk macro calendar: append-only seal log

Each row seals one ``macro-*.json`` (sha256 of the file bytes, also in the
``.sha256`` sidecar). ``tree_options.desk.events.load_macro`` refuses a file
whose bytes do not match. Rows are only ever appended (``seal-macro``).

| sealed (UTC) | file | sha256 | counts | basis |
|---|---|---|---|---|
"""


def load_macro(path: Path) -> dict[str, Any]:
    """The sealed macro document; refuses a missing or mismatched seal."""
    data = path.read_bytes()
    try:
        want = path.with_suffix(".sha256").read_text().split()[0]
    except (OSError, IndexError) as exc:
        raise MacroSealError(f"{path.name}: no sha256 sidecar") from exc
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise MacroSealError(f"{path.name}: sha256 {got[:12]} != sealed {want[:12]}")
    try:
        doc = json.loads(data)
    except ValueError as exc:
        raise MacroSealError(f"{path.name}: not JSON") from exc
    return _check_macro(doc)


@dataclass(frozen=True)
class MacroEvent:
    date: date
    kind: str  # fomc | fomc_notation_vote | fomc_unscheduled | cpi | nfp | opex | vix_expiry
    detail: str


def macro_events(start: date, end: date, *, path: Path | None = None) -> list[MacroEvent]:
    """Every sealed macro event in ``[start, end]``. A window reaching past
    the sealed range raises: silence there would read as "no events"."""
    doc = load_macro(path or paths.events_dir() / MACRO_FILE)
    lo, hi = doc["range"]["from"], doc["range"]["to"]
    if start.isoformat() < lo or end.isoformat() > hi:
        raise EventsError(f"window {start}..{end} is outside the sealed range {lo}..{hi}")
    out: list[MacroEvent] = []
    for f in doc["fomc"]:
        kind = "fomc" if f["kind"] == "scheduled" else f"fomc_{f['kind']}"
        out.append(MacroEvent(date.fromisoformat(f["date"]), kind, "SEP" if f["sep"] else ""))
    for kind in HAND_KINDS:
        out.extend(MacroEvent(date.fromisoformat(x["date"]), kind, x["source"]) for x in doc[kind])
    out.extend(MacroEvent(date.fromisoformat(d), "opex", "") for d in doc["opex"])
    out.extend(MacroEvent(date.fromisoformat(d), "vix_expiry", "") for d in doc["vix_expiry"])
    return sorted((e for e in out if start <= e.date <= end), key=lambda e: (e.date, e.kind))


def macro_gaps(*, path: Path | None = None) -> list[str]:
    """Hand-entered kinds that are still empty (declared gaps)."""
    doc = load_macro(path or paths.events_dir() / MACRO_FILE)
    return [k for k in HAND_KINDS if not doc[k]]


# -------------------------------------------------------- vendor parsers

NASDAQ_TIMING = {
    "time-pre-market": "bmo",
    "time-after-hours": "amc",
    "time-not-supplied": "unknown",
}


def parse_nasdaq(raw: bytes) -> dict[str, tuple[str, str]]:
    """symbol -> (timing, vendor code) for one calendar day. A null or empty
    ``rows`` is an empty answer (never a default); a wrong shape raises."""
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise VendorParseError("nasdaq: body is not JSON") from exc
    if not isinstance(doc, dict) or "data" not in doc:
        raise VendorParseError("nasdaq: no data key")
    data = doc["data"]
    if data is None:
        return {}
    rows = data.get("rows") if isinstance(data, dict) else "bad"
    if rows is None:
        return {}
    if not isinstance(rows, list):
        raise VendorParseError("nasdaq: rows is not a list")
    out: dict[str, tuple[str, str]] = {}
    for row in rows:
        sym = row.get("symbol") if isinstance(row, dict) else None
        if not isinstance(sym, str) or not sym.strip():
            raise VendorParseError("nasdaq: a row without a symbol")
        code = row.get("time")
        code = code if isinstance(code, str) and code else "none"
        out[sym.strip().upper()] = (NASDAQ_TIMING.get(code, "unknown"), code)
    return out


@dataclass(frozen=True)
class EdgarFiling:
    accession: str
    accepted: str  # EDGAR's acceptanceDateTime, verbatim
    form: str
    items: str


_PAGE_NAME = re.compile(r"CIK\d{10}-submissions-\d{3}\.json")
_ACCEPTED = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z?")


def _filings(arrays: Any) -> list[EdgarFiling]:
    if not isinstance(arrays, dict):
        raise VendorParseError("sec: filings are not an object")
    raw_cols = [arrays.get(k) for k in ("accessionNumber", "acceptanceDateTime", "form", "items")]
    cols = [c for c in raw_cols if isinstance(c, list)]
    if len(cols) != len(raw_cols) or len({len(c) for c in cols}) != 1:
        raise VendorParseError("sec: filing arrays missing or ragged")
    out = []
    for acc, accepted, form, items in zip(*cols, strict=True):
        if form != "8-K" or not isinstance(items, str) or not isinstance(accepted, str):
            continue
        if "2.02" not in [i.strip() for i in items.split(",")]:
            continue
        if accepted[:10] < EDGAR_FROM.isoformat():
            continue
        out.append(EdgarFiling(str(acc), accepted, form, items))
    return out


def parse_submissions(raw: bytes) -> tuple[list[EdgarFiling], list[str]]:
    """8-K item 2.02 filings since EDGAR_FROM, plus the older pages worth
    fetching (``filings.files`` reaching EDGAR_FROM). Accepts a company's
    main submissions JSON or one of its older pages."""
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise VendorParseError("sec: body is not JSON") from exc
    if not isinstance(doc, dict):
        raise VendorParseError("sec: not an object")
    if "filings" not in doc:
        return _filings(doc), []
    filings = doc["filings"]
    if not isinstance(filings, dict):
        raise VendorParseError("sec: filings is not an object")
    pages = [
        f["name"]
        for f in filings.get("files") or []
        if isinstance(f, dict)
        and isinstance(f.get("name"), str)
        and _PAGE_NAME.fullmatch(f["name"])
        and str(f.get("filingTo", "")) >= EDGAR_FROM.isoformat()
    ]
    return _filings(filings.get("recent")), pages


def parse_company_tickers(raw: bytes, names: Iterable[str]) -> dict[str, str]:
    """name -> 10-digit CIK from SEC's company_tickers.json (BRK.B is BRK-B)."""
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise VendorParseError("sec tickers: body is not JSON") from exc
    if not isinstance(doc, dict):
        raise VendorParseError("sec tickers: not an object")
    by_ticker: dict[str, int] = {}
    for v in doc.values():
        if isinstance(v, dict) and isinstance(v.get("ticker"), str):
            cik = v.get("cik_str")
            if isinstance(cik, int) and not isinstance(cik, bool):
                by_ticker.setdefault(v["ticker"].upper(), cik)
    out = {}
    for name in names:
        cik = by_ticker.get(name.replace(".", "-").upper())
        if cik is not None:
            out[name] = f"{cik:010d}"
    return out


def acceptance_timing(raw: str, cal: Calendar) -> tuple[date, str, str]:
    """(ET date, bmo|amc|unknown, note) from EDGAR's acceptanceDateTime.
    The digits are read as Eastern wall time (see the module docstring)."""
    m = _ACCEPTED.fullmatch(raw)
    if not m:
        raise VendorParseError(f"sec: unreadable acceptanceDateTime {raw!r}")
    y, mo, d, hh, mi, ss = (int(g) for g in m.groups())
    try:
        wall = datetime(y, mo, d, hh, mi, ss, tzinfo=ET)
    except ValueError as exc:
        raise VendorParseError(f"sec: bad acceptanceDateTime {raw!r}") from exc
    day, t = wall.date(), wall.time()
    if not EDGAR_OPEN <= t < EDGAR_CLOSE:
        return day, "unknown", "outside EDGAR hours (06:00-22:00 ET)"
    if not cal.is_session(day):
        return day, "unknown", "not a session"
    if t < time(9, 30):
        return day, "bmo", ""
    if t >= time(16, 0):
        return day, "amc", ""
    return day, "unknown", "during the session"


# ---------------------------------------------------------- timing file


def load_timing(path: Path) -> dict[str, dict[str, dict[str, str]]]:
    """The timing file ({} when absent); a malformed one raises."""
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise EventsError(f"{path.name}: unreadable ({type(exc).__name__})") from exc
    if not isinstance(doc, dict):
        raise EventsError(f"{path.name}: not an object")
    for name, per_day in doc.items():
        if not isinstance(per_day, dict):
            raise EventsError(f"{path.name}: {name} is not an object")
        for day, e in per_day.items():
            ok = (
                isinstance(e, dict)
                and e.get("timing") in TIMINGS
                and e.get("status") in TIMING_STATUSES
                and isinstance(e.get("source"), str)
            )
            if not ok:
                raise EventsError(f"{path.name}: bad entry {name} {day}")
    return doc


def merge_timing(
    existing: Mapping[str, Mapping[str, Mapping[str, str]]],
    *,
    served: Mapping[str, Mapping[str, tuple[str, str]]],
    evidence_days: Iterable[str],
    confirmed: Mapping[str, Mapping[str, tuple[str, str]]],
    names: Iterable[str],
    fetched_at: str,
) -> dict[str, dict[str, dict[str, str]]]:
    """Fold one run into the timing file. ``served``: day -> {name:
    (timing, code)} for the Nasdaq days that answered; ``evidence_days``:
    those that answered with rows (only they can retract an estimate);
    ``confirmed``: name -> {day: (timing, source)} from EDGAR. An entry
    whose timing/source/status is unchanged keeps its first fetched_at."""
    out = {n: {d: dict(e) for d, e in v.items()} for n, v in existing.items()}
    tracked = set(names)

    def put(name: str, day: str, entry: dict[str, str]) -> None:
        cur = out.setdefault(name, {}).get(day)
        if cur and all(cur.get(k) == entry[k] for k in ("timing", "source", "status")):
            return
        out[name][day] = {**entry, "fetched_at": fetched_at}

    for day, rows in served.items():
        for name, (timing, code) in rows.items():
            cur = out.get(name, {}).get(day)
            if cur and cur.get("status") == "confirmed":
                continue  # never downgraded
            source = f"nasdaq earnings calendar ({code})"
            put(name, day, {"timing": timing, "source": source, "status": "estimated"})
    for day in evidence_days:
        for name in tracked:
            cur = out.get(name, {}).get(day)
            if cur and cur.get("status") == "estimated" and name not in served.get(day, {}):
                del out[name][day]  # the vendor moved this estimate
    for name, per_day in confirmed.items():
        for day, (timing, source) in per_day.items():
            put(name, day, {"timing": timing, "source": source, "status": "confirmed"})
    return {n: dict(sorted(v.items())) for n, v in sorted(out.items()) if v}


def encode_timing(doc: Mapping[str, Any]) -> bytes:
    return (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode("ascii")


# ------------------------------------------------------------- readers


@dataclass(frozen=True)
class EarningsEvent:
    date: date
    timing: str  # bmo | amc | unknown
    status: str  # sealed | confirmed | estimated
    source: str
    blocker_only: bool  # estimated: may block a trade, never trigger PEAD


def _sealed_calendar(paper: Path) -> dict[str, list[str]]:
    path = paper / SEALED_CALENDAR
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        # a missing calendar must not read as "no earnings ahead"
        raise EventsError(f"{SEALED_CALENDAR}: unreadable ({type(exc).__name__})") from exc
    if not isinstance(doc, dict):
        raise EventsError(f"{SEALED_CALENDAR}: not an object")
    return doc


def _upcoming(
    name: str,
    start: date,
    end: date,
    sealed: Mapping[str, list[str]],
    timing: Mapping[str, Mapping[str, Mapping[str, str]]],
) -> list[EarningsEvent]:
    lo, hi = start.isoformat(), end.isoformat()
    per_day = timing.get(name, {})
    out: dict[str, EarningsEvent] = {}
    for d in sealed.get(name, []):
        if lo <= d <= hi:
            e = per_day.get(d)
            src = SEALED_CALENDAR + (f" (sealed); timing: {e['source']}" if e else " (sealed)")
            out[d] = EarningsEvent(
                date.fromisoformat(d), e["timing"] if e else "unknown", "sealed", src, False
            )
    for d, e in per_day.items():
        if lo <= d <= hi and d not in out:
            out[d] = EarningsEvent(
                date.fromisoformat(d),
                e["timing"],
                e["status"],
                e["source"],
                e["status"] != "confirmed",
            )
    return [out[d] for d in sorted(out)]


def upcoming_earnings(
    name: str, from_session: date, to_session: date, *, paper: Path | None = None
) -> list[EarningsEvent]:
    """Report dates for ``name`` in ``[from_session, to_session]``: the
    sealed calendar's (timed from the timing file when known), then any
    timing-file date the sealed calendar lacks (estimated ones are
    blocker-only). No data is an empty list, never a default date."""
    paper = paper or paths.paper_dir()
    sealed = _sealed_calendar(paper)
    timing = load_timing(paper / TIMING_FILE)
    return _upcoming(name, from_session, to_session, sealed, timing)


@dataclass(frozen=True)
class HoldingReports:
    etf: str
    mapped: bool  # the ETF has a holdings map at all
    reports: tuple[tuple[str, EarningsEvent], ...]
    uncovered: tuple[str, ...]  # holdings with no earnings data: the flag is partial


def etf_holding_reports(
    etf: str, start: date, end: date, *, paper: Path | None = None
) -> HoldingReports:
    """Large-holding earnings inside ``[start, end]`` for a mapped ETF."""
    holdings = ETF_HOLDINGS.get(etf)
    if holdings is None:
        return HoldingReports(etf, False, (), ())
    paper = paper or paths.paper_dir()
    sealed = _sealed_calendar(paper)
    timing = load_timing(paper / TIMING_FILE)
    found = [
        (e.date, i, h, e)
        for i, h in enumerate(holdings)
        for e in _upcoming(h, start, end, sealed, timing)
    ]
    uncovered = tuple(h for h in holdings if h not in sealed and h not in timing)
    return HoldingReports(
        etf, True, tuple((h, e) for _d, _i, h, e in sorted(found, key=lambda x: x[:2])), uncovered
    )


# ------------------------------------------------------- update-events


@dataclass(frozen=True)
class EventsResult:
    nasdaq_ok: int
    nasdaq_days: int
    estimated: int
    edgar: str  # skipped | ok | partial | error | tz_suspect
    confirmed: int
    macro: str  # ok | drift | seal_broken | missing | unreadable | unverified
    timing_error: str
    notes: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        """1: a broken seal, drift, an unreadable Fed page, a suspect EDGAR
        clock, an unwritable timing file, or no Nasdaq day answered;
        3: a partial vendor failure (the next weekly run retries); else 0."""
        if (
            self.macro in ("drift", "seal_broken", "missing", "unreadable")
            or self.edgar == "tz_suspect"
            or self.timing_error
            or (self.nasdaq_days and self.nasdaq_ok == 0)
        ):
            return 1
        if (
            self.nasdaq_ok < self.nasdaq_days
            or self.edgar in ("partial", "error")
            or self.macro == "unverified"
        ):
            return 3
        return 0

    def line(self) -> str:
        notes = f" notes={'; '.join(self.notes)}" if self.notes else ""
        return (
            f"update-events nasdaq={self.nasdaq_ok}/{self.nasdaq_days} "
            f"estimated={self.estimated} edgar={self.edgar} confirmed={self.confirmed} "
            f"macro={self.macro} exit={self.exit_code}{notes}"
        )


def _fetch(get: Get, url: str, headers: Mapping[str, str]) -> tuple[bytes | None, str]:
    """(body, "") on HTTP 200, else (None, why)."""
    try:
        status, body = get(url, headers=headers, timeout=TIMEOUT_S)
    except Exception as exc:  # per-request isolation
        return None, type(exc).__name__
    if status != 200:
        return None, f"HTTP {status}"
    return body, ""


def _nasdaq(
    get: Get, sleep: Sleep, days: Sequence[date], names: set[str], notes: list[str]
) -> tuple[dict[str, dict[str, tuple[str, str]]], set[str]]:
    served: dict[str, dict[str, tuple[str, str]]] = {}
    evidence: set[str] = set()
    for k, d in enumerate(days):
        if k:
            sleep(NASDAQ_PACE_S)
        body, why = _fetch(get, NASDAQ_URL.format(day=d.isoformat()), BROWSER_HEADERS)
        try:
            rows = parse_nasdaq(body) if body is not None else None
        except VendorParseError as exc:
            rows, why = None, str(exc)
        if rows is None:
            notes.append(f"nasdaq {d}: {why}")
            continue
        if rows:
            evidence.add(d.isoformat())
        served[d.isoformat()] = {n: v for n, v in rows.items() if n in names}
    return served, evidence


def _edgar(
    get: Get, sleep: Sleep, names: Sequence[str], ua: str, cal: Calendar, notes: list[str]
) -> tuple[str, dict[str, dict[str, tuple[str, str]]]]:
    headers = {"User-Agent": ua}
    body, why = _fetch(get, SEC_TICKERS_URL, headers)
    try:
        ciks = parse_company_tickers(body, names) if body is not None else None
    except VendorParseError as exc:
        ciks, why = None, str(exc)
    if ciks is None:
        notes.append(f"sec tickers: {why}")
        return "error", {}
    missing = [n for n in names if n not in ciks]
    if missing:
        notes.append(f"sec: no CIK for {','.join(missing)}")
    confirmed: dict[str, dict[str, tuple[str, str]]] = {}
    failed = 0
    stamps: list[str] = []
    for name, cik in sorted(ciks.items()):
        sleep(SEC_PACE_S)
        body, why = _fetch(get, SEC_SUBMISSIONS_URL.format(cik=cik), headers)
        try:
            filings, pages = parse_submissions(body) if body is not None else ([], [])
        except VendorParseError as exc:
            body, why = None, str(exc)
        if body is None:
            failed += 1
            notes.append(f"sec {name}: {why}")
            continue
        if len(pages) > SEC_MAX_PAGES:
            notes.append(f"sec {name}: {len(pages) - SEC_MAX_PAGES} older pages skipped")
        for page in pages[:SEC_MAX_PAGES]:
            sleep(SEC_PACE_S)
            pbody, why = _fetch(get, SEC_PAGE_URL.format(name=page), headers)
            try:
                filings += parse_submissions(pbody)[0] if pbody is not None else []
            except VendorParseError as exc:
                pbody, why = None, str(exc)
            if pbody is None:
                failed += 1
                notes.append(f"sec {name} {page}: {why}")
        per_day: dict[str, tuple[str, str]] = {}
        for f in sorted(filings, key=lambda f: f.accepted):
            try:
                day, timing, note = acceptance_timing(f.accepted, cal)
            except VendorParseError as exc:
                notes.append(f"sec {name}: {exc}")
                continue
            stamps.append(note)
            if day.isoformat() in per_day:
                continue  # the day's first 2.02 filing times it
            src = f"sec-edgar 8-K 2.02 {f.accession} accepted {f.accepted} (read as ET)"
            per_day[day.isoformat()] = (timing, src + (f"; {note}" if note else ""))
        if per_day:
            confirmed[name] = per_day
    outside = sum(1 for s in stamps if s.startswith("outside"))
    if len(stamps) >= 5 and outside / len(stamps) > TZ_SUSPECT_SHARE:
        notes.append(f"sec: {outside}/{len(stamps)} acceptance times outside EDGAR hours")
        return "tz_suspect", {}
    return ("ok" if failed == 0 else "partial"), confirmed


def _macro_check(
    get: Get, events_dir: Path, state: Path, now: datetime, macro_file: str, dry_run: bool
) -> tuple[str, str]:
    path = events_dir / macro_file
    if not path.exists():
        return "missing", f"{macro_file} not found"
    try:
        doc = load_macro(path)
    except EventsError as exc:
        return "seal_broken", str(exc)
    body, why = _fetch(get, FOMC_URL, FED_HEADERS)
    if body is None:
        return "unverified", f"fed page: {why}"
    try:
        meetings = parse_fomc(body.decode("utf-8", "replace"))
    except FomcParseError as exc:
        return "unreadable", f"fed page: {exc}"
    lo, hi = doc["range"]["from"], doc["range"]["to"]

    def key(f: Mapping[str, Any]) -> tuple[str, str, bool, str]:
        return (f["start"], f["date"], bool(f["sep"]), f["kind"])

    live = {
        (m.start.isoformat(), m.end.isoformat(), m.sep, m.kind)
        for m in meetings
        if lo <= m.end.isoformat() <= hi
    }
    sealed = {key(f) for f in doc["fomc"]}
    if live == sealed:
        return "ok", ""
    if not dry_run:

        def rows(keys: set[tuple[str, str, bool, str]]) -> list[dict[str, Any]]:
            return [{"start": s, "date": d, "sep": sep, "kind": k} for s, d, sep, k in sorted(keys)]

        drift = {
            "checked_at": now.isoformat(),
            "file": macro_file,
            "url": FOMC_URL,
            "added": rows(live - sealed),
            "removed": rows(sealed - live),
        }
        atomic_write_bytes(
            state / "events" / "macro-drift.json",
            (json.dumps(drift, indent=1, sort_keys=True) + "\n").encode("ascii"),
        )
    return "drift", "fed page differs from the sealed FOMC dates: reseal after review"


def update_events(
    *,
    get: Get,
    clock: Clock,
    sleep: Sleep,
    cal: Calendar,
    paper: Path,
    events_dir: Path,
    state: Path,
    sec_ua: str | None,
    horizon: int = HORIZON,
    names: Sequence[str] | None = None,
    dry_run: bool = False,
    macro_file: str = MACRO_FILE,
) -> EventsResult:
    """The weekly refresh: Nasdaq estimates, EDGAR confirmations (only
    with ``sec_ua``), the timing-file merge, and the macro seal check."""
    now: datetime = clock()
    tracked = tuple(names or EARNINGS_NAMES)
    notes: list[str] = []
    sessions = cal.sessions()
    i = bisect.bisect_right(sessions, now.astimezone(ET).date())
    days = list(sessions[i : i + horizon])
    served, evidence = _nasdaq(get, sleep, days, set(tracked), notes)
    if sec_ua:
        edgar, confirmed = _edgar(get, sleep, tracked, sec_ua, cal, notes)
    else:
        edgar, confirmed = "skipped", {}
        notes.append("sec_ua_missing")
    timing_error = ""
    path = paper / TIMING_FILE
    try:
        merged = merge_timing(
            load_timing(path),
            served=served,
            evidence_days=evidence,
            confirmed=confirmed,
            names=tracked,
            fetched_at=now.isoformat(),
        )
        data = encode_timing(merged)
        if not dry_run and (not path.exists() or path.read_bytes() != data):
            atomic_write_bytes(path, data)
    except (EventsError, OSError) as exc:
        timing_error = f"{TIMING_FILE}: {exc}"
        notes.append(timing_error)
    macro, why = _macro_check(get, events_dir, state, now, macro_file, dry_run)
    if why:
        notes.append(f"macro: {why}")
    return EventsResult(
        nasdaq_ok=len(served),
        nasdaq_days=len(days),
        estimated=sum(len(v) for v in served.values()),
        edgar=edgar,
        confirmed=sum(len(v) for v in confirmed.values()),
        macro=macro,
        timing_error=timing_error,
        notes=tuple(notes),
    )
