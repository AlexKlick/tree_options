"""Ex-dividend dates for the universe (the ex-dividend rail's input).

Source: Polygon ``/v3/reference/dividends`` through
:class:`tree_options.data.massive_client.MassiveClient` (the key is read by
path or env-var NAME inside the client and never logged or stored). One
live request on 2026-09-23 (AAPL) confirmed the Starter plan serves it
(status OK; cash_amount, declaration/ex/pay/record dates, frequency,
dividend_type).

Store: ``DESK_STORE/dividends/<D>/<SYM>.json``, one snapshot per symbol and
session, NEVER rewritten: a re-run for the same session skips the symbol
before any request (idempotent), and a snapshot is published with a hard
link from a private temp file, which fails if the name exists, so of two
concurrent recorders the first to publish wins and the other reports
"exists". A snapshot holds every record with an ex-date on or after
:func:`history_start` (the first of the month two years back), money as
strings. A payer with no records in that window is recorded with an empty
list (a known non-payer), which is different from no snapshot at all.
Failures are reported as fixed codes (``not_entitled``, ``auth``,
``vendor_error``, ``transport``, ``rate_limited``, ``pagination``): the
client keeps vendor text in its exceptions, and that text may echo the key.

Reading (:func:`ex_dividends_for`), as of a session:

* the newest snapshot dated on or before it, if at most
  :data:`MAX_AGE_SESSIONS` sessions old and its document is what its path
  says (schema, source, symbol, session, and an aware ``fetched_at`` not
  before that session); else None (unavailable: the rail fails closed for
  structures with a short call);
* DECLARED ex-dates: every record whose declaration date is on or before
  as-of (a missing declaration date counts as known; it can only block),
  special dividends included, each a one-day interval;
* PROJECTED ex-dates: REGULAR records are every type except the special
  and capital-gain ones (SC, LT, ST), an unknown or missing type included.
  From the latest regular record, whose schedule must be known (frequency
  1, 2, 4 or 12 a year: every 365, 182, 91 or 30 days; missing, 0 or any
  other value makes the whole answer None, fail closed), the k-th next
  ex-date is expected at last + k x period and carries the uncertainty
  interval +/- :data:`PROJECTION_SLACK_DAYS` days; the rail blocks every
  hold that overlaps the interval. Only a later regular payment moves the
  schedule on: a special dividend never suppresses a projection.

Calendar-day steps are epoch arithmetic (the repo bans timedelta outside
``time/``).
"""

from __future__ import annotations

import bisect
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from tree_options.data.massive_client import (
    MassiveApiError,
    MassiveAuthError,
    MassiveAuthRejectedError,
    MassiveClient,
    MassiveError,
    MassiveNotEntitledError,
    MassivePaginationError,
    MassiveRateLimitError,
    MassiveTransportError,
)
from tree_options.desk import paths
from tree_options.desk.rails import ExDividend
from tree_options.trex.clock import ET

ENDPOINT = "/v3/reference/dividends"
SOURCE = "polygon /v3/reference/dividends"
SCHEMA = "desk.dividends/1"
HISTORY_YEARS = 2
MAX_AGE_SESSIONS = 5
PROJECTION_SLACK_DAYS = 7
# Polygon ``frequency`` (payments a year) -> days between ex-dates
PERIOD_DAYS: dict[int, int] = {1: 365, 2: 182, 4: 91, 12: 30}
# special cash and capital-gain distributions: never a schedule
IRREGULAR_TYPES = frozenset({"SC", "LT", "ST"})
PAGE_LIMIT = 1000
# fixed failure codes (vendor text may echo the key: never reported)
_FAILURE_CODES: tuple[tuple[type[MassiveError], str, bool], ...] = (
    (MassiveNotEntitledError, "not_entitled", True),
    (MassiveAuthRejectedError, "auth", True),
    (MassiveAuthError, "auth", True),
    (MassiveRateLimitError, "rate_limited", False),
    (MassiveTransportError, "transport", False),
    (MassivePaginationError, "pagination", False),
    (MassiveApiError, "vendor_error", False),
)
_DAY_S = 86_400
_FIELDS = ("declaration_date", "ex_dividend_date", "pay_date", "record_date")

Clock = Callable[[], datetime]


class SessionCalendar(Protocol):
    def sessions(self) -> tuple[date, ...]: ...


def dividends_dir(store: Path | None = None) -> Path:
    return (store or paths.store_root()) / "dividends"


def history_start(session: date) -> date:
    return date(session.year - HISTORY_YEARS, session.month, 1)


def _plus_days(d: date, days: int) -> date:
    ts = datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() + days * _DAY_S
    return datetime.fromtimestamp(ts, UTC).date()


def _money_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):  # never through binary: the literal's text
        return str(Decimal(repr(value)))
    return str(Decimal(str(value)))


def normalize_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    """One vendor record as stored: ISO date strings, money as a string,
    frequency an int (None when absent)."""
    freq = raw.get("frequency")
    out: dict[str, Any] = {k: (str(raw[k]) if raw.get(k) else None) for k in _FIELDS}
    out["cash_amount"] = _money_str(raw.get("cash_amount"))
    out["currency"] = raw.get("currency")
    out["dividend_type"] = raw.get("dividend_type")
    out["frequency"] = int(freq) if isinstance(freq, int | Decimal) else None
    return out


# ------------------------------------------------------------------ record


@dataclass(frozen=True)
class SymbolResult:
    status: str  # ok | exists | dry-run | failed
    records: int = 0
    detail: str = ""
    hard: bool = False  # a refusal a retry can't cure (entitlement, key)


@dataclass(frozen=True)
class DividendRun:
    session: date
    results: dict[str, SymbolResult]

    @property
    def exit_code(self) -> int:
        """0 every symbol stored (or already stored); 1 a hard refusal
        (entitlement or key) or nothing stored; 3 a retryable gap."""
        failed = [r for r in self.results.values() if r.status == "failed"]
        if not failed:
            return 0
        if any(r.hard for r in failed) or len(failed) == len(self.results):
            return 1
        return 3

    def line(self) -> str:
        counts: dict[str, int] = {}
        for r in self.results.values():
            counts[r.status] = counts.get(r.status, 0) + 1
        parts = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        return f"record-dividends {self.session} {parts} rc={self.exit_code}"


def _publish_once(path: Path, doc: Mapping[str, Any]) -> bool:
    """Write ``doc`` to a private temp file and hard-link it to ``path``:
    atomic, and never replacing: False when ``path`` already exists (a
    concurrent recorder published first; its snapshot stands)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(doc, indent=1, sort_keys=True) + "\n")
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        tmp.unlink(missing_ok=True)


def _failure(exc: MassiveError) -> SymbolResult:
    """A fixed code per failure class: never the exception's text."""
    for cls, code, hard in _FAILURE_CODES:
        if isinstance(exc, cls):
            return SymbolResult("failed", detail=code, hard=hard)
    return SymbolResult("failed", detail="vendor_error")


def record_dividends(
    session: date,
    symbols: Iterable[str],
    *,
    client: MassiveClient,
    clock: Clock,
    store: Path | None = None,
    dry_run: bool = False,
) -> DividendRun:
    """Fetch and store each symbol's dividend history for ``session``;
    a symbol already stored for that session is skipped with no request."""
    out_dir = dividends_dir(store) / session.isoformat()
    since = history_start(session)
    results: dict[str, SymbolResult] = {}
    for sym in symbols:
        target = out_dir / f"{sym}.json"
        if target.exists():
            results[sym] = SymbolResult("exists")
            continue
        try:
            page = client.paginate(
                ENDPOINT,
                {
                    "ticker": sym,
                    "ex_dividend_date.gte": since.isoformat(),
                    "limit": PAGE_LIMIT,
                    "sort": "ex_dividend_date",
                    "order": "asc",
                },
                use_cache=False,
            )
        except MassiveError as exc:
            results[sym] = _failure(exc)
            continue
        records = sorted(
            (normalize_record(r) for r in page.results if r.get("ticker") == sym),
            key=lambda r: (r["ex_dividend_date"] or "", r["cash_amount"] or ""),
        )
        if dry_run:
            results[sym] = SymbolResult("dry-run", len(records))
            continue
        published = _publish_once(
            target,
            {
                "schema": SCHEMA,
                "symbol": sym,
                "session": session.isoformat(),
                "fetched_at": clock().isoformat(),
                "source": SOURCE,
                "since": since.isoformat(),
                "request_ids": list(page.request_ids),
                "records": records,
            },
        )
        results[sym] = SymbolResult("ok", len(records)) if published else SymbolResult("exists")
    return DividendRun(session, results)


# ------------------------------------------------------------------ read


@dataclass(frozen=True)
class DividendRecord:
    ex_date: date
    declared: date | None
    cash_amount: Decimal | None
    frequency: int | None
    dividend_type: str | None


@dataclass(frozen=True)
class DividendSnapshot:
    symbol: str
    session: date
    records: tuple[DividendRecord, ...]


def _date_or_none(raw: object) -> date | None:
    return date.fromisoformat(str(raw)) if raw else None


def snapshot_from_doc(doc: Mapping[str, Any]) -> DividendSnapshot:
    if doc.get("schema") != SCHEMA:
        raise ValueError(f"not a {SCHEMA} document")
    if doc.get("source") != SOURCE:
        raise ValueError(f"source {doc.get('source')!r} is not {SOURCE!r}")
    records = []
    for r in doc["records"]:
        ex = _date_or_none(r.get("ex_dividend_date"))
        if ex is None:
            raise ValueError("record without an ex-dividend date")
        amt = r.get("cash_amount")
        records.append(
            DividendRecord(
                ex_date=ex,
                declared=_date_or_none(r.get("declaration_date")),
                cash_amount=Decimal(amt) if amt is not None else None,
                frequency=r.get("frequency"),
                dividend_type=r.get("dividend_type"),
            )
        )
    session = date.fromisoformat(str(doc["session"]))
    fetched = datetime.fromisoformat(str(doc["fetched_at"]))
    if fetched.tzinfo is None:
        raise ValueError("fetched_at carries no zone")
    if fetched.astimezone(ET).date() < session:
        raise ValueError(f"fetched {fetched.isoformat()} before its session {session}")
    return DividendSnapshot(
        symbol=str(doc["symbol"]),
        session=session,
        records=tuple(sorted(records, key=lambda r: r.ex_date)),
    )


def load_snapshot(
    symbol: str,
    as_of: date,
    cal: SessionCalendar,
    *,
    store: Path | None = None,
    max_age_sessions: int = MAX_AGE_SESSIONS,
) -> DividendSnapshot | None:
    """The newest readable snapshot of ``symbol`` dated on or before
    ``as_of`` and at most ``max_age_sessions`` sessions older; else None."""
    root = dividends_dir(store)
    if not root.is_dir():
        return None
    sessions = cal.sessions()
    ref = bisect.bisect_right(sessions, as_of) - 1
    if ref < 0:
        return None
    dated: list[date] = []
    for d in root.iterdir():
        try:
            day = date.fromisoformat(d.name)
        except ValueError:
            continue
        if day <= as_of and (d / f"{symbol}.json").is_file():
            dated.append(day)
    for day in sorted(dated, reverse=True):
        i = bisect.bisect_left(sessions, day)
        if i >= len(sessions) or sessions[i] != day or ref - i > max_age_sessions:
            return None
        try:
            doc = json.loads((root / day.isoformat() / f"{symbol}.json").read_text())
            snap = snapshot_from_doc(doc)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
            return None
        # the document must be what its path says: no other symbol's
        # history, no older snapshot copied into a newer directory
        if snap.symbol != symbol or snap.session != day:
            return None
        return snap
    return None


def ex_dividends(
    snapshot: DividendSnapshot, *, as_of: date, start: date, end: date
) -> tuple[ExDividend, ...] | None:
    """Declared ex-dates in ``[start, end]`` and projected ex-dates whose
    uncertainty interval overlaps it, known as of ``as_of``; None when the
    latest regular record's schedule is unknown."""
    known = [r for r in snapshot.records if r.declared is None or r.declared <= as_of]
    out = [
        ExDividend(
            ex_date=r.ex_date,
            status="declared",
            earliest=r.ex_date,
            latest=r.ex_date,
            cash_amount=r.cash_amount,
            detail=f"{r.dividend_type} {r.cash_amount}",
        )
        for r in known
        if start <= r.ex_date <= end
    ]
    regular = [r for r in known if r.dividend_type not in IRREGULAR_TYPES]
    if regular:
        last = max(regular, key=lambda r: r.ex_date)
        period = PERIOD_DAYS.get(last.frequency) if isinstance(last.frequency, int) else None
        if period is None:
            return None  # a regular payer with an unknown schedule: fail closed
        k = 1
        while True:
            expected = _plus_days(last.ex_date, k * period)
            earliest = _plus_days(expected, -PROJECTION_SLACK_DAYS)
            latest = _plus_days(expected, PROJECTION_SLACK_DAYS)
            if earliest > end:
                break
            if latest >= start:
                out.append(
                    ExDividend(
                        ex_date=expected,
                        status="projected",
                        earliest=earliest,
                        latest=latest,
                        cash_amount=last.cash_amount,
                        detail=f"{last.ex_date} + {k} x {period}d +/- {PROJECTION_SLACK_DAYS}d",
                    )
                )
            k += 1
    return tuple(sorted(out, key=lambda x: (x.ex_date, x.status)))


def ex_dividends_for(
    symbol: str,
    start: date,
    end: date,
    cal: SessionCalendar,
    *,
    as_of: date,
    store: Path | None = None,
) -> tuple[ExDividend, ...] | None:
    """The rail's ``Candidate.ex_dividends`` for a hold ``[start, end]``;
    None when no fresh snapshot exists (unavailable, fail closed)."""
    snap = load_snapshot(symbol, as_of, cal, store=store)
    if snap is None:
        return None
    return ex_dividends(snap, as_of=as_of, start=start, end=end)
