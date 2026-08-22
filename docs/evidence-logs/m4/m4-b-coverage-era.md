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

_to be appended when the era completes: manifest verify, aggregate
numbers, per-underlying roots/lifecycle/adjustment findings, and the
committed machine copy of the inspector report._

## 3.1 Amendment (owner, 2026-08-22)

The owner clarified the subscription terms: the tier has **no total-request
cap** — only the 5/min rate limit. `--budget` is the lane's own runaway
rail, not a vendor quota. The rail was raised 13,000 → 25,000 (projected
need ~17k; the pass was stopped and relaunched from cache, losing
nothing), so the era completes in one pass instead of a budget-exhaust
relaunch cycle.
