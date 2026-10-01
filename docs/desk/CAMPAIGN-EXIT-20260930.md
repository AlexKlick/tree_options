# Desk strategy campaign — exit packet (2026-09-28 .. 2026-10-01)

The campaign = the options-desk measurement program that ran from the env-v2
rewrite through the v3 long run and its corrected re-score: environment v2 +
the paired long-run harness (landed 2026-09-28, `05b1732`), the 27-arm v3 run
`20260929T094303Z` (1,912/1,913 boards, 240 sessions 2025-09-29..2026-09-25,
A/A choice agreement 23.5%), the 2026-09-29/30 swarms and strategy sweeps,
the 2026-09-30 measured-cost corrections, the eight-PR measurement-fix merge
wave (#45–#52 on `7db4fb3`, plus #53/#54 after), and the corrected re-score
of 2026-10-01. This packet closes it. Its product is a NEGATIVE result plus
a measurement stack that can be trusted to say so.

**Verdict: no demonstrated edge. 0 arms eligible for operator review,
nothing promoted, both walk-forward finalists failed their single
confirmatory look** (refl-patience test net −$241.40, p=0.349; refl-costmin
test net −$1,282.80, p=0.9534; `promotion.promoted: false`). Every
alpha-positive arm is a direction-tilted fixed rule in a window where all
three underlyings rose 14–24% (IWM +16.8% n=243, QQQ +24.3% n=243, SPY
+14.5% n=220, price drift via `outcomes.spot_series`, 2025-09-29..2026-09-25).
Sources for everything below: the corrected re-score artifacts at
`~/.local/state/trex-longrun/v3-final-accounting-20261001/` (`digest.json`,
`SUMMARY.md`, `digest.md`, `cost-rerating-verdict.txt`), the PR descriptions
preserved at `~/.local/state/trex-pr-20260930/*.md`, and file:line cites into
this repo.

## What the campaign established (verified findings)

1. **No LLM or rule result to date shows decision skill.** The trivial
   control `first_row` ("pick the first row on the board") clears the same
   skill bar the arms were judged on — +$2.32/trade at p=0.014 vs
   same-direction controls in the 2026-09-30 per-trade residualization
   (campaign-side figures, corroborated and recorded in the re-score
   SUMMARY's cross-checks) — so
   that bar is not a skill bar. The re-score corroborates the mechanism
   without re-deriving p: `skill.arms.first_row.alpha_total` +371.65 incl.
   `selection_split.underlying` +220.63, while its verdict reads NO SKILL
   DETECTED / FIXED RULE. Residualization against same-direction AND
   same-underlying controls is what kills the apparent alphas
   (call_debit_hold5 +$2.09 p=0.093 → −$2.45 p=0.99; refl-patience +$7.64
   p=0.002 → +$3.31 p=0.026).
2. **The apparent winners were regime beta.** 7 of 7 strategy families came
   back dead or a regime bet; `selection_split.underlying` is 50–78% of the
   selection component for every top arm (ranking IWM/QQQ/SPY by drift).
3. **The A/A noise floor swamps arm differences.** Same policy twice:
   −$2,092.6 vs −$3,660.4; like-for-like on the 444 boards both repeats
   covered, 95% CI [−1,706.71, +3,025.66], 10,000 bootstrap draws
   (`~/.local/state/trex-swarm-20260929/report2.md:328`, 2026-09-29 swarm;
   run-level A/A choice agreement 23.5% per the digest headline).
4. **The measurement itself had to be fixed first** — six defects, fixed by
   #45–#52 and recorded in `artifacts/paper-trades/RESEARCH-LEDGER.md`
   §DATA INTEGRITY (degenerate single-null ranking, abstention-winnable
   promotion rule, mutable roster, flat-cost shape, invisible no-price
   refusals, missing provenance; summary table below).
5. **Vol-gated rules are structurally dark until ~2027-03**: `vol_state`
   needs 120 chain-recorded sessions; the recorder started 2026-09-22 and
   has already lost 09-23 permanently; executed `regime.vol_state()` =
   NOT_EVALUABLE 37/37. No code change fixes this; only time or a vendor.

## Retired numbers — and how each was caught

These four claims circulated during the campaign and are RETIRED. They must
never appear again as live claims; each is preserved here only with its
cause of death.

| Retired claim | What it actually was | How it was caught |
|---|---|---|
| "Cost is **3.7x** optimistic" | A full-vs-half convention error: `outcomes.CostModel` charges HALF the quote per fill (`half_spread_per_share=0.03`, `src/tree_options/desk/outcomes.py:109`, charged at `:118`); the claim crossed the FULL quote on all 4 fills | Reading the model's own convention and restating like-for-like: ~1.0x, not 3.7x |
| "Cost is **5.6x** optimistic" | A mean poisoned by deep-ITM rows: 9.2% of rows (spread ≥ $1.00, median \|delta\| 0.93, 10-lot displayed size) contribute **768% of the mean** — rows the desk does not trade (decomposition: `docs/desk/COST-CORPUS-STATS.md`) | Re-deriving the spread distribution on the tradeable universe (\|delta\| ≤ 0.70, n=18,783); the 612,372-quote distribution reproduced exactly across two independent re-derivations — the mean over the wrong universe was the error (a later draft's "3.56x" died the same way, retracted in PR #48's description) |
| "The book's max loss is understated **31.8x**" ($880 vs $28,000) | A category error: the NVDA structures are LONG DEBIT verticals — max loss = debit paid; width×100×qty ($27,120) is the best possible WIN, and the claimed max LOSS exceeds it. Understatement factor: 1.0x | Pinned impossibility arithmetic, landed as tests in #49 (`rail_reading`); width-based max loss belongs to credit kinds only |
| "Drift was **QQQ +23.9%, IWM −8.9%**" | Those were the CBOE 30-day IMPLIED-VOLATILITY indices: `minute-bars-long.json`'s `iv_context` self-documents `index_for {IWM:RVX, QQQ:VXN, SPY:VIX}`, `source "CBOE 30-day implied-vol index closes"` | Reading the field's own `source` string, then the repo's parity spot (`outcomes.spot_series`): real price drift IWM +16.8% / QQQ +24.3% / SPY +14.5%. All three ROSE — the long-bull tilt was rewarded MORE than the wrong figures implied, so the negative result is STRONGER, not weaker |

## The corrected economics

- **The toll exceeds the ceiling.** The audit put the genuine predictable
  residual of the universe at **$9–19 per fill** against a cheapest measured
  round trip of **$19.30** — a perfect predictor breaks even or loses (PR
  #47 description, `Consequences` §4, preserved at
  `~/.local/state/trex-pr-20260930/promotion-rule-pr-body.md:68`).
- **The flat constant is ~1.0x right on average, wrong per-leg.** Measured
  on 184 CBOE chain files (612,371 quotes; tradeable universe IWM/QQQ/SPY,
  7 ≤ dte ≤ 60, vol > 0, oi > 0, |delta| ≤ 0.70, n=18,783): median 2-leg
  RT $8.60 (0.59x flat), mean $15.76 (1.08x), p90 $34.60 (2.37x); median
  full spread by bucket $0.02 (|d|<0.10) → $0.19 (0.50–0.70) — overall
  figures and their derivation recorded in `docs/desk/COST-CORPUS-STATS.md`.
  The flat
  model over-charges cheap wings ~3x and under-charges near-ATM legs. The
  fix is the SHAPE (#48), never a bigger scalar.
- **On this run the LEVEL, not the shape, decides.** Re-rating the run's
  own entries under #48's model: mean measured cost $26.6968 on 17,511
  priced entries, 1,676 dropped fail-closed; `SET(shaped) == SET(level)` —
  the four survivors are identical with the shape removed, and 6 arms
  change sign under the shape (e.g. refl-trend +279.00 → −262.92). Two
  denominators appear in adjacent sections and are NOT a contradiction:
  PR #48's integration review counted 17,511 priced / 1,676 dropped
  fail-closed (refusals excluded); the re-rating verdict beside the
  re-score counted 16,137 priced / 51 excluded (boundary-clamped
  `delta_out_of_universe` rows ASSIGNED the boundary cell rather than
  refused). Different refusal policies, both stated with their own counts.
  flips are produced by the level (PR #48 description). The shape still
  changes the ordering: 17/22 arms reorder at every sigma in
  [0.20, 0.25, 0.30, 0.40, 0.50] (final re-rating verdict beside the
  re-score; per-arm NET flat→shaped at sigma=0.3: always_put_debit
  −20,710.60→−74,287.29; first_row −18,025.40→−66,955.32; m31-base
  −14,682.40→−57,746.50; refl-trend +279.00→−262.92).
- **The chains the costs were measured from are EOD snapshots**
  (capture window 17:45–06:30 ET), not the 10:00–15:15 ET decision clocks
  the run trades; #48 pins `describes_fill_clock: false` so no measured-cost
  number can be read as fill-clock truth (`src/tree_options/desk/cost.py`).
  This run also never exercises the cheap wing buckets (|delta| < 0.10), so
  it cannot confirm the half of the model that would make wing strategies
  cheaper (the verdict's own scope caveat).

## The corrected standings (re-score, 2026-10-01)

Re-score of run `20260929T094303Z` under the fixed stack (code `6bf369e` =
origin/main `7db4fb3` + the no-fill split), zero model calls, 1,912/1,913
boards, 27 arms; provenance pins the outcome table
`outcome-table-20260929-long.jsonl` sha256 `6b0e95be…` (69,641,145 bytes,
219,275 rows, FLAT $14.60 basis). Order = `vs_random_own.ci95[0]`
descending (the non-degenerate order). Full table with every column:
`~/.local/state/trex-longrun/v3-final-accounting-20261001/SUMMARY.md`.

| # | arm | net_total | vs_random_own [ci95] (p 1-sided) | no-fills ent/leg/mis |
|---|-----|----------:|----------------------------------|----------------------|
| 1 | refl-patience-patience-structure-filte | +10,347.40 | +14,754.81 [4,571, 25,283] (p=0.0018) | 9/45/137 |
| 2 | call_debit_hold5 | +5,355.00 | +14,010.08 [1,647, 26,685] (p=0.0127) | 94/83/250 |
| 3 | refl-costmin-cost-stretched-reversal | +380.00 | +5,542.55 [807, 10,649] (p=0.0115) | 37/54/175 |
| 4 | bull_hold5 | +1,894.40 | +12,212.24 [795, 23,758] (p=0.0175) | 149/96/350 |
| 5 | no_trade (control) | 0.00 | 0.00 [0, 0] | 0/0/0 |
| 6 | refl-trend-persistent-bullish-trend | +279.00 | +533.13 [−69, 1,374] (p=0.0814) | 0/4/16 |
| 7 | m31-fc#1 | −856.00 | +3,863.63 [−820, 9,203] (p=0.0654) | 9/51/250 |
| 8 | m31-fc#2 | −2,531.60 | +2,151.73 [−1,023, 5,596] (p=0.1089) | 7/64/263 |
| 9 | xs_strong5_bull_h5 | +578.20 | +6,924.29 [−2,044, 16,075] (p=0.0672) | 91/65/215 |
| 10 | refl-volprem-credit-reversal | −1,107.00 | +1,310.90 [−3,021, 5,421] (p=0.2768) | 7/32/104 |
| 11 | always_put_credit | −9,224.80 | +236.25 [−4,019, 4,247] (p=0.4578) | 139/89/362 |
| 12 | xs_strong20_bull_h5 | −1,822.20 | +3,717.92 [−5,247, 12,502] (p=0.2084) | 80/62/209 |
| 13 | always_bullish (control) | −10,513.60 | −195.76 [−5,455, 4,561] (p=0.5282) | 149/96/350 |
| 14 | always_call_debit | −9,723.00 | −1,067.92 [−5,669, 3,173] (p=0.6863) | 94/83/250 |
| 15 | bull_eod | −11,044.60 | −726.76 [−6,234, 4,336] (p=0.6099) | 149/96/350 |
| 16 | m31-base-low#2 | −10,662.80 | +1,811.55 [−6,796, 10,580] (p=0.3363) | 14/110/431 |
| 17 | refl-contrarian-contrarian-bullish-hold | −4,475.80 | +1,274.89 [−7,611, 10,116] (p=0.3977) | 47/52/190 |
| 18 | m31-base-low#1 | −12,580.20 | −84.07 [−7,803, 7,572] (p=0.5029) | 10/131/463 |
| 19 | theory_longvol_alt_h5 (control) | −10,243.20 | −3,548.58 [−8,258, 1,055] (p=0.9295) | 50/53/197 |
| 20 | always_call_credit | −15,582.00 | −3,165.74 [−8,577, 1,852] (p=0.8813) | 21/148/556 |
| 21 | theory_shortvol_alt_h5 (control) | −12,564.60 | −4,947.84 [−9,740, −215] (p=0.9785) | 66/76/216 |
| 22 | xs_weak20_bear_h5 | −10,385.40 | −525.00 [−9,862, 8,451] (p=0.5403) | 14/102/293 |
| 23 | m31-base | −14,504.00 | −1,238.21 [−10,421, 8,094] (p=0.5942) | 19/142/521 |
| 24 | first_row (control) | −18,023.20 | −4,140.22 [−10,457, 1,601] (p=0.9132) | 57/133/415 |
| 25 | xs_weak5_bear_h5 | −12,572.40 | −1,869.73 [−10,711, 6,724] (p=0.6555) | 18/124/318 |
| 26 | refl-meanrev-fade-extremes | −13,890.00 | −3,993.30 [−12,632, 4,773] (p=0.8152) | 45/83/365 |
| 27 | always_put_debit | −20,685.60 | −7,659.42 [−13,072, −2,646] (p=0.9974) | 26/104/268 |

Reading (the re-score's own): four arms exclude 0 on the POSITIVE side
(rows 1–4) and two on the negative (rows 21, 27). All four positive arms
are direction-tilted fixed rules in the +14–24% drift window; every
authority field still says no skill (all verdicts no-skill, walk-forward
finalists failed, promotion false, 0 eligible). The own-entry-rate null
killed the `no_trade` phantom: the legacy shared-null column had shown
`no_trade` (0 entries, $0) at **+$12,485.24, p=0.0001, rank 3**; the
own-rate column reads 0.00 [0, 0] at rank 5. Run-wide the flat table
charged $279,166.60 — the sum of the skill block's `cost_drag` column,
**19,121 charge units** of $14.60. That is six units more than the
19,115 standings-level evaluated entries (entered − unevaluable, summed
over arms: six arms — `m31-base-low#1/#2`, `xs_weak20_bear_h5`,
`xs_weak5_bear_h5`, `first_row`, `always_put_debit` — each carry exactly
one extra charge vs that count, an $87.60 / 0.03% difference; per-arm the
"integer × 14.60" identity holds in both blocks). The two numbers are
different derivations, not one identity.

## No-fill vs missing data (the 68.5% distinction)

Entered-but-unevaluable rows split by the outcome row's own reason (#54).
Entry-weighted across the 27 arms, 11,093 no-fills = `missing_later_entry_bars`
7,514 (67.7% MISSING DATA) + `legs_out_of_sync` 2,178 (19.6%) +
`entry_risk_cap` 1,401 (12.6% MARKET no-fill). At the table level (31,325
intraday candidate rows): 9,113 no-fills = 6,238 missing bars (68.4–68.5%)
+ 2,025 leg-sync (22.2%) + 850 risk-cap (9.3%). Roughly two-thirds of
"no fill" is a data gap, not the market refusing the trade. This distinction
is load-bearing for any restart: it is folded into the restart threshold's
denominators (C3/C4 of `docs/desk/RESTART-THRESHOLD.md`).

## What was stopped, and when

- **Burn timers disabled by the operator, 2026-09-30 evening.**
  `desk-lab.timer`, `desk-lab-overnight.timer`, `desk-challenge.timer` are
  disabled and stopped: they had been running 142/142 model-call
  failures/day (missing `EnvironmentFile=`) while exiting 0, so nothing
  alarmed. Do NOT "fix" the token — the loop should not run. Verified live
  2026-10-01: all three `systemctl --user is-enabled` → `disabled`.
  **SUPERSEDED same day (operator acts):** the missing-token root cause was
  fixed via `~/.config/environment.d/51-trex-desk.env.conf` (mode 600), a
  clean probe passed, and the operator re-armed `desk-challenge.timer`
  (19:00 MDT nightly; the 2026-10-01 run went 594/594 model calls with 0
  failures). The two `desk-lab` timers remain disabled.
- **NVDA flatten armed.** The `FLATTEN` kill file was armed 2026-09-30
  21:39 MDT in `~/.local/state/trex/putspread-20260922/` to close the two
  open NVDA debit verticals (nvda-oct 5 @ 0.21, nvda-nov 3 @ 1.24; last
  marks implied ~−$91 unrealized). Off-hours quotes are None so the monitor
  retries until a live per-spread quote exists; exits fire at the next
  session open (09:30 ET, marketable at bid, TIF=DAY). Live read
  2026-10-01 08:19 UTC: both structures still `open`, monitor connected
  and healthy. Operator action once closed: REMOVE the FLATTEN file after
  both structures read CLOSED, or any future entry in that plan
  auto-flattens.
- **No new measurement is running.** The A1 forward capture timer
  (`desk-forward-minutes.timer`) is deployed DISABLED; nothing collects the
  restart corpus until the operator enables it (see Restart conditions).
- No stopped timer was re-enabled by this exit; the supervised-IBKR paper
  path stays drill-gated and no canary was armed (2026-09-29 swarm verdict:
  zero supervised exits ever executed; weekly Sunday 12:00 ET human 2FA).

## Salvage value (what the campaign leaves behind)

- **The harness**: the paired long-run engine + `longrun redigest` with
  own-entry-rate nulls, purged walk-forward with embargo, skill
  decomposition, append-only digest history, and provenance-pinned digests
  (`desk-longrun-provenance/1`) — the machinery that made the negative
  result trustworthy. Re-scoring a finished run costs zero model calls.
- **The measured-cost corpus and model**: `src/tree_options/desk/cost.py`
  (moneyness-aware table from 612,371 CBOE quotes, typed `NO_PRICE`
  refusals, `CostProvenance.measured_corpus()` as the single authority) —
  reusable for any future cost basis, with its EOD-snapshot caveat pinned
  in code.
- **The forward Massive pipeline**: PR #55 (open at writing,
  `feat/a1-forward-minute-capture`) — `desk forward-minutes` + tests (680
  lines) + the deployed-but-DISABLED `desk-forward-minutes.timer` capturing
  Massive (Polygon) minute aggregates for the decision clocks (17:25 ET
  same-day + 06:50 Mon–Sat idempotent catch-up, wire-budgeted, exit-code
  honesty: only vendor-retry exits benign). This is the instrument a
  restart corpus would be collected with.
- **The cockpit**: `/api/desk/longrun` + the SPA render the own-rate null
  headline, the NO PRICE ledger, and the cost-basis caveat (#50, #52). The
  code is merged on origin/main; the SERVING checkout redeploy was blocked
  on the foreign local-main lane and remains an operator sync action.
- **The sealed research chain**: RESEARCH-LEDGER, sealed card rules, and
  the pre-registration discipline — extended by this packet's DATA
  INTEGRITY entries and the sealed restart threshold.

## A2 clock-tier chain-store design — DOCUMENTED-NOT-BUILT

The campaign's lane A2 designed (and deliberately did not build) an
intraday clock tier for the CBOE chain store. The design, recorded here so
it survives the exit:

- **Default `clock="eod"` key.** The store's on-disk layout
  (`chains/<D>/<SYM>.json.gz`) gains a clock dimension keyed so every
  existing reader — all six: `store.py` (owner),
  `desk/shadows.py:144`, `desk/pit.py:428`, `desk/surface.py:592`,
  `desk/gap_check.py` (manifest), `trex_web/options_view.py:125` — keeps
  reading `clock="eod"` bytes unchanged.
- **Additive header field, no schema bump.** A `clock` field is added to
  the snapshot header WITHOUT bumping `desk-chain/1`
  (`src/tree_options/desk/store.py:70`), so old and new documents share one
  schema and validators never fork the version.
- **Branched `validate()` self-freshness.** `store.validate()`
  (`store.py:249`) branches: non-eod clocks require the snapshot's own
  capture instant to be fresh for that clock (self-freshness), while the
  eod tier keeps today's session semantics.
- **Per-clock manifest.** The manifest (`desk-chain-manifest/1`,
  `store.py:71`) gains one manifest per clock tier, so an intraday tier's
  gaps are visible without touching the eod manifest's history.
- **8-clock OnCalendar capture line.** One systemd timer line covering the
  eight decision clocks (`SCHEDULE`, `intraday_action_graph.py:27`: 10:00,
  10:45, 11:30, 12:15, 13:00, 13:45, 14:30, 15:15 ET).

**Why it was not built — the CBOE overnight-publication evidence.** The
free-tier CBOE delayed chain publishes effectively once, overnight: the
recorder's usable window is ~6 h/day around 17:45–06:30 ET, the 23:45 slot
is structurally dead because CBOE publishes ~23:49 ET, and the recorded
proof is the 2026-09-23 session that never existed — on 2026-09-24 the
vendor served the 2026-09-22 EOD snapshot all day (37/37 names stale at
every desk-chain slot through 12:30 ET; the store keeps only what a slot
catches, and the overnight vendor snapshot is the sole source —
RESEARCH-LEDGER §DATA INTEGRITY, 2026-09-25 entry). Capturing 8 intraday
clocks from that feed would store 8 copies of the same overnight snapshot
and lie about the fill clock — the exact class of error #48 pins
`describes_fill_clock: false` for. DOCUMENTED-NOT-BUILT; build it only
behind a vendor that actually publishes intraday quotes.

**ADDENDUM 2026-10-01 (A0 probe, 10:03–10:07 ET): the premise above no
longer holds — the feed NOW publishes intraday.** Three SPY fetches through
the recorder's own transport, two minutes apart, returned ROLLING
publications: payload timestamps 10:02:03 ET (sha256 `c29beaa8…09c6f`,
5.8 MB, 13,098 rows) then 10:03:03 ET (`b6100dd9…12c3`) fetched at 10:06:55
ET — a ~1-minute publication cadence with ~2–4 min content lag, during
RTH. The 2026-09-24 evidence (37/37 names stale through 12:30 ET) was real
when recorded; the vendor's behavior changed within the week. The A2
clock-tier design above therefore REVIVES as a live option: a fetch at
clock+2..3 min carries the clock's book, and the design should be re-costed
against the Massive forward capture (#55) rather than assumed dead. Full
evidence: `~/.local/state/trex/A0-cboe-inaday-probe-20261001.md`. Re-probe
on the day of any build decision; feed behavior is evidently not stable.

**BUILT same day** (operator-approved): the clock tier landed exactly as
designed above — `clock="eod"` default key (`chains/clock=<C>/<D>/…`
namespace, every eod reader byte-unchanged), additive `clock` header field
with no schema bump, branched `validate()` self-freshness (the payload's
own capture instant must land in `[clock, clock+10 min)`), one manifest per
clock tier (`manifest/clock=<C>/<D>.json`), and the 8-clock OnCalendar
timer at clock+3 with a SPY probe that waits for the delayed roll before
the universe sweep (`deploy/desk/desk-chain-clock.{service,timer}`). The
clock tier keeps no raw evidence (8×37×~5.8 MB/day would be ~1.7 GB for
bytes the tier never re-reads; each document's `raw_sha256` still pins its
content).

**Day-1 follow-up (2026-10-01, evening): the persistent late cohort got
its own EVENING tier.** Eight names (CRM, DIS, LLY, PEP, PG, V, XLE, XLV)
were stale at every one of the eight clocks — their delayed publications
never roll inside a 10-minute window — but have fully rolled by evening.
The fix is deliberately NOT a clock backfill (point-in-time discipline:
the clock namespace refuses post-window data by design): a separate
post-close observation tier, `clock="evening"` — namespace
`chains/clock=evening/<D>/`, manifest `manifest/clock=evening/<D>.json`,
freshness = `source_as_of` at or after the session close (16:00 ET), one
18:05 ET weekday capture, single pass (no probe, no retry passes).
Design and rationale: `docs/desk/EVENING-TIER.md`.

## The paid alternative (operator's option)

ThetaData Standard at **$80/mo, cancel anytime** — the cheapest
research-grade EOD+quotes vendor; one-month bulk download ≈ $80 total for a
fixed window (`docs/m4-vendor-decision-brief.md:74`;
`docs/m3-options-schema-spike.md:115`). The standing ThetaData decision is
recorded with the free-tier evidence in the ledger entry cited above. Any
restart that needs true intraday chain observations should be costed
against this line before building the A2 tier against CBOE.

## Measurement defects fixed by #45–#52 (merge wave on `7db4fb3`)

Defect + resolution bullets with reproductions live in
`artifacts/paper-trades/RESEARCH-LEDGER.md` §DATA INTEGRITY. Summary:

| PR | Defect fixed |
|----|--------------|
| #45 | `vs_random` was each arm's net minus ONE shared constant (null built once at the incumbent's entry rate) — a monotone transform of net; `no_trade` ranked 3rd on a phantom +$12,485 |
| #46 | The pre-registration was mutable: `plan["policies"]` was rewritten unconditionally on every resume — the arm roster was silently editable while claimed sealed |
| #47 | The promotion rule was winnable by not trading (finalist with test net $0.00 passed `vs_random_ci_low_above_0` on shared-null credit) |
| #48 | Flat $14.60-everywhere cost + invisible no-price refusals: measured moneyness-aware model beside the flat one, fail-closed `NO_PRICE` |
| #49 | Rail arithmetic unpinned; the 31.8x max-loss claim refuted by tests |
| #50/#52 | Cockpit API + SPA: own-rate null headline, non-degenerate standings order, NO PRICE ledger, cost-basis caveat; "never looked" ≠ "looked, nothing refused" |
| #51 | Digest had zero provenance and one backup slot; now `desk-longrun-provenance/1` + append-only history |

(#53 deduplicated the cost-corpus provenance into `cost.py`'s single
authority; #54 added the no-fill reason split quoted above.)

## Gate evidence

Local gates only (repo policy: no hosted CI; red Actions checks on PRs are
billing-blocked noise). Logs under `/tmp/`, exact tails quoted in the PR
body of this branch:

| Gate | Result (log tail, `/tmp/b2b4-<suite>.log`) |
|---|---|
| `tests/unit/test_desk_digest_provenance.py` | **10 passed** in 1.96s |
| `tests/unit/test_desk_skill.py` | **29 passed** in 1.56s |
| `tests/unit/test_desk_longrun.py` | **67 passed** in 3.45s |
| `tests/unit/test_desk_cost_rerating.py` | **8 passed** in 1.73s (measured-vs-flat re-rating suite) |

No test in the repo references the new doc paths (grepped `tests/` for
`CAMPAIGN-EXIT`, `RESTART-THRESHOLD`, `RESEARCH-LEDGER` before writing);
the suites above are the ones guarding the digest/markdown surfaces this
packet quotes. 114 passed / 0 failed total on this branch's tree
(`exit-packet-b2b4` at origin/main `02e2c0b` + these three documents —
no production code touched).

## Transparency notes

- The re-score is DIAGNOSTIC BOOKKEEPING, not a confirmatory test: the
  measurement protocol changed AFTER the run existed; every p-value and CI
  in the standings is descriptive, multiplicity-uncorrected, read after the
  fact.
- `boards_dropped_unpriced` / `skill.no_price` are None ("never looked") in
  any redigest: the live NoPriceLedger is not persisted with the flat
  table. This flat table has zero `no_price` rows (22,202 closed / 9,113
  no_fill / 10 marked_at_end intraday statuses), so nothing was silently
  dropped at the table seam — a measured-cost table WOULD refuse unpriceable
  candidates and a redigest could not see those refusals (KNOWN RESIDUAL).
- Two invented-zero defects were found and fixed DURING the merge wave
  (2026-10-01 ruling: "never looked" ≠ "looked, nothing refused"; landed in
  #52's commit), recorded rather than absorbed.
- The cockpit serving redeploy is blocked on the foreign local-main lane;
  the merged code is on origin/main. No deploy was attempted here.

## Operator-supervised / out-of-scope (carried)

- NVDA flatten verification and FLATTEN-file removal after both structures
  read CLOSED (operator; live state, do not touch from lanes).
- Any ThetaData purchase and any A2 build decision (operator).
- The supervised-IBKR paper path remains drill-gated; `paper_blockers` in
  `supervised_ibkr.py` untouched by this exit.

## Restart conditions

The program restarts measurement ONLY under the pre-registered forward
go/no-go sealed in **`docs/desk/RESTART-THRESHOLD.md`** (sidecar
`docs/desk/RESTART-THRESHOLD.md.sha256`, sha256
`7352cdc041588e8c13661741d1c90f1be9a6ebdd8a9d2fd2341c8d1a673b4e88`,
sealed 2026-10-01 before any forward corpus exists). Short form: on a
forward-collected corpus with real intraday price observations at the eight
decision clocks, the best arm's gross per FILLED entry must exceed its
measured round-trip cost on a holdout never used for selection, with
missing-data rows excluded from the denominator (a missing bar is not a
no-fill), the paired CI above zero, the first_row control failing the same
bar, and Holm across tested arms. Until then everything stays as stopped
above.

Scope note for the A1 capture (critic followup): the forward-capture
verifier (`forward-verify`, PR #55) asserts bar coverage at the THREE
outcome-table decision clocks (10:00/10:15/15:15 ET,
`outcomes.py decision_clocks_et`) — NOT the eight-clock schedule of
`intraday_action_graph.py`. Its `ok: true` is therefore evidence that the
corpus accumulates, not evidence that threshold C1 (eight-clock
observation) is met: the restart scorer must compute eight-clock coverage
from the captured full-session bars itself.

## No-execution statement (agent attestation)

No orders, broker queries, monitor restarts, timer enables, or sealed-study
reruns were performed in producing this packet. Live state under
`~/.local/state/trex*` was read only (book.json, monitor.json, timer
enablement, the re-score artifacts); the FLATTEN file and all trading state
were not touched. The run dir `artifacts/desk-store/evaluations/longrun/20260929T094303Z`
was read-only for the re-score (`--out` mode; historical digests unchanged).
The only writes are the three documents of this exit packet and the ledger
entries it records. These are agent attestations over the session's own
actions; the re-score artifacts and live-state reads above are the positive
evidence for what DID run.

## Next milestone

None scheduled. The campaign's product is the negative result, the fixed
measurement stack, and the sealed restart threshold; the next milestone, if
ever, is the operator enabling the A1 forward capture to begin collecting
the qualifying corpus — judged only against the sealed threshold, never
against a bar written after the data.
