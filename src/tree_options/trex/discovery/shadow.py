"""Shadow alternatives: forward paper-tracking of candidates NOT taken.

Every scan's accepted candidates that were NOT adopted by a live plan
open a 1-contract SHADOW position at the scan-time debit mid; later scans
(and, from M5, the CBOE full delayed chain) mark it. "How would it have
panned out" accrues forward from real delayed quotes - nothing is
back-filled or fabricated, and the UI labels every row
"forward-shadow - not executed".

Identity: the instrument key is ``underlying|yyyymmdd|short|long`` with
strikes in ``%g`` form (float-artifact-free); an EPISODE is one
instrument opened by one scan run, so re-entry after close opens a new
episode and history is never overwritten. Live plan structures are
excluded (dates/Decimal strikes normalized before comparison).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from tree_options.trex import history

MAX_SHADOW_MARKS_LINES = 20_000


@dataclass
class ShadowPosition:
    episode_id: str
    key: str
    underlying: str
    expiry: str  # yyyymmdd
    short_strike: float
    long_strike: float
    width: float
    opened_at: str
    opened_run_id: str
    qty: int = 1
    debit_paid: float = 0.0
    status: str = "open"  # open | expired
    last_mark: float | None = None
    last_mark_at: str | None = None
    mark_source: str = "none"  # scan | chain | carry | intrinsic-approx | none
    best_pnl: float | None = None
    worst_pnl: float | None = None
    final_pnl: float | None = None

    @property
    def pnl(self) -> float | None:
        if self.status == "expired":
            return self.final_pnl
        if self.last_mark is None:
            return None
        return (self.last_mark - self.debit_paid) * self.qty * 100


@dataclass
class ShadowBook:
    positions: list[ShadowPosition] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "positions": [
                {
                    "episode_id": p.episode_id,
                    "key": p.key,
                    "underlying": p.underlying,
                    "expiry": p.expiry,
                    "short_strike": p.short_strike,
                    "long_strike": p.long_strike,
                    "width": p.width,
                    "qty": p.qty,
                    "debit_paid": p.debit_paid,
                    "opened_at": p.opened_at,
                    "opened_run_id": p.opened_run_id,
                    "status": p.status,
                    "last_mark": p.last_mark,
                    "last_mark_at": p.last_mark_at,
                    "mark_source": p.mark_source,
                    "best_pnl": p.best_pnl,
                    "worst_pnl": p.worst_pnl,
                    "final_pnl": p.final_pnl,
                    "pnl": p.pnl,
                }
                for p in self.positions
            ],
        }

    @classmethod
    def from_payload(cls, doc: dict[str, Any]) -> ShadowBook:
        out = cls()
        for row in doc.get("positions", []):
            out.positions.append(
                ShadowPosition(
                    episode_id=str(row["episode_id"]),
                    key=str(row["key"]),
                    underlying=str(row["underlying"]),
                    expiry=str(row["expiry"]),
                    short_strike=float(row["short_strike"]),
                    long_strike=float(row["long_strike"]),
                    width=float(row["width"]),
                    qty=int(row.get("qty", 1)),
                    debit_paid=float(row.get("debit_paid", 0.0)),
                    opened_at=str(row["opened_at"]),
                    opened_run_id=str(row["opened_run_id"]),
                    status=str(row.get("status", "open")),
                    last_mark=row.get("last_mark"),
                    last_mark_at=row.get("last_mark_at"),
                    mark_source=str(row.get("mark_source", "none")),
                    best_pnl=row.get("best_pnl"),
                    worst_pnl=row.get("worst_pnl"),
                    final_pnl=row.get("final_pnl"),
                )
            )
        return out


def shadow_key(underlying: str, expiry: str, short: float, long: float) -> str:
    return f"{underlying}|{expiry}|{short:g}|{long:g}"


def _norm_expiry(value: object) -> str:
    """date(2026,10,16) | '2026-10-16' | '20261016' -> '20261016'."""
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = str(value).replace("-", "")
    return text[:8]


def load_shadow(state_dir: Path) -> ShadowBook | None:
    path = state_dir / "shadow_book.json"
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return ShadowBook.from_payload(doc)


def save_shadow(state_dir: Path, book: ShadowBook) -> None:
    path = state_dir / "shadow_book.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(book.to_payload(), indent=2) + "\n")
    tmp.replace(path)


def open_from_scan(
    book: ShadowBook,
    payload: dict[str, Any],
    run_id: str,
    now: datetime,
    excluded: list[tuple[str, object, object, object]] | None = None,
) -> ShadowBook:
    """Open one 1-contract shadow per NEW accepted candidate.

    Dedupes by instrument key while open (a re-appearing candidate does
    not double-size the shadow); re-entry after close opens a NEW
    episode. ``excluded`` carries live plan structures as
    (underlying, expiry, short, long) with dates/Decimal-able strikes.
    """
    excluded_keys = {
        shadow_key(u, _norm_expiry(e), float(s), float(lng))  # type: ignore[arg-type]
        for u, e, s, lng in (excluded or [])
    }
    open_keys = {p.key for p in book.positions if p.status == "open"}
    for row in payload.get("candidates", []):
        if not row.get("accepted", False):
            continue
        debit = row.get("debit_mid")
        if debit is None:
            continue
        key = shadow_key(
            str(row["underlying"]),
            _norm_expiry(row["expiry"]),
            float(row["short_strike"]),
            float(row["long_strike"]),
        )
        if key in open_keys or key in excluded_keys:
            continue
        short, long_ = float(row["short_strike"]), float(row["long_strike"])
        book.positions.append(
            ShadowPosition(
                episode_id=f"{key}#{run_id}",
                key=key,
                underlying=str(row["underlying"]),
                expiry=_norm_expiry(row["expiry"]),
                short_strike=short,
                long_strike=long_,
                width=round(long_ - short, 6),
                debit_paid=float(debit),
                opened_at=now.isoformat(),
                opened_run_id=run_id,
            )
        )
        open_keys.add(key)
    return book


def _apply_mark(pos: ShadowPosition, value: float, now: datetime, source: str) -> None:
    pos.last_mark = value
    pos.last_mark_at = now.isoformat()
    pos.mark_source = source
    pnl = pos.pnl
    if pnl is not None:
        pos.best_pnl = pnl if pos.best_pnl is None else max(pos.best_pnl, pnl)
        pos.worst_pnl = pnl if pos.worst_pnl is None else min(pos.worst_pnl, pnl)


def _intrinsic(pos: ShadowPosition, spot: float) -> float:
    return (max(0.0, pos.long_strike - spot) - max(0.0, pos.short_strike - spot))


def mark_from_chain(
    book: ShadowBook,
    chains: dict[str, Any],
    now: datetime,
    spots: dict[str, float] | None = None,
) -> ShadowBook:
    """Mark open shadows from CBOE full-chain rows (the M5 upgrade).

    ``chains``: {symbol: {expiry_yyyymmdd: {strike: {bid, ask, ...}}}}
    - strike keys arrive as JSON strings through the cache; both forms
    are accepted. Value = mid(long) - mid(short); a leg miss carries the
    last mark (source 'carry'). Past expiry finalizes at intrinsic with
    the spot when known.
    """
    spots = spots or {}
    marks: list[dict[str, Any]] = []
    for pos in book.positions:
        if pos.status != "open":
            continue
        expiry = datetime.strptime(pos.expiry, "%Y%m%d").date()
        if now.date() > expiry:
            spot = spots.get(pos.underlying)
            if spot is not None:
                _apply_mark(pos, _intrinsic(pos, spot), now, "intrinsic-approx")
                pos.final_pnl = pos.pnl
                pos.status = "expired"
            continue
        rows = (chains.get(pos.underlying) or {}).get(pos.expiry) or {}
        by_strike: dict[float, dict[str, Any]] = {}
        for key, row in rows.items():
            try:
                by_strike[float(key)] = row
            except (TypeError, ValueError):
                continue
        long_row = by_strike.get(pos.long_strike)
        short_row = by_strike.get(pos.short_strike)

        def _mid(row: dict[str, Any] | None) -> float | None:
            if not row or row.get("bid") is None or row.get("ask") is None:
                return None
            bid, ask = float(row["bid"]), float(row["ask"])
            if bid <= 0 or ask < bid:
                return None
            return (bid + ask) / 2

        long_mid, short_mid = _mid(long_row), _mid(short_row)
        if long_mid is not None and short_mid is not None:
            _apply_mark(pos, long_mid - short_mid, now, "chain")
        elif pos.last_mark is not None:
            pos.mark_source = "carry"
            pos.last_mark_at = now.isoformat()
        else:
            pos.mark_source = "none"
        if pos.pnl is not None:
            marks.append(
                {"key": pos.key, "value": pos.last_mark, "pnl": pos.pnl, "source": pos.mark_source}
            )
    return book


def mark_from_payload(
    book: ShadowBook,
    payload: dict[str, Any],
    now: datetime,
    spots: dict[str, float] | None = None,
) -> ShadowBook:
    """Mark open shadows from the scan's own rows (candidates + rejected
    all carry debit_mid at scan time). A miss carries the last mark with
    the source disclosed. Past-expiry positions finalize at intrinsic
    using the quote spot (mark_source 'intrinsic-approx')."""
    spots = spots or {}
    rows = list(payload.get("candidates", [])) + list(payload.get("rejected", []))
    row_by_key: dict[str, float] = {}
    for row in rows:
        debit = row.get("debit_mid")
        if debit is None:
            continue
        key = shadow_key(
            str(row["underlying"]),
            _norm_expiry(row["expiry"]),
            float(row["short_strike"]),
            float(row["long_strike"]),
        )
        row_by_key[key] = float(debit)
    marks: list[dict[str, Any]] = []
    for pos in book.positions:
        if pos.status != "open":
            continue
        expiry = datetime.strptime(pos.expiry, "%Y%m%d").date()
        if now.date() > expiry:
            spot = spots.get(pos.underlying)
            if spot is not None:
                _apply_mark(pos, _intrinsic(pos, spot), now, "intrinsic-approx")
                pos.final_pnl = pos.pnl
                pos.status = "expired"
            continue
        if pos.key in row_by_key:
            _apply_mark(pos, row_by_key[pos.key], now, "scan")
        elif pos.last_mark is not None:
            pos.mark_source = "carry"
            pos.last_mark_at = now.isoformat()
        else:
            pos.mark_source = "none"
        if pos.pnl is not None:
            marks.append(
                {"key": pos.key, "value": pos.last_mark, "pnl": pos.pnl, "source": pos.mark_source}
            )
    return book


def append_shadow_mark(
    state_dir: Path, run_id: str, marks: list[dict[str, Any]], now: datetime
) -> None:
    path = state_dir / "shadow_marks.jsonl"
    history.repair_torn_tail(path)
    history.append_line(path, {"ts": now.isoformat(), "run_id": run_id, "marks": marks})
    if history.count_lines(path) > MAX_SHADOW_MARKS_LINES:
        history.rotate_halving(path, MAX_SHADOW_MARKS_LINES)


def shadow_stats(book: ShadowBook) -> dict[str, Any]:
    """Honest rollup; positions without any mark are excluded from the
    pnl aggregates (never counted as zero)."""
    open_ct = sum(1 for p in book.positions if p.status == "open")
    expired = [p for p in book.positions if p.status == "expired"]
    pnls = [p.pnl for p in book.positions if p.pnl is not None]
    return {
        "open": open_ct,
        "expired": len(expired),
        "mean_pnl": sum(pnls) / len(pnls) if pnls else None,
        "hit_rate": (sum(1 for v in pnls if v > 0) / len(pnls)) if pnls else None,
        "not_executed": True,
    }
