# VRP-COND — conditioning the allowed directions on the implied-vs-forecast vol spread (PRE-REGISTRATION)

Written 2026-09-23, before any conditioned outcome is computed. Slot
`vrp-cond` of campaign-2026-09. The primitive — the implied-vs-forecast spread
— was unlocked by FORECAST-001 (PASS, sealed 2026-09-23) and IVHIST-001
(PARTIAL, run 2 verdict of record, 2026-09-23); every census family in
`artifacts/paper-trades/RESEARCH-LEDGER.md` predates both. This file is the
only artifact this slot authors; no gate, no trial, no backtest runs here.

## 1. Hypothesis (falsifiable)

Per-trade net returns of the two ALLOWED directions — XSMOM-TOP3 monthly longs
and PEAD beats — are higher when the entry session's 30-day ATM IV is cheap
relative to the HAR h=20 variance forecast (within-name percentile of
IV30/HAR-vol in the bottom tercile, or the IWM spread percentile <= 1/3) than
the same rules earn unconditionally over the same sessions.

Falsified if the promoted config's sealed-window conditioned-minus-base delta
is <= 0, or fails any pre-committed check in section 6.

## 2. Declared direction/use

- **CONTEXT/EXECUTION ONLY** (plan D5, `src/tree_options/desk/signals.py`:
  `ALLOWED_DIRECTION = {xsmom_top3, pead_beat}`, `CONTEXT_ONLY = {trend,
  breadth, vix_term}`). The spread never points a direction, never selects a
  name, never inverts a sign: the XSMOM ranking (`close(t)/close(t-273)-1`,
  top 3, first session of the month, sealed operator ruling 2026-09-23) and
  the PEAD beat test (first post-report session, `move >= +1.5%`) are
  byte-identical to `signals.py`. The spread gates only: entry timing (fire /
  defer), size (full / half), and — scope O, data-gated — the long-only
  expression vehicle.
- Distinct from `BANNED` `short_vol_gated`: this family never sells
  volatility, never shorts, holds no short legs (protocol
  `short_options.policy: prohibited`).
- Distinct from `CONTEXT_ONLY` `vix_term`: that is the VIX term structure;
  this is a per-name IV30-vs-forecast spread. If ever adopted, `vrp_cond`
  joins `CONTEXT_ONLY` — a successor code change, not part of this
  registration.
- Adoption, if any, runs only through >= 20 forward sealed cards
  (`artifacts/paper-trades/promotion.py` gate); this study can at most
  nominate.

## 3. Feature definition and data sources (exact)

Per name `i` and session `t` (decision instant = session close; every input
dated <= t):

- `iv30_i(t)`: `artifacts/desk-store/iv-history/vwap_atm.json` (schema
  `desk-ivhist/1`, sha256 `105aaabecbc2a2ae...` per the consistency check in
  `docs/desk/IVHIST-001-results.md`). 35 names x 508 sessions
  2024-08-26..2026-09-03, 13,610 evaluable name-sessions. Labels
  (`docs/desk/IVHIST-001-verdict.json`): **IWM `ok`; 28 names
  `low-fidelity`; GLD, SMH, SOXX, XLE, XLV, XLF `not-evaluable`** (no option
  bars on disk).
- `harvol20_i(t) = sqrt(F_HAR,i,h20(t) * 365 / c(t, t+20))`, the annualized
  h=20 HAR forecast from `src/tree_options/desk/har.py` (monthly expanding
  refits, strictly point in time; `forecast_source() == "har"` because
  FORECAST-001 PASSed). Inputs: `artifacts/paper-trades/ohlc-panel.json`
  (read under the shared flock, sha256 recorded at run) and the sealed
  `artifacts/paper-trades/earnings-calendar.json` (29 keys; 26 reporters with
  dates, span 2024-09-05..2026-11-17; sha `916569ad...` at sealing). This is
  exactly the denominator of `vrp()` in `src/tree_options/desk/surface.py`
  (line 418), so `s_i(t) = iv30_i(t) - harvol20_i(t)` IS the desk's displayed
  `vrp_har` field (`surface.py` line 521).
- Ratio `r_i(t) = iv30_i(t) / harvol20_i(t)`; percentile `p_i(t)` = rank of
  `r_i(t)` among `r_i` over the trailing 252 sessions (evaluable sessions
  only), requiring n >= 126, else NOT_EVALUABLE.
- **Fidelity split (binding).** Absolute thresholds on `r` or `s` are used
  ONLY for IWM (the `ok` name). The 28 `low-fidelity` names appear only in
  within-name constructs (`p_i`, and within-name terciles of `r_i`).
  Justification: every IVHIST-001 pair failure is a level-bias failure
  (median bias -2.1..-2.9 vol pts) with level correlations 0.933..0.962
  (`docs/desk/IVHIST-001-results.md`, pair table); a fixed name-level
  multiplicative bias cancels in within-name temporal ranks. The residual
  risk — a time-varying bias — is declared in section 8, not remedied.
- Book condition for XSMOM: `pbar(t)` = mean of `p_i(t)` over the month's
  top-3 picks with evaluable `p_i`; a rebalance with none evaluable is
  NOT_EVALUABLE and the config abstains (counted, never silently ON).
- PEAD conditions read `p_i` at **t-1** — the last session before the entry
  session — because the report session's own IV30 embeds the event premium
  (the reason `implied_event_move` exists in `surface.py`).
- Known negative construction bias, inherited and not corrected: FORECAST-001
  observes `IV30 - sqrt(forecast)` is biased negative (Jensen of the
  bias-corrected mean-variance forecast, plus the ATM-vs-variance-swap gap);
  raw-ratio thresholds are therefore percentile-first, and the single raw
  threshold cell (`r_IWM < 1.0`, the playbook "cheap" line) is flagged as
  alignment-only (`docs/desk/FORECAST-001-results.md`, VRP-bias observation;
  the playbook thresholds were never a census cell — Wave 2 decision still
  open).
- Universe: pinned to the **35 full-history chain-universe names**
  (FORECAST-001's set). PLTR and SPCX joined `desk-universe.toml` 2026-09-23
  but are not yet in the ohlc panel — excluded here; a backfill amendment is
  a successor registration, never a mid-stream swap.
- Calendar: `data/calendar/nyse_sessions_2018_01_02_2026_12_31.json` with
  2025-01-09 treated as a non-session (ledger ruling 2026-09-23).
- Evaluation basis (equity arm): the census spot-proxy convention —
  signal-close entry, close-to-close holds of 20 sessions
  (`signals.py HOLD_SESSIONS`), 5bp round-turn primary and 15bp robustness,
  day-clustered t (`artifacts/paper-trades/RULES.md` lineage; GATE-001 bar).
  Panel traps apply as everywhere: META hole 2022-01-28..2022-06-09 (outside
  this window), NFLX 10:1 split 2025-11-17 (panel is split-adjusted), and
  the XSMOM gap exclusion (`signals.py`).
- Options-expression arm (scope O): recorded chains
  `artifacts/desk-store/chains/2026-09-22/*.json.gz` (one session only);
  long-dated option-bar capture in flight via
  `scripts/desk_longdated_capture.sh` (ETA ~2026-09-27); fills bid/ask
  primary per protocol `fills.primary`; candidates per
  `option_candidate_defaults` (dte 30-60, |delta| 0.30-0.60, OI >= 500,
  same-day volume >= 100, spread <= 10% of mid, $50M 20d median dollar
  volume, no earnings spanning hold). IV30-from-chains cross-checked against
  RVX only for IWM (`artifacts/desk-store/indices/RVX.csv`).

## 4. Inner/outer fold mapping

Feature window = the IV history: sessions 2024-08-26..2026-09-03, 508
sessions (the panel runs to 2026-09-23; no IV exists past 2026-09-03, so
nothing later is scored). Protocol fold shape (anchored_expanding, min_train
252, validation 126, roll 63, test 63, embargo 5):

| region | ordinals | sessions | use |
|---|---|---|---|
| warm-up | 1..252 | 2024-08-26..2025-08-27 | min_train; never scored (HAR origins start 2024-09-03; percentile n<126 before ~2025-03 — both NOT_EVALUABLE there anyway) |
| inner V1 | 253..378 | 2025-08-28..2026-02-27 | tuning (126) |
| inner V2 | 316..441 | 2025-11-26..2026-05-29 | tuning (roll 63 from V1; overlaps V1 by design) |
| pre-seal shoulder | 442 | 2026-06-01 | unused (not tuned, not scored; V2 start corrected 317 -> 316 at campaign assembly so the roll is the protocol's 63, not 64) |
| sealed test | 443..505 | 2026-06-02..2026-08-31 | **the single sealed window** (63), opened only after the promoted config is frozen |
| coda | 506..508 | 2026-09-01..2026-09-03 | unused |

Tuning entries are additionally restricted to ordinal <= 417 (2026-04-24) so
every 20-session hold label window ends before the sealed window with the
5-session embargo — the INV-06 purge applied at the desk hold length
(protocol `label_horizon_sessions: 5` governs the registry folds; the desk
hold is 20, so the stricter desk length is used for the boundary). Tuning
XSMOM rebalances: 8 (2025-09-02..2026-04-01; 24 entries). Sealed XSMOM
rebalances: 3 (2026-06-02, 2026-07-01, 2026-08-03; 9 entries). Scope O has
no evaluatable window at all (one recorded chain session): DATA_GATED.

## 5. Config grid — 24 registered configs (scope E: 20, scope O: 4; cap 32/scope)

All rows go into the trial registry (INV-13) before any outcome is viewed.
`pbar`/`p_i`/`p_IWM` as defined in section 3; terciles of the percentile:
lo <= 1/3, hi >= 2/3, mid between.

Scope E — equity timing (spot proxy, direction unchanged):

| id | gates | rule |
|---|---|---|
| xe-book-lo | XSMOM entry | fire the month's top-3 only if pbar(t) <= 1/3 |
| xe-book-hi | XSMOM entry | fire only if pbar(t) >= 2/3 |
| xe-book-mid | XSMOM entry | fire only if 1/3 < pbar(t) < 2/3 (symmetry control) |
| xe-mkt-lo | XSMOM entry | fire only if p_IWM(t) <= 1/3 |
| xe-mkt-hi | XSMOM entry | fire only if p_IWM(t) >= 2/3 |
| xe-playbook | XSMOM entry | fire only if r_IWM(t) < 1.0 (IWM raw threshold; alignment-only, bias-flagged) |
| xe-size-mkt | XSMOM size | always fire; half size when p_IWM(t) >= 2/3 |
| xe-size-book | XSMOM size | always fire; half size when pbar(t) >= 2/3 |
| xe-defer | XSMOM timing | when p_IWM(t) >= 2/3 at rebalance, defer entry one session (names unchanged) |
| xp-name-lo | PEAD entry | fire the beat only if p_i(t-1) <= 1/3 |
| xp-name-hi | PEAD entry | fire only if p_i(t-1) >= 2/3 |
| xp-name-mid | PEAD entry | fire only if 1/3 < p_i(t-1) < 2/3 (control) |
| xp-mkt-lo | PEAD entry | fire only if p_IWM(t-1) <= 1/3 |
| xp-mkt-hi | PEAD entry | fire only if p_IWM(t-1) >= 2/3 |
| xp-playbook | PEAD entry | fire only if r_IWM(t-1) < 1.0 |
| xp-size-name | PEAD size | always fire; half size when p_i(t-1) >= 2/3 |
| xp-size-mkt | PEAD size | always fire; half size when p_IWM(t-1) >= 2/3 |
| xe-lag21 | placebo | xe-book-lo's rule with pbar lagged 21 sessions |
| xp-lag21 | placebo | xp-name-lo's rule with p_i lagged 21 sessions |
| xp-shuffle | placebo | xp-name-lo's rule with p_i permuted across reporter-events by the deterministic permutation sha256("vrp-cond-xp-shuffle-1") (seed fixed here) |

Scope O — options expression (long calls only; naked shorts prohibited):
DATA_GATED, not run before the gates in section 6 clear:

| id | gates | rule |
|---|---|---|
| ox-cheap | XSMOM vehicle | top-3 pick with p_i(t) <= 1/3: express the long via a long call (protocol candidate defaults, bid/ask fills) instead of spot |
| ox-rich-fallback | XSMOM vehicle | top-3 pick with p_i(t) >= 2/3: spot only (paired skip cell) |
| op-cheap | PEAD vehicle | beat with p_i(t-1) <= 1/3: long call expression |
| op-rich-fallback | PEAD vehicle | beat with p_i(t-1) >= 2/3: spot only |

## 6. Acceptance criteria and verdict vocabulary (fixed now)

Per-config verdicts: **PASS / FAIL / NOT_EVALUABLE** (scope O adds
**DATA_GATED**, **WITHDRAWN**). Power floors: xp-* n_ON >= 8 sealed trades;
xe-* n_ON >= 6 sealed entries; below the floor the config is NOT_EVALUABLE,
never FAIL.

- **Selection (tuning only).** Within each family (xe, xp), the promoted
  config is the one with the largest pooled V1+V2 conditioned-minus-base
  per-trade net delta (5bp RT), among configs meeting the same floors on the
  tuning region. Ties: sparser gate (smaller ON fraction), then slot id
  lexicographic.
- **Sealed acceptance** for a promoted config, ALL four required:
  1. sealed ON-minus-OFF-session base delta > 0 (the matched-sessions
     conditional column is decisive — SQUEEZE precedent);
  2. day-clustered block bootstrap (block 10 sessions, 2,000 resamples,
     one-sided) p < 0.10 on that delta;
  3. the delta exceeds BOTH same-family placebo deltas run through the
     identical path;
  4. the delta stays > 0 at 15bp RT.
- **Family verdicts.** All four hold: **SURVIVOR-CANDIDATE** (nominated to
  CONTEXT_ONLY; still requires >= 20 forward sealed cards before anything
  changes in `signals.py`). Any miss: **DEFLATED** (looked real; the
  pre-committed checks killed it). Floors unmet: **NOT_EVALUABLE-SEALED** —
  the promoted config continues as a report-only monitored gate (the
  SEMI-gate precedent in RESEARCH-LEDGER.md), no adoption. Scope O:
  **DATA_GATED** until (i) the long-dated capture lands with >= 126 sessions
  of history, (ii) an IVHIST-002-successor fidelity verdict covers the new
  source; if the capture fails, **WITHDRAWN** without running.
- **Deflation plan, pre-committed:** every attempted config registered
  before its outcome is viewed (INV-13); no re-gridding after results — a new
  idea is a successor pre-registration; multiplicity is bounded by the
  registered grid (24 cells, placebo pair included), not by post-hoc
  counting; the base-rate control is the same-sessions unconditional base of
  the unchanged direction rule; and because NO pre-2024 holdout exists for
  any IV construct (the IV history starts 2024-08-26), the missing holdout
  deflation is replaced by the sealed 63-session window plus the program's
  forward-card gate — nothing in this family adopts on in-window evidence
  alone. Era disclosure: the window holds no 2020-style crash (declared in
  FORECAST-001's limitations).

## 7. Novelty against the census (files read)

- The census (`artifacts/paper-trades/RESEARCH-LEDGER.md`, ~1,220 cells:
  EXIT-GRID 52, SWEEP-001/002 1008, OOS-2021, SEMI-GATE 12, GATE-001 48,
  EXEC-001 4, HORIZON-001 18, PEAD-DEEP 20, MOM60-REPL 10, XSMOM 8+legs,
  SQUEEZE 6, GAPS 12, SECT-ROT 8, XU-XSMOM, XSMOM-EXITGRID 26, XSMOM-12-1
  32) contains **no implied-vol input anywhere and no conditioning of the
  two ALLOWED directions on any vol-spread**. Both enabling studies were
  sealed 2026-09-23 (`docs/desk/IVHIST-001.md`, `docs/desk/FORECAST-001.md`),
  after every census family ran.
- `artifacts/paper-trades/GATE-001.md`: the 48 gate cells were price/volume
  regime gates — spyup, breadth, spyvol x {MR, R3f+up, CONT, VOLSPIKE} x hold
  {1,2} — on rules that are now DEAD or BANNED, not on `xsmom_top3` /
  `pead_beat`. The closest prior (gates lift nothing to t >= 2 on the
  holdout) sets an explicitly pessimistic prior; the novel claim here is the
  primitive, not the gating method.
- `artifacts/paper-trades/SQUEEZE.md`: realized-vol squeeze breakouts —
  realized vol only, no implied input, killed by the conditional deflation.
- `src/tree_options/desk/signals.py`: `CONTEXT_ONLY` holds trend, breadth,
  vix_term — no IV-vs-forecast condition; `BANNED` `short_vol_gated` is a
  different, refuted family this slot deliberately does not touch.
- `docs/desk/FORECAST-001-results.md`: the desk displays `vrp_har`
  (`surface.py`) and defers the playbook cheap/fair/rich thresholds to a
  Wave 2 decision — display and playbook pending, never tested against
  trade outcomes in the census.
- What is NOT novel: conditioning as such (GATE-001's ON>OFF-but-no-lift
  result), and the deflation machinery this slot reuses. If a sibling
  campaign slot has since registered an IV-percentile gate, this slot
  withdraws the overlapping cells rather than double-running them.

## 8. Risks and the most likely way this fails

1. **Power (most likely sealed outcome).** The sealed window holds 3 XSMOM
   rebalances (9 entries; a tercile gate fires ~3) and roughly 8-15 PEAD
   beats across 26 reporters in 63 sessions. Expect NOT_EVALUABLE-SEALED for
   most xe cells on floors alone; the honest family result may be "question
   open, forward cards decide."
2. **Regime collinearity (most likely true refutation).** Cheap-vol terciles
   proxy calm markets — close to the breadth/trend states GATE-001 already
   showed cannot lift rules to significance. If the spread adds nothing past
   those conditions, the delta is ~0 and the family is DEFLATED.
3. **Fidelity contamination.** 28 of 29 IV names are `low-fidelity`. If the
   ATM-vs-variance-swap bias is time-varying (steepest in selloffs), the
   within-name percentile misranks exactly in the states that matter —
   spurious tercile assignment, biased ON cells.
4. **Hard window edge.** IV ends 2026-09-03, so the sealed window is pinned
   to 2026-08-31; extending it to the panel's 2026-09-23 edge would peek
   past the feature's coverage — forbidden.
5. **Schedule assumption inherited.** The HAR denominator carries
   FORECAST-001's declared earnings-schedule assumption; names whose forward
   schedule is not pinned through h=20 get `har_status: degraded` and the
   condition is withheld (`surface.forecast_block`), thinning PEAD cells.
6. **Exposure disclosure.** The desk has already displayed `vrp_har` values
   (`artifacts/desk-store/features/2026-09-22.json`); no outcome-conditioned
   selection has been made on returns, but the feature is not literally
   unseen. Registered before any conditioned outcome is computed.
7. **Options arm likely stays gated.** The capture may land monthlies-only
   (~55% bracket coverage in IVHIST-001's scan) and needs >= 126 sessions
   before any sealed scope-O window exists (~2027-01 earliest); WITHDRAWN is
   a live outcome.
8. **XSMOM convention.** The sealed no-skip construction
   (`close(t)/close(t-273)-1`, hold 20) is the evidence-backed rule; the
   prose 12-1 is out (TESTED, NOT PREFERRED, XSMOM-12-1.md). Using the wrong
   convention would silently change the conditioned book.
