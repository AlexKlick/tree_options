#!/usr/bin/env python3
"""Extend the XU-62 successor panel (XU-SUCCESSOR-001 §6 item 1 plumbing).

PAPER-ONLY, research lane. This is the fetch_ohlc.py pattern applied to
``artifacts/paper-trades/ohlc-panel-xu.json`` — the frozen 62-name panel of
the XU successor trigger (docs/desk/XU-SUCCESSOR-001.md §4.1) — extended
past its 2026-09-11 freeze so the shadow book's monthly drafts have forward
closes. Read-only vs the vendor; ``use_cache=False`` everywhere; nothing is
written into artifacts/massive-cache.

The roster is a FROZEN CONSTANT here (below), derived once from the frozen
panel bytes: sha256 ``3753dcb862eafd67cb9635116b5cb66ed056891dd8251cb4c73ad25a036c0a5a``
(the exact sha pinned in XU-SUCCESSOR-001 §4.1), 62 names, verified
2026-11-16. It is NEVER read from ``desk-universe.toml`` at runtime: adding
the 62 to ``[panel]`` would silently enroll them in the CBOE chain recorder
(the §6 item-1 trap). The roster is data pinned to frozen bytes, not a rule.

BK truncation, resolved BY EVIDENCE (probe 2026-11-16, read-only):
``/v3/reference/tickers/BK`` answers HTTP 404 NOT_FOUND ("Ticker not
found"), the exact-ticker search ``?ticker=BK`` returns [], the aggregates
themselves stop at 2026-05-20 (last close 137.16, matched by the panel), and
no successor/re-listed BNY Mellon common stock exists in the reference
searches — the same signature as WBA (details 404; aggregates stop
2025-08-27, last close 11.98). BK is genuinely delisted: KEPT TRUNCATED
(the survivorship-honest WBA precedent, §5). ``SYMBOL_MAP`` stays empty; a
future remap evidence line would add one entry plus a full-window re-fetch.

Modes (like fetch_ohlc.py):

  Daily extend (the eod-equity-style step; default target yesterday UTC):
    uv run --frozen python scripts/desk/fetch_ohlc_xu.py [YYYY-MM-DD]
    -> one range request per name from its stored last sessions through the
       target session; adjusted=true price-return bars; 500-cap head-chase.

Every write is atomic (.tmp + os.replace) under an exclusive flock on
``ohlc-panel-xu.json.lock`` and appends provenance lines to
``ohlc-panel-xu.provenance.md``. The adjustment-rebase check re-fetches the
last 5 stored sessions per name; a stored close off by > 0.5% triggers a
full-window re-fetch that REPLACES the name's series (a "rebase <NAME>"
provenance line). Delisted names (BK, WBA) without a target-session bar are
kept truncated and get a "truncate <NAME>" provenance line EVERY run — the
decision is never silent. Exit 0 written; 3 vendor lag / rejection (nothing
written); 1 roster drift / unusable panel. No ``timedelta`` (the repo's AST
guard): all date math is epoch/ms arithmetic.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

# --- the frozen roster (§4.1): 62 names, alphabetical ------------------------
# Derived 2026-11-16 from ohlc-panel-xu.json at sha256 3753dcb8…036c0a5a
# (XU-SUCCESSOR-001 §4.1 pins those exact bytes). Never re-derived at runtime.
XU_ROSTER: tuple[str, ...] = (
    "ABBV",
    "ABT",
    "ACN",
    "ADI",
    "AMGN",
    "APO",
    "AXP",
    "BA",
    "BK",
    "BLK",
    "BMY",
    "BRK.B",
    "C",
    "CAT",
    "CME",
    "COF",
    "COP",
    "CVX",
    "DD",
    "DE",
    "DHR",
    "DOW",
    "EMR",
    "F",
    "FCX",
    "GE",
    "GILD",
    "GM",
    "HON",
    "ICE",
    "ISRG",
    "JNJ",
    "KHC",
    "KKR",
    "LIN",
    "LMT",
    "MCD",
    "MDLZ",
    "MMM",
    "MO",
    "MRK",
    "MU",
    "NKE",
    "NUE",
    "PFE",
    "PLD",
    "PNC",
    "RTX",
    "SBUX",
    "SCHW",
    "SPG",
    "SYK",
    "T",
    "TGT",
    "TMO",
    "TXN",
    "UNP",
    "USB",
    "VZ",
    "WBA",
    "WFC",
    "WMT",
)
XU_PANEL_SHA256_FROZEN = "3753dcb862eafd67cb9635116b5cb66ed056891dd8251cb4c73ad25a036c0a5a"

# Vendor symbol remap (panel name -> vendor ticker). EMPTY: the 2026-11-16
# probe found no remap for the truncated names (see module docstring). An
# entry here routes vendor requests for the panel name to the vendor ticker
# and forces a full-window re-fetch of that name.
SYMBOL_MAP: dict[str, str] = {}

# Names the vendor itself has no more data for (probe evidence in the module
# docstring). They never count as vendor lag; their series stays truncated
# (survivorship-honest) and every run records a "truncate" provenance line.
DELISTED: dict[str, str] = {
    "BK": "delisted per /v3/reference/tickers/BK 404 + aggs end 2026-05-20 (probe 2026-11-16)",
    "WBA": "delisted per /v3/reference/tickers/WBA 404 + aggs end 2025-08-27 (probe 2026-11-16)",
}

BACKFILL_START = "2021-09-13"  # the frozen panel's first session
REBASE_OVERLAP = 5  # stored sessions re-fetched with each extend
REBASE_TOL = Decimal("0.005")  # a stored close off by more => adjustment moved
REBASE_MIN_SESSIONS = 250  # a shorter full refetch is a vendor problem, not a rebase
DAY_MS = 86_400_000


def panel_path(paper: Path) -> Path:
    return paper / "ohlc-panel-xu.json"


def lock_path(paper: Path) -> Path:
    return paper / "ohlc-panel-xu.json.lock"


def provenance_path(paper: Path) -> Path:
    return paper / "ohlc-panel-xu.provenance.md"


@contextmanager
def panel_lock(paper: Path):
    """Exclusive writer lock for one read/merge/write transaction (the
    fetch_ohlc.py convention; readers take it shared)."""
    with open(lock_path(paper), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


class VendorRejected(Exception):
    pass


class FetchedBars(dict[str, dict[str, Any]]):
    """Session bars, with the vendor's reason if it refused part of the range."""

    refusal: str | None = None


def _prev_day(iso: str) -> str:
    """The calendar day before ISO date ``iso`` (epoch math, no timedelta)."""
    ts = dt.datetime.fromisoformat(iso).replace(tzinfo=dt.UTC).timestamp()
    return dt.datetime.fromtimestamp(ts - DAY_MS / 1000, tz=dt.UTC).date().isoformat()


def _row_to_bar(row: dict, where: str) -> tuple[str, dict[str, Any]]:
    """One vendor agg row -> (session, bar) with exact string prices (the
    fetch_ohlc.py convention; Decimal volumes truncate toward zero)."""
    missing = [k for k in ("o", "h", "l", "c", "v") if row.get(k) is None]
    if missing:
        raise ValueError(f"{where}: row missing {','.join(missing)}: {row!r}")
    session = dt.datetime.fromtimestamp(row["t"] / 1000, tz=dt.UTC).date().isoformat()
    volume = row["v"]
    if isinstance(volume, Decimal):
        if volume < 0:
            raise ValueError(f"{where}: negative volume {volume}")
        volume = int(volume)  # truncate toward zero, like the v2 capture
    if not isinstance(volume, int):
        raise ValueError(f"{where}: non-int volume {volume!r}")
    bar = {
        "open": str(row["o"]),
        "high": str(row["h"]),
        "low": str(row["l"]),
        "close": str(row["c"]),
        "volume": volume,
    }
    return session, bar


def fetch_range(client, ticker: str, start: str, end: str) -> FetchedBars:
    """Fetch [start, end], chasing earlier sessions dropped by the 500-result cap.

    If the plan refuses a request, return the bars collected so far and the
    refusal. A refusal on the first request yields zero bars with a reason.
    """
    from tree_options.data.massive_client import MassiveAuthRejectedError, MassiveNotEntitledError

    bars = FetchedBars()
    range_start = start
    original_end = end
    while True:
        try:
            body = client.get_json(
                f"/v2/aggs/ticker/{ticker}/range/1/day/{range_start}/{end}",
                # adjusted=true: SPLIT-adjusted price-return closes, matching
                # the frozen XU panel's own convention (§5: split-adjusted,
                # price-return). NOT dividend-adjusted.
                {"adjusted": "true"},
                use_cache=False,
            )
        except (MassiveAuthRejectedError, MassiveNotEntitledError) as exc:
            if (
                isinstance(exc, MassiveNotEntitledError)
                or end != original_end
                or "timeframe" in str(exc)
            ):
                bars.refusal = str(exc)
                break
            raise VendorRejected(f"{ticker}: {exc}") from exc
        results = body.get("results") or []
        if not results:
            break
        for row in results:
            session, bar = _row_to_bar(row, where=f"{ticker}")
            bars[session] = bar
        oldest = min(bars)
        if oldest <= range_start or len(results) < 500:
            break
        end = _prev_day(oldest)  # chase moves leftward only
    return bars


def adjustment_moved(stored: dict, fetched: dict, overlap: list[str]) -> bool:
    """A stored overlap close off by more than REBASE_TOL => the vendor's
    split adjustment changed (a new split) — same semantics as fetch_ohlc.py."""
    for d in overlap:
        if d in fetched and d in stored:
            old = Decimal(str(stored[d]["close"]))
            new = Decimal(str(fetched[d]["close"]))
            if old > 0 and abs(new / old - 1) > REBASE_TOL:
                return True
    return False


def _atomic_write_json(path: Path, payload: object) -> str:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def append_provenance(paper: Path, mode: str, range_: str, requests: int, sha: str) -> None:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with provenance_path(paper).open("a") as fh:
        fh.write(f"| {stamp} | {mode} | {range_} | {requests} | {sha[:16]}… |\n")


def vendor_client():
    """The Massive/Polygon client with the operator's key pinned when the
    env var is absent (the detached-$HOME trap, see fetch_closes.py)."""
    from tree_options.data.massive_client import client_from_environment

    kwargs: dict[str, Any] = {}
    if not os.environ.get("POLYGON_API_KEY"):
        kwargs["key_path"] = Path("/home/alexk/.config/tree_options/polygon.key")
    return client_from_environment(**kwargs)


def extend(
    client,
    paper: Path,
    target: str,
    *,
    requests: int = 0,
) -> tuple[int, str]:
    """One extend pass. Returns (exit_code, summary). Exit 3 writes nothing."""
    path = panel_path(paper)
    stored: dict[str, dict[str, dict[str, Any]]] = {}
    if path.exists():
        # unlocked snapshot (writes are atomic); the merge below re-reads
        # under the lock, exactly like fetch_ohlc.py
        stored = json.loads(path.read_text())
        drift = [n for n in XU_ROSTER if n not in stored]
        if drift:
            return 1, (
                f"roster drift: {len(drift)} frozen-roster name(s) absent from the stored "
                f"panel ({', '.join(drift[:5])}); the roster is pinned to "
                f"sha256 {XU_PANEL_SHA256_FROZEN[:16]}… — investigate before writing"
            )

    def fetch_complete(name: str, start: str) -> dict[str, dict[str, Any]]:
        bars = fetch_range(client, SYMBOL_MAP.get(name, name), start, target)
        if bars.refusal is not None:
            raise VendorRejected(
                f"{name}: incomplete range {start}..{target} ({len(bars)} bars fetched): "
                f"{bars.refusal}"
            )
        return bars

    fetched: dict[str, dict[str, dict[str, Any]]] = {}
    rebased: list[str] = []
    truncated: list[str] = []
    missing: list[str] = []
    req = requests
    try:
        for name in XU_ROSTER:
            series = stored.get(name, {})
            overlap = sorted(d for d in series if d < target)[-REBASE_OVERLAP:]
            bars = fetch_complete(name, overlap[0] if overlap else BACKFILL_START)
            req += 1
            if adjustment_moved(series, bars, overlap) or name in SYMBOL_MAP:
                # a moved adjustment (or a pinned remap) re-fetches the whole
                # window and REPLACES the series
                bars = fetch_complete(name, BACKFILL_START)
                req += 1
                if len(bars) < REBASE_MIN_SESSIONS:
                    raise VendorRejected(f"{name}: adjustment moved but full refetch short")
                rebased.append(name)
                print(f"{name}: ADJUSTMENT MOVED - full-window refetch", flush=True)
            if target not in bars:
                if name in DELISTED and bars:
                    truncated.append(name)  # evidence-backed truncation: keep
                else:
                    missing.append(name)
                    continue
            fetched[name] = bars
            span = f"{min(bars)}..{max(bars)}" if bars else "-"
            print(f"{name}: {len(bars)} bars {span}", flush=True)
    except VendorRejected as exc:
        print(f"VENDOR REJECTED {exc}")
        print("re-arm per the ladder; nothing written")
        return 3, f"vendor rejected: {exc}"

    if missing:
        print(
            f"VENDOR LAG: no {target} bar for {len(missing)}/{len(XU_ROSTER)}: "
            f"{', '.join(missing)} — re-arm per the ladder; nothing written"
        )
        return 3, f"vendor lag: {len(missing)} name(s) missing {target}"

    with panel_lock(paper):  # one read/merge/write transaction at a time
        panel = json.loads(path.read_text()) if path.exists() else stored
        for name, bars in fetched.items():
            if name in rebased:  # re-adjusted history replaces the series
                newer = {d: b for d, b in panel.get(name, {}).items() if d > target}
                panel[name] = {**newer, **bars}  # keep a concurrent later merge
            else:
                series = panel.setdefault(name, {})
                series.update({d: b for d, b in bars.items() if d == target or d not in series})
        sha = _atomic_write_json(path, panel)
        starts = [min(bars) for bars in fetched.values() if bars] or [target]
        append_provenance(paper, "extend", f"{min(starts)}..{target}", req, sha)
        for name in rebased:
            append_provenance(paper, f"rebase {name}", f"{BACKFILL_START}..{target}", 1, sha)
        for name in truncated:
            append_provenance(paper, f"truncate {name} kept", DELISTED[name], 0, "-")
    n_sess = sum(len(s) for s in panel.values())
    print(f"wrote {path} (sha256 {sha[:16]}…; {n_sess} name-sessions)")
    if truncated:
        print(f"kept truncated (delisted, probe evidence): {', '.join(truncated)}")
    return 0, (
        f"extended through {target}: {len(fetched)} names, {len(rebased)} rebased, "
        f"{len(truncated)} kept-truncated"
    )


def main(argv: list[str] | None = None, *, client=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("date", nargs="?", help="target session YYYY-MM-DD (default: yesterday UTC)")
    args = ap.parse_args(argv)

    from tree_options.desk.paths import paper_dir

    paper = paper_dir()
    if args.date is None:
        ts = dt.datetime.now(dt.UTC).timestamp() - DAY_MS / 1000
        target = dt.datetime.fromtimestamp(ts, tz=dt.UTC).date().isoformat()
    else:
        target = args.date

    code, summary = extend(client or vendor_client(), paper, target)
    print(summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
