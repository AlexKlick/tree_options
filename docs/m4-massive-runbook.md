# M4-B — Massive (Polygon) free-tier capture runbook

Operator procedures for the Massive structural-coverage lane. The vendor is
Polygon (branded **Massive**; endpoints still on `api.polygon.io`), free
options tier, 2-year EOD history. The lane is **structural coverage only**:
contract universe, strike ladders, tenor, exercise style, lifecycle, and the
session calendar. **No quotes, greeks, or open interest exist on this tier**
(probed live 2026-08-21 — `NOT_AUTHORIZED`; evidence
`docs/evidence-logs/m4/m4-b-massive-structural.md` §4), so the M3 candidate
filter and executable-price fills remain blocked in this lane by design.

Pieces: `src/tree_options/data/massive_client.py` (wire: key custody, rate
governor, cache, entitlement gate), `src/tree_options/data/massive_options.py`
(option semantics), `scripts/capture_massive_structural.py` (the capture
bridge and CLI — the ONLY networked tool in the lane),
`scripts/inspect_structural_coverage.py` (offline analysis of captures on
disk).

## 1. Key management

- The key lives at `~/.config/tree_options/polygon.key`, mode **0600** (the
  directory 0700). An over-permissive mode is REFUSED at load time — the
  refusal names the mode, never the key.
- `POLYGON_API_KEY` in the environment overrides the file.
- **Rotation**: replace the file (or the env var), then confirm with a
  `--dry-run` pass — cache-only, so verification costs zero requests.
- The key must **never be printed, committed, or cached**. The client
  guarantees this by construction: the key is appended to a URL only at
  request time, is excluded from cache keys and cache filenames, every cached
  body is redacted on write, and every outbound string (exceptions, reprs,
  logs) passes through `redact` first. The adversarial review (evidence §8)
  confirmed zero occurrences in the working tree, cache, artifacts, and
  `.git`, raw and base64.

## 2. Cache

- Lives at `artifacts/massive-cache/` (gitignored on purpose — fetched with a
  quota-bearing key). Override with `TREE_OPTIONS_MASSIVE_CACHE` or the
  `--cache-dir` flag.
- Content-addressed: sha256 over the endpoint path plus sorted query params,
  **excluding the API key** — two calls differing only by key share one entry,
  and no cache filename can carry the secret.
- A hit costs **no request and no rate token**: the cache is consulted before
  the rate governor is ever touched.
- Corrupt entries self-heal from the wire — an unusable cached body is treated
  as a miss and re-fetched.

## 3. Failure semantics (exit codes)

| exit | meaning | operator action |
|---|---|---|
| 0 | ok — or a partial run whose captures are on disk | read the stderr summary; resume per §5 |
| 2 | `NOT_ENTITLED` — tier boundary: the endpoint needs a paid tier (the vendor answers HTTP 200 with body status `NOT_AUTHORIZED`) | nothing is wrong with the key; either accept the gap or take the purchase decision (brief §5) |
| 3 | `AUTH_REJECTED` — HTTP 401/403: dead or rotated key | rotate the key (§1); exactly one probe request is spent to discover this |
| 4 | nothing captured — budget exhausted before anything landed, or every master errored | re-run with a larger `--budget` (resume is free for already-captured pages); if masters errored, read the manifest notes |
| *(traceback)* | unexpected exception — a bug, not a designed outcome | the `finally` still wrote the manifest and every capture landed so far; report the traceback |

On any partial run stderr prints `PARTIAL: x/y masters complete` — read it
before trusting the outputs. A truncated master is also recorded as
`capture_complete=false` in the capture manifest and the inspector's report;
truncation is never silent.

A $0 derived-quote capability now exists on top of these captures (the M4-C
lane, `tree_options.data.massive_derived` + `massive_overlay`; evidence
`docs/evidence-logs/m4/m4-c-derived-lane.md`), but candidate wiring stays
gated on the G3 amendment packet — exit 2 remains the honest answer for a
quote-bearing run.

## 4. Budget math

- The free tier allows **5 requests/minute**, so the governor spaces real
  requests by `60 / 5 = 12 s`.
- The budget **pre-charges `max_attempts` (4) per wire call and refunds
  unused attempts**: a call that succeeds first try refunds 3, and a call
  that burns its 429 backoff is fully accounted. The cap therefore bounds
  WIRE REQUESTS, not logical calls — a budget of 45 can never spend 46
  requests even under rate-limit retries.
- Cache hits are free (§2): on a resume, only uncached pages spend budget.
- Expect wall-clock ≈ `12 s × new wire requests`; the 44-request M4-B run
  took ~7.5 minutes of governor sleep.

## 5. Resume recipe

Re-run **the same command with the same `--out-dir`**. Completed pages come
back from the cache at zero cost; only uncaptured pages spend budget. Deepening
advances in **uniform, all-or-nothing rounds** — every still-truncated
(name, as_of) master gains a page, or none does — so per-`as_of` columns
(ladder depth, births/deaths) stay comparable instead of measuring where the
budget happened to fall. A round the remaining budget cannot cover in full is
deliberately left unspent and said so in the manifest.

## 6. CLI profile

```
PYTHONPATH=src python scripts/capture_massive_structural.py \
    --underlyings SPY,TSLA \
    --as-of 2024-09-16 --as-of 2025-03-14 \
    --pages-per-master 4 --bars 6 --dte-min 30 --dte-max 60 \
    --cache-dir artifacts/massive-cache \
    --timeout 30 --max-pages 25 \
    --dry-run \
    --budget 45 \
    --out-dir artifacts/m4b-captures
```

| flag | meaning |
|---|---|
| `--underlyings` | comma-separated underlying tickers |
| `--as-of` | repeatable ISO dates; each (underlying, as_of) is one contract master |
| `--pages-per-master` | initial page cap per master; stopping leaves `next_url` in place so truncation is recorded |
| `--bars` | how many daily bar series to pull for representative in-band contracts; under `atm-grid`, the cap on TOTAL series |
| `--bars-mode` | bar selection strategy: `representative` (default; exactly the pre-flag behavior) or `atm-grid` — see §6.1 |
| `--bars-strike-band` | atm-grid only: DISTINCT strikes above and below ATM per in-band expiry (default 3; a RANK in the ladder, never an absolute strike range) |
| `--bars-expiries` | atm-grid only: `all` in-band expiries (default) or `monthly` — third-Friday monthlies only |
| `--bars-sides` | atm-grid only: `both` calls + puts (default) or `call` alone |
| `--dte-min`, `--dte-max` | the DTE band used to pick bar contracts (and restated by the inspector) |
| `--cache-dir` | response cache location (default `artifacts/massive-cache/`) |
| `--timeout` | per-request HTTP timeout in seconds |
| `--max-pages` | hard pagination guard per wire walk |
| `--dry-run` | cache-only: zero wire requests; use to verify key rotation or inspect what is already local |
| `--budget` | hard cap on live wire requests (must be > 0) |
| `--out-dir` | gitignored capture directory (masters/, bars/, spot_proxy.json, capture_manifest.json) |

Analysis is offline and re-runnable for free:
`scripts/inspect_structural_coverage.py` reads the capture files, never the
wire (full invocation in evidence §5).

### 6.1 `--bars-mode atm-grid` and its cost model

`representative` (the default) keeps the original three-picks-per-underlying
behavior — nothing about an unflagged run changes. `atm-grid` instead covers
EVERY (underlying, as_of) master, not just the first captured one: expiries
inside the DTE band (optionally only third-Friday monthlies), and per in-band
expiry the ATM ± `--bars-strike-band` DISTINCT strikes — ranked by
|strike − spot| ascending, ties by strike then ticker, on the chosen sides.
Selection is deterministic: masters in (underlying, as_of) order, expiries
ascending, strikes in rank order, calls before puts.

The cost model, in one line: **a series is fetched once per contract LIFE.**
Selection is deduped by ticker across the WHOLE run — a contract picked at
two `as_of`s costs ONE fetch, duplicates are counted into a manifest note,
and `--bars` caps TOTAL series, truncating to the deterministic prefix and
saying so in the same note.

- **monthly = the third-Friday rule** (`tree_options.time.monthlies`, the
  sanctioned home of weekday arithmetic): a Friday in days 15–21. Its
  approximation is NAMED in the predicate — when the exchange closes on a
  third Friday (Good Friday, rare) the traded expiry moves, typically to the
  Thursday before; the filter answers the calendar question only.
- **A missing spot close notes + skips the slice.** A master whose
  (underlying, as_of) has no spot close is skipped with a note: anchoring a
  grid on the representative mode's median-strike fallback would misdescribe
  "at-the-money" for every strike it named, so here the fallback is refusal.

## 7. ENTITLEMENT-WINDOW WARNING — READ BEFORE PLANNING ANY CAPTURE

> **The free tier carries a rolling ~2-year lookback.** An `as_of` older than
> the window is gone from the wire: `as_of 2024-09-16` ages out around
> **2026-09-16**, after which that capture **can no longer be re-pulled from
> scratch**. Pages already in the cache remain usable (they are local and
> immutable), but any page never captured before the roll is unobtainable at
> this tier. **Capture what you need before the roll** — when in doubt, spend
> budget now; a cached page is permanent, an uncached one has an expiry date.

## 8. Non-goals and known limits (deliberate deferrals)

- **No cache TTL or size bounds.** Point-in-time masters are immutable —
  re-fetching could only change nothing — and the live-window aggregates are
  documented as such. Revisit only if the cache grows past convenience.
- **Timeout is per operation (30 s), not a total deadline.** A slow vendor
  stretches a run; it cannot make a single request hang forever.
- **No scheduler.** Runs are manual or operator cron; nothing in-repo
  schedules captures (and §7 is the reason a "just re-pull it later" habit is
  forbidden).
- **DTE band constants are restated in the bridge** (`--dte-min`/`--dte-max`
  defaults) while `research_protocol.yaml` (0.1.0) stays frozen; revisit at
  the G3 amendment window, not before.
- **Structural coverage only** — no quotes, greeks, or OI on this tier (see
  the header and evidence §4); `build_option_candidate_inputs()` in
  `massive_options` raises unconditionally so the lane cannot be wired into
  candidates by accident.
