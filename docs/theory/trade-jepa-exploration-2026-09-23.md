# Trade JEPA — exploration memo (NOT a registration)

Written 2026-09-23. Status: EXPLORATION. Nothing here is sealed, registered,
or run. No encoder was fit, no signal scored, no panel statistic beyond the
file inspections named in section 3 was computed. This memo proposes a
candidate family and a minimal viable experiment (MVE) for the operator to
accept, amend, or reject; if accepted, the pre-registration is a NEW sealed
document with its own sha256, in the FORECAST-001 manner.

## 1. Concept mapping

JEPA (LeCun; I-JEPA and V-JEPA are the design references) predicts the
EMBEDDING of future or occluded content, never the content itself. An encoder
f maps an input block to a latent z; a predictor g forecasts z of the target
block; the loss is the latent-space distance ||g(z_context) - f(target)||.
Because the decoder is absent, pixel-level (here: price-path) noise is never
the objective; because a trivial constant solution exists, the family needs
an anti-collapse term — VICReg's variance-covariance regularization, or an
anchor that fixes f independently of the loss.

Market analog, one line per object:

- state_t: the session-t feature block for one name (section 2.1);
- encoder f: state_t -> z_t in R^k (linear, or small MLP; CPU-sized);
- predictor g: z_t -> z-hat_{t+h}, a linear transition in latent space;
- surprise s_t = || g(z_t) - f(state_{t+h}) ||: how badly the world model
  tracked this name over h sessions — a per-name, per-horizon scalar.

The claim under test is not that g forecasts prices. It is that the ERROR of
a compressed linear world model is itself informative about forward risk and
return — that "the model lost track of this name" is a state variable.

Contrast with GEPA-style reflective search. GEPA-style loops search over
candidate RULES (strategies, prompts) by reflecting on evaluated attempts;
the search is the learning. JEPA is the opposite shape: a single learned
world model that SCORES states, with no rule search inside. They compose
rather than compete: a surprise score is exactly the kind of numeric input a
candidate registry consumes, and any later reflective search over expressions
would treat s_t as one more declared feature. This memo stops at the score;
no expression, order, or card follows from it.

## 2. Minimal viable experiment (equity-only, CPU, days not weeks)

### 2.1 State vector (MVE: panel + indices only)

Per name i, per session t (all features computed from bars dated <= t; the
panel is read under the shared flock, FORECAST-001 convention):

- multi-horizon returns: r_1, r_5, r_21, r_63 (log, close-to-close);
- variance proxies: ln v_t and ln mean(v_{t-20..t}), with v the FORECAST-001
  range/overnight proxy already defined in `desk/rv.py`/`desk/har.py` terms
  (reuse the existing definition verbatim; no new proxy is invented);
- liquidity: ln(dollar volume_t) - ln(mean dollar volume_{t-62..t}).

Seven equity features. Cross-sectional treatment, day one: subtract the
session's cross-sectional mean and divide by its cross-sectional SD (a
cross-sectional z). This market-demeans by construction (the mean IS the
panel drift) and needs no sector map. A sector-ETF-residualized variant
(single stocks residualized on SPY/QQQ/IWM plus the in-panel sector ETF) is
variant V4 below and needs one static hand-declared map (build item 4.3).

Index block (shared by all names; varies only over t): VIX/VIX3M - 1,
VIX9D/VIX - 1, 21-session change in ln VIX, VVIX level (training-fold
z-scored). Four features. The CBOE histories are multi-year (section 3), so
the whole state exists from the 2021-09 panel floor. No chain, surface,
IV-rank, or skew feature enters the MVE: those have 1 session of history
(section 3). Excluded with reason, not forgotten.

Dimension d = 11 per name-session. Roster: the panel names with full
history at the first training origin — today the 35 chain-universe names on
disk (TQQQ/SQQQ excluded; PLTR/SPCX are in the universe TOML as of
2026-09-23 but their panel backfill is a separate in-flight change; the
registration pins the roster and its panel sha).

### 2.2 Encoder, predictor, anti-collapse

- **V1/V2 (PCA-anchor, primary).** f = the top-k principal components of the
  training-fold standardized state (k = 8 for V1, 16 for V2; signs fixed by
  largest-abs-loading convention so refits are comparable). g = ridge
  regression of z_{t+h} on z_t fit on the same training fold. Collapse is
  impossible by construction: f depends only on inputs, not on the loss.
  Deterministic, numpy-only, auditable (an SVD and two lstsq calls).
- **V3 (VICReg-style, learned).** Linear f and g trained jointly to minimize
  ||g(z_t) - z_{t+h}||^2 + lambda_v * sum_j max(0, 1 - sd(z_j))^2 +
  lambda_c * (off-diagonal covariance penalty), lambda fixed in the
  registration (one value, no grid). Plain numpy SGD; no new dependency.
- **V4 = V1 with the sector-residualized state** (build item 4.3).

HAR is the degenerate special case already in production: FORECAST-001's
pooled log-HAR is exactly (f = fixed 3-feature "encoder" [ln v_d, ln v_w,
ln v_m], g = linear, no collapse guard, per-name intercepts). The MVE
therefore generalizes a PASS-ing desk model rather than importing something
foreign — and inherits HAR as its hardest baseline on the vol channel.

Surprise comparability across refits: s_{i,t} is divided by the training
fold's cross-sectional SD of s before ranking (the PCA basis rotates across
refits; the standardization is what keeps the rank meaningful). Collapse
tripwire (V3): if any eval-fold latent dimension's variance falls below
1e-2 x its training-fold variance, that run is NOT_EVALUABLE — never
quietly scored.

h in {5, 21} sessions, both registered up front. h = 5 matches the
protocol's label horizon; h = 21 needs its own declared geometry (4.2).

### 2.3 Signal definitions — direction declared before any evaluation

(a) **Per-name latent surprise, cross-sectional rank.** Ranks s_{i,t}
 across names each session; the scored object is the daily cross-sectional
 Spearman IC of the surprise rank against the forward h-session return.
 DECLARED DIRECTION: long LOW surprise / short HIGH surprise. Rationale,
 fixed now: a name the compressed model tracks poorly is in a fast-moving,
 informationally turbulent state; the desk's existing risk economy
 (cheap/fair/rich on IV vs forecast, low IV-rank preference) already favors
 the well-anticipated name, and low surprise is that preference's return
 side. The reverse direction is NOT available post hoc on the same data: if
 inner folds show the opposite sign, the flipped variant must be registered
 as a new candidate consuming one of the four slots, and only then may an
 outer run occur.

(b) **Aggregate surprise as a regime/vol filter on existing sealed rules.**
 S_t = cross-sectional mean of standardized s_{i,t} (and mean |.| as the
 dispersion twin, declared). Hypothesis: high S_t precedes higher forward
 21-session SPY realized variance and deeper forward max drawdowns. Primary
 metric: Spearman rank correlation of S_t with forward RV21(SPY) and forward
 max drawdown, against the incumbent context signal vix_term (VIX/VIX3M - 1)
 with the identical test on the identical origins. Secondary, descriptive
 only: XSMOM-TOP3 / PEAD card outcomes when gated by S_t tercile (fit on
 training folds). Because JEPA output is not an allowed direction signal
 (`desk/signals.py`: ALLOWED_DIRECTION = {xsmom_top3, pead_beat};
 vix_term is CONTEXT_ONLY), (b) is the only channel through which a JEPA
 quantity could touch a trade this year — as a context condition that shapes
 or withholds, never picks. Any promotion of (a) to a direction input is a
 separate later sealed study.

### 2.4 Splits (walk-forward; nothing after t is ever seen at t)

Panel: 2021-09-13 .. 2026-09-23 cutoff (1263 union sessions; the cutoff is
the earliest last session among the roster at run time, FORECAST-001
convention, recorded in the results).

- Training (initial): 2021-09-13 .. 2024-06-30 (~718 sessions; >> the
  protocol's min_train 252).
- Inner folds (ALL tuning: k, lambda, h choice, direction confirmation,
  incrementality diagnostics): origins 2024-07-01 .. 2025-12-31, quarterly
  anchored-expanding refits (6 refits; encoder, scalers, PCA basis, ridge,
  and the surprise standardization refit together — INV-07's "fit only
  inside training data" covers every one of these).
- **Sealed outer window, declared in advance: origins 2026-01-02 through
  cutoff - h.** At h = 21 that is ~164 complete origins x 35 names ≈ 5,700
  scored name-cells; at h = 5, ~184 origins. One run, one verdict, no
  re-gridding; a successor needs its own sealed pre-registration
  (FORECAST-001's failure-handling language, adopted verbatim).
- Purge/embargo: the protocol's purge rule scales with the label horizon;
  at h = 21 the registration declares purge gap = 21 + 5 sessions so no
  training label overlaps an eval origin (INV-06 analog). A future-poison
  test pins the whole path: altering any panel/index data dated after t
  must not change the signal at t.

### 2.5 Baselines (all run on the identical origins)

1. No-signal null: IC = 0 with a temporal-block bootstrap band (and, if the
   operator prefers the theory-lane idiom, a 3-seed randomized-score null
   spread in the wave-0 T-NULL style — the calibration precedent).
2. Incumbent cross-sectional return signal: the XSMOM score itself
   (close(t)/close(t-273) - 1) scored as an IC on the same origins.
3. Cheap-surprise control: ln mean(v_{t-20..t}) (and |r_21|) as a
   zero-parameter stand-in for s_t. If the learned surprise does not beat
   its own vol-level proxy, the encoder added nothing.
4. For (b): vix_term as the incumbent regime signal; HAR QLIKE stays the
   vol-forecast incumbent (untouched; (b) does not compete with FORECAST-001).

### 2.6 Metrics and acceptance (stated before any run)

(a) primary: mean daily cross-sectional Spearman IC on the sealed outer
window, in the DECLARED direction, one-sided p < 0.05 under a Newey-West /
block-bootstrap t on the daily IC series (lag >= h). Verdict names fixed
now: **PASS** = significant in the declared direction; **PASS-REDUNDANT** =
significant, but the partial IC after regressing out the XSMOM score and
lnRV21 has |t| < 1 (the surprise is a re-labeled momentum/vol proxy —
recorded, never promoted); **FAIL** = everything else, including the right
sign with p >= 0.05. Power disclosure: ~164 outer origins detect only
sizable effects (daily IC of roughly 0.03-0.05); a borderline PASS defers
to forward monitoring, which never promotes.
(b) primary: S_t beats vix_term on the forward-vol rank correlation with
one-sided paired block-bootstrap p < 0.05 on the outer origins. The
card-gate lift is disclosed, never a verdict.

### 2.7 Variant cap (multiple testing)

Exactly four pre-declared variants — V1 PCA8, V2 PCA16, V3 VICReg-linear,
V4 sector-residualized PCA8 — times two horizons = 8 registered
configurations, inside the protocol's inner_loop budget of 32. Nothing else
may be fit on the panel without a new registration. Registration precedes
outcome viewing (INV-13 idiom).

## 3. Data / compute feasibility (verified facts, then estimates)

Inspected for this memo and nothing else: the panel's name count and
first/last sessions (37 names on disk, 2021-09-13 .. 2026-09-23, 1263 union
sessions, ~6 MB); directory listings of the chain store and `features/`
(1 session each: 2026-09-22); first/last rows of the index CSVs; the venv's
package listing; the source files cited above.

- **5-year equity block: available.** 37 names x 1263 sessions ≈ 46.7k
  name-sessions; minus per-name warmup (63 + h), the META vendor hole
  (2022-01-28..2022-06-09, declared in FORECAST-001), and the
  late-listed pair once added. Rows are plentiful; the binding constraint
  is cross-SECTIONAL breadth (35 names), which caps detectable daily IC.
- **Index term block: available in full.** Stored histories run VIX
  1990-01-02, VIX9D 2011-01-04, VIX3M 2009-09-18, VIX1Y 2007-01-03, VVIX
  2006-03-06, SKEW 1990-01-02, DTB3 1954-01-04, all through 2026-09-22.
  Full coverage of the panel window with decades of margin.
- **Option-surface features: NOT available for the MVE.** Recorded chains
  exist for exactly one session (2026-09-22); `features/` holds one file.
  The IVHIST-001 VWAP ATM history (Polygon cache, 2024-08-26+) covers the
  original 29 names but is fidelity-labeled ok for exactly one (IWM). The
  long-dated ATM cache (35 names, 2024-09..2026-09, ~23.5k series) is being
  built now (3.3-day run launched 2026-09-23). Consequence, fixed: the MVE
  is equity+index only; a surface-augmented state (ATM term, 25d skew, IV
  rank, term slope per `desk/surface.py`) is a LATER pre-registered
  variant on the 2024-09+ subwindow with ~2 years of history and its own
  power calculation — never silently merged.
- **Dependencies: numpy 2.5.2 only.** No torch, no sklearn, no pandas in
  the venv. V1/V2/V4 are closed-form (SVD + lstsq). V3's VICReg objective
  is a small hand-rolled numpy SGD (~100 lines). Adding torch-cpu would be
  a build item, not a requirement; GPUs are fully reserved by resident
  lanes and nothing here wants them.
- **Cost estimate.** State construction: one pass over a 6 MB panel,
  minutes. Per refit: PCA on ~700 sessions x 35 names x 11 features —
  milliseconds. The full program (4 variants x 2 horizons x 6 inner refits
  + outer run) is minutes of CPU inside `host-work --profile test`, well
  under FORECAST-001's footprint. The real cost is analyst days: ~2-4 for
  the registration, implementation, sealed run, and results doc.
- **Risks.** (i) Look-ahead: every transform (z-scoring, PCA, ridge,
  surprise standardization, tercile cuts) refits inside training folds
  only; the future-poison test is part of the sealed run. (ii)
  Non-stationarity/collapse: PCA-anchor makes V1/V2/V4 collapse-proof; V3
  carries the variance tripwire (2.2) and dies NOT_EVALUABLE rather than
  quietly. (iii) Basis drift across refits is handled by the surprise
  standardization; residual drift shows up as IC decay across inner folds
  and is disclosed, not tuned away. (iv) Multiple testing: the 4-variant
  cap plus registration-before-outcome. (v) Laundry risk: the BANNED list
  in `desk/signals.py` (semi_reversion, mom variants, et al.) — the
  PASS-REDUNDANT verdict exists so a dressed-up banned family cannot
  promote through the latent door.

## 4. Protocol compliance

- **Hypothesis text template** (per registered config, wave-0 slot idiom):
  family JEPA-SURPRISE; slot id; encoder/predictor spec; h; declared
  direction and rationale; the state-vector definition by reference; the
  falsifiers (IC inside the null band; wrong sign; PASS-REDUNDANT partial
  IC). One paragraph, no free parameters left unstated.
- **Fold mapping to `research_protocol.yaml`:** `folds.shape =
  anchored_expanding` (quarterly refits); `min_train_sessions: 252`
  satisfied; label horizon declared per config (5 or 21) with the purge gap
  scaled to h + 5 under the `purge_gap_rule` reading; INV-05 (same-session
  rows stay in one split — the cross-sectional z makes this mandatory, and
  it holds since splits are by session); INV-07 (fit-on-train, section
  2.4); INV-13 (register before outcome); INV-14 (stamped results: git
  sha, config hash, panel sha, cutoff — the FORECAST-001 results-doc
  idiom). Inner loop = the 2024-07..2025-12 origins; the sealed outer
  window = the single declared evaluation. Selection never touches the
  outer fold; forward monitoring follows the desk rule (demote-or-hold,
  never promote).
- **Artifacts/evidence:** a sealed pre-registration md + sha256 sidecar
  (docs/desk pattern), machine-readable results JSON beside it, and a
  registration record. Verdict vocabulary fixed in 2.6: PASS,
  PASS-REDUNDANT, FAIL, NOT_EVALUABLE — plus "defective run" handling by
  operator ruling only, as IVHIST-001 had to do.
- **Build items (named, not built):**
  1. a desk-lane registration record for exploratory signal families — the
     theory-lane JSON shape in `docs/theory/*-registration.json` already
     fits (program, slots, hypotheses, criteria, registered_at_head);
  2. the h = 21 fold-geometry declaration (the protocol block fixes
     label_horizon 5; the registration carries its own geometry, as
     wave-0's `geometry_grid_fridays` did — no protocol edit required);
  3. the static single-stock -> in-panel sector ETF map (precedent:
     `ETF_HOLDINGS` in `desk/events.py`: hand-declared, disclosed as not
     machine-verified, quarterly re-check) — needed only for V4;
  4. optional torch-cpu dependency if a deeper encoder is ever wanted;
  5. PLTR/SPCX panel backfill (in flight, separate change) if the roster
     is to widen.

## 5. Relationship to existing repo work

- **HAR / FORECAST-001 is the hand-built world-model baseline.** A pooled
  linear predictor of a fixed 3-feature compression of variance IS a JEPA
  with an identity-by-hand encoder. The MVE's burden is therefore precise:
  beat the desk's own degenerate JEPA where it competes (vol channel,
  baseline 3 in 2.5) and beat zero-parameter surprise controls, or record
  that learning f added nothing. HAR's sealed PASS and its forward
  monitoring are untouched by this memo.
- **Synthetic-era discipline (M2) governs any world-model simulation.** The
  M2 rule — model-generated evidence carries dataset provenance
  (dataset=synthetic/vN), and no real-market performance or discovery
  claim may rest on it — extends verbatim: any backtest whose FILLS or
  price paths are simulated by a JEPA world model (g rolling forward,
  decoding or synthesizing states) is SYNTHETIC-CLASS evidence, labeled and
  stored as such, never promotion-grade. Promotion runs only through the
  sealed real-data lane on the real panel. The MVE as specified never
  simulates a fill, so it stays on real data end to end.
- **The banned/context ledgers apply unchanged.** JEPA output enters, if at
  all, through the CONTEXT_ONLY door (alongside vix_term) until a sealed
  direction study says otherwise; the BANNED families stay banned even in
  latent disguise (the PASS-REDUNDANT check is the tripwire).

## 6. Verdict-ready summary

1. Proposal: a linear, CPU, numpy-only JEPA (PCA-anchored encoder, linear
   latent predictor) over an 11-feature equity+index state; surprise =
   latent forecast error.
2. Two declared uses: (a) per-name cross-sectional surprise rank, direction
   LONG-low/SHORT-high fixed before any run; (b) aggregate surprise as a
   regime/vol context filter competing with vix_term.
3. One sealed outer window (2026-01-02 .. cutoff - h) after inner-fold
   tuning on 2024-07..2025-12; four pre-declared variants, 8 registered
   configs, inside the 32 budget.
4. Baselines: null band, the XSMOM score, a zero-parameter vol-level
   "cheap surprise", and vix_term. Verdicts named in advance: PASS,
   PASS-REDUNDANT, FAIL, NOT_EVALUABLE.
5. Everything the MVE needs exists today: 5y panel, multi-year CBOE index
   histories, numpy. Nothing needs a GPU; compute is minutes.
6. The option-surface state is deferred by data, not choice: chains exist
   for one session; the IV history is one-name-ok; the long-dated cache
   lands in days and unlocks a later 2024-09+ variant.
7. Expected information value: medium-high even on FAIL — it prices
   whether a learned compression beats the desk's hand-built one (HAR
   kinship), and either answer closes a family the operator has not yet
   tested.
8. Cost: 2-4 analyst days, minutes of CPU, zero new hard dependencies,
   three small build items (registration record, h=21 geometry, optional
   sector map).
9. Single most likely failure: the surprise collapses into a re-labeled
   volatility/momentum level — PASS-REDUNDANT — because an 11-feature
   linear state may not contain structure the 3 HAR features and lnRV21
   miss; the incrementality diagnostic is designed to say exactly this.
10. Recommendation: run it, as (b)-first if the operator wants trade
    adjacency sooner (context gating on sealed rules), and (a) as the
    clean falsifiable core. Do not run any variant outside the four.
