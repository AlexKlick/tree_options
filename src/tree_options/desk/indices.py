"""D1 indices: the free CBOE index-history CSVs and FRED DTB3, daily.

Sources (each probed 2026-09-23 with one small ranged GET; all 14 CBOE
files exist, HTTP 206)::

    https://cdn.cboe.com/api/global/us_indices/daily_prices/{X}_History.csv
        X in VIX VIX9D VIX1D VIX3M VIX6M VIX1Y VVIX SKEW VXN RVX GVZ VXAPL
        VXAZN VXGOG. Header "DATE,OPEN,HIGH,LOW,CLOSE" (MM/DD/YYYY dates,
        six-decimal values), except VVIX, SKEW and GVZ, which serve
        "DATE,<X>" (one value per day). Last-Modified 2026-09-23 01:51 UTC
        (21:51 ET) carried the 09-22 row (VIX3M: 22:01 UTC): the files
        update late in the evening, so the timer runs the next morning
        (06:40 ET); an evening run sees the prior session (``lagging``).
    https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3
        "observation_date,DTB3", ISO dates, an EMPTY value on days without
        a rate (800 of 18972 rows, e.g. Labor Day 2026-09-07). The 09-22
        row was published 2026-09-23 20:16 UTC: FRED trails one session.

Store (under the desk store root, ``DESK_STORE``)::

    indices/<X>.csv              "date,open,high,low,close": ISO dates, the
                                 vendor's value strings verbatim; a one-value
                                 series fills close only (open/high/low "")
    indices/<X>.<D>[-n].bak      the prior file, kept whenever a vendor
                                 revision replaces it (D = fetch date, ET)
    indices/provenance.jsonl     append-only: one line per source per run
                                 (fetched_at, sha256, rows, last_date, ...)
    indices/changes.jsonl        append-only: every revised or dropped row
    indices/gaps.jsonl           append-only: every source a run could not
                                 refresh (missing/invalid/error + why)

Every request is time-bounded (TIMEOUT_S) and isolated per source: a
transport failure (timeout, 5xx) is a soft ``error`` gap, exit 3 (the next
run re-fetches the whole history); a vanished or bad file is exit 1.

A payload that only appends rows is ``updated``; one that changes past
values is ``revised`` (backup and change log first, then the new file).
One that drops ANY stored date, or carries a row dated after the fetch's ET
date, is ``invalid`` and the store is kept whole (a truncated or wrong
body; an intentional vendor deletion needs the operator: move the stored
file aside and the next run records the history as ``new``). Identical
content is ``unchanged`` and nothing is rewritten. Values are checked to be
finite decimals but never converted: no float round trip.

The morning timer slot passes ``skip_current``: a source whose stored
history already reaches the latest completed session (within its
``max_lag``) is ``current`` and not fetched. On the day after an exchange
holiday nothing new can exist, so the slot makes no request (sessions come
from the NYSE calendar; no date arithmetic).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tree_options.desk.http import CDN_HEADERS, TOOL_UA, Get
from tree_options.desk.sessions import Calendar, latest_completed_session, sessions_after
from tree_options.desk.store import atomic_create_bytes, atomic_write_bytes
from tree_options.trex.clock import ET

CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{sym}_History.csv"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
CBOE_NAMES: tuple[str, ...] = (
    "VIX",
    "VIX9D",
    "VIX1D",
    "VIX3M",
    "VIX6M",
    "VIX1Y",
    "VVIX",
    "SKEW",
    "VXN",
    "RVX",
    "GVZ",
    "VXAPL",
    "VXAZN",
    "VXGOG",
)
STORE_HEADER = "date,open,high,low,close"
MAX_BYTES = 8_000_000  # the longest file (VIX, from 1990) was 472,921 bytes
PACE_S = 1.0
TIMEOUT_S = 30.0

RECORDED = frozenset({"new", "updated", "revised", "unchanged", "current"})
HARD_FAILURES = frozenset({"missing", "invalid"})  # "error" (transport) is transient
STATUSES = (
    "new",
    "updated",
    "revised",
    "unchanged",
    "current",
    "missing",
    "invalid",
    "error",
)

Row = tuple[str, str, str, str, str]
Clock = Callable[[], datetime]
Sleep = Callable[[float], None]


@dataclass(frozen=True)
class Source:
    name: str  # the store file stem
    kind: str  # "cboe" | "fred"
    url: str
    max_lag: int  # sessions the newest row may trail the latest completed session

    @property
    def headers(self) -> Mapping[str, str]:
        return CDN_HEADERS if self.kind == "cboe" else FRED_HEADERS


# FRED's edge stalls a request that carries no Accept header until the
# client times out (2 of 2 urllib probes on 2026-09-23, ~25 s each) and
# answers the same request with "Accept: */*" in about 0.5 s (2 of 2). It
# also reset a browser UA over HTTP/2, so: a plain tool UA plus Accept.
FRED_HEADERS: Mapping[str, str] = {"User-Agent": TOOL_UA, "Accept": "*/*"}


SOURCES: tuple[Source, ...] = (
    *(Source(x, "cboe", CBOE_URL.format(sym=x), 0) for x in CBOE_NAMES),
    Source("DTB3", "fred", FRED_URL.format(series="DTB3"), 1),
)


class IndexParseError(ValueError):
    """The body is not a usable history for the requested series."""


# ------------------------------------------------------------------ parsing


def _value(raw: str, kind: str, where: str) -> str:
    v = raw.strip()
    if kind == "fred" and v in ("", "."):
        return ""  # FRED's no-observation marker: kept empty, never filled
    try:
        ok = Decimal(v).is_finite()
    except InvalidOperation:
        ok = False
    if not ok:
        raise IndexParseError(f"{where}: value {raw!r} is not a finite decimal")
    return v


def _date(raw: str, kind: str, where: str) -> str:
    try:
        if kind == "cboe":
            return datetime.strptime(raw.strip(), "%m/%d/%Y").date().isoformat()
        return date.fromisoformat(raw.strip()).isoformat()
    except ValueError as exc:
        raise IndexParseError(f"{where}: bad date {raw!r}") from exc


def _width(header: list[str], kind: str, name: str) -> int:
    if kind == "cboe" and header == ["DATE", "OPEN", "HIGH", "LOW", "CLOSE"]:
        return 5
    if kind == "cboe" and header == ["DATE", name]:
        return 2
    if kind == "fred" and header in (["observation_date", name], ["DATE", name]):
        return 2
    raise IndexParseError(f"{name}: unexpected header {','.join(header)!r}")


def parse_csv(raw: bytes, kind: str, name: str) -> list[Row]:
    """The vendor CSV as normalized rows (ISO date, open, high, low, close),
    values verbatim. Refuses anything it cannot read whole: an empty or
    header-only body, a foreign header, a bad cell, unsorted or repeated
    dates. Never returns an empty history."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IndexParseError(f"{name}: body is not UTF-8") from exc
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise IndexParseError(f"{name}: empty body")
    width = _width([h.strip() for h in lines[0].split(",")], kind, name)
    rows: list[Row] = []
    for lineno, line in enumerate(lines[1:], start=2):
        where = f"{name} line {lineno}"
        cells = line.split(",")
        if len(cells) != width:
            raise IndexParseError(f"{where}: {len(cells)} fields, expected {width}")
        d = _date(cells[0], kind, where)
        vals = [_value(c, kind, where) for c in cells[1:]]
        if width == 5:
            rows.append((d, vals[0], vals[1], vals[2], vals[3]))
        else:
            rows.append((d, "", "", "", vals[0]))
    if not rows:
        raise IndexParseError(f"{name}: no rows")
    for a, b in itertools.pairwise(rows):
        if b[0] <= a[0]:
            raise IndexParseError(f"{name}: dates not strictly increasing at {b[0]}")
    return rows


def render(rows: Iterable[Row]) -> str:
    return STORE_HEADER + "\n" + "".join(",".join(r) + "\n" for r in rows)


def read_store(path: Path) -> list[Row]:
    """A stored ``<X>.csv`` back into rows (our own format, strictly)."""
    lines = path.read_text().splitlines()
    if not lines or lines[0] != STORE_HEADER:
        raise ValueError(f"{path.name}: not a desk index file")
    rows: list[Row] = []
    for line in lines[1:]:
        cells = line.split(",")
        if len(cells) != 5:
            raise ValueError(f"{path.name}: malformed row {line!r}")
        rows.append((cells[0], cells[1], cells[2], cells[3], cells[4]))
    return rows


@dataclass(frozen=True)
class Diff:
    added: list[Row]
    changed: list[tuple[Row, Row]]
    dropped: list[Row]


def diff_rows(old: list[Row], new: list[Row]) -> Diff:
    old_by = {r[0]: r for r in old}
    new_by = {r[0]: r for r in new}
    return Diff(
        added=[r for r in new if r[0] not in old_by],
        changed=[(old_by[r[0]], r) for r in new if r[0] in old_by and old_by[r[0]] != r],
        dropped=[r for r in old if r[0] not in new_by],
    )


# -------------------------------------------------------------------- store


@dataclass(frozen=True)
class SourceResult:
    status: str
    rows: int
    last_date: str | None
    lagging: bool
    detail: str
    sha256: str | None


@dataclass(frozen=True)
class IndicesSummary:
    session: date  # the latest completed session when the run started
    results: dict[str, SourceResult]

    def counts(self) -> dict[str, int]:
        c = Counter(r.status for r in self.results.values())
        return {s: c.get(s, 0) for s in STATUSES}

    def line(self, rc: int) -> str:
        counts = " ".join(f"{k}={v}" for k, v in self.counts().items())
        lagging = sum(1 for r in self.results.values() if r.lagging)
        return (
            f"record-indices session={self.session.isoformat()} "
            f"sources={len(self.results)} {counts} lagging={lagging} exit={rc}"
        )


def exit_code(summary: IndicesSummary) -> int:
    """1 for an empty invocation or any hard failure (a vendor file gone or
    bad: missing/invalid); then 3 when anything is transient (a transport
    ``error``, even every source: each file carries the full history, so
    the next run closes the gap) or a vendor has not published the latest
    session yet; else 0."""
    results = summary.results.values()
    if not summary.results or any(r.status in HARD_FAILURES for r in results):
        return 1
    if any(r.status == "error" or r.lagging for r in results):
        return 3
    return 0


def _append_jsonl(path: Path, lines: Iterable[dict[str, Any]]) -> None:
    text = "".join(json.dumps(x, sort_keys=True) + "\n" for x in lines)
    if not text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def _backup(path: Path, name: str, day: date) -> str:
    """Keep the prior file under a name no later backup can overwrite."""
    data = path.read_bytes()
    for n in itertools.count(1):
        stem = f"{name}.{day.isoformat()}" + ("" if n == 1 else f"-{n}")
        if atomic_create_bytes(path.with_name(f"{stem}.bak"), data):
            return f"{stem}.bak"
    raise AssertionError("unreachable")


def _lag(last: str, expected: date, cal: Calendar) -> int:
    d = date.fromisoformat(last)
    return len(sessions_after(d, expected, cal)) if d < expected else 0


def record_source(
    src: Source,
    *,
    root: Path,
    get: Get,
    clock: Clock,
    expected: date,
    cal: Calendar,
    dry_run: bool = False,
) -> SourceResult:
    res = _record_source(
        src, root=root, get=get, clock=clock, expected=expected, cal=cal, dry_run=dry_run
    )
    if not dry_run and res.status not in RECORDED:
        _append_jsonl(  # the gap: this run left the source's history unrefreshed
            root / "indices" / "gaps.jsonl",
            [
                {
                    "at": clock().isoformat(),
                    "source": src.name,
                    "status": res.status,
                    "detail": res.detail,
                }
            ],
        )
    if not dry_run:
        _append_jsonl(
            root / "indices" / "provenance.jsonl",
            [
                {
                    "fetched_at": clock().isoformat(),
                    "source": src.name,
                    "url": src.url,
                    "status": res.status,
                    "sha256": res.sha256,
                    "rows": res.rows,
                    "last_date": res.last_date,
                    "lagging": res.lagging,
                    "detail": res.detail,
                }
            ],
        )
    return res


def _record_source(
    src: Source,
    *,
    root: Path,
    get: Get,
    clock: Clock,
    expected: date,
    cal: Calendar,
    dry_run: bool,
) -> SourceResult:
    try:
        status, body = get(src.url, headers=src.headers, timeout=TIMEOUT_S)
    except Exception as exc:  # per-source isolation
        return SourceResult("error", 0, None, False, f"{type(exc).__name__}: {exc}", None)
    if status == 404:
        return SourceResult("missing", 0, None, False, "HTTP 404: the vendor serves no file", None)
    if status != 200:
        return SourceResult("error", 0, None, False, f"HTTP {status}", None)
    sha = hashlib.sha256(body).hexdigest()
    if len(body) > MAX_BYTES:
        return SourceResult("invalid", 0, None, False, f"body over {MAX_BYTES} bytes", sha)
    try:
        rows = parse_csv(body, src.kind, src.name)
    except IndexParseError as exc:
        return SourceResult("invalid", 0, None, False, str(exc), sha)
    last = rows[-1][0]
    fetch_day = clock().astimezone(ET).date().isoformat()
    if last > fetch_day:  # rows are strictly increasing: the last is the latest
        return SourceResult(
            "invalid", 0, None, False, f"future-dated row {last} (fetched {fetch_day})", sha
        )
    lagging = _lag(last, expected, cal) > src.max_lag

    def result(status: str, detail: str = "") -> SourceResult:
        return SourceResult(status, len(rows), last, lagging, detail, sha)

    path = root / "indices" / f"{src.name}.csv"
    if not path.exists():
        if not dry_run:
            atomic_write_bytes(path, render(rows).encode("ascii"))
        return result("new")
    try:
        old = read_store(path)
    except (OSError, ValueError) as exc:
        return SourceResult(
            "error",
            0,
            None,
            False,
            f"stored file unreadable ({type(exc).__name__}); left as is",
            sha,
        )
    if old and last < old[-1][0]:
        return result("invalid", f"shrunk: history ends {last}, before the stored {old[-1][0]}")
    d = diff_rows(old, rows)
    if d.dropped:  # never replace a history with one missing stored dates
        shown = ", ".join(r[0] for r in d.dropped[:3]) + (" ..." if len(d.dropped) > 3 else "")
        return result("invalid", f"shrunk: {len(d.dropped)} stored dates missing ({shown})")
    if not (d.added or d.changed):
        return result("unchanged")
    if not d.changed:
        if not dry_run:
            atomic_write_bytes(path, render(rows).encode("ascii"))
        return result("updated", f"{len(d.added)} added")
    now = clock()
    bak = "(dry run)"
    if not dry_run:
        bak = _backup(path, src.name, now.astimezone(ET).date())
        _append_jsonl(
            root / "indices" / "changes.jsonl",
            [
                {
                    "at": now.isoformat(),
                    "source": src.name,
                    "date": o[0],
                    "old": list(o[1:]),
                    "new": list(n[1:]),
                    "backup": bak,
                    "sha256": sha,
                }
                for o, n in d.changed
            ],
        )
        atomic_write_bytes(path, render(rows).encode("ascii"))
    return result("revised", f"{len(d.changed)} revised, {len(d.added)} added; prior kept as {bak}")


def record_indices(
    sources: Iterable[Source],
    *,
    root: Path,
    get: Get,
    clock: Clock,
    sleep: Sleep,
    cal: Calendar,
    dry_run: bool = False,
    pace_s: float = PACE_S,
    skip_current: bool = False,
) -> IndicesSummary:
    """Fetch and store every source (per-source isolation, paced). With
    ``skip_current``, a source already stored through the latest completed
    session (within its max_lag) is ``current`` and makes no request."""
    expected = latest_completed_session(clock(), cal)
    results: dict[str, SourceResult] = {}
    fetched = 0
    for src in sources:
        held = _stored_current(src, root, expected, cal) if skip_current else None
        if held is not None:
            results[src.name] = held
            continue
        if fetched:
            sleep(pace_s)
        fetched += 1
        results[src.name] = record_source(
            src, root=root, get=get, clock=clock, expected=expected, cal=cal, dry_run=dry_run
        )
    return IndicesSummary(expected, results)


def _stored_current(src: Source, root: Path, expected: date, cal: Calendar) -> SourceResult | None:
    """A ``current`` result when the stored history is as fresh as the
    vendor can be (lag within max_lag); None when a fetch is due."""
    try:
        rows = read_store(root / "indices" / f"{src.name}.csv")
    except (OSError, ValueError):
        return None
    if not rows or _lag(rows[-1][0], expected, cal) > src.max_lag:
        return None
    return SourceResult("current", len(rows), rows[-1][0], False, "stored; no request", None)
