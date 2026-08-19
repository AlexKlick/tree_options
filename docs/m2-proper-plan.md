# M2 proper — labels, models, trials, validation gate, equity backtest

Status: DRAFT for owner sign-off 2026-08-19. Base: `main` @ `f459577`
(the PR #3 merge commit; tree-identical to reviewed head `363f774`).

## 1. Context and owner decisions (recorded verbatim)

M2 entry criteria are met per `docs/m2-synthetic-era-plan.md` §6. The
owner decisions of 2026-08-19 (this campaign's /grill-me round):

1. **Power stratum added (pre-trial registry extension).** The frozen
   coefficient 0.002 gives ~50% single-world detection power (arithmetic
   in §7), so the validation registry is extended BEFORE any trial with
   two full-scale alpha worlds at coefficient 0.005 (`706`/`707`).
   Worlds 701–705 stay byte-frozen and untouched. The extension is
   declared from first-principles arithmetic, not from peeking at trial
   results — no M2 trial exists yet.
2. **Both label horizons.** H=1 (aligned to the planted effect; primary
   arm) and H=5 (protocol-purge-canonical; sensitivity arm) are both
   built and both pre-registered. Expected power is pre-stated per
   horizon in §7 and is part of the registration.
3. **numpy is added now** as a declared runtime dependency — the
   "declared, protocol-relevant change" the M2-era plan reserved. The
   `synth/` package stays stdlib-only (any byte change stales the world
   registry pin). Determinism obligations are in §3.D.
4. **A minimal long-only equity backtest lane ships in M2** (top-quintile
   by model score, next-open execution, fees via the FeeModel seam,
   FIFO ledger). Synthetic PnL is machinery validation only — the
   amended M2 rule stands verbatim: *"M2 pipeline machinery may be built
   and validated on synthetic data with known ground truth; every
   artifact must carry dataset provenance (dataset=synthetic/vN); and
   any real-market performance or discovery claim remains prohibited
   until real-data gates are green."*

Standing rules carried forward unchanged: no GitHub Actions (local gates
are release authority); expensive commands run once teed to a log; red-first
tests; merges that preserve reviewed evidence use normal merge commits,
never squash; the mutation harness must show zero of everything except
KILLED; `research_protocol.yaml` stays frozen (no edit in this campaign —
nothing here weakens an invariant, so no version bump is triggered).

## 2. What exists and is reused (no duplication)

- **Planted effect (the detection target)**: 1-session cross-sectionally
  demeaned momentum — `ret_{t+1} += coefficient · (ret_t − cross_mean)`,
  registry coefficient 0.002 (weak stratum) and 0.005 (power stratum);
  null worlds structurally lack the code path.
- `LabelEvent` schema (`schemas/features.py:39-55`) — defined, zero call
  sites; `PanelRow.label` slot reserved. This campaign constructs it.
- `PointInTimeDataset` (`data/authority.py`): `visible_bars`,
  `universe_as_of`, `features_as_of` (V1: `ret_1`, `dol_vol`) — the M2
  label/feature surface. No authority API changes planned.
- `FittingGuard` (`guards/fitting.py`): fit-once/apply-only core; the
  module docstring's "full pipeline enforcement lands with M2" is
  discharged here (§3.D).
- `TrialRegistry` (INV-13) + `ArtifactStamp` (INV-14) + the deliberately
  deferred snapshot→`dataset_manifest_hash` wiring (synthetic-era
  criterion 7 partial) — delivered in §3.F.
- `WalkForwardSplitter` + `check_folds`: H=5 purge, val 126, test 63,
  roll 63, min_train 252 → first test at ordinal 388, ~29 folds, ~1,827
  test sessions per full world. Small worlds yield zero folds, so
  registered dev trials run on the full worlds 103/104 only; small
  worlds remain unit-test fixtures.
- `ledger/book.py` (FIFO, exact Decimal) + `ledger/fees.py` (FeeModel
  seam) + `guards/fills.py` (fill engine) — consumed by §3.G, unchanged.
- Per-session Spearman rank IC is invariant to within-session monotone
  transforms: labels are stored as RAW forward log returns; demeaning
  is implicit in the rank statistic. No demeaned copies of anything.

## 3. Workstreams

### A — Registry extension: power worlds (blocking, pre-trial)

Add `synth-v1-val-alpha-706` / `synth-v1-val-alpha-707`: full scale (500
securities × 2263 sessions), coefficient 0.005, fresh seeds, pinned via
`scripts/verify_worlds.py` (quality-gated regeneration, expected hashes +
generator pin recorded — the pin itself must remain byte-identical since
`synth/` is untouched). Registry note records the power rationale and
the owner decision. Red-first: the world-registry test's exact
(seed, kind, world_id) triples gain 706/707; dev/validation disjointness
re-proven; full-pool verify becomes WORLDS_OK=11. The amendment window
CLOSES when the first M2 trial is registered — after that the registry is
frozen through the campaign, no exceptions.

### B — Label construction over visible_bars

`labels/build.py`: for (security, decision_session, H∈{1,5}) → forward
H-session log return from the decision session's close, computed only
from bars with `available_at <= decision_at` plus the forward window's
bars (label outcome, not input). Label window = sessions (d+1 … d+H);
`observed_at` = publication instant of the d+H bar (23:00 UTC
convention); insufficient forward history (delisting inside the window,
world end) ⇒ no label — absent, never imputed. Purge alignment: labels
whose window touches a fold's eval sessions are excluded from that
fold's training material by construction (the splitter's existing
geometry; re-proven by test). Red-first tests: lookahead (label value
unknowable before `observed_at`), wrong-window, horizon-boundary,
delisting-inside-window, end-of-world truncation, and
splitter-purge consistency.

**Pre-implementation correction (2026-08-19, discovered before any label
code was written; the signed text above is preserved as the plan of
record and this correction supersedes its label formula).** The
publication instant (23:00 UTC same-date) is 2–3h AFTER
`session_close`, so at the decision instant of session d the last
VISIBLE bar is session d−1 and the freshest feature is `ret_{d−1}`.
The planted effect is `ret_t ← β·ret_{t−1}`, so the first-order
alignment is feature `ret_{d−1}` → `ret_d` — but the signed window
(d+1 … d+H) EXCLUDES `ret_d`, capturing the effect only through the
second-order chain (β² ≈ 4e−6 at the weak stratum) and contradicting
the signed §7 arithmetic (per-session IC ≈ coefficient, which the
owner's power-stratum decision was priced on). The label is therefore
defined from the decision-time information boundary: with b = the
calendar session immediately preceding d,

    value        = ln(close[b+H] / close[b])
    label_window = (b+1, b+H) == (d, d+H−1)

The base close is the last decision-time VISIBLE close (the d-session
bar legitimately postdates the decision); the window is strictly
post-decision; a security whose last visible bar is older than b
(lapse straddling the decision) contributes no label that session; the
bar series must be contiguous b+1…b+H (any gap — delisting, lapse —
means no label). Purge safety: for a train session s the window ends
at s+H−1 while the gap invariant requires ordinal(first eval) >
s+H+E — strictly MORE margin than the original (d+1 … d+H) window.
`observed_at` = publication instant of the b+H bar; label provenance
(`source`/`source_record_id`) carries that end bar's provenance, since
its publication is what makes the value knowable. The executable PnL
path (backtest, §3.G) necessarily starts at open(d+1) and cannot earn
ret_d — the label/PnL gap is an honest consequence of the publication
lag and is recorded in the evidence doc, not papered over.

### C — Features v2 (cross-sectional, PIT)

Extend feature construction with the momentum family and controls:
`mom_1` (= `ret_1`, aligned to the planted effect), `mom_5`, `mom_20`
(wrong-horizon controls — pre-registered expectation ≈0 incremental IC
on alpha worlds), `dol_vol_20` (liquidity). Per-session cross-sectional
assembly over the PIT universe; every feature carries full provenance
fields (INV-01/02) and passes `AvailabilityGuard.audit_panel` with zero
rejections. No truth-sidecar access anywhere in production code (import
boundary holds; the boundary lint stays in the suite). Red-first:
availability rejection paths, too-little-history absence, control-feature
neutrality on a tiny hand-checked world.

### D — Model + numpy + FittingGuard pipeline enforcement

- Declare `numpy` in `[project.dependencies]`, pin via `uv.lock`.
- `models/pipeline.py`: Standardizer + Ridge (normal equations,
  `numpy.linalg.solve`) behind one `fit()`/`apply()` interface. Fitting
  discipline is enforced BY the pipeline, not by convention: the
  pipeline calls `FittingGuard.fit_on` at fit time and
  `FittingGuard.apply_to` at score time; `assert_fit_excludes` runs
  against every eval session set before scoring. This discharges the
  INV-07 pipeline-enforcement debt (the yaml's `enforced_by:
  registry.budget` note stays as-is — frozen file — and the evidence doc
  records the mapping).
- Determinism: BLAS threading pinned (`OPENBLAS_NUM_THREADS=1` and
  siblings) inside the gate and trial runner; k×k solve with k≤6.
  Red-first: refit refused, score-before-fit refused, eval-session
  leakage raises, and a two-run + cross-process byte-identical
  coefficients test.

### E — Evaluation statistics

`evaluation/stats.py` (pure functions): per-session cross-sectional
Spearman IC (feature score vs label), fold-level and pooled t-statistics
(mean/std·√T over test sessions), null-world fold-level rejection rates,
binomial FP assessment, and the backtest summary stats (§3.G). Red-first
tests on hand-constructed panels with known IC, tie handling, and
singleton-cross-section edges.

### F — Trial runner + provenance wiring (INV-13/14, deferred criterion 7)

`trials/run.py`: compose A–E into registered trials. Wiring delivered:
world id in `TrialScope.feature_set_id`; `snapshot.manifest
.content_sha256` → `TrialRecord.dataset_manifest_hash` and
`ArtifactStamp.dataset_manifest_hash` via `build_stamp` (clean-worktree
rule stands); metrics written as stamped artifacts (`write_artifact`)
including the run log URI. State machine REGISTERED→RUNNING→COMPLETED/
FAILED with the provenance triple match at `mark_running`. Dev phase
pre-registered config list (budget 32, dev registry):
  - D1 H=1 ridge, world 104 — expect t≈1.6 (weak stratum arithmetic)
  - D2 H=1 ridge, world 103 (null) — expect NO rejection (FP dry-run)
  - D3 H=5 ridge, world 104 — expect t≈0.7
  - D4 H=1 univariate `mom_1` IC baseline, world 104 — measured
    per-session IC must land near the predicted 0.002 (empirical
    confirmation of the power arithmetic BEFORE any validation peek)
Backtest evaluation rides inside each trial's metrics (no extra trial
ids). Validation phase uses a separate sealed registry DB created at
gate time, budget 32, never reused.

### G — Minimal long-only equity backtest

`backtest/equity.py`: per rebalance session, rank PIT universe by model
score, hold the top quintile equal-weight; execution at next session's
open (execution session strictly after decision session, per timestamp
semantics); fills through the fill engine; costs via a FeeModel fixed
for the campaign (5 bps per side); positions through the FIFO ledger.
Outputs: total return, per-session return series, turnover, hit-rate
against labels. Delistings inside a holding period resolve via the
world's delisting bars (bankruptcy loss applies). Red-first: no
execution on the decision session, fee application, delisting
resolution, and ledger reconciliation to the penny. All PnL artifacts
carry dataset=synthetic/v1 provenance and support no claim beyond
machinery validation (§1.4).

### H — Validation gate (one-shot, sealed)

Run once, after D1–D4 confirm the arithmetic, on the frozen pool:
nulls 701/702/703 (FP arm), weak 704/705 (reported, no requirement —
pre-declared coin flip at 0.002), power 706/707 (power arm). Two
horizons evaluated as registered. Pass criteria:
- **FP arm (primary, H=1)**: pooled-null t within ±2.5; fold-level
  rejection rate across the 3 null worlds × ~29 folds (n≈87) does not
  exceed the exact one-sided binomial 95% threshold above 5% (threshold
  computed and frozen into the evidence doc before the run; planning
  estimate ≈9–10 of 87). H=5 must pass the same FP arm independently.
- **Power arm (primary, H=1)**: rejection on BOTH 706 and 707
  individually (t≈4 each; joint success probability >0.999 under the
  planted effect).
- H=5 power expectation pre-registered as ≈1.8 on 706/707 (borderline —
  a non-rejection is the expected outcome, not a failure).
- Weak stratum 704/705 reported with no pass/fail semantics.
Outcome recorded whatever it is; a FAIL is evidence, not a rerun
trigger. Any re-run of the gate requires a new campaign and an owner
decision recorded in the evidence doc.

### I — Evidence, mutants, review (closeout)

`docs/m2-proper-evidence.md`; new owner-scoped mutants for every new
module (numbering continues past M84; M82 stays retired); gate once at
the final head teed to a retained log; clean-clone proof; ONE bounded
Codex review at the stable head; merge by normal merge commit. Every
remediation commit bumps the retained-log pointer (standing rule).

## 4. Acceptance criteria

1. Worlds 706/707 pinned; full-pool verify `WORLDS_OK=11 MISMATCH=0`,
   generator pin unchanged; registry amendment note records the
   rationale and the pre-trial timestamp.
2. Labels: both horizons constructed for full worlds; the red-first
   battery green; no label is input-visible before `observed_at`
   (proven by test, not assertion).
3. Features: `audit_panel` zero rejections on generated panels;
   provenance fields complete; control features neutral on a
   hand-checked alpha world.
4. Pipeline: refit/leakage/score-before-fit all raise; ridge
   coefficients byte-identical across runs and processes; numpy pinned
   and BLAS-threading pinned.
5. Trials: D1–D4 registered→completed with matching provenance triples;
   stamped metrics artifacts on disk; dataset lineage visible end-to-end
   (world → manifest hash → trial record → artifact stamp).
6. Backtest: ledger reconciles exactly; execution/session ordering
   invariants hold; PnL artifacts stamped with synthetic provenance.
7. Gate run: sealed registry; FP and power arms evaluated exactly as
   §3.H pre-registers; outcome recorded verbatim.
8. Suite + mutations green at final head (0 failed / 0 skipped;
   SURVIVED=INVALID=MUTATION_DRIFT=HARNESS_ERROR=0); clean-clone
   ARTIFACTS_IDENTICAL=1; one bounded review at the head; merge-commit
   instruction on the PR.
9. Scope: `research_protocol.yaml` byte-unchanged; `synth/` byte-
   unchanged (registry pin proof); no real data; no real-market claim
   anywhere; short legs nowhere.

## 5. Non-goals / deferred

Options overlay (v2, behind the M3 spike); options backtester
(the README's later milestone — this campaign's equity backtest is the
machinery seam, not that milestone); real vendor data; minute bars;
model families beyond ridge; hyperparameter search beyond the
pre-registered list; any tuning against validation worlds in ANY
iteration of dev work.

## 6. Entry criteria unlocked (next milestones)

A green M2 gate unlocks: the options backtester milestone (consumes the
backtest seam + M3 schema); real-data campaign planning (vendor
decision + budget, holdout re-declaration per the standing correction).

## 7. Pre-registered power arithmetic (frozen into this packet)

Per-session population rank IC ≈ coefficient (ρ = β·σ_f/σ_l with
σ_l ≈ σ_f by construction). Cross-section n≈370 ⇒ per-session IC SE ≈
0.053. Test sessions per full world ≈ 1,827 (~29 folds × 63).
Pooled t = (ρ/0.053)·√1827:

| horizon | coefficient | single-world t | expectation |
|---|---|---|---|
| H=1 | 0.002 (704/705) | ≈1.6 | coin flip; reported, no requirement |
| H=1 | 0.005 (706/707) | ≈4.0 | power arm; reject on both |
| H=5 | 0.002 | ≈0.7 | expected non-rejection |
| H=5 | 0.005 | ≈1.8 | borderline; non-rejection is success-consistent |

Pooling two worlds multiplies t by √2 where stated. D4 must land within
±20% of the predicted per-session IC on world 104 before the gate runs;
a larger deviation halts the campaign for root-cause, not silent
adjustment.

## 8. Risks

- **numpy determinism across hosts**: mitigated by uv lock + single-
  thread BLAS pins + the cross-process determinism test; if the clean
  clone still diverges, the fallback is a pure-Python solve for k≤6
  (declared deviation, owner notified).
- **Registry amendment discipline**: the 706/707 window closes at the
  first trial registration; the registry test enforces the frozen state
  thereafter.
- **Fat-tail SE inflation**: t(5) idio widens the empirical IC SE vs
  the Gaussian 0.053; D4's measured check absorbs this before the gate.
- **Backtest scope creep**: the lane is deliberately minimal (one
  portfolio rule, one fee model, no tuning); anything beyond ships in
  the options-backtester milestone.

## 9. Pre-gate corrections (2026-08-19, owner-ruled after dev trials D1–D4)

The signed text above is preserved; these corrections were ruled on
BEFORE the sealed gate ran, from dev-world evidence only (all four dev
trials COMPLETED at head 84027cd; artifacts under
artifacts/m2-proper-dev/).

1. **D4 tripwire re-calibrated.** The §7 ±20% band on the measured mean
   IC ignored estimation noise: the band is 0.28 standard errors of the
   statistic (measured session-level IC sd 0.056, n≈1,546 sessions ⇒
   SE(mean) ≈ 0.00142), i.e. a ~78% false-fire probability when
   everything is correct. Corrected criterion: the measured pooled t
   must lie within 2 SE of the predicted t. D4 measured t=2.048 against
   a corrected prediction of ≈1.4 — an ordinary 0.65σ draw, PASS. The
   measured per-session IC SE came in +5% over the Gaussian plan (0.056
   vs 0.053) — the mild fat-tail inflation §8 anticipated.
2. **H=5 false-positive arm goes pooled-only.** D3's 8/29 fold-level
   rejections (with pooled t=0.47) prove that overlapping H=5 label
   windows (adjacent decision sessions share 4 of 5 window sessions)
   autocorrelate per-session ICs inside a fold, inflating fold-level
   |t| in both directions. H=5 FP is therefore evaluated ONLY on the
   pooled-null |t| ≤ 2.5 criterion; the fold-level exact-binomial arm
   (threshold computed from the actual fold count; 8 of 87 at the
   planning estimate) applies to H=1, where D2's non-overlapping labels
   proved it honest (2/29 on the null dev world).
3. **Power arm is the univariate aligned score.** D1 showed the
   4-feature ridge dilutes the aligned signal ~3× (t=0.77 vs D4's
   2.05); scaled to the power stratum the ridge sits at t≈1.9 (coin
   flip) while the univariate mom_1 sits at t≈3.7. PRIMARY power arm =
   univariate H=1, reject on BOTH 706 and 707. The ridge runs as a
   REPORTED secondary on 706/707 at both horizons and is not a gate
   criterion.

Sealed trial set (pre-declared before the run; 16 registered trials):
univariate × {701, 702, 703, 706, 707} × {H1, H5} (10),
univariate × {704, 705} × {H1} (2, weak stratum, reported only),
ridge × {706, 707} × {H1, H5} (4, secondary, reported only).

## 10. Gate #1 disposition: corrected power gate #2 (2026-08-19, owner-ruled)

Sealed gate #1 verdict: **FAIL** (exit 4), recorded verbatim in
`docs/m2-proper-evidence.md` §5 and the retained log. The FP arm passed
all three criteria (pooled |t| 0.21 H1 / 1.06 H5 vs the 2.5 limit; 3/87
fold rejections vs 8 allowed). The power arm failed on one of two
worlds: 706 t=1.51 < 1.96 (707 t=2.93 passed). Root cause is §7's power
arithmetic, which assumed realized per-session rank IC ≈ planted
coefficient and ignored the drift-wall attenuation already measured by
the workstream-C alignment pin (0.187 realized at coefficient 0.5 =
37%): worlds 706/707 realized 43%/84% of the coefficient, an ordinary
draw from the attenuated band, mapping to t ∈ [1.5, 3.1] — exactly what
was observed. A FAIL is evidence, not a rerun trigger (§9); the owner
has ruled a NEW power lane in the SAME campaign. Gate #1, its registry,
artifacts, and FAIL record are immutable and stay in the packet.

**Owner ruling (2026-08-19, disposition question answered "New power
lane, same campaign"):** add power worlds 708/709 (fresh seeds, never
generated or observed before this amendment) at coefficient 0.01, sized
so that even the pessimistic attenuation draw measured at gate #1
(0.43×) yields per-world t ≈ 3.1. Run sealed gate #2 over them with
power-only criteria. The FP arm is NOT re-run: it passed at gate #1 and
worlds 701–705 are frozen forever — no validation payload is re-rolled,
tuned against, or inspected for this amendment. Sizing arithmetic
(pre-declared): attenuated per-session rank IC ∈ [0.0043, 0.0100] ⇒
per-world t = IC·√n/SE with n ≈ 1,546 test sessions and SE 0.056 ⇒
t ∈ [3.0, 7.0]; criterion requires both worlds ≥ 1.96. Residual risk:
attenuation at coefficient 0.01 is extrapolated from measurements at
0.005 (43–84%) and 0.5 (37%); the pessimistic bound covers the worst
observed draw with ≥1.1 t-units of margin.

Sealed gate #2 trial set (pre-declared before the run; 8 registered
trials, fresh registry `artifacts/m2-proper-sealed-2.db`, fresh
artifacts dir `artifacts/m2-proper-sealed-2/`):
univariate × {708, 709} × {H1} — criterion: pooled t ≥ 1.96 on BOTH;
univariate × {708, 709} × {H5} + ridge × {708, 709} × {H1, H5} —
reported only (H5 single-world statistics carry the §9.2 overlap
inflation; the ridge carries the §9.3 dilution/sign instability).
Verdict PASS requires the single power criterion; anything else is FAIL
and is recorded verbatim with no further re-run inside this campaign.
