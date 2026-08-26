# M4-B coverage era — 30 underlyings × 105 Fridays on the Massive free tier

- date: 2026-08-21 (launched), branch `m4/coverage-era-20260821`
- out-dir `artifacts/m4b-coverage-era/` (gitignored, host-durable); wire
  pages shared with the M4-B response cache (`artifacts/massive-cache/`)
- resumable wrapper: `artifacts/m4b-coverage-era/run.sh` (re-run to
  resume; cached pages are free, progress is durable)

## 1. Design

- **Universe (30, curated — a structural probe, NOT a census):**
  AAPL MSFT NVDA GOOGL AMZN META TSLA AVGO LLY JPM V UNH XOM PG MA COST
  HD ADBE NFLX CRM AMD PEP KO DIS INTC QCOM + SPY QQQ IWM. Selection
  rationale: liquid US single names across sectors plus the three broad
  ETFs; no rank fidelity is claimed.
- **as_of grid:** every Friday 2024-08-23 .. 2026-08-21 inclusive
  (105 dates) — the widest grid the rolling 2-year window still serves
  today. Weekly resolution supports lifecycle (births/deaths),
  root-stability, and adjustment-timeline claims; COMPLETE masters
  (`--pages-per-master 25`; truncation, if any, records in the manifest,
  never silent) because a page-capped prefix cannot see far-expiry
  births (the §2.1 lesson).
- **No bars** (`--bars 0`): the session calendar was already proven on
  the M4-B bar set; bar volume is not an era question.
- **Budget:** 13,000 wire requests (≈ masters ~9.5k pages + ~3.2k spot
  proxies ≈ 12.6k), ≈ 42 h at the 12 s governor cadence.

## 2. Entitlement-roll derisk (why two passes)

The bridge walks **underlying-outer** (a name's full 105-Friday series
before the next name). The free window's OLDEST dates age out around
2026-09-16 — but `as_of 2024-08-23` specifically leaves the window
~2026-08-23, i.e. ~2 days after launch. A single 42 h pass would fetch
the last names' oldest slices with only hours of margin. Pass 1
therefore captures the four most perishable Fridays (2024-08-23,
2024-08-30, 2024-09-06, 2024-09-13) for ALL 30 names first (~500
requests, budget 700); pass 2 is the full grid, with pass-1 pages
served free from cache.

## 3. Launch record

- Pass 1 launched 2026-08-21 ~20:55 MDT, log `/tmp/m4h-era-slice1.log`,
  marker `/tmp/m4h-era-slice1.done`.
- Pass 2 (full grid) launches on pass-1 completion via
  `artifacts/m4b-coverage-era/run.sh`; same out-dir.
- Interruptions: re-run the wrapper; the cache makes progress durable.
  The manifest is rewritten from disk on every exit path.

## 4. Results

Era closed at merged main `0c13e8f` (PR #13; stage-0 fast-forward
`1f7c388` → `0c13e8f`, fast-forward exit 0, tree clean before and
after). The A6 closeout runbook's machine checklist was then run to the
subset that needs no owner input, with full capture; every log cited
below lives under `/home/alexk/documents/tree_options-logs/`.

- **Era exit:** `artifacts/m4b-coverage-era/era.log` tail-1 = `ERA_EXIT=0`.
  The wrapper's `/tmp/m4h-era.log` was re-copied into the repo dir
  (idempotent; `cmp` byte-identical, `m4-closeout-copy-era-log.log`).
- **Masters 3,045 == universe `expected_masters` 3,045** (29 underlyings
  × 105 Fridays; `masters/` holds exactly 3,045 files). Rows: declared
  10,049,160 == parsed 10,049,160; distinct contracts 1,046,940; zero
  truncated / error / missing masters. The committed machine copy for
  this era is the census pair (`m4-b-coverage-census.md` / `.json`);
  no separate inspector pass was run at closeout.
- **`capture_manifest.json` sha256 =
  `1732e1d053d9da3c2d76afe2ac61a78ce8c350026dfc81d3c2a9d586a5c41029`**
  (`m4-closeout-manifest-hash.log`; re-verified inside census
  provenance). Manifest client stats: budget 25,000 / requests charged
  7,451; cache hits 4,263 (misses 7,450); pages fetched 11,684;
  rate-limit retries 1; cache self-heals 1; governor slept 82,926.5 s
  (~23.0 h).

### 4.1 Census — exit 5, emitted-but-incomplete (owner escalation)

`uv run --frozen python scripts/build_coverage_census.py --capture-dir
artifacts/m4b-coverage-era` ran exactly once (17:06:02Z → 17:07:29Z,
`m4-census.log`) and exited **5 — emitted-but-incomplete: this is NOT
whole coverage**. Per the exit contract the emitted artifacts stand and
the census is never re-run; the gap is an owner-escalation item.

- content hash
  `43b0b040ea3c7936fc08e6b1028ce446e46c99f44ca1d87da9fec02099e12e14` →
  `artifacts/census/43b0b040ea3c/` (`census.json`, `census.json.sha256`,
  `census.md`; gitignored — the elided committed copy is
  `m4-b-coverage-census.json`).
- Pair classes (sum 3,045): COMPLETE 2,871 / SPOT_MISSING_HOLIDAY 145 /
  **SPOT_MISSING_SESSION 29 — the exit-5 driver** / TRUNCATED 0 /
  ERROR 0 / MISSING 0.
- The 29 session gaps are ONE Friday: 2026-08-21, all 29 underlyings,
  detail "session friday with no spot close (vendor availability gap)".
  The 145 holiday gaps (2025-04-18, 2025-07-04, 2026-04-03, 2026-06-19,
  2026-07-03 × 29) are EXPECTED and do not block exit 0.
- spot_sessions_with_close 2,871; spot_holiday_fridays 5;
  bar_volume_observations 0 (NOT_EVALUABLE — `--bars 0`, by design).
- Values taxonomy: `flow_min_session_volume` AWAITING_OWNER_RULE (the
  G3 derivation-source contradiction is reproduced verbatim in
  census.md); `final_holdout_window` AWAITING_OWNER_DECLARATION;
  `owner_ratified_policy_value` empty by construction — **this evidence
  ratifies no 0.2.1 value**.
- Provenance: code_sha `0c13e8f3…` == HEAD; protocol hash `77903fc7…`
  (raw file sha `04743de1…`); input_manifest `1732e1d0…` == the manifest
  sha above; universe_manifest `4553fc7a…` (content hash; raw-bytes
  `bd34fd35…`); uv.lock `e97782ff…`.

### 4.2 Universe regen byte-identity + G4 preflight

- Universe regenerated from the held `run.sh`
  (`--from-run-sh artifacts/m4b-coverage-era/run.sh --out
  artifacts/census/universe-regen-check.json`) → fridays 105,
  underlyings 29, expected_masters 3,045; `cmp` against the committed
  `data/coverage/coverage_universe.json` → **byte-identical** (both
  sha256 `bd34fd3573526003163252b46a4db3f6a2ace5269e6ab2ec7e5947b22189eac0`;
  `m4-closeout-universe-regen.log`, `m4-closeout-universe-cmp.log`).
- `g4_seal.py preflight` → exit **2**: the lane1 parent `artifacts/lane1`
  is absent → typed bundle refused, verdict null, **no G4 authority
  consumed** (`m4-closeout-g4-preflight.log`).
- Final `era_status.py --json` at closeout → exit 4 (NO RUN FOUND —
  pre-adoption; `m4-closeout-final-status.log`). Superseded by §4.3:
  after adoption the same read exits 0.

### 4.3 Runstate adoption COMPLETE (2026-08-26, owner-ratified identity)

The owner authored the final-incarnation identity
`artifacts/runstate/m4-coverage-era-identity.json` (operator file; it
stays untracked — `artifacts/` is gitignored) and the adopt-* checklist
then ran to completion. Canonical run_id
`m4-coverage-era-20260822-3dfe6aa1` = `compute_run_id` over: campaign
`m4-coverage-era`; protocol_hash
`04743de114aa39f717a528b1aa650ffd4163fce97dadfda7ebc82e093476660f`
(sha256 of `research_protocol.yaml` @ 0.2.0); code_sha
`1f7c3887526f4665ad789b6327e4f6b644065ee0`; provider
`massive-polygon-free/1`; capture_version `m4b-capture/1`;
universe_manifest_sha256
`bd34fd3573526003163252b46a4db3f6a2ace5269e6ab2ec7e5947b22189eac0`;
args_hash
`dc6ac9b6fa721756a5a1ac78dc7dca27cd0e61934b7be37ba12593c9b362d709`
(sha256 of the recovered 2,321-byte amended argv — the args_hash
RECIPE was DEFINED by the owner ruling 2026-08-26); started_epoch
1787437488; run_nonce omitted (null). Re-derived at PR-prep with the
repo's own `canonical_run_id`: matches the store directory
(`pr-prep-03-runid-check.log`).

Incarnation provenance — the identity pins the COMPLETING pass:
boot_id `0b8f2760-8441-42fc-b0f2-08f4ac28b2ff`, pid 2017909,
pid_start_ticks 877300 (a DERIVED floor; the true value lies in
[877300, 877400)). A mid-era reboot (2026-08-22 13:58, boot
`16f1b020…`) killed pass 2 (pid 3183242) and the wrapper's budget was
amended 13,000 → 25,000 (§3.1); the creation incarnation was
deliberately not chosen.

Adopt sequence — all exit 0 (`m4-adopt-0{1,2,3,4}-*.log`):
`--create-identity` → seq 1 (PLANNED, "store created"); `CAPTURING` →
seq 2 ("pre-journal legacy era; journaling the observed lane");
`--pin-manifest
1732e1d053d9da3c2d76afe2ac61a78ce8c350026dfc81d3c2a9d586a5c41029` →
seq 3 (MANIFEST_PINNED); `CAPTURE_COMPLETE` → seq 4 ("wrapper
ERA_EXIT=0; log copied; manifest pinned"). Final
`era_status.py --json` exits **0**: state `CAPTURE_COMPLETE`, seq 4,
classification `ALIVE`, `pinned_manifest 1732e1d053d9`, journal tail
`a56862225904`, `tail_damaged` false (`m4-adopt-05-status.log`). This
resolves the pre-adoption exit-4 "NO RUN FOUND" and with it the
checklist expect_exit mismatches on steps 1/2 (coded 0/3 vs actual 4):
exit 4 was the runbook value for a pre-adoption store with an exited
wrapper (`m4-closeout-status-journaled.log`,
`m4-closeout-status-legacy.log`), not a defect.

**Bars-era preflight now refuses on the PROTOCOL gate alone.**
`launch_bars_era.py` preflight (read-only) still exits **2 — the
documented correct answer** — and the refusing check is now
specifically `protocol_gate`: "PREFLIGHT REFUSED (protocol_gate):
protocol version '0.2.0' != '0.2.1'" (`m4-adopt-06-bars-preflight.log`).
The run-state gate PASSED — the adopted store was found and accepted —
so the bars era now waits solely on the owner's 0.2.1 ratification. Not
a conflict: the bars gate's protocol evidence hash (`77903fc7…`) is
that tool's own internal protocol hashing, distinct from the identity's
`protocol_hash` convention (raw `research_protocol.yaml` sha256 =
`04743de1…`) — two purposes, two hashes, both correct.

### 4.4 RECORDED discrepancy — 30 vs 29 underlyings (owner reconciles)

This document's header/§1 and the G4 plan say **30 underlyings × 105
Fridays = 3,150**; the wrapper and the committed universe declare **29 ×
105 = 3,045**, and the census measured 3,045 == expected 3,045. The
grid is 29 × 105 = 3,045 per `run.sh:15` (its `--underlyings` list has
29 names) + the committed universe manifest + the filesystem (exactly
3,045 master files); the G4 plan's "3,150" is stale prose — as is
`run.sh`'s own header comment, which still says "30 curated
underlyings" a few lines above the 29-name list. The census's 29
SPOT_MISSING_SESSION findings (§4.1), all on 2026-08-21, are the
recorded residue of the pre-amendment first pass: that Friday was
probed before the budget amendment, returned no vendor close, and the
durable cache meant no later pass re-probed it
(`spot_proxy.json` carries 99 closes per underlying, last 2026-08-14).
Per owner decision 2026-08-23 the docs numbers are reconciled BY THE
OWNER at era-results; neither number is hand-fixed here.

### Discrepancies (adversarial verify, 2026-08-26)

Independent re-derivation (`/home/alexk/documents/tree_options-logs/m4-verify.log`,
verdict CLEAN) confirmed the claims above and recorded three minor
discrepancies, reproduced verbatim:

1. Digest semantics: sha256(census.json bytes) is 60ff0123..., not
   43b0b040... — the claimed "content hash" is the canonical
   domain-separated hash (dir name + self-field), and the sidecar
   correctly carries the raw-bytes digest. Both are internally
   consistent; the executor's wording is accurate but anyone
   re-checking with `sha256sum census.json` will get 60ff0123 and must
   not misread that as tampering.
2. `sha256sum -c census.json.sha256` is unusable (bare-hex sidecar
   format) — builder design, not a defect.
3. Executor-reported expect_exit mismatches on steps 1/2 (checklist
   coded 0/3 vs actual 4) stand as recorded; runbook §1.2 confirms 4 is
   the correct post-exit/pre-adoption value. (Now historical: adoption
   is complete, §4.3.)

## 3.1 Amendment (owner, 2026-08-22)

The owner clarified the subscription terms: the tier has **no total-request
cap** — only the 5/min rate limit. `--budget` is the lane's own runaway
rail, not a vendor quota. The rail was raised 13,000 → 25,000 (projected
need ~17k; the pass was stopped and relaunched from cache, losing
nothing), so the era completes in one pass instead of a budget-exhaust
relaunch cycle.
