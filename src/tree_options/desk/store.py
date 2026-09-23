"""The desk chain store: one columnar, gzipped JSON file per (session, symbol).

Layout under the store root (:func:`tree_options.desk.paths.store_root`)::

    chains/<D>/<SYM>.json.gz            schema desk-chain/1 (never rewritten)
    chains/<D>/<SYM>.conflict.json.gz   a later, different payload for <D>
    raw/<D>/<SYM>.json.gz               the vendor bytes, newest 20 sessions
    manifest/<D>.json                   per-symbol status of every run for <D>
    gaps.jsonl                          append-only: sessions/symbols lost

A chain file is ``{"header": {...}, "columns": {name: [n values]}}``; the
header carries schema, session, underlying, source, source_as_of,
fetched_at, underlying_quote, raw_sha256 and n. Numbers stay JSON
numbers, OCC symbols strings.

Session validation (all must hold, else nothing is written): the modal
option last-trade date (the underlying's when no row traded) equals D;
``source_as_of`` is at or after D 16:15 ET; at least half the rows have a
bid. An older payload is ``stale`` (not published yet: retry), a newer
one ``missing`` (the feed moved past D).
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
from tree_options.desk.sessions import Calendar, cutoff_instant, sessions_after
from tree_options.trex.discovery.market import Transport

SCHEMA = "desk-chain/1"
MANIFEST_SCHEMA = "desk-chain-manifest/1"
RAW_KEEP_SESSIONS = 20
MIN_BID_FRACTION = 0.5
OK_FRACTION = 0.9  # the run succeeds when this share is recorded (ok/exists/conflict)
PACE_S = 1.0  # one request per second

RECORDED = frozenset({"ok", "exists", "conflict"})
GAP_STATUSES = frozenset({"missing", "invalid", "error", "conflict"})
STATUSES = ("ok", "exists", "conflict", "stale", "missing", "invalid", "error")

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

    def raw_path(self, session: date, sym: str) -> Path:
        return self.root / "raw" / session.isoformat() / f"{sym}.json.gz"

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
    status: str  # ok | stale | missing | invalid
    detail: str
    payload_session: date | None


def payload_session(parsed: ParsedChain) -> date | None:
    """The session the snapshot describes: the modal ET date of the option
    rows' last trades (ties go to the later date), else the underlying's."""
    days = Counter(t[:10] for t in parsed.columns["last_time"] if t)
    if days:
        best = max(days.items(), key=lambda kv: (kv[1], kv[0]))[0]
        return date.fromisoformat(best)
    ltt = parsed.underlying_quote.get("last_trade_time")
    return date.fromisoformat(ltt[:10]) if isinstance(ltt, str) else None


def validate(parsed: ParsedChain, session: date) -> Verdict:
    if parsed.n == 0:
        return Verdict("invalid", "no parseable option rows", None)
    ps = payload_session(parsed)
    if ps is None:
        return Verdict("invalid", "no trade timestamps to date the payload", None)
    if ps < session:
        return Verdict("stale", f"payload describes {ps}, not {session} yet", ps)
    if ps > session:
        return Verdict("missing", f"feed already moved past {session} to {ps}", ps)
    if parsed.source_as_of < cutoff_instant(session):
        return Verdict(
            "stale", f"source_as_of {parsed.source_as_of.isoformat()} before {session} 16:15 ET", ps
        )
    bids = sum(1 for b in parsed.columns["bid"] if isinstance(b, (int, float)) and b > 0)
    if bids / parsed.n < MIN_BID_FRACTION:
        return Verdict("invalid", f"only {bids}/{parsed.n} rows have bid > 0", ps)
    return Verdict("ok", "", ps)


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
    if any(r.status == "stale" for r in summary.results.values()):
        return 3
    return 1


def _existing(store: ChainStore, session: date, sym: str) -> dict[str, Any]:
    return read_chain(store.chain_path(session, sym))["header"]


def record_symbol(
    store: ChainStore,
    session: date,
    sym: str,
    *,
    transport: Transport,
    clock: Clock,
    dry_run: bool = False,
    recheck: bool = False,
) -> SymbolResult:
    path = store.chain_path(session, sym)
    existing: dict[str, Any] | None = None
    if path.exists():
        try:
            existing = _existing(store, session, sym)
        except (OSError, ValueError) as exc:
            return SymbolResult(
                "error", 0, None, f"recorded file unreadable ({type(exc).__name__}); left as is"
            )
        if not recheck:
            return SymbolResult("exists", int(existing.get("n", 0)), existing.get("raw_sha256"), "")
    try:
        raw = fetch_raw(sym, transport)
    except Exception as exc:  # per-symbol isolation
        return SymbolResult("error", 0, None, f"{type(exc).__name__}: {exc}")
    raw_sha = hashlib.sha256(raw).hexdigest()
    fetched_at = clock()
    try:
        parsed = parse_chain(raw, sym)
    except ChainParseError as exc:
        return SymbolResult("invalid", 0, raw_sha, str(exc))
    verdict = validate(parsed, session)

    if existing is None and verdict.status == "ok" and not dry_run:
        doc = encode_document(build_document(parsed, session, raw_sha, fetched_at))
        if atomic_create_bytes(path, doc):
            atomic_write_bytes(store.raw_path(session, sym), gzip.compress(raw, mtime=0))
            return SymbolResult("ok", parsed.n, raw_sha, "")
        existing = _existing(store, session, sym)  # lost a race: compare like a recheck
    if existing is None:
        detail = verdict.detail or ("dry run: not written" if dry_run else "")
        return SymbolResult(verdict.status, parsed.n, raw_sha, detail)

    known = existing.get("raw_sha256")
    n_known = int(existing.get("n", 0))
    if verdict.status != "ok":
        return SymbolResult("exists", n_known, known, f"recheck: payload {verdict.status}")
    if raw_sha == known:
        return SymbolResult("exists", n_known, known, "recheck: identical payload")
    if not dry_run:
        doc = encode_document(build_document(parsed, session, raw_sha, fetched_at))
        atomic_write_bytes(store.conflict_path(session, sym), doc)
    return SymbolResult(
        "conflict", parsed.n, raw_sha, f"payload differs from the recorded {str(known)[:12]}"
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
    cal: Calendar,
    dry_run: bool = False,
    recheck: bool = False,
    pace_s: float = PACE_S,
) -> RunSummary:
    """Record every symbol for ``session`` (per-symbol isolation, paced),
    then (unless dry-run) the manifest, gaps and raw retention."""
    results: dict[str, SymbolResult] = {}
    fetched = False
    for sym in symbols:
        will_fetch = recheck or not store.chain_path(session, sym).exists()
        if will_fetch and fetched:
            sleep(pace_s)
        results[sym] = record_symbol(
            store,
            session,
            sym,
            transport=transport,
            clock=clock,
            dry_run=dry_run,
            recheck=recheck,
        )
        fetched = fetched or will_fetch
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
