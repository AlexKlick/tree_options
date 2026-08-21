# M4-B — Massive (Polygon) free tier: live structural coverage

- date: 2026-08-21, branch `main` (uncommitted integration tree)
- vendor: Polygon, branded **Massive**; endpoints still `api.polygon.io`.
  Provider tag `massive-polygon-free/1`, tier `massive-free/options-basic`.
- stack under test: `scripts/capture_massive_structural.py` (live capture) →
  `tree_options.data.massive_client` (WS-D1 wire) → gitignored capture files →
  `scripts/inspect_structural_coverage.py` (WS-D2 analysis).
- machine copy: `m4-b-massive-structural.json` (same run; derived numbers only —
  see §6).
- **secrets**: the API key is read only by `massive_client.load_api_key`
  (`POLYGON_API_KEY`, else the 0600 key file). It appears in no artifact, no
  test, no cache filename and no cached body — the cache redacts on write.

## 0. Cross-workstream reconciliation

The two lanes were built independently and met here. Two drifts, both real,
both fixed with the fewest symbols and on the consumer side wherever the
choice existed.

**D-1 — the lanes did not compose at all (structural).** WS-D1's
`MassiveClient.paginate()` returns the **flattened** records across pages.
WS-D2's `parse_contract_master()` wants the vendor's **own page bodies**
inside a `{"as_of": ..., "pages": [...]}` envelope, because `next_url` on the
last page is precisely how it decides a capture is incomplete. Nothing in
either lane produced that envelope, so no capture WS-D1 could take was
readable by WS-D2.

Fixed by adding the consumer, not by moving either public API:
`scripts/capture_massive_structural.py` drives `get_json()` + `split_url()`
itself and assembles the envelope. Neither lane's exported names changed.

Two properties of that adapter are load-bearing and tested:

- The envelope is assembled as **text** from the bytes the client cached, never
  by re-serialising a decoded body. Re-encoding would route `587.5` through a
  float and quietly turn WS-D1's *provable* exactness into an approximation.
  (`test_the_vendor_number_token_survives_into_the_capture`.)
- Stopping at the page cap **leaves `next_url` in place**. WS-D1's
  `paginate()` raises on a pending `next_url` rather than truncate; that is
  right for a caller that wants all-or-nothing, but this lane is explicitly
  budget-bounded, so it stops and lets the dangling cursor carry the
  truncation into the report as `capture_complete=false`. Truncation is
  therefore recorded by construction, never silent.
  (`test_the_page_cap_leaves_next_url_so_truncation_is_visible`.)

**D-2 — provenance was anonymous (one symbol).** WS-D2's `adapter_status()`
probes the adapter module for `MASSIVE_SCHEMA_VERSION` / `MASSIVE_PROVIDER` /
`__version__` and reported a bare `PRESENT`, because `massive_options` imported
only `MassiveError` from `massive_client`. Adapter identity belongs on the
adapter module, so `massive_options` now re-exports `MASSIVE_PROVIDER` (one
import + one `__all__` entry, purely additive). The report line now reads
`PRESENT (massive-polygon-free/1)` — the artifact names the provider that
produced it.

No other drift: WS-D2 had already been written against the probed vendor key
shapes, and every field WS-D1 parses was accepted by WS-D2 unchanged
(`unknown_result_keys: []` over all 34,884 rows — zero schema drift).

## 1. Requests consumed

**44 live requests consumed of a 45 budget; 1 left unspent.** Independently
confirmed: `artifacts/massive-cache/` holds exactly 44 entries, and the cache
is content-addressed by (path, sorted params) with every entry written on a
miss that reached the wire.

| run | purpose | requests | cache hits | governor sleeps | slept (s) | 429 retries |
|---|---|---|---|---|---|---|
| main capture | spot proxy (2), 8 masters (32), bars (5) | 39 | 1 | 38 | 410.99 | 0 |
| residual A | deepen 3 truncated TSLA masters | 4 | 12 | 3 | 33.34 | 0 |
| residual B | finish TSLA 2026-03-16 | 1 | 6 | 0 | 0.00 | 0 |
| **total** | | **44** | **19** | **41** | **444.33** | **0** |

The 5 req/min governor was never violated and the vendor never answered 429.
Nineteen page reads were served free from the cache, which is what made the
two residual passes cost 5 requests instead of 44.

## 2. Capture inventory and TRUNCATIONS

Protocol: `underlying_ticker=X&as_of=YYYY-MM-DD&limit=1000`, `next_url`
followed, initial cap 4 pages per (name, as_of).

| underlying | as_of | pages | rows | complete? |
|---|---|---|---|---|
| SPY | 2024-09-16 | 4 | 4000 | **NO — TRUNCATED** |
| SPY | 2025-03-14 | 4 | 4000 | **NO — TRUNCATED** |
| SPY | 2025-09-15 | 4 | 4000 | **NO — TRUNCATED** |
| SPY | 2026-03-16 | 4 | 4000 | **NO — TRUNCATED** |
| TSLA | 2024-09-16 | 4 | 3194 | yes |
| TSLA | 2025-03-14 | 5 | 4948 | yes |
| TSLA | 2025-09-15 | 5 | 4632 | yes |
| TSLA | 2026-03-16 | 7 | 6110 | yes |

Residual budget was spent on TSLA rather than SPY because TSLA was within a
page or two of complete (2024-09-16 finished inside the original cap at 3194
rows), so 5 requests bought a **fully complete 4-point TSLA series**. The same
5 requests spread over SPY would have left all four SPY captures truncated and
merely moved the cut.

Deepening is all-or-nothing per round: a partial round would leave some
(name, as_of) pairs a page deeper than others, and universe size and
births/deaths would then partly measure how the budget fell rather than the
contract universe. The main run therefore left 5 requests unspent rather than
deepen 5 of 8 captures, and said so in its manifest.

### 2.1 READ THIS BEFORE USING THE SPY ROWS

The SPY captures are a **nearest-expiry prefix, not a sample.** Vendor results
are ordered by OCC ticker, which sorts root → `YYMMDD` → C/P → strike, so a
truncated capture is the *N earliest-expiring contracts*, not a cross-section.
The report proves it: at `as_of` 2024-09-16 the 4,000 SPY rows cover expiries
2024-09-16 … 2024-10-18 only (14 expiries, farthest 32 DTE) and the tenor
histogram reads `dte_61_180 = dte_181_365 = dte_gt_365 = 0` — SPY unquestionably
lists LEAPS, so those zeros are the cut, not the market.

Two consequences that must not be misread:

- **SPY births = 4000 / deaths = 4000 at every transition is arithmetic on
  disjoint nearest-expiry windows, not contract turnover.** SPY's
  `full_span = 0` (no contract seen at all four `as_of`s) says the same thing.
  Use the TSLA lifecycle numbers; discard SPY's.
- **`dte_30_60 = 0` for SPY at three of four `as_of`s does NOT mean SPY has no
  30–60 DTE expiries.** The prefix ends before reaching them (farthest DTE
  28 / 15 / 15 days respectively).

The SPY strike ladders, exercise-style, `shares_per_contract` and `cfi`
distributions remain valid *for the covered expiries* — they are per-row
facts, not universe aggregates.

**Two further SPY quarantines (added after adversarial review — the first
draft of this section wrongly whitelisted them):**

- **SPY root distribution is NOT a universe claim and cannot support
  "no second root".** The vendor sorts `order=asc&sort=ticker`, and the
  SPY prefix stops at `O:SPY241018P00546000`. `O:SPYW…` sorts *after*
  `O:SPY2…` (`W` = 0x57 > `2` = 0x32), so a second root is structurally
  invisible to a truncated prefix. Absence of a second SPY root is
  unfalsifiable here; the same applies to SPY's `root_appeared` /
  `root_disappeared` events.
- **SPY cannot produce adjustment events or active delistings BY
  CONSTRUCTION.** Consecutive SPY masters have an empty ticker
  intersection (0, 0, 0 — versus TSLA's 1420 / 2116 / 2222), and
  `shares_per_contract_change` is detected by iterating that
  intersection; SPY's `died_active` is likewise forced to zero because
  its prefix expiries have all expired by the next `as_of`. SPY's zeros
  in those two metrics are structurally impossible values, not null
  results.

## 3. Structural findings

### 3.1 Pooled over all 34,884 captured rows (both names, 4 as_ofs)

- distinct contracts: **29,126**; masters: 8; underlyings: 2
- contract_type: **call 17,681 / put 17,203**
- exercise_style: **american 34,884 — 100%. Zero european rows.**
- shares_per_contract: **100 → 34,884 — 100%. NON-STANDARD DELIVERABLES: 0.**
  No adjusted/split/merger deliverable appeared anywhere in the sample.
- adjustment events: **0**; active delistings (a contract that vanished while
  un-expired): **0** — but these pooled zeros are carried **entirely by TSLA**:
  SPY cannot produce either metric by construction (§2.1), so read them as
  "zero across TSLA's four complete captures", not across both names
- multi-root underlyings: **none observed**, i.e. no TSLA/TSLA1 collision across
  TSLA's complete captures. **This is NOT established for SPY** — a `SPYW`-style
  root sorts after the truncation point and would be invisible (§2.1). So this
  neither confirms nor refutes a SPY analogue of the SPX/SPXW collision M4-A
  found in the Cboe sample; a complete SPY capture is required to settle it
- `cfi`: OCASPS 17,681 (calls) / OPASPS 17,203 (puts) — a clean 1:1 with
  `contract_type`, so `cfi` carries no information this lane does not already have
- `primary_exchange`: **BATO for all 34,884 rows** — single-valued, no signal
- schema drift: **`unknown_result_keys: []`** — the vendor sent exactly the nine
  probed keys on every row

### 3.2 TSLA — complete captures, so these are structural facts

| as_of | universe | strikes | K range | span/spot | expiries | farthest | band expiries | in-band contracts | quarterly | LEAPS | call/put |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2024-09-16 | 3194 | 125 | 5 … 610 | 2.667784 | 21 | 851 d | 4 | 630 | 7 | 6 | 1597 / 1597 |
| 2025-03-14 | 4948 | 210 | 5 … 960 | 3.820306 | 22 | 825 d | 3 | 530 | 7 | 6 | 2474 / 2474 |
| 2025-09-15 | 4632 | 212 | 5 … 960 | 2.329041 | 23 | 858 d | 3 | 546 | 6 | 6 | 2316 / 2316 |
| 2026-03-16 | 6110 | 208 | 5 … 990 | 2.490141 | 26 | 1005 d | 4 | 1104 | 7 | 6 | 3055 / 3055 |

Tenor histograms (calendar days, contracts):

| as_of | 0–7 | 8–29 | 30–60 | 61–180 | 181–365 | >365 |
|---|---|---|---|---|---|---|
| 2024-09-16 | 240 | 430 | 630 | 474 | 592 | 828 |
| 2025-03-14 | 720 | 638 | 530 | 944 | 970 | 1146 |
| 2025-09-15 | 384 | 648 | 546 | 832 | 1138 | 1084 |
| 2026-03-16 | 898 | 986 | 1104 | 764 | 1058 | 1300 |

- Calls and puts are **exactly balanced at every as_of** (1597/1597, 2474/2474,
  2316/2316, 3055/3055) — the listing is a strict call/put grid with no
  orphaned legs.
- The protocol's 30–60 DTE band is **always populated**: 3–4 expiries and
  530–1,104 contracts. The M3 tenor rule has something to select on this tier.
- The ladder reaches far below and far above spot (min strike \$5 at every
  as_of; span 2.33–3.82× spot).
- Universe grew 3,194 → 6,110 (+91%) over the 18-month window while ladder
  depth stayed roughly flat after 2025-03-14 (210/212/208) — the growth is in
  **expiries** (21 → 26) and their density, not in new strikes.

Lifecycle (13,126 distinct TSLA contracts over the 4 as_ofs):

| transition | births | deaths | died un-expired |
|---|---|---|---|
| 2024-09-16 → 2025-03-14 | 3528 | 1774 | 0 |
| 2025-03-14 → 2025-09-15 | 2516 | 2832 | 0 |
| 2025-09-15 → 2026-03-16 | 3888 | 2410 | 0 |

**332 contracts are present at all four `as_of`s** (`full_span = 332`) — the
long-dated LEAPS that outlive a 6-month sampling step. Every death is an
expiry: **zero contracts vanished while still un-expired** across the whole
series, i.e. no silent delisting in this sample.

### 3.3 Bars — 5 series, 145 daily bars

| scope | series | bars | total volume | min | median | p90 | max | zero-vol | v ≥ 100 |
|---|---|---|---|---|---|---|---|---|---|
| SPY | 2 | 50 | 53,014 | 25 | 362 | 2,113 | 12,867 | 0 | 39 |
| TSLA | 3 | 95 | 750,788 | 40 | 1,206 | 9,614 | 123,666 | 0 | 93 |
| all | 5 | 145 | 803,802 | 25 | 848 | 7,001 | 123,666 | 0 | 132 |

Series: `O:SPY241018C00563000`, `O:SPY241018P00546000`,
`O:TSLA241018C00225000`, `O:TSLA241018P00225000`, `O:TSLA241115C00225000`
(ATM against the live spot proxy, on 30–60 DTE expiries). Unmatched bar
tickers: none.

**Implied session calendar** (union, 2024-09-16 … 2024-11-15): **45 sessions
over 61 calendar days, 16 weekend days skipped, ZERO weekday gaps.**
61 − 16 = 45 exactly, and there is in fact no NYSE holiday in that window
(Columbus Day and Veterans Day are not equity-market holidays). So the session
dates decoded from the vendor's ms-epoch `t` reconcile with the NYSE calendar
with no residual — a clean independent check on `bar_session()`'s ET-midnight
decoding. The inspector still declines to *assert* a holiday calendar from
bars alone, which is the right call: absence of a gap is only evidence over
the span actually observed.

Spot proxy (underlying close on each `as_of`, from `/v2/aggs`, 2 requests):
SPY 562.84 / 562.81 / 660.91 / 669.03; TSLA 226.78 / 249.98 / 410.04 / 395.56.

**PIT note on the denominator (corrected after adversarial review).** The
first capture requested the equity series with `adjusted=true`, whose
split history is restated *to the present*, while a strike ladder is
denominated in its own `as_of` date's share terms — mixing two unit
systems for any name that splits after `as_of`. The capture now requests
`adjusted=false` (the PIT-correct denominator; rationale in
`scripts/capture_massive_structural.py:capture_spot_proxy`). Re-pulled
unadjusted, **all eight closes are byte-identical to the adjusted values
above** — neither SPY nor TSLA split in 2024–2026 — so every `span/spot`
figure in §3.2 is unaffected. The fix matters for future names that do
split, not for this sample.

## 4. What this tier still cannot do (unchanged, re-confirmed live)

The free tier returned **no bid/ask, no greeks, no open interest** on any of
the 44 responses, and `/v3/snapshot/options/{underlying}` answers
`NOT_AUTHORIZED` at HTTP 200. Therefore:

- **M3 candidate filter: BLOCKED** — needs `abs_delta`, `min_open_interest`,
  `max_spread_fraction_of_midpoint`; all three are NOT_EVALUABLE here.
- **Backtest INV-11 executable prices: BLOCKED** — bars are trade prints;
  `close` is the last trade, not a price anything could have been filled at.
- `min_underlying_20d_median_dollar_volume` and `exclude_earnings_spanning_hold`
  remain NOT_EVALUABLE in this lane (different endpoint / no event source).

Satisfiable from this tier: `dte_min`/`dte_max`,
`standard_deliverable_only` (`shares_per_contract == 100`), and
`min_same_day_volume` from daily bar volume (no intraday detail).

## 5. Reproduce

```
# live capture (needs the key; writes only into gitignored artifacts/)
PYTHONPATH=src python scripts/capture_massive_structural.py \
    --out-dir artifacts/m4b-captures --budget 45

# analysis (offline; re-runs are free, the cache holds every page)
PYTHONPATH=src python scripts/inspect_structural_coverage.py \
    --contracts-json artifacts/m4b-captures/masters \
    --bars-json      artifacts/m4b-captures/bars \
    --spot-json      artifacts/m4b-captures/spot_proxy.json \
    --out-json       m4-b-massive-structural.json
```

Raw vendor payloads live in `artifacts/massive-cache/` and
`artifacts/m4b-captures/` and are **gitignored on purpose** — they are fetched
with a quota-bearing key and are not committed.

## 6. Artifact elision

`m4-b-massive-structural.json` is the inspector's JSON with its six top-level
keys and order intact, minus three bulk fields that are the vendor's contract
universe keyed by ticker rather than derived numbers:
`per_underlying.*.lifecycle.contracts` (29,126 entries) and the per-`as_of`
`born` / `died_expired` ticker lists. Each is replaced in place by its count
plus the reason; every aggregate computed from them (`distinct_contracts`,
`full_span`, `births`, `deaths`) is retained verbatim. `died_active` is kept
in full — it is the load-bearing one, and it is empty everywhere.

## 7. Nonclaims

1. **SPY universe-level numbers are not claimed** (§2.1): four truncated
   nearest-expiry prefixes. Only the TSLA series supports universe, tenor and
   lifecycle claims.
2. Two underlyings at four `as_of`s, six months apart, is a **shape probe, not
   a coverage census**. Zero non-standard deliverables here does not mean the
   tier never reports them — it means none occurred in SPY/TSLA on these dates.
3. Bars were pulled for 5 ATM contracts only; the volume distribution describes
   those contracts, not the tier's liquidity coverage generally.
4. The implied session calendar is evidence over 2024-09-16 … 2024-11-15 only.
5. Nothing here changes the M3/backtest blockers in §4; this lane feeds
   structural questions only, and `build_option_candidate_inputs()` in
   `massive_options` raises unconditionally so it cannot be wired in by
   accident.

## 8. Adversarial review and corrections

An independent verify pass attacked this lane's claims (secret hygiene,
entitlement handling, rate governor, PIT `as_of` flow, Decimal exactness,
epoch→session DST mapping, artifact integrity, overclaiming).

**Could not be refuted** (methodology validated against a planted canary
first): the API key appears nowhere in the working tree, cache, artifacts,
docs or `.git` (0 matches, raw and base64); `artifacts/` is gitignored;
101 lane tests pass with outbound sockets blocked, `HOME` redirected and
a sentinel key in the environment (0 intercepted network attempts).
`NOT_AUTHORIZED` at HTTP 200 raises and is never cached. The governor's
`min_interval` is exactly 12.0 s and a cache hit consumes neither a wire
request nor a token; 429 backoff is capped (4 attempts, 14 s, hostile
`Retry-After: 99999` clamped to 60). `as_of` provably reached the wire,
and the paginator's opaque cursor base64-decodes to carry it forward, so
PIT survives pagination. All 145 cached bars land exactly at ET midnight
across the 2024-11-03 fall-back (135 EDT / 10 EST). Every pooled figure
in §3.1 and the TSLA 2024-09-16 row of §3.2 was recomputed by hand from
the cached vendor bytes with no repo imports and matched exactly.

**Corrections applied to this artifact and the code** (the findings were
all in the interpretation layer, none on the wire):

1. §2.1 — SPY's root distribution no longer counts as a universe claim,
   and §3.1's "multi-root: none" is now scoped to TSLA. A `SPYW`-style
   root sorts after the truncation point and is structurally invisible
   to a prefix capture.
2. §2.1 / §3.1 — SPY cannot produce `adjustment_events` or
   `active_delistings` at all (empty ticker intersection between
   consecutive masters), so the pooled zeros are labelled as TSLA-only
   rather than presented as a two-name null result.
3. §3.2 — the spot-proxy denominator moved from `adjusted=true` to
   `adjusted=false` for point-in-time correctness; re-pulled values are
   identical for this sample (no splits), so no figure changed.
4. `scripts/capture_massive_structural.py` — the request budget now
   accounts wire requests a 429 backoff spends beyond the charged one
   (`Budget.charge_retries`), so the cap bounds requests rather than
   calls. No 429 occurred in this run (`rate_limit_retries: 0`), so the
   44-request accounting above is unchanged.
5. `artifacts/m4b-captures/capture_manifest.json` was stale relative to
   the two residual capture passes (gitignored, local-audit only); the
   authoritative page/row counts are the ones in §2 of this document,
   which match the master files on disk.
