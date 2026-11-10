"""Free daily history for AI-BEAR-001: FRED series and Yahoo chart closes.

Store (under the desk store root, ``DESK_STORE``)::

    history/<NAME>.csv          "date,close": ISO dates, one close per session
    history/provenance.jsonl    append-only: one line per source per fetch
                                (fetched_at, url, sha256 of the raw body, rows,
                                first/last date, status)

Sources:

* FRED ``fredgraph.csv?id=<SERIES>``: NASDAQ100 (-> NDX), VXNCLS (-> VXN).
  "." or empty marks a day without a value, and that row is skipped. FRED
  needs ``Accept: */*`` and HTTP/1.1 (see ``desk/indices.py``); urllib uses
  HTTP/1.1.
* Yahoo ``v8/finance/chart/<SYM>``: split- and dividend-adjusted closes
  (``adjclose``; the plain close for an index). Each timestamp is shifted by
  the exchange ``gmtoffset`` before taking its date. Null closes (halts,
  vendor holes) are skipped.

A fetched body is parsed as data only. A failed or implausible fetch never
replaces a stored file (fewer than 250 rows, non-increasing dates, or a
non-positive close), and the failure is written to provenance. Rows dated on
or after the fetch's ET date are dropped (``dropped_in_progress``), because
the session may still be trading; a next-morning fetch records its close.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from tree_options.desk.http import TOOL_UA, Get, urllib_get
from tree_options.desk.paths import store_root
from tree_options.desk.store import atomic_write_bytes
from tree_options.time.sessions import shift_instant
from tree_options.trex.clock import ET

FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
YAHOO_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    "?period1={p1}&period2={p2}&interval=1d&events=div%2Csplit"
)
YAHOO_PERIOD1 = 631152000  # 1990-01-01T00:00:00Z
FRED_HEADERS: Mapping[str, str] = {"User-Agent": TOOL_UA, "Accept": "*/*"}
YAHOO_HEADERS: Mapping[str, str] = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
    "Accept": "*/*",
}
MIN_ROWS = 250
PACE_S = 1.0
TIMEOUT_S = 60.0

Point = tuple[date, float]


@dataclass(frozen=True)
class HistorySource:
    name: str  # store file stem
    kind: str  # "fred" | "yahoo"
    ref: str  # FRED series id or Yahoo symbol

    def url(self, now: datetime) -> str:
        if self.kind == "fred":
            return FRED_URL.format(series=self.ref)
        return YAHOO_URL.format(
            sym=self.ref.replace("^", "%5E"), p1=YAHOO_PERIOD1, p2=int(now.timestamp())
        )

    @property
    def headers(self) -> Mapping[str, str]:
        return FRED_HEADERS if self.kind == "fred" else YAHOO_HEADERS


SOURCES: tuple[HistorySource, ...] = (
    HistorySource("NDX", "fred", "NASDAQ100"),
    HistorySource("VXN", "fred", "VXNCLS"),
    HistorySource("SOX", "yahoo", "^SOX"),
    *(
        HistorySource(s, "yahoo", s)
        for s in (
            "CSCO",
            "INTC",
            "ORCL",
            "QCOM",
            "MSFT",
            "AMD",
            "NVDA",
            "AVGO",
            "TSLA",
            "META",
            "PLTR",
            "QQQ",
            "SMH",
            "SOXX",
        )
    ),
)


class HistoryParseError(ValueError):
    pass


def _finite_positive(v: float, where: str) -> float:
    if not (math.isfinite(v) and v > 0.0):
        raise HistoryParseError(f"{where}: non-positive or non-finite close {v!r}")
    return v


def parse_fred_csv(raw: bytes, series: str) -> list[Point]:
    text = raw.decode("utf-8-sig", errors="strict")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or len(header) != 2 or header[1].strip() != series:
        raise HistoryParseError(f"FRED {series}: unexpected header {header!r}")
    out: list[Point] = []
    for row in reader:
        if not row:
            continue
        if len(row) != 2:
            raise HistoryParseError(f"FRED {series}: bad row {row!r}")
        d, v = row[0].strip(), row[1].strip()
        if v in ("", "."):
            continue
        try:
            out.append((date.fromisoformat(d), _finite_positive(float(v), f"FRED {series} {d}")))
        except ValueError as exc:
            raise HistoryParseError(f"FRED {series}: {exc}") from None
    return out


def parse_yahoo_chart(raw: bytes, sym: str) -> list[Point]:
    try:
        doc = json.loads(raw)
        result = doc["chart"]["result"][0]
        stamps = result["timestamp"]
        offset = int(result["meta"].get("gmtoffset", 0))
        ind = result["indicators"]
        adj = (ind.get("adjclose") or [{}])[0].get("adjclose")
        closes = adj if adj is not None else ind["quote"][0]["close"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise HistoryParseError(f"Yahoo {sym}: malformed chart ({type(exc).__name__})") from None
    if len(stamps) != len(closes):
        raise HistoryParseError(f"Yahoo {sym}: {len(stamps)} stamps but {len(closes)} closes")
    out: list[Point] = []
    for ts, c in zip(stamps, closes, strict=True):
        if c is None:
            continue
        d = shift_instant(datetime.fromtimestamp(int(ts), UTC), offset).date()
        out.append((d, _finite_positive(float(c), f"Yahoo {sym} {d}")))
    return out


def validate(points: Sequence[Point], name: str) -> None:
    if len(points) < MIN_ROWS:
        raise HistoryParseError(f"{name}: only {len(points)} rows (< {MIN_ROWS})")
    for (d0, _), (d1, _) in pairwise(points):
        if d1 <= d0:
            raise HistoryParseError(f"{name}: dates not increasing at {d0} -> {d1}")


def render(points: Iterable[Point]) -> bytes:
    lines = ["date,close"] + [f"{d.isoformat()},{format(v, '.10g')}" for d, v in points]
    return ("\n".join(lines) + "\n").encode()


def read_history(name: str, root: Path | None = None) -> list[Point]:
    path = (root or store_root()) / "history" / f"{name}.csv"
    rows = path.read_text().splitlines()
    if not rows or rows[0] != "date,close":
        raise HistoryParseError(f"{path}: not a history file")
    out = [(date.fromisoformat(a), float(b)) for a, b in (r.split(",") for r in rows[1:] if r)]
    validate(out, name)
    return out


def _append_provenance(path: Path, line: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")


def fetch_one(src: HistorySource, *, get: Get, root: Path, now: datetime) -> dict[str, Any]:
    url = src.url(now)
    line: dict[str, Any] = {
        "source": src.name,
        "kind": src.kind,
        "ref": src.ref,
        "url": url.split("?")[0] if src.kind == "yahoo" else url,
        "fetched_at": now.isoformat(),
    }
    try:
        status, body = get(url, headers=src.headers, timeout=TIMEOUT_S)
    except Exception as exc:  # transport errors are a recorded gap, never a crash
        line.update(status="error", detail=type(exc).__name__)
        return line
    line["http"] = status
    if status != 200 or not body:
        line.update(status="missing", detail=f"HTTP {status}")
        return line
    line["sha256"] = hashlib.sha256(body).hexdigest()
    try:
        points = (
            parse_fred_csv(body, src.ref)
            if src.kind == "fred"
            else parse_yahoo_chart(body, src.ref)
        )
        # completed sessions only: a row dated on or after the fetch's ET date
        # can be a session still in progress (Yahoo serves the live price)
        today_et = now.astimezone(ET).date()
        dropped = sum(1 for d, _ in points if d >= today_et)
        points = [(d, v) for d, v in points if d < today_et]
        if dropped:
            line["dropped_in_progress"] = dropped
        validate(points, src.name)
    except HistoryParseError as exc:
        line.update(status="invalid", detail=str(exc)[:200])
        return line
    data = render(points)
    atomic_write_bytes(root / "history" / f"{src.name}.csv", data)
    line.update(
        status="recorded",
        rows=len(points),
        first_date=points[0][0].isoformat(),
        last_date=points[-1][0].isoformat(),
        file_sha256=hashlib.sha256(data).hexdigest(),
    )
    return line


def fetch_history(
    *,
    only: Sequence[str] = (),
    get: Get = urllib_get,
    root: Path | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Fetch every source (or ``only`` those names); returns provenance lines."""
    base = root or store_root()
    wanted = [s for s in SOURCES if not only or s.name in only]
    unknown = set(only) - {s.name for s in SOURCES}
    if unknown:
        raise ValueError(f"unknown history sources: {sorted(unknown)}")
    lines = []
    for n, src in enumerate(wanted):
        if n:
            sleep(PACE_S)
        line = fetch_one(src, get=get, root=base, now=clock())
        _append_provenance(base / "history" / "provenance.jsonl", line)
        lines.append(line)
    return lines
