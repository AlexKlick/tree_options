# PEAD-DEEP-2 — conditioning deepening of the PEAD-beats survivor (PRE-REGISTRATION)

Written 2026-09-23, before any run. Campaign-2026-09 slot `pead-deep-2`, under
`research_protocol.yaml` v0.2.2 (protocol hash computed over the validated model).
Baseline rule under study: the sealed PEAD-BIGSURPRISE card rule
(`artifacts/paper-trades/CRON-paper-engine.md` step 6; direction signal
`pead_beat` in `src/tree_options/desk/signals.py`).

What was inspected before sealing (nothing else): the RESEARCH-LEDGER census and
the PEAD family reports and generators named in section 7; the name lists, date
spans and session counts of `ohlc-panel.json`, `earnings-calendar.json`,
`earnings-timing.json`, `artifacts/desk-store/indices/VIX.csv`; the IVHIST-001
verdict labels; the FORECAST-001 verdict. No event-level return, no beat
population, no stratum outcome was computed. Session ordinals below come from
the SPY bar series only.

## 1. Hypothesis (one falsifiable sentence)

Conditioning the issuance of `pead_beat` cards on report timing, beat magnitude,
and the prevailing volatility regime separates the beat population into strata
whose per-card forward edge differs from the unconditioned beats baseline by a
positive conditional margin that survives the sealed window — and if no
pre-registered stratum does, the conditioning set is null and the question
closes.

Sub-hypotheses (one sentence each, same falsifier shape):
- **T (timing)**: AMC-timed beats, whose sealed entry sits at the close of the
  reaction session itself, show forward drift >= BMO-timed beats, whose sealed
  entry already sits one full session past the reaction, because drift decays
  from the reaction print.
- **M (magnitude)**: within beats (move >= +1.5%), the forward edge is
  increasing in the beat move (the PEAD-SIGN asymmetry has a within-sign
  gradient, not just a sign split).
- **V (vol regime)**: the beat edge concentrates in one VIX-level / realized-vol
  tercile (direction not assumed; the strata are symmetric and pre-declared).

## 2. Declared direction / use (ALLOWED / CONTEXT_ONLY split)

- Direction stays `pead_beat` (and only `pead_beat`) for every cell in this
  registration. No cell enters a name into `ALLOWED_DIRECTION`
  (`src/tree_options/desk/signals.py`: `ALLOWED_DIRECTION = {xsmom_top3,
  pead_beat}`); no BANNED name is touched.
- Every conditioning in this study is a **context gate on execution timing of an
  ALLOWED direction** (the `CONTEXT_ONLY` role, of which `vix_term` is already a
  member): it may suppress, delay, or size-zero a `pead_beat` card; it may never
  fire a card the sealed rule would not fire, never point a new direction, and
  never create a short leg (`short_options.policy: prohibited`).
- Deployment path, fixed now: a surviving gate is a SUCCESSOR CANDIDATE only,
  evaluated after the sealed PEAD rule's own 20-card review (kill-switch:
  retire if the first 20 valid cards' mean <= 0, `RESEARCH-LEDGER.md`
  DESK-CHECKS), exactly as `XSMOM-12-1-PREREG.md` pins for momentum: never a
  mid-stream swap of a card in flight. Any promotion is re-based on the card
  engine's option fills (bid/ask primary, $0.65/contract, $1/order minimum;
  candidate defaults dte 30-60, abs_delta 0.30-0.60, min OI 500, min same-day
  volume 100, max spread 10% of mid, $50M 20d median dollar volume, no earnings
  spanning hold) before it touches a card.

## 3. Feature definitions and exact data sources

- **Panel (entries, returns, RV)**: `artifacts/paper-trades/ohlc-panel.json` —
  37 names, 2021-09-13..2026-09-23, split-adjusted (NFLX 10:1 of 2025-11-17
  adjusted; META hole 2022-01-28..2022-06-09 outside the calendar era; traps
  per `RESEARCH-LEDGER.md` DATA INTEGRITY). Read under the shared flock on
  `ohlc-panel.json.lock`; sha256 recorded at run time.
- **Universe**: the 35 full-history chain-universe names = panel minus TQQQ and
  SQQQ (`desk-universe.toml` `no_options_expression`). Reporters with calendar
  dates: 26 of the 35 (`docs/desk/FORECAST-001.md`: "the 26 names with at least
  one date"). PLTR and SPCX joined the universe config 2026-09-23 but are NOT
  in the ohlc panel — excluded here, noted as later additions.
- **Earnings events**: `artifacts/paper-trades/earnings-calendar.json` (29
  keys; 26 reporters; dates 2024-09-05..2026-11-17; append-only seals in
  `earnings-calendar.seals.md`, build sha256 `916569ad…7383`; the run-time value
  is recorded). Entry session s = first NYSE session strictly after the report
  date; move m = close(s)/close(prior calendar session) − 1; fire iff
  m >= +1.5%; clean-back 5, hole and prior-gap guards — all exactly
  `pead_beats()` in `src/tree_options/desk/signals.py`. Entry at close(s), exit
  at close(s+h), net = close(s+h)/close(s) − 1 − 0.0005 (5bp round trip,
  matching `iter002.py` `stats_of`). The same-close filter m is the sealed card
  rule's own variable (CRON step 6, sealed 2026-09-10); this study adds no new
  same-close feature beyond it (INV-10 disclosure).
- **Vol regime (VIX level)**: `artifacts/desk-store/indices/VIX.csv` close on
  the PRIOR NYSE session (file spans 1990-01-02..2026-09-22, covering the whole
  era plus one session; CBOE's own index, NOT the IVHIST-derived history, so
  the IV fidelity gate does not apply). Prior-session pin keeps the feature
  strictly before the entry fill (INV-10-clean).
- **Vol regime (realized)**: RV20(t) = population stdev of SPY simple daily
  returns over the 20 sessions ending at t−1, from the panel's SPY closes.
- **Timing stratum**: `artifacts/paper-trades/earnings-timing.json`. Verified
  coverage: 27 rows, every report date >= 2026-09-24, every row status
  `estimated`; timing known for 9 (7 bmo: HD, JNJ, JPM, LLY, PEP, PG, UNH; 2
  amc: COST, NFLX), `unknown` for 18. JNJ is not in the calendar or the panel,
  so in-universe timed coverage today is 8 next reports (6 bmo, 2 amc).
  Historical timing coverage is ZERO: the bmo/amc stratum is forward-only.
- **Banned source**: per-name IV level / IV rank from
  `artifacts/desk-store/iv-history/vwap_atm.json` is EXCLUDED — IVHIST-001
  labels only IWM `ok`, all other names `low-fidelity` or `not-evaluable`
  (`docs/desk/IVHIST-001-verdict.json`, `single_stock_generalization: false`),
  and `docs/desk/IVHIST-001.md` excludes low-fidelity histories from any rule
  thresholding IV until an IVHIST-002 successor passes.
- **Dropped source**: the validated HAR h=20 forecast
  (`docs/desk/FORECAST-001-results.md`, verdict PASS, `forecast_source: har`)
  was considered as the regime variable and dropped: it is a per-name forecast
  with no defined market aggregate, and the level axis is already covered by
  VIX + RV20. A HAR aggregate is a successor-study idea, not this one.
- **Calendar trap**: 2025-01-09 is listed by the static protocol calendar but
  is a NON-session (no panel bar; `RESEARCH-LEDGER.md` 2026-09-23 entry). The
  walk treats it as a non-session; the sealed protocol calendar file stays
  byte-identical.

## 4. Inner / outer fold mapping

Era: entry sessions 2024-09-05..2026-09-23 = 514 NYSE sessions (counted from
SPY bars). Protocol folds: anchored_expanding, label_horizon per cell = hold
(10/20/40; the yaml's 5 is the default, the purge uses the actual label
length), embargo 5, validation 126, test 63, roll 63, min_train 252.

- **Outer anchored-expanding walk (diagnostic only, decides nothing)**:
  fold 1 train = sessions 1..252 (2024-09-05..2025-09-08), val = 253..378
  (..2026-03-10), test = 2026-03-11..2026-06-09; fold 2 rolls +63: test =
  2026-06-10..2026-09-09. Ten tail sessions (2026-09-10..2026-09-23) exceed
  the second roll and belong to the sealed window only.
- **Inner loop (edge fitting + cell screening; INV-07)**: all beat events with
  entry session <= **2026-04-20** — the strictest purge boundary
  (ordinal(s) + 40 + 5 < ordinal(2026-06-25)), so no tuning label of any hold
  overlaps the sealed window (INV-06). Strata edges (medians, terciles) are
  quantiles of the conditioning variable over this span only.
- **SEALED window (single, evaluated once, decides)**: the final 63 sessions,
  entries **2026-06-25..2026-09-23**, frozen edges from the inner span. Per-hold
  label completion as in `iter002.py`: entries with s+h beyond the panel are
  dropped and counted, disclosed per cell. No re-gridding, no alternative
  edges, no second look; a new idea is a new pre-registration
  (`XSMOM-12-1-PREREG.md` precedent).
- **INV-13**: all 24 cell IDs below are registered in the trial registry before
  any outcome is viewed; dataset manifest and calendar/panel sha256s stamped on
  the run (INV-14).
- **Forward timing arm** is not fold-mapped (zero historical coverage); it
  attaches labels to sealed cards from 2026-10-01 on.

## 5. Config count and grid (24 registered cells, one scope `pead-deep-2`, cap 32)

| cell ID | arm | conditioning | hold | n cells |
|---|---|---|---|---|
| PD2-B-h10/h20/h40 | 0 baseline | none (beats only, unconditioned) | 10, 20, 40 | 3 |
| PD2-M-lo/M-hi x h10/h20/h40 | 1 magnitude | beat-move median split (edge from inner span) | 10, 20, 40 | 6 |
| PD2-L-mod/str/ext | 1b fixed ladder | fixed bands: +1.5..+3%, +3..+6%, >= +6% (no fitted edge) | 20 | 3 |
| PD2-V-lo/mid/hi x h10/h20/h40 | 2 VIX level | prior-session VIX close tercile (edges from inner span) | 10, 20, 40 | 9 |
| PD2-R-lo/mid/hi | 3 realized vol | SPY RV20(t−1) tercile (edges from inner span) | 20 | 3 |

24 total; 8 slots of the 32 left unused on purpose. Conventions identical to
the PEAD family for 1:1 comparison: percent-return spot-proxy basis, 5bp RT,
day-clustered t (ddof=1) per `iter002.py stats_of`, hole guard > 10 calendar
days, conditional column mandatory (stratum mean minus PD2-B same-hold mean —
beats carry market beta; PEAD vs SPY monthly correlation +0.44,
`RESEARCH-LEDGER.md` DESK-CHECKS).

Forward-only timing stratum (0 backtest cells, no fold mapping): PD2-T-bmo,
PD2-T-amc, PD2-T-unknown — labels attached to sealed PEAD-BIGSURPRISE cards
from 2026-10-01 on, purely observational, no behavior change, sourced from
`earnings-timing.json` as it grows.

## 6. Acceptance criteria and verdict vocabulary (fixed in advance)

- **Cell bar (inner span, PEAD-DEEP's bar + mandatory deflation)**: mean > 0
  AND day-clustered t >= 2 AND n_days >= 30 AND n >= 20 AND conditional
  (stratum − PD2-B same hold) > 0. Passing = `CANDIDATE`; failing = `NULL`;
  n or n_days below the minima = `INSUFFICIENT_N` (never read as pass or fail).
- **Sealed confirmation**: a family is `STRATUM-CONFIRMED` only if its
  directional contrast (stratum ordering or gate ON−OFF as pre-declared in
  section 1) keeps the same sign in the sealed window with pooled sealed
  n >= 10 beats; sign reversal = `FAMILY-NULL`; n < 10 = `INSUFFICIENT_N`.
- **Family verdicts**: `STRATUM-CONFIRMED` / `FAMILY-NULL` / `INSUFFICIENT_N`;
  overall terminal vocabulary: `PROMOTE-AS-GATE` (successor candidate only,
  per section 2) or `CLOSE` (tested, not preferred; the question closes as in
  `XSMOM-12-1.md`). `WITHDRAWN` reserved for a data-defect void (calendar seal
  break, panel rewrite), never for a bad number.
- **Timing arm**: no verdict until BOTH PD2-T-bmo and PD2-T-amc hold >= 12
  valid timed cards; until then the standing verdict is
  `INSUFFICIENT_COVERAGE`, and the stratum can never gate a card regardless of
  any interim reading.
- **Multiplicity posture**: 24 cells in one scope; anything surviving the cell
  bar must also survive the sealed window and the census's base-rate deflation
  (`RESEARCH-LEDGER.md` Global multiplicity posture; the DSR posture of the
  DESK-CHECKS section is reported for any PROMOTE-AS-GATE at N = 24).
- No re-gridding after results: bands, terciles, holds, the sealed window, and
  these bars are frozen. One scored run.

## 7. Novelty against the census (files read; duplicates dropped)

- `artifacts/paper-trades/PEAD-DEEP.md` (generator `iter002.py`, cells
  holds {5,10,20,40} x {all, big, small, up, down}, 20 cells) already ran the
  POOLED-SIGN magnitude split (big/small vs era-median |surprise|) and the
  SPY-mom20 sign regime (up/down). Both are pooled over beats and misses and
  the surprise variable there is `abs(close(ei−1)/close(ei−2) − 1)`
  (`iter002.py` line 120) — the move of the session at-or-before the report
  date — which for AMC reports is the PRE-report move. **Dropped as
  duplicates**: any pooled-sign big/small re-run, any SPY-mom20 regime re-run.
  The delta here is within-beat magnitude on the sealed rule's own variable m,
  which was never run.
- `artifacts/paper-trades/PEAD-Q.md` already ran |move| quintiles and the
  XSMOM rank-half interaction at holds 10/20/40, pooled sign (24 cells, zero
  candidates). **Dropped as duplicates**: pooled-sign quintiles (replaced by
  the within-beat median split at fewer cells), the XSMOM-half interaction
  (not re-run; its top>bot ordering never passed the bar).
- `artifacts/paper-trades/PEAD-SIGN.md` / `PROTOCOL-PEAD.md` established the
  sign asymmetry itself (beats n=63 mean +97 USD/card vs misses n=81 +2) and
  the 144-card sealed backtest. The sign split is done; this study conditions
  WITHIN the beat leg only.
- `artifacts/paper-trades/HORIZON-001.md` ran unconditioned PEAD at holds
  {10,20} (t 1.97 / 1.52) and states the era limit: no pre-2024 PEAD test is
  possible. The hold axis pooled-sign is done; hold x conditioning within beats
  is the delta.
- `artifacts/paper-trades/GATE-001.md` ran spyup/breadth/spyvol gates on
  MR/R3f+up/CONT/VOLSPIKE rules at holds 1-2 — never on PEAD, and `spyvol`
  there is a short-horizon SPY-RV gate on other families. The V/R arms are the
  PEAD analogue; the family prior is GATE-001's result (gates point the right
  way, ON > OFF, but none lifts a rule to t >= 2 out of sample).
- `artifacts/paper-trades/RESEARCH-LEDGER.md` census: ~1,220 cells; PEAD is the
  strongest survivor; no timing, within-beat magnitude, or vol-regime PEAD cell
  exists anywhere in the ledger families list (EXIT-GRID, SWEEP-001/002,
  OOS-2021, SEMI-GATE, GATE-001, EXEC-001, HORIZON-001, PEAD-DEEP, MOM60-REPL,
  XSMOM+legs, SQUEEZE, GAPS, XU, XSMOM-12-1).
- Data-gated candidates declared and dropped: per-name IV conditioning
  (IVHIST-001 low-fidelity gate, section 3); any options-expression arm needing
  historical option fills (long-dated bars capture in flight, ETA ~2026-09-27).

## 8. Risks and the most likely failure mode

- **Thin n is the dominant risk**: the beat population is ~63-70 events in the
  whole era (63 through 2026-08-06 per `PEAD-SIGN.md`); strata hold n ~ 20-30
  inner-span and ~5-8 sealed. Most likely failure: noisy strata, nothing
  passes the conditional bar, sealed window underpowered — `FAMILY-NULL` /
  `INSUFFICIENT_N` across the board, matching the census base rate (GATE-001:
  0/48; PEAD-Q: 0/24).
- **Magnitude arm**: may just re-derive PEAD-Q's non-monotone quintile mess
  (Q3/Q5 strong, Q1 negative, pooled sign) — the within-beat gradient can
  flatten to nothing once the sign split already took the variance.
- **Vol arm**: VIX terciles can proxy calendar clusters rather than a regime
  (the PEAD edge sits in a few earnings seasons); day-clustered t partially
  guards this, n_days ~ 130 does not fully.
- **Timing arm**: labels are `estimated`, coverage is 8 in-universe reports,
  and the amc stratum is effectively a 2-name contrast (COST, NFLX) — a
  name-fixed-effect confound that no within-study control removes; years to the
  n >= 12 bar. Declared, forward-only, observational.
- **Era limitation**: one regime, 2024-09..2026-09; no holdout-era replication
  exists or can exist for PEAD (`HORIZON-001.md`).
- **Definitional**: the sealed filter m is a same-close variable and the entry
  fills at that same close (the sealed card convention; INV-10 disclosure in
  section 3). Spot-proxy 5bp RT understates option-fill frictions; promotion
  requires the card-engine re-basis (section 2).
- **Kill-switch inheritance**: if the base rule dies at its 20-card review,
  this study's families die with it; a gate cannot rescue a dead direction.
