"""Bounded LIVE capture of Massive (Polygon) free-tier structural data.

This is the ONLY networked tool in the M4-B lane and the reproducibility seam
between the two workstreams:

* WS-D1 (`tree_options.data.massive_client`) owns the wire: the key, the
  5 requests/minute governor, the 429 backoff, the entitlement gate, and the
  key-redacted response cache.
* WS-D2 (`scripts/inspect_structural_coverage.py`) owns the analysis and
  consumes *captures on disk*, deliberately importing nothing from WS-D1.

The two do not meet on their own. `MassiveClient.paginate()` returns the
FLATTENED records across pages, while the inspector wants the vendor's own
page bodies inside a `{"as_of": ..., "pages": [...]}` envelope so it can see
`next_url`, `status` and `request_id` per page and decide for itself whether
a capture is complete. This script is that adapter, written on the CONSUMER
side so neither lane's public API had to move.

Three disciplines are load-bearing here:

1. **Byte-verbatim pages.** The envelope is assembled as TEXT from the
   redacted bytes the client cached, never by re-serialising a decoded
   object. Re-serialising would route `587.5` through a float and silently
   launder the vendor's exact strike; the inspector then refuses floats, so
   the drift would surface as a crash rather than a wrong number -- but the
   right fix is to never lose the token in the first place.
2. **Truncation is recorded, never silent.** The page cap stops the walk and
   LEAVES `next_url` on the last captured page. The inspector reads that as
   `capture_complete=false` and reports an incomplete capture. A short
   universe therefore says so instead of misdescribing coverage.
3. **A hard request budget.** The free tier is 5 requests/minute and every
   call is spent from a shared quota. The budget is charged before the wire
   is touched, a cache hit costs nothing, and exhausting it stops the run
   cleanly with everything captured so far written to disk.

The API key is never logged, never written into a capture, and never placed
in this file: it comes from `POLYGON_API_KEY` or the 0600 key file via
`massive_client.load_api_key`, and the cache redacts it out of every stored
body before this script ever reads one.

Usage (writes gitignored captures under `artifacts/`):

    PYTHONPATH=src python scripts/capture_massive_structural.py \
        --out-dir artifacts/m4b-captures --budget 45
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(REPO_ROOT / "src"))

from tree_options.data.massive_client import (  # noqa: E402
    MassiveClient,
    MassiveError,
    MassiveNotEntitledError,
    cache_key_for,
    client_from_environment,
)
from tree_options.data.massive_options import (  # noqa: E402
    AGGS_PATH_TEMPLATE,
    CONTRACTS_PATH,
    MASSIVE_PROVIDER,
    session_of_epoch_ms,
)

CAPTURE_VERSION = "m4b-capture/1"

# The probed 2-year entitled window, sampled at four points ~6 months apart so
# the lifecycle series has three transitions per underlying.
AS_OF_DATES: tuple[date, ...] = (
    date(2024, 9, 16),
    date(2025, 3, 14),
    date(2025, 9, 15),
    date(2026, 3, 16),
)
UNDERLYINGS: tuple[str, ...] = ("SPY", "TSLA")

CONTRACTS_LIMIT = 1000
PAGES_PER_MASTER = 4
BARS_WANTED = 6
DTE_MIN = 30
DTE_MAX = 60


class BudgetExhausted(RuntimeError):
    """The request budget ran out. Everything captured so far is still good."""


@dataclass
class Budget:
    """Hard cap on LIVE requests. A cache hit is free and is never charged."""

    limit: int
    spent: int = 0
    reserved: int = 0
    log: list[str] = field(default_factory=list)

    @property
    def available(self) -> int:
        return self.limit - self.spent - self.reserved

    def charge(self, what: str) -> None:
        if self.available < 1:
            raise BudgetExhausted(
                f"request budget {self.limit} exhausted (spent {self.spent},"
                f" reserved {self.reserved}) before {what}"
            )
        self.spent += 1
        self.log.append(what)

    def charge_retries(self, what: str, extra: int) -> None:
        """Account wire requests a 429 backoff spent beyond the charged one.

        `charge` is paid before the call, but the client may retry a rate-limited
        request several times, so one charge can cost several wire requests. The
        budget is a cap on REQUESTS, not on calls: the extras are recorded here
        (never refused after the fact — the wire cost is already incurred) so the
        NEXT charge sees the true balance and the manifest reports what was spent.
        """
        if extra < 1:
            return
        self.spent += extra
        self.log.extend(f"{what} [429 retry {i + 1}]" for i in range(extra))

    def reserve(self, count: int) -> None:
        self.reserved += count

    def release(self, count: int) -> None:
        self.reserved = max(0, self.reserved - count)


@dataclass
class CapturedPage:
    """One vendor response: decoded for control flow, verbatim for the file."""

    body: Mapping[str, Any]
    text: str
    from_cache: bool


@dataclass
class MasterCapture:
    underlying: str
    as_of: date
    pages: list[CapturedPage] = field(default_factory=list)
    truncated: bool = False
    pending_next_url: bool = False
    error: str | None = None

    @property
    def rows(self) -> int:
        return sum(len(page.body.get("results") or ()) for page in self.pages)

    @property
    def filename(self) -> str:
        return f"{self.underlying}_{self.as_of.isoformat()}.json"


def fetch_page(
    client: MassiveClient, path: str, params: Mapping[str, Any], *, budget: Budget
) -> CapturedPage:
    """One page, charged to the budget only if it actually hits the wire.

    The verbatim text is read back out of the client's cache, which stores
    the response bytes with the API key redacted. That is the only way to
    keep the vendor's exact number tokens: the decoded body has already
    turned them into `Decimal`s and re-encoding would not round-trip.
    """
    if client.cache is None:  # pragma: no cover - the CLI always caches
        raise RuntimeError("capture requires the response cache (verbatim bodies live there)")
    key = cache_key_for(path, params)
    hit = client.cache.get(key) is not None
    if not hit:
        budget.charge(client.display_url(path, params))
    wire_before = client.stats.requests
    body = client.get_json(path, params)
    if not hit:
        budget.charge_retries(
            client.display_url(path, params), client.stats.requests - wire_before - 1
        )
    raw = client.cache.get(key)
    if raw is None:  # pragma: no cover - put() failed, which would have raised
        raise RuntimeError(f"{client.display_url(path, params)}: body not cached")
    return CapturedPage(body=body, text=raw.decode("utf-8").strip(), from_cache=hit)


def capture_master(
    client: MassiveClient,
    underlying: str,
    as_of: date,
    *,
    budget: Budget,
    max_pages: int,
) -> MasterCapture:
    """Walk `next_url` up to `max_pages`, then stop and say so.

    Stopping leaves `next_url` in place on the last captured page, which is
    precisely how the inspector detects an incomplete capture. Nothing here
    trims or rewrites a page.
    """
    capture = MasterCapture(underlying=underlying, as_of=as_of)
    path = CONTRACTS_PATH
    params: dict[str, Any] = {
        "underlying_ticker": underlying,
        "as_of": as_of.isoformat(),
        "limit": CONTRACTS_LIMIT,
    }
    while True:
        if len(capture.pages) >= max_pages:
            capture.truncated = True
            capture.pending_next_url = True
            return capture
        try:
            page = fetch_page(client, path, params, budget=budget)
        except BudgetExhausted:
            if capture.pages:
                capture.truncated = True
                capture.pending_next_url = True
                return capture
            raise
        except MassiveError as exc:
            capture.error = f"{type(exc).__name__}: {exc}"
            return capture
        capture.pages.append(page)
        next_url = page.body.get("next_url")
        if not (isinstance(next_url, str) and next_url):
            return capture
        path, params = client.split_url(next_url)


def master_envelope(capture: MasterCapture) -> str:
    """`{"as_of":..., "underlying_ticker":..., "pages":[<verbatim>, ...]}`.

    Assembled as text so every page is the exact JSON the vendor sent.
    """
    header = json.dumps(
        {
            "capture_version": CAPTURE_VERSION,
            "provider": MASSIVE_PROVIDER,
            "as_of": capture.as_of.isoformat(),
            "underlying_ticker": capture.underlying,
        }
    )
    pages = ",".join(page.text for page in capture.pages)
    return f'{header[:-1]},"pages":[{pages}]}}\n'


def _plain(value: Decimal) -> str:
    """Exponent-free decimal text, matching the inspector's own convention."""
    return format(value.normalize(), "f")


def _as_decimal(value: Any, where: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    raise ValueError(f"{where}: refusing to coerce {type(value).__name__} into a price")


def capture_spot_proxy(
    client: MassiveClient, underlyings: Sequence[str], as_ofs: Sequence[date], *, budget: Budget
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Underlying closes on the as_of dates, for strike-grid-in-spot-units.

    One equity aggregate call per name covers every as_of. Not being
    entitled is recorded and the metric degrades to NOT_EVALUABLE in the
    report -- it is never guessed.

    `adjusted=false` is deliberate and load-bearing. The vendor's adjusted
    series restates split history *to the present*, but an option strike
    ladder is denominated in the share terms of its own `as_of` date. Dividing
    a to-today-adjusted close into an as_of-dated ladder mixes two unit
    systems for any name that splits after `as_of`, which is exactly the
    point-in-time error this repo refuses everywhere else. The unadjusted
    close is the PIT-correct denominator.
    """
    proxy: dict[str, dict[str, str]] = {}
    notes: list[str] = []
    lo, hi = min(as_ofs), max(as_ofs)
    wanted = {d.isoformat() for d in as_ofs}
    for underlying in underlyings:
        path = AGGS_PATH_TEMPLATE.format(
            ticker=underlying, start=lo.isoformat(), end=hi.isoformat()
        )
        try:
            page = fetch_page(client, path, {"adjusted": "false"}, budget=budget)
        except (BudgetExhausted, MassiveError) as exc:
            notes.append(f"{underlying}: spot proxy unavailable ({type(exc).__name__}: {exc})")
            continue
        closes: dict[str, str] = {}
        for row in page.body.get("results") or ():
            if not isinstance(row, Mapping):
                continue
            session = session_of_epoch_ms(int(row["t"])).isoformat()
            if session in wanted:
                closes[session] = _plain(_as_decimal(row["c"], f"{underlying} {session}.c"))
        if closes:
            proxy[underlying] = dict(sorted(closes.items()))
        missing = sorted(wanted - set(closes))
        if missing:
            notes.append(
                f"{underlying}: no session close on {', '.join(missing)} (not a trading day)"
            )
    return proxy, notes


def _band_expirations(rows: Sequence[Mapping[str, Any]], as_of: date) -> list[date]:
    expirations = {date.fromisoformat(str(r["expiration_date"])) for r in rows}
    return sorted(e for e in expirations if DTE_MIN <= (e - as_of).days <= DTE_MAX)


def choose_bar_contracts(
    captures: Sequence[MasterCapture],
    spot: Mapping[str, Mapping[str, str]],
    *,
    wanted: int,
) -> tuple[list[tuple[str, date, date]], list[str]]:
    """Deterministically pick liquid, representative contracts to price.

    At-the-money call+put on the nearest 30-60 DTE expiry plus an ATM call on
    the far end of the band, per underlying. ATM is chosen against the spot
    proxy when it exists and against the ladder's median strike when it does
    not -- deep-OTM picks would return empty series, and the inspector
    (rightly) refuses an empty series rather than reporting zero coverage.
    """
    picks: list[tuple[str, date, date]] = []
    notes: list[str] = []
    seen: set[str] = set()
    by_underlying: dict[str, MasterCapture] = {}
    for capture in sorted(captures, key=lambda c: (c.underlying, c.as_of)):
        if capture.pages and capture.underlying not in by_underlying:
            by_underlying[capture.underlying] = capture

    for underlying, capture in sorted(by_underlying.items()):
        rows = [r for page in capture.pages for r in (page.body.get("results") or ())]
        band = _band_expirations(rows, capture.as_of)
        if not band:
            notes.append(f"{underlying}: no 30-60 DTE expiry in the {capture.as_of} master")
            continue
        as_of_key = capture.as_of.isoformat()
        reference = spot.get(underlying, {}).get(as_of_key)
        targets = [(band[0], "call"), (band[0], "put"), (band[-1], "call")]
        for expiration, kind in targets:
            ladder = sorted(
                (_as_decimal(r["strike_price"], f"{r['ticker']}.strike_price"), str(r["ticker"]))
                for r in rows
                if date.fromisoformat(str(r["expiration_date"])) == expiration
                and str(r["contract_type"]) == kind
            )
            if not ladder:
                continue
            if reference is not None:
                anchor = Decimal(reference)
            else:
                anchor = ladder[len(ladder) // 2][0]
                notes.append(f"{underlying} {expiration}: no spot proxy, using the median strike")
            _, ticker = min(ladder, key=lambda item: (abs(item[0] - anchor), item[1]))
            # A band with a single expiry makes `band[0]` and `band[-1]` the
            # same date, so the near-call and far-call targets collide. Paying
            # for the same contract twice would quietly shrink the bar sample
            # below the quota, so collisions are dropped and counted.
            if ticker in seen:
                notes.append(
                    f"{underlying}: {ticker} already picked (the 30-60 DTE band holds "
                    f"{len(band)} expiry/expiries) — one fewer bar series than requested"
                )
                continue
            seen.add(ticker)
            picks.append((ticker, capture.as_of, expiration))
    return picks[:wanted], notes


def capture_bars(
    client: MassiveClient, picks: Sequence[tuple[str, date, date]], *, budget: Budget
) -> tuple[list[tuple[str, str]], list[str]]:
    """Daily bars over each contract's life inside the window."""
    files: list[tuple[str, str]] = []
    notes: list[str] = []
    for ticker, start, end in picks:
        path = AGGS_PATH_TEMPLATE.format(
            ticker=ticker, start=start.isoformat(), end=end.isoformat()
        )
        try:
            page = fetch_page(client, path, {"adjusted": "true"}, budget=budget)
        except (BudgetExhausted, MassiveError) as exc:
            notes.append(f"{ticker}: bars unavailable ({type(exc).__name__}: {exc})")
            if isinstance(exc, BudgetExhausted):
                break
            continue
        results = page.body.get("results") or ()
        if not results:
            notes.append(f"{ticker}: no prints between {start} and {end} -- not written")
            continue
        safe = ticker.replace(":", "_")
        files.append((f"{safe}.json", page.text + "\n"))
    return files, notes


def run_capture(client: MassiveClient, out_dir: Path, *, budget: Budget) -> dict[str, Any]:
    """Spot proxy, then contract masters, then bars -- writing as we go."""
    masters_dir = out_dir / "masters"
    bars_dir = out_dir / "bars"
    masters_dir.mkdir(parents=True, exist_ok=True)
    bars_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    # Bars are picked from the masters, so the masters must come first -- but
    # their budget must not eat the bar allocation.
    budget.reserve(BARS_WANTED)
    spot, spot_notes = capture_spot_proxy(client, UNDERLYINGS, AS_OF_DATES, budget=budget)
    notes.extend(spot_notes)

    captures: list[MasterCapture] = []
    for underlying in UNDERLYINGS:
        for as_of in AS_OF_DATES:
            try:
                capture = capture_master(
                    client, underlying, as_of, budget=budget, max_pages=PAGES_PER_MASTER
                )
            except BudgetExhausted as exc:
                notes.append(f"{underlying} {as_of}: not captured ({exc})")
                continue
            captures.append(capture)
            if capture.error is not None:
                notes.append(f"{underlying} {as_of}: {capture.error}")
                continue
            (masters_dir / capture.filename).write_text(master_envelope(capture), encoding="utf-8")

    # Second pass: spend what the first pass left, but only in COMPLETE
    # rounds. Deepening some (name, as_of) pairs and not others would make
    # the births/deaths series a measure of capture depth rather than of the
    # contract universe -- uniform truncation keeps the columns comparable.
    deepened = _deepen(client, captures, masters_dir, budget=budget)
    notes.extend(deepened)

    budget.release(BARS_WANTED)
    picks, pick_notes = choose_bar_contracts(captures, spot, wanted=BARS_WANTED)
    notes.extend(pick_notes)
    bar_files, bar_notes = capture_bars(client, picks, budget=budget)
    notes.extend(bar_notes)
    for name, text in bar_files:
        (bars_dir / name).write_text(text, encoding="utf-8")

    spot_path = out_dir / "spot_proxy.json"
    if spot:
        spot_path.write_text(json.dumps(spot, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    manifest: dict[str, Any] = {
        "capture_version": CAPTURE_VERSION,
        "provider": MASSIVE_PROVIDER,
        "budget_limit": budget.limit,
        "requests_charged": budget.spent,
        "client_stats": client.stats.snapshot(),
        "masters": [
            {
                "underlying": c.underlying,
                "as_of": c.as_of.isoformat(),
                "pages": len(c.pages),
                "rows": c.rows,
                "complete": not c.pending_next_url and c.error is None,
                "truncated": c.truncated,
                "error": c.error,
                "file": c.filename if c.pages else None,
            }
            for c in captures
        ],
        "bars": [name for name, _ in bar_files],
        "spot_proxy": spot,
        "spot_proxy_file": spot_path.name if spot else None,
        "notes": notes,
    }
    (out_dir / "capture_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _deepen(
    client: MassiveClient,
    captures: Sequence[MasterCapture],
    masters_dir: Path,
    *,
    budget: Budget,
) -> list[str]:
    """Add one page to EVERY still-truncated capture, or to none of them.

    A partial round would leave some (name, as_of) pairs one page deeper
    than others, and the births/deaths and universe-size columns would then
    partly measure how the budget happened to fall. Rounds are therefore
    all-or-nothing: if the remaining budget cannot cover every pending
    capture, the leftover is deliberately left unspent and said so.
    """
    notes: list[str] = []
    pending = [c for c in captures if c.pending_next_url and c.pages]
    rounds = 0
    while pending and budget.available >= len(pending):
        still: list[MasterCapture] = []
        for capture in pending:
            next_url = capture.pages[-1].body.get("next_url")
            if not (isinstance(next_url, str) and next_url):
                capture.pending_next_url = False
                capture.truncated = False
                continue
            path, params = client.split_url(next_url)
            try:
                page = fetch_page(client, path, params, budget=budget)
            except BudgetExhausted:  # pragma: no cover - the round was pre-checked
                still.append(capture)
                continue
            except MassiveError as exc:
                notes.append(f"{capture.underlying} {capture.as_of}: deepening failed ({exc})")
                continue
            capture.pages.append(page)
            more = page.body.get("next_url")
            capture.pending_next_url = bool(isinstance(more, str) and more)
            capture.truncated = capture.pending_next_url
            (masters_dir / capture.filename).write_text(master_envelope(capture), encoding="utf-8")
            if capture.pending_next_url:
                still.append(capture)
        rounds += 1
        pending = still
    if pending:
        notes.append(
            f"uniform-depth stop: {len(pending)} capture(s) still truncated after {rounds} "
            f"extra round(s); {budget.available} request(s) left unspent because a partial "
            "round would make the per-as_of columns incomparable"
        )
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out-dir", type=Path, required=True, help="gitignored capture directory")
    parser.add_argument("--budget", type=int, default=45, help="hard cap on LIVE requests")
    args = parser.parse_args(argv)

    budget = Budget(limit=args.budget)
    client = client_from_environment(max_pages=PAGES_PER_MASTER)
    try:
        manifest = run_capture(client, args.out_dir, budget=budget)
    except MassiveNotEntitledError as exc:
        print(f"NOT ENTITLED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
