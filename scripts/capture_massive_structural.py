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

Four disciplines are load-bearing here:

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
   wire request is spent from a shared quota. Each wire call is PRE-CHARGED
   its worst case (`backoff.max_attempts` -- one request plus every retry
   the backoff might burn) before the wire is touched and REFUNDED what it
   did not spend, so the cap bounds WIRE REQUESTS, not logical calls, and a
   call that cannot pay its worst case never goes out. A cache hit costs
   nothing, and exhausting the budget stops the run cleanly with everything
   captured so far written to disk.
4. **The manifest always lands.** The run body is wrapped so that a crash,
   a rejected key, or a budget stop still writes `capture_manifest.json`
   pinning whatever actually reached the out dir (`massive_manifest`
   refuses a manifest the disk disagrees with).

The API key is never logged, never written into a capture, and never placed
in this file: it comes from the environment or the 0600 key file via
`massive_client.load_api_key`, and the cache redacts it out of every stored
body before this script ever reads one.

Usage (writes gitignored captures under `artifacts/`; the full CLI profile
and the exit-code contract live in `docs/m4-massive-runbook.md`):

    PYTHONPATH=src python scripts/capture_massive_structural.py \
        --underlyings SPY,TSLA --as-of 2025-03-14 --budget 45 \
        --out-dir artifacts/m4b-captures
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
    MassiveAuthRejectedError,
    MassiveClient,
    MassiveError,
    MassiveNotEntitledError,
    cache_key_for,
    client_from_environment,
)
from tree_options.data.massive_manifest import (  # noqa: E402
    CAPTURE_MANIFEST_FILENAME,
    MassiveCaptureManifest,
    build_massive_capture_manifest,
)
from tree_options.data.massive_options import (  # noqa: E402
    AGGS_PATH_TEMPLATE,
    CONTRACTS_PATH,
    MASSIVE_PROVIDER,
    session_of_epoch_ms,
)
from tree_options.time.monthlies import is_monthly_expiry  # noqa: E402

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
# Bar-selection profile. The defaults are the original representative picks:
# nothing about an unflagged run changes when the atm-grid flags exist.
BARS_MODE = "representative"
BARS_STRIKE_BAND = 3
BARS_EXPIRIES = "all"
BARS_SIDES = "both"


class BudgetExhausted(RuntimeError):
    """The request budget ran out. Everything captured so far is still good."""


@dataclass
class Budget:
    """Hard cap on LIVE wire requests, pre-charged and refunded.

    The cap bounds WIRE REQUESTS, not logical calls. `charge_block` pays a
    call's WORST case (`backoff.max_attempts` -- one request plus every
    retry the backoff policy might burn) before the wire is touched, so a
    call that cannot pay its worst case never goes out; `refund` hands back
    the attempts the call did not spend the moment it returns. A call that
    RAISES forfeits the difference, so the budget is an upper bound on wire
    requests even on the failure paths.

    The one cost charged AFTER the wire is the cache self-heal: a "hit"
    whose cached body no longer decodes refetches from the wire inside the
    client, and this bridge only learns of it by comparing the client's
    request counter across the call. Pre-detecting it here would mean
    decoding every cached body a second time on every fetch (once to check,
    once inside the client to use) -- the post-hoc charge is the cheaper
    side of that trade-off, and it stays bounded at `max_attempts` because
    one client call can never attempt more than that. A clean cache hit is
    free and is never charged.
    """

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

    def charge_block(self, what: str, blocks: int) -> None:
        """Pre-pay `blocks` wire requests, or refuse the call outright.

        One log line per block, so the log audits against the client's own
        request counter."""
        if self.available < blocks:
            raise BudgetExhausted(
                f"request budget {self.limit} exhausted (spent {self.spent},"
                f" reserved {self.reserved}) before {what}:"
                f" needs {blocks}, has {self.available}"
            )
        self.spent += blocks
        self.log.extend([what] * blocks)

    def refund(self, what: str, unused: int) -> None:
        """Return pre-charged blocks the call never spent, as one log line."""
        if unused < 1:
            return
        self.spent -= unused
        self.log.append(f"{what} [refund {unused}]")

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
    client: MassiveClient,
    path: str,
    params: Mapping[str, Any],
    *,
    budget: Budget,
    dry_run: bool = False,
) -> CapturedPage:
    """One page, charged to the budget only if it actually hits the wire.

    The verbatim text is read back out of the client's cache, which stores
    the response bytes with the API key redacted. That is the only way to
    keep the vendor's exact number tokens: the decoded body has already
    turned them into `Decimal`s and re-encoding would not round-trip.

    A miss is PRE-CHARGED the call's worst case (`max_attempts`) and
    refunded the difference, so the budget caps wire requests rather than
    calls; a call that raises forfeits the difference. In `dry_run` a miss
    never reaches the wire at all -- it raises `BudgetExhausted`, which the
    per-item handlers turn into a manifest note.
    """
    if client.cache is None:  # pragma: no cover - the CLI always caches
        raise RuntimeError("capture requires the response cache (verbatim bodies live there)")
    key = cache_key_for(path, params)
    what = client.display_url(path, params)
    hit = client.cache.get(key) is not None
    if not hit and dry_run:
        raise BudgetExhausted(f"dry-run: {what} not in cache")
    if not hit:
        budget.charge_block(what, blocks=client.backoff.max_attempts)
    wire_before = client.stats.requests
    body = client.get_json(path, params)
    wire_requests = client.stats.requests - wire_before
    assert wire_requests <= client.backoff.max_attempts, (what, wire_requests)
    if not hit:
        budget.refund(what, client.backoff.max_attempts - wire_requests)
    elif wire_requests:
        # A "hit" that self-healed: the cached body no longer decoded, the
        # client discarded it and refetched. The cost can only be charged
        # after the fact -- see Budget's docstring for the trade-off.
        budget.charge_block(f"{what} [self-heal refetch]", blocks=wire_requests)
    raw = client.cache.get(key)
    if raw is None:  # pragma: no cover - put() failed, which would have raised
        raise RuntimeError(f"{what}: body not cached")
    return CapturedPage(body=body, text=raw.decode("utf-8").strip(), from_cache=hit)


def capture_master(
    client: MassiveClient,
    underlying: str,
    as_of: date,
    *,
    budget: Budget,
    max_pages: int,
    dry_run: bool = False,
) -> MasterCapture:
    """Walk `next_url` up to `max_pages`, then stop and say so.

    Stopping leaves `next_url` in place on the last captured page, which is
    precisely how the inspector detects an incomplete capture. Nothing here
    trims or rewrites a page. A rejected key is terminal and propagates; a
    pagination refusal (a `next_url` on a foreign host) becomes this
    capture's `error` rather than aborting the whole run.
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
            page = fetch_page(client, path, params, budget=budget, dry_run=dry_run)
        except BudgetExhausted:
            if capture.pages:
                capture.truncated = True
                capture.pending_next_url = True
                return capture
            raise
        except MassiveAuthRejectedError:
            raise
        except MassiveError as exc:
            capture.error = f"{type(exc).__name__}: {exc}"
            return capture
        capture.pages.append(page)
        next_url = page.body.get("next_url")
        if not (isinstance(next_url, str) and next_url):
            return capture
        try:
            # Inside a guard: a foreign-host cursor must refuse THIS capture
            # -- keeping every page already fetched and paid for -- rather
            # than aborting the whole run.
            path, params = client.split_url(next_url)
        except MassiveError as exc:
            capture.error = f"{type(exc).__name__}: {exc}"
            return capture


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
    client: MassiveClient,
    underlyings: Sequence[str],
    as_ofs: Sequence[date],
    *,
    budget: Budget,
    dry_run: bool = False,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Underlying closes on the as_of dates, for strike-grid-in-spot-units.

    One equity aggregate call per name covers every as_of. Not being
    entitled is recorded and the metric degrades to NOT_EVALUABLE in the
    report -- it is never guessed. A row too malformed to read is skipped
    and counted into a note: never a crash, never silent.

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
            page = fetch_page(client, path, {"adjusted": "false"}, budget=budget, dry_run=dry_run)
        except MassiveAuthRejectedError:
            raise
        except (BudgetExhausted, MassiveError) as exc:
            notes.append(f"{underlying}: spot proxy unavailable ({type(exc).__name__}: {exc})")
            continue
        closes: dict[str, str] = {}
        malformed = 0
        for row in page.body.get("results") or ():
            if not isinstance(row, Mapping):
                malformed += 1
                continue
            try:
                session = session_of_epoch_ms(int(row["t"])).isoformat()
                close = _plain(_as_decimal(row["c"], f"{underlying} {session}.c"))
            except (KeyError, TypeError, ValueError):
                malformed += 1
                continue
            if session in wanted:
                closes[session] = close
        if closes:
            proxy[underlying] = dict(sorted(closes.items()))
        if malformed:
            notes.append(f"{underlying}: {malformed} malformed row(s) skipped")
        missing = sorted(wanted - set(closes))
        if missing:
            notes.append(
                f"{underlying}: no session close on {', '.join(missing)} (not a trading day)"
            )
    return proxy, notes


def _band_expirations(
    rows: Sequence[Mapping[str, Any]], as_of: date, *, dte_min: int, dte_max: int
) -> tuple[list[date], int]:
    """In-band expirations, plus how many rows were too malformed to read.

    A row that cannot yield a date is skipped and counted, never a crash and
    never silent: the caller turns the count into a manifest note.
    """
    expirations: set[date] = set()
    malformed = 0
    for row in rows:
        try:
            expirations.add(date.fromisoformat(str(row["expiration_date"])))
        except (KeyError, TypeError, ValueError):
            malformed += 1
    band = sorted(e for e in expirations if dte_min <= (e - as_of).days <= dte_max)
    return band, malformed


def choose_bar_contracts(
    captures: Sequence[MasterCapture],
    spot: Mapping[str, Mapping[str, str]],
    *,
    wanted: int,
    dte_min: int | None = None,
    dte_max: int | None = None,
) -> tuple[list[tuple[str, date, date]], list[str]]:
    """Deterministically pick liquid, representative contracts to price.

    At-the-money call+put on the nearest in-band expiry (`dte_min`..`dte_max`
    DTE) plus an ATM call on the far end of the band, per underlying. ATM is
    chosen against the spot proxy when it exists and against the ladder's
    median strike when it does not -- deep-OTM picks would return empty
    series, and the inspector (rightly) refuses an empty series rather than
    reporting zero coverage. Malformed rows are skipped and counted into
    notes, never a crash and never silent.
    """
    dte_min = DTE_MIN if dte_min is None else dte_min
    dte_max = DTE_MAX if dte_max is None else dte_max
    picks: list[tuple[str, date, date]] = []
    notes: list[str] = []
    seen: set[str] = set()
    by_underlying: dict[str, MasterCapture] = {}
    for capture in sorted(captures, key=lambda c: (c.underlying, c.as_of)):
        if capture.pages and capture.underlying not in by_underlying:
            by_underlying[capture.underlying] = capture

    for underlying, capture in sorted(by_underlying.items()):
        rows = [r for page in capture.pages for r in (page.body.get("results") or ())]
        band, malformed = _band_expirations(rows, capture.as_of, dte_min=dte_min, dte_max=dte_max)
        if malformed:
            notes.append(f"{underlying}: {malformed} malformed row(s) skipped")
        if not band:
            notes.append(
                f"{underlying}: no {dte_min}-{dte_max} DTE expiry in the {capture.as_of} master"
            )
            continue
        as_of_key = capture.as_of.isoformat()
        reference = spot.get(underlying, {}).get(as_of_key)
        targets = [(band[0], "call"), (band[0], "put"), (band[-1], "call")]
        for expiration, kind in targets:
            ladder: list[tuple[Decimal, str]] = []
            skipped = 0
            for r in rows:
                try:
                    if date.fromisoformat(str(r["expiration_date"])) != expiration:
                        continue
                    if str(r["contract_type"]) != kind:
                        continue
                    ladder.append(
                        (
                            _as_decimal(r["strike_price"], f"{r['ticker']}.strike_price"),
                            str(r["ticker"]),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    skipped += 1
                    continue
            if skipped:
                notes.append(f"{underlying} {expiration}: {skipped} malformed row(s) skipped")
            ladder.sort()
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
                    f"{underlying}: {ticker} already picked (the {dte_min}-{dte_max} DTE band "
                    f"holds {len(band)} expiry/expiries) — one fewer bar series than requested"
                )
                continue
            seen.add(ticker)
            picks.append((ticker, capture.as_of, expiration))
    return picks[:wanted], notes


def select_atm_grid_bars(
    captures: Sequence[MasterCapture],
    spot: Mapping[str, Mapping[str, str]],
    *,
    wanted: int,
    dte_min: int | None = None,
    dte_max: int | None = None,
    strike_band: int | None = None,
    expiries: str | None = None,
    sides: str | None = None,
) -> tuple[list[tuple[str, date, date]], list[str]]:
    """Deterministically pick an ATM strike GRID per (underlying, as_of) master.

    Unlike `choose_bar_contracts` (three representative contracts per
    underlying, from the FIRST capture that has pages), the grid covers EVERY
    master: expiries inside the DTE band, optionally only third-Friday
    monthlies (`expiries="monthly"`, via `tree_options.time.monthlies`), and
    per expiry the `strike_band` distinct strikes above and below spot plus
    spot itself — RANKED by |strike - spot| ascending with ties broken by
    strike then ticker, which is a rank in the ladder, never an absolute
    strike-distance range. `sides="call"` halves the grid to calls only.

    The spot anchor is the in-memory spot map captured earlier in the run: a
    master whose (underlying, as_of) has no close is NOTED and SKIPPED. The
    representative chooser falls back to the median strike, but a grid
    anchored on a guess would misdescribe "at-the-money" for every strike it
    named, so here the fallback is refusal. A master with no pages was never
    captured (the run already noted why) and is skipped silently.

    Selection is pure and deterministic: masters in (underlying, as_of)
    order, expiries ascending, strikes in rank order, calls before puts. It
    is deduped across the WHOLE run by ticker — a contract's series is
    fetched once per run, PER CONTRACT LIFE, which is the cost model — and
    `wanted` still caps TOTAL series, truncating to the deterministic prefix
    and saying so. Malformed rows are skipped and counted into notes, never a
    crash and never silent.
    """
    dte_min = DTE_MIN if dte_min is None else dte_min
    dte_max = DTE_MAX if dte_max is None else dte_max
    strike_band = BARS_STRIKE_BAND if strike_band is None else strike_band
    expiries = BARS_EXPIRIES if expiries is None else expiries
    sides = BARS_SIDES if sides is None else sides
    if expiries not in ("all", "monthly"):
        raise ValueError(f"unknown expiries filter {expiries!r} (want 'all' or 'monthly')")
    if sides not in ("call", "both"):
        raise ValueError(f"unknown sides filter {sides!r} (want 'call' or 'both')")
    if strike_band < 0:
        raise ValueError(f"strike_band must be >= 0, got {strike_band}")
    kinds = ("call",) if sides == "call" else ("call", "put")

    picks: list[tuple[str, date, date]] = []
    notes: list[str] = []
    seen: set[str] = set()
    duplicates = 0
    for capture in sorted(captures, key=lambda c: (c.underlying, c.as_of)):
        if not capture.pages:
            continue
        underlying = capture.underlying
        rows = [r for page in capture.pages for r in (page.body.get("results") or ())]
        band, malformed = _band_expirations(rows, capture.as_of, dte_min=dte_min, dte_max=dte_max)
        if malformed:
            notes.append(f"{underlying}: {malformed} malformed row(s) skipped")
        if not band:
            notes.append(
                f"{underlying}: no {dte_min}-{dte_max} DTE expiry in the {capture.as_of} master"
            )
            continue
        if expiries == "monthly":
            band = [e for e in band if is_monthly_expiry(e)]
            if not band:
                notes.append(
                    f"{underlying} {capture.as_of}: no monthly expiry in the "
                    f"{dte_min}-{dte_max} DTE band"
                )
                continue
        reference = spot.get(underlying, {}).get(capture.as_of.isoformat())
        if reference is None:
            notes.append(
                f"{underlying} {capture.as_of}: no spot close — ATM grid skipped "
                "(a median-strike fallback would misdescribe at-the-money)"
            )
            continue
        anchor = Decimal(reference)
        for expiration in band:
            ladder: list[tuple[Decimal, str, str]] = []  # (strike, kind, ticker)
            skipped = 0
            for r in rows:
                try:
                    if date.fromisoformat(str(r["expiration_date"])) != expiration:
                        continue
                    kind = str(r["contract_type"])
                    if kind not in kinds:
                        continue
                    ladder.append(
                        (
                            _as_decimal(r["strike_price"], f"{r['ticker']}.strike_price"),
                            kind,
                            str(r["ticker"]),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    skipped += 1
                    continue
            if skipped:
                notes.append(f"{underlying} {expiration}: {skipped} malformed row(s) skipped")
            if not ladder:
                continue
            by_strike: dict[Decimal, list[tuple[Decimal, str, str]]] = {}
            for item in ladder:
                by_strike.setdefault(item[0], []).append(item)
            # ATM +/- band: rank DISTINCT strikes by distance from spot (ties
            # by strike ascending) and keep the closest 2*band+1 of them.
            ranked = sorted(by_strike, key=lambda s: (abs(s - anchor), s))
            for strike in ranked[: 2 * strike_band + 1]:
                # Within a strike, calls before puts, then ticker.
                for _, _kind, ticker in sorted(by_strike[strike], key=lambda it: (it[1], it[2])):
                    if ticker in seen:
                        duplicates += 1
                        continue
                    seen.add(ticker)
                    picks.append((ticker, capture.as_of, expiration))
    if duplicates:
        notes.append(
            f"atm-grid: {duplicates} duplicate selection(s) deduped — a contract's "
            "series is fetched once per run (per contract life)"
        )
    if len(picks) > wanted:
        notes.append(
            f"atm-grid: --bars {wanted} caps the selection to {wanted} of {len(picks)} "
            "series (deterministic prefix)"
        )
        picks = picks[:wanted]
    return picks, notes


def capture_bars(
    client: MassiveClient,
    picks: Sequence[tuple[str, date, date]],
    *,
    budget: Budget,
    dry_run: bool = False,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Daily bars over each contract's life inside the window."""
    files: list[tuple[str, str]] = []
    notes: list[str] = []
    for ticker, start, end in picks:
        path = AGGS_PATH_TEMPLATE.format(
            ticker=ticker, start=start.isoformat(), end=end.isoformat()
        )
        try:
            page = fetch_page(client, path, {"adjusted": "true"}, budget=budget, dry_run=dry_run)
        except MassiveAuthRejectedError:
            raise
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


def build_manifest(
    client: MassiveClient,
    out_dir: Path,
    *,
    budget: Budget,
    captures: Sequence[MasterCapture],
    bar_files: Sequence[tuple[str, str]],
    spot: Mapping[str, Mapping[str, str]],
    notes: Sequence[str],
) -> MassiveCaptureManifest:
    """Assemble the capture manifest from what is ACTUALLY on disk.

    The file listing comes from the directory scan inside
    `build_massive_capture_manifest` (every `masters/*.json`, `bars/*.json`
    and `spot_proxy.json` in `out_dir`), never from this bridge's own
    bookkeeping, so `files[]` and `masters[].file` agree with the disk by
    construction -- including on a crashed or half-budgeted run.
    """
    client_stats = dict(client.stats.snapshot())
    # The bridge walks pages itself (it never calls `paginate`), so the
    # client's own pages_fetched is always 0: this is the bridge-level truth.
    client_stats["pages_fetched"] = sum(len(c.pages) for c in captures)
    return build_massive_capture_manifest(
        out_dir,
        capture_version=CAPTURE_VERSION,
        budget_limit=budget.limit,
        requests_charged=budget.spent,
        client_stats=client_stats,
        masters=[
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
        bars=[name for name, _ in bar_files],
        spot_proxy=spot,
        notes=list(notes),
    )


def run_capture(
    client: MassiveClient,
    out_dir: Path,
    *,
    budget: Budget,
    underlyings: Sequence[str] | None = None,
    as_ofs: Sequence[date] | None = None,
    pages_per_master: int | None = None,
    bars_wanted: int | None = None,
    dte_min: int | None = None,
    dte_max: int | None = None,
    bars_mode: str | None = None,
    bars_strike_band: int | None = None,
    bars_expiries: str | None = None,
    bars_sides: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Spot proxy, then contract masters, then bars -- writing as we go.

    The manifest is written in a `finally`: a crash, a rejected key, or a
    budget stop still leaves `capture_manifest.json` pinning everything that
    DID land. Omitted profile arguments fall back to this module's globals,
    which is what keeps the CLI defaults and the test monkeypatches on one
    source of truth. `bars_mode` picks the bar strategy: "representative"
    (the default, unchanged) or the "atm-grid" grid of `select_atm_grid_bars`.
    """
    underlyings = UNDERLYINGS if underlyings is None else tuple(underlyings)
    as_ofs = AS_OF_DATES if as_ofs is None else tuple(as_ofs)
    pages_per_master = PAGES_PER_MASTER if pages_per_master is None else pages_per_master
    bars_wanted = BARS_WANTED if bars_wanted is None else bars_wanted
    dte_min = DTE_MIN if dte_min is None else dte_min
    dte_max = DTE_MAX if dte_max is None else dte_max
    bars_mode = BARS_MODE if bars_mode is None else bars_mode
    if bars_mode not in ("representative", "atm-grid"):
        raise ValueError(f"unknown bars_mode {bars_mode!r}")
    bars_strike_band = BARS_STRIKE_BAND if bars_strike_band is None else bars_strike_band
    bars_expiries = BARS_EXPIRIES if bars_expiries is None else bars_expiries
    bars_sides = BARS_SIDES if bars_sides is None else bars_sides

    masters_dir = out_dir / "masters"
    bars_dir = out_dir / "bars"
    masters_dir.mkdir(parents=True, exist_ok=True)
    bars_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    captures: list[MasterCapture] = []
    bar_files: list[tuple[str, str]] = []
    spot: dict[str, dict[str, str]] = {}

    try:
        # Bars are picked from the masters, so the masters must come first -- but
        # their budget must not eat the bar allocation.
        budget.reserve(bars_wanted)
        spot, spot_notes = capture_spot_proxy(
            client, underlyings, as_ofs, budget=budget, dry_run=dry_run
        )
        notes.extend(spot_notes)
        if spot:
            # Written as soon as it exists, not at the end: a crash later in
            # the run must not strand the spot data in memory only.
            (out_dir / "spot_proxy.json").write_text(
                json.dumps(spot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

        for underlying in underlyings:
            for as_of in as_ofs:
                try:
                    capture = capture_master(
                        client,
                        underlying,
                        as_of,
                        budget=budget,
                        max_pages=pages_per_master,
                        dry_run=dry_run,
                    )
                except BudgetExhausted as exc:
                    notes.append(f"{underlying} {as_of}: not captured ({exc})")
                    continue
                captures.append(capture)
                if capture.error is not None:
                    notes.append(f"{underlying} {as_of}: {capture.error}")
                # Pages already fetched are real captured data even when the
                # walk then failed: the envelope is written whenever any page
                # exists, and the manifest records the error alongside.
                if capture.pages:
                    (masters_dir / capture.filename).write_text(
                        master_envelope(capture), encoding="utf-8"
                    )

        # Second pass: spend what the first pass left, but only in COMPLETE
        # rounds. Deepening some (name, as_of) pairs and not others would make
        # the births/deaths series a measure of capture depth rather than of the
        # contract universe -- uniform truncation keeps the columns comparable.
        deepened = _deepen(client, captures, masters_dir, budget=budget, dry_run=dry_run)
        notes.extend(deepened)

        budget.release(bars_wanted)
        if bars_mode == "atm-grid":
            picks, pick_notes = select_atm_grid_bars(
                captures,
                spot,
                wanted=bars_wanted,
                dte_min=dte_min,
                dte_max=dte_max,
                strike_band=bars_strike_band,
                expiries=bars_expiries,
                sides=bars_sides,
            )
        else:
            picks, pick_notes = choose_bar_contracts(
                captures, spot, wanted=bars_wanted, dte_min=dte_min, dte_max=dte_max
            )
        notes.extend(pick_notes)
        bar_files, bar_notes = capture_bars(client, picks, budget=budget, dry_run=dry_run)
        notes.extend(bar_notes)
        for name, text in bar_files:
            (bars_dir / name).write_text(text, encoding="utf-8")
    finally:
        manifest = build_manifest(
            client,
            out_dir,
            budget=budget,
            captures=captures,
            bar_files=bar_files,
            spot=spot,
            notes=notes,
        )
        (out_dir / CAPTURE_MANIFEST_FILENAME).write_text(
            manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    return manifest.model_dump(mode="json")


def _deepen(
    client: MassiveClient,
    captures: Sequence[MasterCapture],
    masters_dir: Path,
    *,
    budget: Budget,
    dry_run: bool = False,
) -> list[str]:
    """Add one page to EVERY still-truncated capture, or to none of them.

    A partial round would leave some (name, as_of) pairs one page deeper
    than others, and the births/deaths and universe-size columns would then
    partly measure how the budget happened to fall. Rounds are therefore
    all-or-nothing: a round only starts when the remaining budget can
    PRE-CHARGE every pending capture's worst case, and a round the budget
    cannot cover in full is deliberately left unspent and said so.
    """
    notes: list[str] = []
    pending = [c for c in captures if c.pending_next_url and c.pages]
    rounds = 0
    while pending and budget.available >= len(pending) * client.backoff.max_attempts:
        still: list[MasterCapture] = []
        deepened = 0
        exhausted = False
        for capture in pending:
            next_url = capture.pages[-1].body.get("next_url")
            if not (isinstance(next_url, str) and next_url):
                capture.pending_next_url = False
                capture.truncated = False
                continue
            try:
                path, params = client.split_url(next_url)
                page = fetch_page(client, path, params, budget=budget, dry_run=dry_run)
            except BudgetExhausted:
                # Budget mode pre-checks the round, so this is reachable only
                # when a self-healed "hit" spent post-hoc past the pre-check,
                # or in --dry-run, where a miss raises without spending
                # anything. Either way the walk must stop on the flag below:
                # the balance may never move, and the while guard alone would
                # not terminate.
                still.append(capture)
                exhausted = True
                continue
            except MassiveAuthRejectedError:
                raise
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
            deepened += 1
        rounds += 1
        pending = still
        if exhausted and deepened == 0:
            break
    if pending:
        notes.append(
            f"uniform-depth stop: {len(pending)} capture(s) still truncated after {rounds} "
            f"extra round(s); {budget.available} request(s) left unspent because a partial "
            "round would make the per-as_of columns incomparable"
        )
    return notes


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--as-of wants an ISO date (YYYY-MM-DD), got {value!r}"
        ) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out-dir", type=Path, required=True, help="gitignored capture directory")
    parser.add_argument(
        "--budget", type=int, default=45, help="hard cap on live wire requests (must be > 0)"
    )
    parser.add_argument("--underlyings", default=None, help="comma-separated underlying tickers")
    parser.add_argument(
        "--as-of",
        action="append",
        type=_iso_date,
        metavar="ISO-DATE",
        help="repeatable as_of date; each (underlying, as_of) is one contract master",
    )
    parser.add_argument(
        "--pages-per-master", type=int, help="initial page cap per master walk (default: 4)"
    )
    parser.add_argument("--bars", type=int, help="how many daily bar series to pull (default: 6)")
    parser.add_argument(
        "--dte-min", type=int, help="lower edge of the DTE band for bar picks (default: 30)"
    )
    parser.add_argument(
        "--dte-max", type=int, help="upper edge of the DTE band for bar picks (default: 60)"
    )
    parser.add_argument(
        "--bars-mode",
        choices=("representative", "atm-grid"),
        help=(
            "bar selection strategy (default: representative). atm-grid ranks each master's "
            "in-band strikes by |strike - spot| and pulls the ATM +/- --bars-strike-band "
            "distinct strikes per expiry on the --bars-sides sides; a contract's series is "
            "fetched once per run (per-contract-LIFE cost), so --bars caps TOTAL series"
        ),
    )
    parser.add_argument(
        "--bars-strike-band",
        type=int,
        help=(
            "atm-grid only: distinct strikes above and below ATM per expiry "
            "(default: 3; a rank in the ladder, NOT an absolute strike range). Each strike "
            "adds one series per side per contract life"
        ),
    )
    parser.add_argument(
        "--bars-expiries",
        choices=("all", "monthly"),
        help=(
            "atm-grid only: keep every in-band expiry or only third-Friday monthlies "
            "(default: all). Each expiry multiplies the grid, one series per contract life"
        ),
    )
    parser.add_argument(
        "--bars-sides",
        choices=("call", "both"),
        help=(
            "atm-grid only: calls alone or calls and puts (default: both). Each side is "
            "one more series per strike per contract life"
        ),
    )
    parser.add_argument(
        "--cache-dir", type=Path, help="response cache location (default: artifacts/massive-cache)"
    )
    parser.add_argument("--timeout", type=float, help="per-request HTTP timeout in seconds")
    parser.add_argument("--max-pages", type=int, help="hard pagination guard per wire walk")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="cache-only: a miss is noted, never fetched from the wire",
    )
    args = parser.parse_args(argv)
    if args.budget <= 0:
        parser.error(f"--budget must be > 0, got {args.budget}")
    if args.bars_strike_band is not None and args.bars_strike_band < 0:
        parser.error(f"--bars-strike-band must be >= 0, got {args.bars_strike_band}")

    # Defaults come from the module globals so the CLI profile and the test
    # monkeypatches share one source of truth.
    underlyings = tuple(filter(None, (args.underlyings or "").split(","))) or UNDERLYINGS
    as_ofs = tuple(args.as_of or ()) or AS_OF_DATES
    client_kwargs: dict[str, Any] = {}
    if args.cache_dir is not None:
        client_kwargs["cache_dir"] = args.cache_dir
    if args.timeout is not None:
        client_kwargs["timeout"] = args.timeout
    if args.max_pages is not None:
        client_kwargs["max_pages"] = args.max_pages

    budget = Budget(limit=args.budget)
    client = client_from_environment(**client_kwargs)
    try:
        manifest = run_capture(
            client,
            args.out_dir,
            budget=budget,
            underlyings=underlyings,
            as_ofs=as_ofs,
            pages_per_master=args.pages_per_master,
            bars_wanted=args.bars,
            dte_min=args.dte_min,
            dte_max=args.dte_max,
            bars_mode=args.bars_mode,
            bars_strike_band=args.bars_strike_band,
            bars_expiries=args.bars_expiries,
            bars_sides=args.bars_sides,
            dry_run=args.dry_run,
        )
    except MassiveNotEntitledError as exc:
        print(f"NOT ENTITLED: {exc}", file=sys.stderr)
        return 2
    except MassiveAuthRejectedError as exc:
        print(f"AUTH REJECTED: {exc}", file=sys.stderr)
        return 3
    except BudgetExhausted as exc:
        # run_capture's finally has already written the manifest.
        print(f"BUDGET EXHAUSTED: {exc}", file=sys.stderr)
        return 4

    print(json.dumps(manifest, indent=2, sort_keys=True))
    masters = manifest["masters"]
    complete = sum(1 for m in masters if m["complete"])
    if masters and complete < len(masters):
        print(f"PARTIAL: {complete}/{len(masters)} masters complete", file=sys.stderr)
    if not (any(m["pages"] for m in masters) or manifest["bars"] or manifest["spot_proxy"]):
        print("NOTHING CAPTURED: no master pages, bars, or spot entries landed", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
