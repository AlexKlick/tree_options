# JEPA-FILTER — the latent-surprise context filter (pre-registration slot)

```
program:              campaign-2026-09
slot_id:              jepa-filter
family:               JEPA-SURPRISE
configs_declared:     8            (budget: inner_loop.max_registered_configs = 32,
                                    research_protocol.yaml)
protocol_version:     0.2.2
registered_at_head:   f075997
source memo:          docs/theory/trade-jepa-exploration-2026-09-23.md
status:               PRE-REGISTRATION — no encoder fit, no signal scored,
                      no JEPA outcome viewed (INV-13 idiom)
sealing:              sha256 sidecar at campaign assembly (docs/desk pattern;
                      FORECAST-001.md.sha256 precedent)
```

This document is the sealed translation of the exploration memo named
above. The memo was translated, not redesigned: every choice the memo left
open is pinned here and marked **[PINNED]**. Where this document and the
memo differ in notation, this document governs (the difference is dating
convention only; see 3.2).

## 0. What was inspected before this registration was authored

Read in full: the source memo; `research_protocol.yaml`;
`artifacts/paper-trades/RESEARCH-LEDGER.md`; `src/tree_options/desk/signals.py`;
`src/tree_options/desk/rv.py`; `src/tree_options/desk/universe.py` and
`desk-universe.toml`; `docs/desk/FORECAST-001.md` and
`docs/desk/FORECAST-001-results.md`; `docs/desk/IVHIST-001.md`,
`docs/desk/IVHIST-001-results.md`, `docs/desk/IVHIST-001-verdict.json`;
`docs/theory/wave0-registration.json`; `src/tree_options/desk/indices.py`
(availability of the CBOE files). Read-only numeric inspections, and nothing
else: the panel's name count and first/last sessions per name, the count of
chain-universe names and union sessions, the META hole bounds, a 2025-01-09
presence check, and the first/last rows of the four index CSVs. No encoder
was fit, no surprise computed, no IC scored, no config outcome viewed.

## 1. Hypotheses (fixed before any run)

**Primary (the filter — this slot's reason to exist), one sentence:** if the
aggregate latent-surprise state carries regime information beyond the
volatility term structure, then the cross-sectional mean `S_u` of the
standardized latent forecast error of a PCA-anchored linear JEPA over the
11-feature equity+index state beats the incumbent context signal `vix_term`
(VIX/VIX3M − 1) on the Spearman rank correlation with forward 21-session SPY
realized variance over the sealed outer origins (one-sided paired block
bootstrap, p < 0.05), and the family is falsified otherwise.

**Companion core (a) (the clean falsifiable question the memo runs on the same
machinery), one sentence:** per-name standardized latent surprise ranks
forward h-session returns in the declared direction — long LOW surprise /
short HIGH surprise — with mean daily cross-sectional Spearman IC > 0 at
one-sided p < 0.05 (block bootstrap, lag ≥ h) on the sealed outer window at
h ∈ {5, 21}, and a significant association that does NOT survive partialing
out the XSMOM score and lnRV21 is recorded as re-labeled momentum/vol, never
promoted.

## 2. Declared direction and use

- The JEPA family enters the desk, if at all, through the **CONTEXT_ONLY**
  door only (`src/tree_options/desk/signals.py` line 53:
  `CONTEXT_ONLY = {trend, breadth, vix_term}`), competing with `vix_term`.
  It never points a direction: `ALLOWED_DIRECTION = {xsmom_top3, pead_beat}`
  and `require_direction_signal` refuses everything else
  (`src/tree_options/desk/signals.py`). A PASS here authorizes **gating or
  timing the expression of ALLOWED directions at most** — never a new trade.
- (a)'s long-low/short-high is a **scoring convention for a one-sided rank
  test, not a trade**: this slot places no orders, takes no fills, holds no
  position (the `fills` block of `research_protocol.yaml` is inert here; no
  option expression is authored, so `option_candidate_defaults` binds
  nothing). The census killed shorts at every horizon
  (`artifacts/paper-trades/RESEARCH-LEDGER.md`, DEAD) and naked short options
  are prohibited (`research_protocol.yaml` `short_options.policy`), so even a
  PASS on (a) authorizes nothing. Promotion of (a) to a direction input is a
  separate later sealed study (memo §2.3b).
- The reverse direction is NOT available post hoc on the same data: if inner
  folds show the opposite sign, the flipped variant must be registered as a
  new candidate consuming one of the four variant slots (see 5, FLIP), and
  only then may an outer run occur.
- Consequences, fixed now. On (b) PASS: `S_u` is recorded as a candidate
  context input beside `vix_term` for future sealed structure studies; it
  enters no live rule by itself. On (b) FAIL: `vix_term` remains the sole
  vol-regime context input from this challenge; the family closes; a
  successor needs its own sealed pre-registration (FORECAST-001
  failure-handling language, adopted verbatim). The XSMOM-TOP3 and
  PEAD-BIGSURPRISE sealed cards, and FORECAST-001's HAR and its forward
  monitoring, are untouched either way (memo §5).

## 3. Feature definition and exact data sources

### 3.1 State vector (d = 11, per name i, per session)

All features are computed from bars dated ≤ the session; complete-window
only, missing never zero-filled (`rv.py` None convention).

Equity block (7), from the split-adjusted panel:
- multi-horizon log returns: `r_1`, `r_5`, `r_21`, `r_63`
  (close-to-close, ln);
- variance proxies: `ln v` and `ln mean(v[-20..0])`, with `v` the
  FORECAST-001 daily proxy reused VERBATIM from
  `src/tree_options/desk/rv.py::variance_proxy`:
  `v_t = ln(O_t/C_{t-1})^2 + 0.5·ln(H_t/L_t)^2 − (2·ln2 − 1)·ln(C_t/O_t)^2`;
- liquidity: `ln(dollar volume) − ln(mean dollar volume[-62..0])`
  (dollar volume = close × volume of the panel bar).

Index block (4, shared by all names, varies over sessions only), CLOSE
columns of the stored CBOE CSVs: `VIX/VIX3M − 1`; `VIX9D/VIX − 1`;
21-session change in `ln VIX` (panel-session offsets); VVIX level.
Training-fold z-scored (per refit).

Cross-sectional treatment (day-one): each equity feature is replaced by its
per-session cross-sectional z (subtract the session's cross-sectional mean,
divide by its cross-sectional SD). This market-demeans by construction and
needs no sector map. INV-05: the z uses same-session data only and splits
are by session.

**[PINNED] Availability of the index block (INV-02).** The CBOE files update
in the late evening — `src/tree_options/desk/indices.py` documents
Last-Modified 01:51 UTC carrying the prior session's row — so the session-u
index close is not demonstrably available at the 16:00 ET decision instant.
Every index input (the state's index block at any session, AND the `vix_term`
comparator) therefore uses the index observation dated on or before the
PREVIOUS NYSE session. The identical one-session lag on challenger and
incumbent keeps the (b) paired test fair. The join is last-observation-on-
or-before, which is inert to calendar mismatches (verified: `VIX.csv` carries
a 2025-01-09 row, the panel does not — the panel-session grid never reads
it; VVIX.csv has no such row).

### 3.2 Surprise, standardization, aggregates (dating pinned)

The memo writes `s_t = ||g(z_t) − f(state_{t+h})||` and speaks of origins t.
**[PINNED]** The leakage-clean sealing indexes the surprise by its
COMPLETION session u: with v the quarterly refit in force at session u−h,

```
s_{i,u} = || g^(v)( z^(v)_{i,u−h} ) − f^(v)( state_{i,u} ) ||,
z^(v)_{i,·} = f^(v)( state_{i,·} )
```

a function of panel/index data dated ≤ u only; decision instant = session
close of u; both f and g are the vintage that made the forecast (no
cross-vintage basis comparison). Before ranking, `s_{i,u}` is divided by
vintage v's training-fold cross-sectional SD of s (basis drift across refits
is handled by this standardization; residual drift is disclosed as inner-fold
IC decay, never tuned away). Aggregate for (b): `S_u` = cross-sectional mean
of standardized `s_{i,u}`; dispersion twin (declared, disclosed):
cross-sectional mean of |standardized `s_{i,u}`|.

Future-poison test (part of the sealed run, precondition of evaluability):
altering any panel/index data dated after u must not change `s_{i,u}`.

### 3.3 Roster

The 35 chain-universe names on disk: the panel minus TQQQ/SQQQ
(`src/tree_options/desk/universe.py::CHAIN_UNIVERSE` reading
`desk-universe.toml`). Verified today: all 35 have first session 2021-09-13
and last session 2026-09-23; 1263 union sessions; the META vendor hole
2022-01-28..2022-06-09 drops the rows that touch it (documented in
`artifacts/paper-trades/RESEARCH-LEDGER.md` and
`docs/desk/FORECAST-001.md`). PLTR and SPCX joined `desk-universe.toml` on
2026-09-23 but are NOT in the panel (backfill in flight); they are LATER
ADDITIONS — widening the roster requires a successor registration, never a
silent edit. **[PINNED]** V4's cross-section is the 26 single stocks only
(the ETFs are the residualization regressors' own family); V1/V2/V3 score
all 35. **[PINNED]** Breadth floor: an origin with fewer than 25 complete
names is skipped and counted; if more than 10% of a window's origins are
skipped, the run is defective (operator ruling).

### 3.4 Sources (paths)

- `artifacts/paper-trades/ohlc-panel.json` — split-adjusted OHLCV; read
  under the SHARED flock on `ohlc-panel.json.lock`; sha256 recorded at run
  time (FORECAST-001 convention). 37 names on disk; the 35 chain-universe
  names all span 2021-09-13..2026-09-23.
- `artifacts/desk-store/indices/VIX.csv`, `VIX9D.csv`, `VIX3M.csv`,
  `VVIX.csv` — verified first/last rows: VIX 1990-01-02..2026-09-22;
  VIX9D 2011-01-04..; VIX3M 2009-09-18..; VVIX 2006-03-06.. (close-only
  column). Store format and provenance per `src/tree_options/desk/indices.py`
  and `indices/provenance.jsonl`.
- `src/tree_options/desk/rv.py` — the variance proxy, verbatim.
- `src/tree_options/desk/signals.py` — the XSMOM baseline score
  (`XSMOM_CONVENTION`: `close(t)/close(t-273) − 1`, the sealed TESTED
  construction per the 2026-09-23 operator ruling in
  `artifacts/paper-trades/RESEARCH-LEDGER.md`) and the
  ALLOWED/CONTEXT_ONLY/BANNED ledgers.
- `data/calendar/nyse_sessions_2018_01_02_2026_12_31.json` — protocol
  calendar; 2025-01-09 is listed but is NOT a session (ledger,
  2026-09-23); the panel-union session grid excludes it by construction.

### 3.5 Excluded inputs, with reasons (excluded, not forgotten)

- Option chains: recorded chains exist for exactly one session
  (`artifacts/desk-store/chains/`, 2026-09-22). No surface feature exists.
- IV history: `artifacts/desk-store/iv-history/vwap_atm.json` is fidelity-
  labeled `ok` for IWM ONLY (IVHIST-001 run 2, verdict of record;
  `docs/desk/IVHIST-001-results.md`) — excluded from any IV feature until an
  IVHIST-002 successor passes.
- Long-dated ATM option bars: capture IN FLIGHT (ETA ~2026-09-27). A
  surface-augmented state (ATM term, 25d skew, IV rank per
  `src/tree_options/desk/surface.py`) is a LATER pre-registered variant on
  the 2024-09+ subwindow with its own power calculation — never silently
  merged, and not data-gated inside this slot.
- Earnings calendar: the state uses RAW v, not FORECAST-001's earnings-
  cleaned v*, and carries no earnings feature (keeps the state free of the
  calendar's 2024-09 coverage boundary).
- Macro calendar: exists for 2026-01-01..2027-12-31 only — no historical
  macro dates, so no macro feature is possible.

## 4. Fold mapping (which sessions tune; which single sealed window evaluates)

- Initial training: 2021-09-13..2024-06-30 = 703 panel-union sessions
  (≥ `min_train_sessions` 252).
- **Inner folds — ALL tuning** (variant selection, direction confirmation,
  incrementality diagnostics): origins 2024-07-01..2025-12-31 = 378
  sessions. Quarterly anchored-expanding refits at the first session of
  2024-07, 2024-10, 2025-01, 2025-04, 2025-07, 2025-10 (6 refits); encoder,
  scalers, PCA basis, ridge/VICReg, surprise standardization, residualization
  coefficients (V4) and tercile cuts refit together — INV-07's "fit only
  inside training data" covers every one. Training rows of the refit at
  quarter Q satisfy `ordinal(t) + h + 5 < ordinal(first session of Q)`
  (the `purge_gap_rule` with the per-config horizon).
- **Sealed outer window — the single declared evaluation**: origins
  2026-01-02 through cutoff − h, where cutoff = the earliest last session
  among the 35 at run time (today 2026-09-23). 182 sessions in the span;
  complete origins 177 at h = 5 (~6,195 name-cells), 161 at h = 21
  (~5,635). The quarterly refit schedule continues across the outer window
  (2026-01, 2026-04, 2026-07) under the identical rule; the window is scored
  ONCE, after the inner-loop selection is frozen and recorded. One run, one
  verdict, no re-gridding.
- Geometry declarations vs the protocol block (the registration carries its
  own geometry, as wave-0's `geometry_grid_fridays` did — no protocol edit):
  `folds.shape = anchored_expanding` ✓; roll = quarterly = `roll_forward_
  sessions` 63 ✓; embargo 5 rides inside the declared purge gap h + 5
  (INV-06 analog: no training label overlaps an eval origin); label horizon
  is 5 (protocol) for the h = 5 configs and 21 (DECLARED here) for the
  h = 21 configs; the sealed outer span (182 sessions) replaces repeated
  63-session test folds as the single evaluation window. Inner scored
  origins require a vintage in force at u−h, so the first ~h inner sessions
  carry no surprise at h = 21; the realized inner origin count is reported
  at run time.

## 5. Config count and grid (8 ≤ 32; enumerated)

| # | config_id | variant | encoder f | latent k | predictor g | state | h |
|---|-----------|---------|-----------|----------|--------------|-------|---|
| 1 | JF-V1-H5  | V1 PCA-anchor | top-k PCs of training-fold standardized state, signs fixed by largest-abs loading | 8 | ridge of z_{u} on z_{u−h} | raw (cross-sectional z) | 5 |
| 2 | JF-V1-H21 | V1 | same | 8 | same | raw | 21 |
| 3 | JF-V2-H5  | V2 PCA-anchor | same | 16 | same | raw | 5 |
| 4 | JF-V2-H21 | V2 | same | 16 | same | raw | 21 |
| 5 | JF-V3-H5  | V3 VICReg-linear | linear f, trained jointly | 8 **[PINNED]** | linear g, in the joint objective | raw | 5 |
| 6 | JF-V3-H21 | V3 | same | 8 | same | raw | 21 |
| 7 | JF-V4-H5  | V4 sector-residual V1 | PCA-anchor | 8 | ridge | sector-residualized (26 single stocks) | 5 |
| 8 | JF-V4-H21 | V4 | same | 8 | same | sector-residualized | 21 |

V3 objective (one value, no grid): `||g(z_{u−h}) − z_u||^2 + λ_v Σ_j
max(0, 1 − sd(z_j))^2 + λ_c · (off-diagonal covariance penalty)` with
**[PINNED]** `λ_v = λ_c = 25.0` (the VICReg reference-scale anti-collapse
pressure), full-batch plain gradient descent, 2000 iterations, learning rate
1e-2, init from `numpy.random.Generator(PCG64(22))` scaled by 1e-2 — plain
numpy, deterministic, auditable. **[PINNED]** Ridge penalty for V1/V2/V4
g: `alpha = 1.0` on standardized latents.

**[PINNED] V4 residualization and sector map** (hand-declared, disclosed as
not machine-verified, quarterly re-check — the `ETF_HOLDINGS` precedent in
`src/tree_options/desk/events.py`): each single stock's 7 equity features
x_{i,u} are replaced by the residual of a training-fold OLS on {1, the same
feature of SPY, QQQ, IWM, and the mapped sector ETF}, coefficients per refit,
applied walk-forward. Map — SMH: NVDA, AMD, AVGO, INTC, QCOM (SOXX declared
unused: SMH/SOXX are near-duplicates, one is pinned); XLE: XOM; XLV: LLY,
UNH; XLF: JPM, V, MA; GLD: none; market trio only (no sector term): AAPL,
MSFT, GOOGL, AMZN, META, TSLA, ADBE, NFLX, CRM, PEP, KO, DIS, COST, HD, PG.
The residualized features then take the same cross-sectional z over the 26
single stocks.

**[PINNED] Selection rules (declared before any inner outcome is viewed):**

- SEL-a: per horizon h, the (a) outer scoring uses the variant with the
  highest mean inner daily Spearman IC in the declared direction among
  {V1, V2, V3, V4} at that h, requiring mean > 0; tie-break order V1, V2,
  V4, V3. If NO variant has positive mean inner IC at that h, the (a)
  question at that horizon is recorded FAIL on inner evidence (wrong-sign
  falsifier) with no outer scoring.
- SEL-b: the (b) outer scoring is FIXED to JF-V1-H21 (the memo's primary,
  collapse-proof, deterministic variant), independent of SEL-a — removing a
  researcher degree of freedom.
- FLIP: a direction flip discovered at inner folds must be registered as a
  new candidate consuming one of the four variant slots (displacing the
  lowest inner-ranked variant, recorded NOT_RUN); attempted configs stay
  ≤ 8; only then may an outer run occur for the flipped slot.

Nothing outside this table may be fit on the panel without a new
registration. All 8 configs plus SEL-a/SEL-b/FLIP are recorded in the
registry before any outcome is viewed (INV-13;
`trials.register_before_outcome: true`, storage sqlite).

## 6. Acceptance criteria and verdict vocabulary (fixed in advance)

Labels r_{i,u→u+h} = ln(C_{i,u+h}/C_{i,u}), complete windows only.
Machinery **[PINNED]**: circular block bootstrap on the daily origin-level
series, block length h (21 for the (b) targets), B = 2000 resamples,
resampling seed `numpy.random.Generator(PCG64(22))`; Newey-West (Bartlett,
lag h) t reported beside, never sole authority.

(a) track, per horizon, on the SEL-a config:
- **PASS** iff mean daily cross-sectional Spearman IC on the sealed outer
  origins, in the declared direction, one-sided p < 0.05.
- **PASS-REDUNDANT** iff PASS but the partial IC has |t| < 1. Partial IC
  **[PINNED]**: per outer origin, OLS-residualize the standardized surprise
  and the forward return on {XSMOM score, lnRV21 = ln mean(v[-20..0])}
  (intercept, per-session cross-section), Spearman of the residuals, same
  bootstrap; |mean|/SE < 1. Recorded, never promoted.
- **FAIL** = everything else, including the right sign with p ≥ 0.05 and the
  inner wrong-sign case of SEL-a.
- Power disclosure (memo §2.6): 161–177 outer origins detect only sizable
  effects (daily IC roughly 0.03–0.05); a borderline PASS defers to forward
  monitoring, which never promotes.

(b) track, on JF-V1-H21's S_u, identical origins for the incumbent:
- **PASS** iff S_u beats `vix_term_u` (VIX/VIX3M − 1, same one-session lag)
  on the Spearman rank correlation with forward RV21(SPY) = Σ_{j=1..21}
  v^SPY_{u+j} (`rv.py` proxy), one-sided paired circular block bootstrap
  (joint resample of origins, recompute both correlations) p < 0.05.
  **[PINNED]** The forward max-drawdown twin MDD21_u = max_{j=1..21}
  (1 − C^SPY_{u+j}/max(C^SPY_u, C^SPY_{u+1..u+j})) is reported with the
  identical machinery; a PASS with an MDD21 loss is recorded PASS with that
  disclosure (the memo's §2.6 names the forward-vol correlation as the
  verdict). The dispersion twin and the S_u-tercile card-gate lift
  (XSMOM-TOP3 / PEAD card outcomes by training-fold tercile) are DISCLOSED,
  never verdicts.

NOT_EVALUABLE: V3 collapse tripwire (any eval-fold latent dimension's
variance < 1e-2 × its training-fold variance — never quietly scored);
future-poison failure; complete outer origins < 126 (the protocol's own
validation-window minimum, borrowed as the power floor); the > 10% skipped-
origin defect of 3.3. Defective-run handling is by operator ruling only, as
IVHIST-001 had to do (`docs/desk/IVHIST-001-results.md`, run 1). The
vocabulary is CLOSED: PASS, PASS-REDUNDANT, FAIL, NOT_EVALUABLE.

Baselines, all on the identical origins: (1) no-signal null — IC = 0 with
the temporal-block bootstrap band as the gate **[PINNED]**, plus a disclosed-
only 3-seed randomized-score null spread in the wave-0 T-NULL style
(`score_seed` = jepa-null-1/2/3, deterministic hash-random scores;
`docs/theory/wave0-registration.json` calibration precedent); (2) the XSMOM
score itself (`close(u)/close(u−273) − 1`) scored as an IC; (3) cheap-
surprise controls ln mean(v[-20..0]) and |r_21| — if the learned surprise
does not beat its own vol-level proxy, the encoder added nothing (recorded
conclusion); (4) for (b), `vix_term` as the incumbent; HAR QLIKE stays the
vol-forecast incumbent and is untouched (`docs/desk/FORECAST-001-results.md`:
sealed PASS; `har.FORECAST_001_VERDICT = "PASS"`).

Three sealed verdicts total: (a) at h = 5, (a) at h = 21, (b). Multiplicity
is bounded by the 8-config cap and registration-before-outcome; results are
stamped with git sha, config hash, panel sha256, cutoff, and trial ids
(INV-14).

## 7. Novelty against the census evidence (files read)

- The census (`artifacts/paper-trades/RESEARCH-LEDGER.md`, ~1,220 cells:
  EXIT-GRID 52, SWEEP-001/002 1008, OOS-2021 16, SEMI-GATE 12, GATE-001 48,
  EXEC-001 4, HORIZON-001 18, PEAD-DEEP 20, MOM60-REPL 10, XSMOM 8 + legs,
  SQUEEZE 6, GAPS 12) contains NO learned-state family: every cell is a
  threshold/rule/gate over raw price, volume, earnings, or index features.
  No prior cell fits an encoder+predictor and scores the forecast error.
  This slot is not a re-run of anything in the ledger.
- Nearest neighbors, and why each is not this: **GATE-001** (48 gate cells,
  ledger DEAD: gates point the right way but none lifts any rule to t ≥ 2 on
  the holdout) — same CONTEXT door, but the gating variable there is a raw
  breadth/regime statistic; here the quantity is new and the verdict is a
  paired win vs `vix_term` on forward vol, not a rule lift. **Momentum**
  (BANNED: `mom_top_tercile`, `ts_momentum`, `mom60_h60`, `xsmom_60skip5`;
  DEFLATED/DEAD in the ledger) — the surprise rank could degenerate to
  momentum; that possibility is not a reason to withdraw, it is the
  PASS-REDUNDANT tripwire with the XSMOM score as a named baseline. **Vol
  families** (SQUEEZE dead after deflation; `volspike` banned) — the cheap-
  surprise control lnRV21 absorbs the vol-level explanation by design.
  **Short-horizon long rules** (OOS-2021 dead) — why (a) is information-
  only and can never become a direction signal on this evidence.
- **HAR/FORECAST-001 is the degenerate special case already in production**
  (`docs/desk/FORECAST-001.md`, `src/tree_options/desk/har.py`: fixed
  3-feature encoder, linear predictor, no collapse guard): the MVE
  generalizes a PASS-ing desk model rather than importing something foreign,
  and inherits HAR as its hardest baseline on the vol channel. The
  exploration memo itself (`docs/theory/trade-jepa-exploration-2026-09-23.md`
  §3) records that no encoder was fit and no signal scored before this
  registration; nothing in `docs/desk/` or the ledger has run a JEPA or
  latent-surprise cell.

## 8. Risks, and the most likely way it fails

- **Single most likely failure (memo §6.9, adopted): PASS-REDUNDANT** — the
  surprise collapses into a re-labeled volatility/momentum level, because an
  11-feature linear state may not contain structure the 3 HAR features and
  lnRV21 miss. The partial-IC diagnostic exists to say exactly this.
- Look-ahead: every transform (z-scoring, PCA, ridge/VICReg, surprise
  standardization, residualization, tercile cuts) refits inside training
  folds only; the future-poison test is part of the sealed run (a failure is
  a defect, not a re-run).
- Cross-sectional breadth: 35 names (26 for V4) caps detectable daily IC;
  the power disclosure is part of the verdict record.
- Non-stationarity/collapse: PCA-anchor makes V1/V2/V4 collapse-proof by
  construction; V3 carries the variance tripwire and dies NOT_EVALUABLE
  rather than quietly.
- Basis drift across refits: handled by the surprise standardization;
  residual drift shows up as inner-fold IC decay and is disclosed.
- Laundry risk: a BANNED family in latent disguise (`signals.py` BANNED,
  22 entries incl. `semi_reversion`, the mom variants) — PASS-REDUNDANT is
  the tripwire; CONTEXT_ONLY is the only door.
- Data edges: the META hole (rows dropped, never filled); 2025-01-09
  non-session (panel-union grid); VVIX close-only; the one-session index
  lag (pinned; identical on challenger and incumbent).
- Multiple testing: the 4-variant × 2-horizon cap, the three-verdict seal,
  and registration-before-outcome (INV-13).
- The (b) filter can be an expensive `vix_term`: the paired test on
  identical origins kills exactly this.
