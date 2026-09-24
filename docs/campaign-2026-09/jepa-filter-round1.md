# JEPA-FILTER round 1 — inner folds (campaign-2026-09, slot order 5)

Executor: jepa-filter slot agent, 2026-09-24. Branch `exec/campaign-2026-09`
(execution at HEAD `8fae3c7`). Runner:
`scripts/campaign/jepa-filter_run.py` (this commit). Registry:
`artifacts/campaign-2026-09/jepa-filter.db` (scope `c09-jf`, 8/8 terminal:
6 COMPLETED + 2 FAILED-defective, one scope commitment, 8/32 cap). Per-config
artifacts: `artifacts/campaign-2026-09/jepa-filter/trials/c09-jf-<id>.json`;
the frozen inner-loop selection:
`artifacts/campaign-2026-09/jepa-filter/inner-round1.json`. Run logs:
`/tmp/jepa-filter-r1-{plan,register,execute,execute2,select}.log`.

## Binding

- Menu: `docs/theory/campaign-2026-09-registration.json` v3, sha256
  `4aca21014f6efa640d97bf2abd3fdbb912af5a7b9acb68f65b842f924379b10a`
  (sidecar-verified). Slot doc `docs/theory/campaign-2026-09/slots/jepa-filter.md`
  sha256 `280a7e57e3749ce544e042ebd46f5f2d3e024ab1fd9fe870da8f1ae69354b8d0`
  (menu-pinned). Protocol raw sha `2fde83db…` = menu pin.
- Inputs pinned and verified at bind: ohlc-panel (`0861f525…`), the sealed
  calendar (`7f9cccba…`), and VIX/VIX9D/VIX3M/VVIX CSVs — all sha256 match the
  menu's `dataset_pinning`. Grid: 1263 panel-union sessions 2021-09-13..
  2026-09-23; cutoff (earliest last session, chain-35) 2026-09-23; phantom
  2025-01-09 absent by construction; roster chain-35 (V1/V2/V3) and the pinned
  26 singles (V4, SOXX unused).
- Null gate: `artifacts/campaign-2026-09/tnull/calibration-v3.json`, verdict
  **CALIBRATED**, binds this menu sha, cutoff matches. B read, never
  recomputed: B(xsmom, jepa-outer) = +0.014492, B(event, jepa-outer) =
  +0.013189 — **disclosure only**: jepa-outer is an EVALUABLE null window, and
  this slot's criteria are rank-relative (IC / paired rank correlation), so no
  absolute-mean leg reads B; the values are carried in every artifact stamp's
  calibration block for the sealed round's context.

## Round-1 scope (inner folds ONLY)

The sealed outer window — origins 2026-01-02 through cutoff−h (2026-09-16 at
h=5, 2026-08-24 at h=21; 182 sessions in span) — was NEVER scored, never read
for tuning, never plotted. No (b)-track quantity (S_u, vix_term comparison,
MDD21, the paired bootstrap, the 3-seed randomized-score null) was computed.
Executed exactly the registered inner geometry: origins 2024-07-01..2025-12-31
(378 sessions), quarterly anchored-expanding refits at the first session of
2024-07/2024-10/2025-01/2025-04/2025-07/2025-10, training rows of refit Q
satisfying ordinal(s)+h+5 < ordinal(first session of Q); surprise indexed by
completion session u with the vintage in force at u−h; one-session index lag
(INV-02) on every index input.

Realized origin counts match the registration's own note: 373/378 scored at
h=5 (first 5 sessions carry no vintage), 357/378 at h=21; 0 breadth-skipped
origins and 0 degenerate cross-sections on every config (the >10% skip defect
never approached). Future-poison precondition: PASS on all six scored configs
— 6 samples each (first scored origin per vintage segment), max abs diff
3.6e-15 against the 1e-12 bar.

## INV-13 record

All 8 config ids (`c09-jf-JF-V1-H5 … JF-V4-H21`) were written to
`jepa-filter.db` (REGISTERED, no outcome) at HEAD `8fae3c7` BEFORE any
outcome existed; execution is one-shot per trial and immutable. **Disclosed
interruption**: the first `--execute` (runner build `6d2ed406…`, log
`/tmp/jepa-filter-r1-execute.log`) completed JF-V1-H5/H21 and JF-V2-H5/H21
and crashed on JF-V3-H5 when the PINNED VICReg optimizer left finite
arithmetic (overflow → inf/NaN training surprises) inside the FIRST V3 refit
— before any V3 outcome existed. The repaired build turns that crash into a
per-trial FAILED defect + continue, resumes past finished trials, and teaches
`--select` the FAILED state. The four artifacts executed by build `6d2ed406…`
bind that sha in their stamps (accepted by `--select` as the disclosed
EXECUTING_SHA, term-gate round-1 precedent); JF-V4-* artifacts bind the
repaired build `5c6c2ead…`. Verification the repair touched no scored math:
a finite-difference gradient check of `vicreg_fit` against the registered
objective (mean-over-rows squared k-norm + λ·hinges + λ·off-diag²) passes
bit-identically before and after the repair (max |fd−analytic| 3.3e-07).

## Inner results (declared direction: long LOW surprise / short HIGH)

| config | encoder | k_eff | h | origins | mean IC | NW t (lag h) | status |
|---|---|---:|---:|---:|---:|---:|---|
| JF-V2-H5 | PCA-anchor | 11 (k=16 capped by d=11, disclosed) | 5 | 373 | **+0.012937** | +0.95 | INNER-RECORDED |
| JF-V1-H5 | PCA-anchor | 8 | 5 | 373 | +0.006707 | +0.50 | INNER-RECORDED |
| JF-V4-H5 | PCA-anchor, sector-residual (26) | 8 | 5 | 373 | −0.015141 | −0.85 | INNER-RECORDED |
| JF-V2-H21 | PCA-anchor | 11 | 21 | 357 | −0.004749 | −0.18 | INNER-RECORDED |
| JF-V1-H21 | PCA-anchor | 8 | 21 | 357 | −0.010829 | −0.43 | INNER-RECORDED |
| JF-V4-H21 | PCA-anchor, sector-residual (26) | 8 | 21 | 357 | −0.018528 | −0.55 | INNER-RECORDED |
| JF-V3-H5 | VICReg-linear | — | 5 | 0 | — | — | NOT_EVALUABLE (defective; operator ruling requested) |
| JF-V3-H21 | VICReg-linear | — | 21 | 0 | — | — | NOT_EVALUABLE (defective; operator ruling requested) |

Baseline disclosures on the identical origin/name cells (disclosed, never
verdicts): XSMOM-score IC +0.019266 (h=5) / +0.032446 (h=21); cheap-surprise
controls under the same long-low convention: lnRV21 −0.008204 / −0.064438,
|r_21| −0.025137 / −0.041363 (h=5 / h=21). Reading: at h=5 the learned
surprise beats BOTH of its own vol-level proxies (the "encoder added nothing"
control) but does not exceed the XSMOM-score disclosure on the same cells —
the momentum-re-labeling question is exactly what the sealed round's partial
IC tripwire decides. Basis drift across refits is visible and disclosed, not
tuned away: vintage surprise SD rises 1.011→1.098 (h=5) / 1.044→1.154 (h=21),
and per-vintage-segment mean IC decays (every variant's worst segment is
2025-07: −0.050 to −0.142).

## V3 defect (NOT_EVALUABLE; operator ruling requested)

The PINNED V3 constants (linear f and g trained jointly, full-batch plain GD,
2000 iterations, lr 1e-2, init `Generator(PCG64(22))·1e-2`, λ_v = λ_c = 25.0)
**diverge on the registered inputs**: diagnostic re-run on the same sealed
inputs (first refit vintage 2024-07, `/tmp/jepa-v3probe.log`) shows GD leaving
finite arithmetic at **iteration 7** at both h=5 and h=21 (loss ≈ 185.2,
|W|max 0.026, |A|max 0.025 at iteration 0). Mechanism: the state's index
block is identical across all 35 names, so the full-batch design is strongly
rank-heavy; the hinge variance-pressure gradient at init (sd ≈ 0.05 →
∂L/∂C_jj ≈ −λ·hinge/sd ≈ −475) under lr 1e-2 overshoots within a few
iterations (an i.i.d. synthetic sanity probe converges — the divergence is a
property of the pinned constants on THIS data). Per the slot doc, defective
runs are NOT_EVALUABLE **by operator ruling only**, and the pinned constants
are the registration's: no lr change, clipping, or re-init was applied (that
would be tuning away a defect). Both trials are FAILED in the registry with
no artifact and no score; SEL-a proceeds over the evaluable variants exactly
as its pre-declared rule allows. The collapse tripwire itself never fired
(no finite latents existed to test).

## Frozen round-1 selection (pre-declared SEL-a / SEL-b / FLIP)

- **SEL-a h=5: JF-V2-H5** (mean inner IC **+0.012937**, the inner-fold best;
  ranked: V2 +0.012937 > V1 +0.006707 > V4 −0.015141; V3 excluded
  NOT_EVALUABLE). Outer scoring PENDING (sealed round).
- **SEL-a h=21: FAIL on inner evidence** — no evaluable variant has positive
  mean inner IC (V2 −0.0047 > V1 −0.0108 > V4 −0.0185, all negative): the
  pre-declared wrong-sign falsifier; (a) at h=21 receives NO outer scoring.
- **FLIP h=21: indicated** (all three evaluable variants negative) — a
  flipped variant MAY be registered as a new candidate consuming one of the
  four variant slots (displacing the lowest inner-ranked variant, recorded
  NOT_RUN). This is a registration decision for the consolidator/operator,
  NOT taken by this executor.
- **SEL-b: JF-V1-H21, FIXED** by the registration, independent of SEL-a —
  the (b) filter track's outer config regardless of V1's inner sign.

## Verdict summary (pre-declared vocabulary only)

- 8 configs registered and terminal; 0 withdrawn (this slot declares no
  data-gated arm; the menu's gated arms — vrp-cond scope O, exit-grid-2
  scope C — belong to other slots and were withdrawn there).
- NOT_EVALUABLE: 2 (JF-V3-H5, JF-V3-H21 — defective run under the pinned
  constants; operator ruling requested, recorded in
  `inner-round1.json.operator_rulings_requested`).
- (a) at h=21: recorded FAIL on inner evidence per SEL-a's pre-declared rule
  (wrong-sign falsifier, no outer scoring).
- No round-1 verdict otherwise: the six scored cells are INNER-RECORDED;
  PASS / PASS-REDUNDANT / FAIL belong to the three sealed questions, scored
  once on the outer window (2026-01-02..cutoff−h) under their own run.

## Sealed round handoff (not run here)

(a) at h=5 on JF-V2-H5: mean daily cross-sectional Spearman IC on the sealed
outer origins in the declared direction, one-sided circular block bootstrap
(block h, B = 2000, seed PCG64(22)); PASS-REDUNDANT tripwire = partial IC vs
{XSMOM score, lnRV21} with |t| < 1. (b) on JF-V1-H21's S_u vs the incumbent
vix_term (same one-session lag) on Spearman with forward RV21(SPY), one-sided
PAIRED circular block bootstrap; MDD21 twin reported; dispersion twin and
tercile card-gate lift disclosed-only. Baselines on identical origins: the
no-signal null band, the disclosed-only 3-seed randomized-score null
(jepa-null-1/2/3), the XSMOM score, lnRV21, |r_21|. Floors: complete outer
origins ≥ 126 (177 expected at h=5 / 161 at h=21), ≤ 10% skipped origins,
future-poison precondition, V3-style defects by operator ruling only.

Nothing is adopted, nothing seals a card, no RESEARCH-LEDGER entry is made by
this executor. Blockers for the operator: (1) the V3 divergence ruling; (2)
the FLIP-h21 registration decision.
