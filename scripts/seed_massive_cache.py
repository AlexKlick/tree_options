"""Seed a NEW Massive response cache with contract-master pages another cache
already holds: read-only on the source, never overwriting the destination.

Why this exists: the desk's long-dated capture (`docs/desk-runbook.md`,
"Long-dated option capture") writes to its own cache,
`artifacts/massive-cache-desk`, so the load-bearing research cache
`artifacts/massive-cache` is never written. The M4-B coverage era already
paid about 2,570 wire requests for many of the very contract masters the desk
needs (same underlyings, same Fridays). A cache entry is a content-addressed,
key-redacted vendor body (`cache_key_for` over the path plus sorted params),
so copying entries byte-for-byte turns those masters into free cache hits in
the new cache. The capture tool is unchanged by this, and a reader of the
new cache sees exactly the bytes the wire would have given, minus the wait.

The walk per (underlying, as_of): the first page's key is the capture's own
(`CONTRACTS_PATH` with underlying_ticker, as_of and limit); each later page's
key comes from the previous page's `next_url`, split by the client's own
`split_url` so the keys cannot drift from the capture's. A page the source
lacks, or holds unusable (undecodable, or a non-OK status), ends that
master's walk; the capture fetches the rest live. A destination entry that
already exists is left alone when byte-identical and REFUSED (reported, exit
2) when it differs.

Usage:

    PYTHONPATH=src python scripts/seed_massive_cache.py \
        --from-cache artifacts/massive-cache --to-cache artifacts/massive-cache-desk \
        --underlyings SPY,TSLA --as-of 2024-09-27 --as-of 2024-10-25
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.data.massive_client import (  # noqa: E402
    OK_STATUSES,
    HttpResponse,
    MassiveClient,
    MassiveError,
    ResponseCache,
    cache_key_for,
    loads_exact,
)
from tree_options.data.massive_options import CONTRACTS_PATH  # noqa: E402

CONTRACTS_LIMIT = 1000  # the capture's page size (capture_massive_structural)
MAX_PAGES = 50  # a runaway guard on a cursor chain; SPY's master is 10 pages


def _no_wire(url: str, *, timeout: float) -> HttpResponse:
    raise AssertionError("seed_massive_cache never touches the wire")


# Only `split_url` is used: it needs the vendor host, never a real key.
_SPLITTER = MassiveClient(api_key="seed-only-never-sent", transport=_no_wire, cache_dir=None)


@dataclass
class SeedReport:
    copied: int = 0
    present: int = 0
    masters_complete: int = 0
    masters_partial: list[str] = field(default_factory=list)
    masters_missing: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def _usable(raw: bytes) -> Mapping[str, Any] | None:
    try:
        body = loads_exact(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, Mapping) or body.get("status") not in OK_STATUSES:
        return None
    return body


def _seed_one(
    src: ResponseCache, dst: ResponseCache, underlying: str, as_of: date, report: SeedReport
) -> None:
    label = f"{underlying} {as_of.isoformat()}"
    path = CONTRACTS_PATH
    params: dict[str, Any] = {
        "underlying_ticker": underlying,
        "as_of": as_of.isoformat(),
        "limit": CONTRACTS_LIMIT,
    }
    for page_no in range(1, MAX_PAGES + 1):
        key = cache_key_for(path, params)
        raw = src.get(key)
        body = None if raw is None else _usable(raw)
        if raw is None or body is None:
            if page_no == 1:
                report.masters_missing.append(label)
            else:
                report.masters_partial.append(label)
            return
        existing = dst.get(key)
        if existing is None:
            dst.put(key, raw)
            report.copied += 1
        elif existing == raw:
            report.present += 1
        else:
            report.conflicts.append(f"{label} page {page_no} ({key})")
        next_url = body.get("next_url")
        if not (isinstance(next_url, str) and next_url):
            report.masters_complete += 1
            return
        try:
            path, params = _SPLITTER.split_url(next_url)
        except MassiveError:
            report.masters_partial.append(label)
            return
    report.masters_partial.append(label)


def seed_masters(
    from_cache: Path, to_cache: Path, underlyings: Sequence[str], as_ofs: Sequence[date]
) -> SeedReport:
    """Copy every (underlying, as_of) master chain `from_cache` holds."""
    src, dst = ResponseCache(from_cache), ResponseCache(to_cache)
    report = SeedReport()
    for underlying in underlyings:
        for as_of in as_ofs:
            _seed_one(src, dst, underlying, as_of, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--from-cache", type=Path, required=True, help="source cache (read-only)")
    parser.add_argument("--to-cache", type=Path, required=True, help="destination cache")
    parser.add_argument("--underlyings", required=True, help="comma-separated tickers")
    parser.add_argument(
        "--as-of", action="append", required=True, type=date.fromisoformat, metavar="ISO-DATE"
    )
    args = parser.parse_args(argv)
    if args.from_cache.resolve() == args.to_cache.resolve():
        parser.error("--from-cache and --to-cache are the same directory")
    underlyings = [u for u in args.underlyings.split(",") if u]
    report = seed_masters(args.from_cache, args.to_cache, underlyings, args.as_of)
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 2 if report.conflicts else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
