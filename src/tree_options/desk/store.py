"""The desk chain store: one columnar, gzipped JSON file per (session, symbol).

Layout under the store root (:func:`tree_options.desk.paths.store_root`)::

    chains/<D>/<SYM>.json.gz            schema desk-chain/1 (never rewritten)
    chains/<D>/<SYM>.conflict.json.gz   a later, different payload for <D>
    raw/<D>/<SYM>-<sha256>.json.gz      the vendor bytes (hash-addressed),
                                        newest 20 sessions; written BEFORE
                                        the chain is published
    manifest/<D>.json                   per-symbol status of every run for <D>
    gaps.jsonl                          append-only: sessions/symbols lost

A chain file is ``{"header": {...}, "columns": {name: [n values]}}``; the
header carries schema, session, underlying, source, source_as_of,
fetched_at, underlying_quote, raw_sha256 and n. Numbers stay JSON
numbers, OCC symbols strings.

Session validation (all must hold, else nothing is written): the
underlying's last trade is dated D; no option row traded after D; the
modal option last-trade date is D; ``source_as_of`` is at or after the
symbol's options close on D (the equity close only for a class listed in
``universe.REGULAR_CLOSE_OPTIONS`` whose options printed nothing after
it; 16:15 ET for everything else, listed late-close or unclassified;
early-close sessions use the calendar's close, 13:00 / 13:15); and the
underlying last traded within
5 minutes of the equity close (a snapshot stamped after the close can
still hold intraday content). An older payload is ``stale`` (not
published yet: retry), a
newer one ``missing`` (the feed moved past D). Completeness (both rights
with >= MIN_PER_RIGHT contracts, >= 50% of rows and of expiries with a
bid) must also hold, else ``incomplete`` (a partial publication: retry).
A recorded (D, SYM) whose raw evidence vanished inside the retention
window is re-fetched once and repaired when the payload is identical.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import shutil
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from tree_options.desk.chains import (
    SOURCE,
    ChainParseError,
    ParsedChain,
    fetch_raw,
    parse_chain,
)
from tree_options.desk.sessions import (
    Calendar,
    ClosingCalendar,
    equity_close,
    options_close,
    sessions_after,
)
from tree_options.desk.universe import REGULAR_CLOSE_OPTIONS
from tree_options.time.sessions import shift_instant
from tree_options.trex.clock import ET
from tree_options.trex.discovery.market import Transport

SCHEMA = "desk-chain/1"
MANIFEST_SCHEMA = "desk-chain-manifest/1"
RAW_KEEP_SESSIONS = 20
# completeness floors: a partially published payload must stay retryable
# (KO's real chain on 2026-09-22: 1038 rows over 18 expiries, 89% bid)
MIN_PER_RIGHT = 10
MIN_BID_FRACTION = 0.5
MIN_EXPIRY_BID_FRACTION = 0.5
OK_FRACTION = 0.9  # the run succeeds when this share is recorded (ok/exists/conflict)
PACE_S = 1.0  # one request per second
# the underlying's last trade must be this close to the equity close (every
# recorded 2026-09-22 name last traded at 15:59:59 or 16:00:00 ET)
CLOSE_TOLERANCE_S = 5 * 60

RECORDED = frozenset({"ok", "exists", "conflict"})
RETRYABLE = frozenset({"stale", "incomplete"})  # not published (whole) yet
GAP_STATUSES = frozenset({"missing", "invalid", "error", "conflict"})
STATUSES = ("ok", "exists", "conflict", "stale", "incomplete", "missing", "invalid", "error")

Clock = Callable[[], datetime]
Sleep = Callable[[float], None]


# ------------------------------------------------------------------ file I/O


def _tmp_for(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.tmp")


def _write_tmp(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    return tmp


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """tmp + rename (replaces an existing file)."""
    os.replace(_write_tmp(path, data), path)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_bytes(path, (json.dumps(obj, indent=1, sort_keys=True) + "\n").encode())


def atomic_create_bytes(path: Path, data: bytes) -> bool:
    """tmp + link: publishes ``path`` only if it does not exist yet
    (False, nothing changed, if another writer got there first)."""
    tmp = _write_tmp(path, data)
    try:
        os.link(tmp, path)
    except FileExistsError:
        return False
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    return True


def read_chain(path: Path) -> dict[str, Any]:
    doc = json.loads(gzip.decompress(path.read_bytes()))
    if not isinstance(doc, dict) or not isinstance(doc.get("header"), dict):
        raise ValueError(f"{path.name}: not a desk chain document")
    return doc


def encode_document(doc: dict[str, Any]) -> bytes:
    text = json.dumps(doc, separators=(",", ":"), sort_keys=True, allow_nan=False)
    return gzip.compress(text.encode("utf-8"), mtime=0)


# -------------------------------------------------------------------- store


class ChainStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def chain_path(self, session: date, sym: str) -> Path:
        return self.root / "chains" / session.isoformat() / f"{sym}.json.gz"

    def conflict_path(self, session: date, sym: str) -> Path:
        return self.root / "chains" / session.isoformat() / f"{sym}.conflict.json.gz"

    def raw_path(self, session: date, sym: str, sha256: str) -> Path:
        """Hash-addressed: a conflict's bytes never overwrite the original's."""
        return self.root / "raw" / session.isoformat() / f"{sym}-{sha256}.json.gz"

    def raw_retained(self, session: date) -> bool:
        """Whether ``session`` is still inside the raw retention window (fewer
        than RAW_KEEP_SESSIONS newer raw session directories)."""
        root = self.root / "raw"
        if not root.is_dir():
            return True
        newer = 0
        for p in root.iterdir():
            with contextlib.suppress(ValueError):
                if p.is_dir() and date.fromisoformat(p.name) > session:
                    newer += 1
        return newer < RAW_KEEP_SESSIONS

    def manifest_path(self, session: date) -> Path:
        return self.root / "manifest" / f"{session.isoformat()}.json"

    @property
    def gaps_path(self) -> Path:
        return self.root / "gaps.jsonl"

    def read_manifest(self, session: date) -> dict[str, Any] | None:
        try:
            doc = json.loads(self.manifest_path(session).read_text())
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) and isinstance(doc.get("symbols"), dict) else None

    def manifest_sessions(self) -> list[date]:
        out = []
        for p in (self.root / "manifest").glob("*.json"):
            with contextlib.suppress(ValueError):
                out.append(date.fromisoformat(p.stem))
        return sorted(out)


# --------------------------------------------------------------- validation


@dataclass(frozen=True)
class Verdict:
    status: str  # ok | stale | incomplete | missing | invalid
    detail: str
    payload_session: date | None


def payload_session(parsed: ParsedChain) -> date | None:
    """The modal ET date of the option rows' last trades (ties go to the
    later date), else the underlying's last-trade date."""
    days = Counter(t[:10] for t in parsed.columns["last_time"] if t)
    if days:
        best = max(days.items(), key=lambda kv: (kv[1], kv[0]))[0]
        return date.fromisoformat(best)
    return underlying_session(parsed)


def underlying_session(parsed: ParsedChain) -> date | None:
    ltt = parsed.underlying_quote.get("last_trade_time")
    return date.fromisoformat(ltt[:10]) if isinstance(ltt, str) else None


def _completeness(parsed: ParsedChain) -> str:
    """'' when the chain looks whole; else why it may be a partial publish."""
    cols = parsed.columns
    for right in ("C", "P"):
        count = sum(1 for r in cols["right"] if r == right)
        if count < MIN_PER_RIGHT:
            return f"{right}: {count} contracts < {MIN_PER_RIGHT}"
    bids = [isinstance(b, (int, float)) and b > 0 for b in cols["bid"]]
    if sum(bids) / parsed.n < MIN_BID_FRACTION:
        return f"only {sum(bids)}/{parsed.n} rows have bid > 0"
    expiries = set(cols["exp"])
    with_bid = {e for e, has in zip(cols["exp"], bids, strict=True) if has}
    if len(with_bid) / len(expiries) < MIN_EXPIRY_BID_FRACTION:
        return f"only {len(with_bid)}/{len(expiries)} expiries have a bid"
    return ""


def _late_close(parsed: ParsedChain, close: datetime) -> bool:
    """Only an explicitly regular-close class, with no option printed after
    the equity close, gets the equity-close cutoff. Everything else (the
    late list, any unclassified symbol, a listed name whose options printed
    late) waits for the late close: absent late prints prove nothing."""
    if parsed.underlying not in REGULAR_CLOSE_OPTIONS:
        return True
    return any(t and datetime.fromisoformat(t) > close for t in parsed.columns["last_time"])


def validate(parsed: ParsedChain, session: date, cal: ClosingCalendar) -> Verdict:
    """Every piece of session evidence must point at D, and the chain must
    look complete, before a snapshot may become D's immutable record."""
    us = underlying_session(parsed)
    if us is None:
        return Verdict("invalid", "underlying has no last-trade time to date the payload", None)
    if us < session:
        return Verdict("stale", f"underlying last traded {us}, not {session} yet", us)
    if us > session:
        return Verdict("missing", f"feed already moved past {session} to {us}", us)
    later = sorted({t[:10] for t in parsed.columns["last_time"] if t and t[:10] > us.isoformat()})
    if later:
        return Verdict("missing", f"option trades dated after {session} ({later[-1]})", us)
    ps = payload_session(parsed)
    if ps is not None and ps < session:
        return Verdict("stale", f"options mostly last traded {ps}, not {session} yet", ps)
    close = equity_close(session, cal)
    cutoff = options_close(session, cal, late=_late_close(parsed, close))
    if parsed.source_as_of < cutoff:
        return Verdict(
            "stale",
            f"source_as_of {parsed.source_as_of.isoformat()} before the "
            f"{session} {cutoff:%H:%M} ET options close",
            us,
        )
    # a snapshot stamped after the close may still hold intraday content
    # (XLV 2026-09-22: stamped 16:01:18 ET, frozen at 15:46): the underlying
    # must have traded within CLOSE_TOLERANCE_S of the equity close
    ltt = datetime.fromisoformat(str(parsed.underlying_quote["last_trade_time"]))
    if ltt < shift_instant(close, -CLOSE_TOLERANCE_S):
        return Verdict(
            "stale",
            f"underlying last traded {ltt.astimezone(ET):%H:%M:%S} ET, before the "
            f"{close:%H:%M} ET close: the snapshot predates the settle",
            us,
        )
    if parsed.n == 0:
        return Verdict("incomplete", "no parseable option rows", us)
    why = _completeness(parsed)
    if why:
        return Verdict("incomplete", why, us)
    return Verdict("ok", "", us)


def build_document(
    parsed: ParsedChain, session: date, raw_sha256: str, fetched_at: datetime
) -> dict[str, Any]:
    return {
        "header": {
            "schema": SCHEMA,
            "session": session.isoformat(),
            "underlying": parsed.underlying,
            "source": SOURCE,
            "source_as_of": parsed.source_as_of.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "underlying_quote": parsed.underlying_quote,
            "raw_sha256": raw_sha256,
            "n": parsed.n,
            "n_skipped": parsed.n_skipped,
        },
        "columns": parsed.columns,
    }


# ---------------------------------------------------------------- recording


@dataclass(frozen=True)
class SymbolResult:
    status: str
    n: int
    raw_sha256: str | None
    detail: str


@dataclass(frozen=True)
class RunSummary:
    session: date
    results: dict[str, SymbolResult]

    def counts(self) -> dict[str, int]:
        c = Counter(r.status for r in self.results.values())
        return {s: c.get(s, 0) for s in STATUSES}

    def line(self, rc: int) -> str:
        counts = " ".join(f"{k}={v}" for k, v in self.counts().items())
        return (
            f"record-chains session={self.session.isoformat()} "
            f"symbols={len(self.results)} {counts} exit={rc}"
        )


def exit_code(summary: RunSummary) -> int:
    """0 when >= 90% of the symbols are recorded; 3 when the rest is only
    not-yet-published (the timer retries); 1 otherwise."""
    total = len(summary.results)
    if total == 0:
        return 1
    recorded = sum(1 for r in summary.results.values() if r.status in RECORDED)
    if recorded / total >= OK_FRACTION:
        return 0
    if any(r.status in RETRYABLE for r in summary.results.values()):
        return 3
    return 1


def _existing(store: ChainStore, session: date, sym: str) -> dict[str, Any]:
    return read_chain(store.chain_path(session, sym))["header"]


def _write_raw(store: ChainStore, session: date, sym: str, raw: bytes, sha: str) -> None:
    atomic_write_bytes(store.raw_path(session, sym, sha), gzip.compress(raw, mtime=0))


def record_symbol(
    store: ChainStore,
    session: date,
    sym: str,
    *,
    transport: Transport,
    clock: Clock,
    cal: ClosingCalendar,
    dry_run: bool = False,
    recheck: bool = False,
    before_fetch: Callable[[], None] = lambda: None,
) -> SymbolResult:
    try:
        return _record_symbol(
            store,
            session,
            sym,
            transport=transport,
            clock=clock,
            cal=cal,
            dry_run=dry_run,
            recheck=recheck,
            before_fetch=before_fetch,
        )
    except OSError as exc:  # a write failure stays per-symbol (and retryable next run)
        return SymbolResult("error", 0, None, f"{type(exc).__name__}: {exc}")


def _record_symbol(
    store: ChainStore,
    session: date,
    sym: str,
    *,
    transport: Transport,
    clock: Clock,
    cal: ClosingCalendar,
    dry_run: bool,
    recheck: bool,
    before_fetch: Callable[[], None],
) -> SymbolResult:
    path = store.chain_path(session, sym)
    existing: dict[str, Any] | None = None
    repair = False
    if path.exists():
        try:
            existing = _existing(store, session, sym)
        except (OSError, ValueError) as exc:
            return SymbolResult(
                "error", 0, None, f"recorded file unreadable ({type(exc).__name__}); left as is"
            )
        known = str(existing.get("raw_sha256"))
        repair = store.raw_retained(session) and not store.raw_path(session, sym, known).exists()
        if not recheck and not repair:
            return SymbolResult("exists", int(existing.get("n", 0)), known, "")
    before_fetch()
    try:
        raw = fetch_raw(sym, transport)
    except Exception as exc:  # per-symbol isolation
        if existing is not None:
            return SymbolResult(
                "exists",
                int(existing.get("n", 0)),
                existing.get("raw_sha256"),
                f"raw evidence missing; repair fetch failed ({type(exc).__name__})"
                if repair
                else f"recheck fetch failed ({type(exc).__name__})",
            )
        return SymbolResult("error", 0, None, f"{type(exc).__name__}: {exc}")
    raw_sha = hashlib.sha256(raw).hexdigest()
    fetched_at = clock()
    try:
        parsed = parse_chain(raw, sym)
    except ChainParseError as exc:
        return SymbolResult("invalid", 0, raw_sha, str(exc))
    verdict = validate(parsed, session, cal)

    if existing is None and verdict.status == "ok" and not dry_run:
        # evidence first: the hash-addressed raw bytes, then the immutable chain
        _write_raw(store, session, sym, raw, raw_sha)
        doc = encode_document(build_document(parsed, session, raw_sha, fetched_at))
        if atomic_create_bytes(path, doc):
            return SymbolResult("ok", parsed.n, raw_sha, "")
        existing = _existing(store, session, sym)  # lost a race: compare like a recheck
    if existing is None:
        detail = verdict.detail or ("dry run: not written" if dry_run else "")
        return SymbolResult(verdict.status, parsed.n, raw_sha, detail)

    known = str(existing.get("raw_sha256"))
    n_known = int(existing.get("n", 0))
    lost = repair and raw_sha != known
    if raw_sha == known:
        if repair and not dry_run:
            _write_raw(store, session, sym, raw, raw_sha)
            return SymbolResult("exists", n_known, known, "raw evidence repaired")
        return SymbolResult("exists", n_known, known, "recheck: identical payload")
    if verdict.status != "ok":
        note = "raw evidence missing; " if lost else ""
        return SymbolResult("exists", n_known, known, f"{note}recheck: payload {verdict.status}")
    if not dry_run:
        _write_raw(store, session, sym, raw, raw_sha)
        doc = encode_document(build_document(parsed, session, raw_sha, fetched_at))
        atomic_write_bytes(store.conflict_path(session, sym), doc)
    note = "; raw evidence for the recorded payload is lost" if lost else ""
    return SymbolResult(
        "conflict", parsed.n, raw_sha, f"payload differs from the recorded {known[:12]}{note}"
    )


def _gap(now: datetime, session: date, sym: str, status: str, detail: str) -> dict[str, Any]:
    return {
        "at": now.isoformat(),
        "session": session.isoformat(),
        "sym": sym,
        "status": status,
        "detail": detail,
    }


def append_gaps(store: ChainStore, lines: Iterable[dict[str, Any]]) -> None:
    text = "".join(json.dumps(line, sort_keys=True) + "\n" for line in lines)
    if not text:
        return
    store.gaps_path.parent.mkdir(parents=True, exist_ok=True)
    with open(store.gaps_path, "a") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def update_manifest(
    store: ChainStore, session: date, results: dict[str, SymbolResult], now: datetime
) -> None:
    doc = store.read_manifest(session) or {
        "schema": MANIFEST_SCHEMA,
        "session": session.isoformat(),
        "symbols": {},
    }
    symbols: dict[str, Any] = doc["symbols"]
    for sym, r in results.items():
        old = symbols.get(sym)
        if r.status == "exists" and isinstance(old, dict) and old.get("status") in RECORDED:
            continue  # "exists" never downgrades the run that recorded it
        symbols[sym] = {
            "status": r.status,
            "n": r.n,
            "raw_sha256": r.raw_sha256,
            "detail": r.detail,
            "at": now.isoformat(),
        }
    doc["updated_at"] = now.isoformat()
    atomic_write_json(store.manifest_path(session), doc)


def finalize_prior(store: ChainStore, session: date, cal: Calendar, now: datetime) -> None:
    """Close the books on earlier sessions, once each: sessions no run ever
    attempted become a ``*`` gap, and symbols the latest attempted session
    never recorded become ``missing``."""
    prior = [s for s in store.manifest_sessions() if s < session]
    if not prior:
        return
    last = prior[-1]
    lines = []
    for s in sessions_after(last, session, cal):
        if s >= session:
            break
        atomic_write_json(
            store.manifest_path(s),
            {
                "schema": MANIFEST_SCHEMA,
                "session": s.isoformat(),
                "symbols": {},
                "note": "no recorder run for this session",
                "updated_at": now.isoformat(),
            },
        )
        lines.append(_gap(now, s, "*", "missing", "no recorder run for this session"))
    doc = store.read_manifest(last)
    if doc is not None:
        changed = False
        for sym, entry in sorted(doc["symbols"].items()):
            if store.chain_path(last, sym).exists():
                continue
            if isinstance(entry, dict) and entry.get("status") == "missing":
                continue
            prev = entry.get("status") if isinstance(entry, dict) else None
            doc["symbols"][sym] = {
                "status": "missing",
                "n": 0,
                "raw_sha256": None,
                "detail": f"never recorded (last status {prev})",
                "at": now.isoformat(),
            }
            lines.append(_gap(now, last, sym, "missing", f"never recorded (last status {prev})"))
            changed = True
        if changed:
            doc["updated_at"] = now.isoformat()
            atomic_write_json(store.manifest_path(last), doc)
    append_gaps(store, lines)


def prune_raw(store: ChainStore, keep: int = RAW_KEEP_SESSIONS) -> None:
    root = store.root / "raw"
    if not root.is_dir():
        return
    dated = []
    for p in root.iterdir():
        with contextlib.suppress(ValueError):
            if p.is_dir():
                dated.append((date.fromisoformat(p.name), p))
    dated.sort()
    for _d, p in dated[: max(0, len(dated) - keep)]:
        shutil.rmtree(p)


def record_session(
    session: date,
    symbols: Iterable[str],
    *,
    store: ChainStore,
    transport: Transport,
    clock: Clock,
    sleep: Sleep,
    cal: ClosingCalendar,
    dry_run: bool = False,
    recheck: bool = False,
    pace_s: float = PACE_S,
) -> RunSummary:
    """Record every symbol for ``session`` (per-symbol isolation, paced),
    then (unless dry-run) the manifest, gaps and raw retention."""
    results: dict[str, SymbolResult] = {}
    fetched = [False]

    def pace() -> None:  # called right before every real request
        if fetched[0]:
            sleep(pace_s)
        fetched[0] = True

    for sym in symbols:
        results[sym] = record_symbol(
            store,
            session,
            sym,
            transport=transport,
            clock=clock,
            cal=cal,
            dry_run=dry_run,
            recheck=recheck,
            before_fetch=pace,
        )
    summary = RunSummary(session, results)
    if dry_run:
        return summary
    now = clock()
    finalize_prior(store, session, cal, now)
    update_manifest(store, session, results, now)
    append_gaps(
        store,
        [
            _gap(now, session, sym, r.status, r.detail)
            for sym, r in results.items()
            if r.status in GAP_STATUSES
        ],
    )
    prune_raw(store)
    return summary
