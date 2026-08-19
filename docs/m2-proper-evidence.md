# M2-proper evidence packet — labels, models, trials, sealed validation gate

Status: FINAL record of the one-shot sealed gate (verdict: **FAIL**, one
check; recorded verbatim in §5 — the FAIL is evidence, and any re-run
requires a new campaign and an owner decision, plan §9/§6). Branch
`m2/proper-20260819`, base `main` @ `f459577` (the PR #3 merge commit).
Plan of record: `docs/m2-proper-plan.md` (§1 owner decisions verbatim;
§8-signed corrections at §9).

## 1. Coordinates

- Base: `f459577` (merge of PR #3; tree-identical to reviewed `363f774`)
- Head at gate: `3462910` (pre-gate corrections + sealed driver)
- Commits by workstream (provenance attributed honestly — this campaign
  ran TWO implementers in one checkout before the owner parked the
  second lane and ruled the sealed phase single-driver; see §8):
  - `cf29b64` plan of record (owner-signed "gogo")
  - `e2062b3` A registry extension (Claude lane)
  - `555fd29` B label-correction commit (Claude lane)
  - `6a91e4f` B labels (Claude lane)
  - `12fa432` + `d21583e` C features v2 (Claude lane, then second-lane polish)
  - `892914d` E evaluation statistics (second lane)
  - `559fad1` D ridge + numpy + BLAS pins (second lane)
  - `cb99f5c` F trial runner (Claude lane, then second-lane hardening)
  - `184b322` G equity backtest (second lane)
  - `46dbe99` runner/backtest integration + univariate baseline (second lane)
  - `84027cd` dev trial driver (second lane; dev trials executed here)
  - `3462910` pre-gate corrections + sealed gate driver (Claude lane)

## 2. Workstream record (A–G)

- **A** — worlds 706/707 (coefficient 0.005) pinned pre-trial; all 11
  worlds recomputed through the quality gates, the 9 original hashes
  byte-identical, generator pin unchanged. Registry note records the
  amendment and its closing rule. Test pins the exact 11-world
  composition and per-world coefficients.
- **B** — labels from the decision-time information boundary (plan §3.B
  correction, discovered pre-implementation: 23:00 publication postdates
  the close, so the signed (d+1..d+H) window excluded the planted
  session ret_d and would have captured the effect only at β²).
  Total-return adjusted (uniform n/d share multiplier for
  split/reverse/stock-dividend; cash held unreinvested); absent never
  imputed; purge-consistency proven against real fold geometry.
- **C** — mom_1/mom_5/mom_20 (uniform log family) + dol_vol_20 +
  dol_vol/ret_1 (V1 unchanged); calendar-contiguous lookbacks only;
  monotone-verified bisect visibility (linear fallback preserved;
  equivalence pinned). End-to-end alignment pin: same-seed null/alpha
  twins — aligned family ranks next-session returns positively in
  alpha (0.187 at coefficient 0.5 — the drift wall dampens even huge
  plantings), ~0 in null.
- **D** — numpy 2.5.2 declared (the reserved protocol-relevant change);
  single-thread BLAS forced in conftest/gate/drivers; RidgePipeline
  with FittingGuard INSIDE (fit-once, fit/eval disjointness at score,
  rows outside declared targets refused, missing features skipped);
  coefficients byte-identical across runs and a subprocess. Discharges
  the INV-07 pipeline-enforcement debt named in guards/fitting.py.
- **E** — pure stdlib statistics: average-rank Spearman, one-sample t,
  exact binomial upper tail + max-allowed-rejections, FP assessment,
  backtest summary. None for unevaluable — never a fabricated zero.
- **F** — trial runner: stamp built early so the provenance triple is
  identical at register/mark_running/artifact; world identity in
  TrialScope.feature_set_id; register→running→complete with
  fail-through on any execution error; folds outside the world's
  session range dropped whole; decision sessions hashed into the
  config. Deferred criterion-7 lineage wiring delivered
  (authority.manifest_hash ↔ stamp ↔ TrialRecord, asserted by test).
- **G** — top-quintile equal-weight daily reallocation, next-open
  execution (session strictly after decision), 5bp/side campaign-fixed,
  corporate-action CONVERSION fills on raw prices (zero-fee, cash in
  lieu), final-bar resolution for names without a next bar, FIFO
  LedgerBook with its independent conservation oracle asserted every
  run. PnL artifacts stamped dataset provenance; machinery validation
  only — no performance claim rides on them.

## 3. Development trial record (D1–D4, worlds 103/104 only)

Executed by the second lane at head `84027cd` before it was parked;
registry `artifacts/m2-proper-dev-trials.db` (fresh, sealed-against-
reuse), artifacts under `artifacts/m2-proper-dev/`. All four COMPLETED;
stamps verified (git_sha `84027cd…`, manifest hashes match the frozen
world entries).

| trial | world | model | H | mean IC | pooled t | fold FP |
|---|---|---|---|---|---|---|
| D1 | 104 alpha | ridge | 1 | 0.001033 | 0.772 | 0/29 |
| D2 | 103 null | ridge | 1 | −0.000204 | −0.145 | 2/29 ✓ |
| D3 | 104 alpha | ridge | 5 | 0.000628 | 0.468 | 8/29 ⚠ |
| D4 | 104 alpha | univariate | 1 | 0.002904 | 2.048 | 0/29 |

Findings ruled on pre-gate (plan §9): D4's tripwire band was 0.28 SE
(~78% false-fire) — corrected criterion passes D4 (2.05 vs predicted
≈1.4, a 0.65σ draw; measured session-IC SE 0.056, +5% over the
Gaussian plan — the anticipated fat-tail inflation). D3's 8/29 with
pooled t=0.47 proves overlapping H=5 label windows autocorrelate
session ICs within folds → H=5 FP goes pooled-only. D1 vs D4 shows the
ridge dilutes the aligned signal ~3× → power arm is the univariate.

## 4. Pre-gate corrections

Recorded in plan §9 with the owner's three rulings (tripwire
re-calibration; H=5 pooled-only FP; univariate primary power arm) and
the pre-declared sealed trial set (16 trials). Ruled BEFORE the sealed
run; no validation-world payload was loaded by any dev-phase code.

## 5. Sealed validation gate (one-shot)

- Ran 2026-08-19 10:56–11:23 UTC-6 at head `3462910`, driver
  `scripts/run_m2_sealed_gate.py` (committed pre-run in the same head —
  the driver and the run share one commit; nothing changed in between,
  working tree clean).
- Retained log: `/tmp/m2p-sealed-gate.log`,
  sha256 `7c14f038666848694de295d8354fe5c92d2012fd2551a65931e59cf42ff67528`.
  Standing rule: this pointer is bumped on every remediation; the file
  is attached to the campaign PR.
- Sealed registry: `artifacts/m2-proper-sealed.db` (fresh; the driver
  refuses reuse). Artifacts: `artifacts/m2-proper-sealed/` (16 trials +
  summary, all stamped git_sha `3462910c…`, dataset_manifest_hash =
  sha256 of `data/worlds/registry.json`).
- Worlds regenerated and byte-verified against the frozen registry
  (content_sha256 + counts, all 7 exact):
  701/672,773 · 702/699,723 · 703/689,203 · 704/682,354 · 705/684,340 ·
  706/689,821 · 707/680,141 bars.
- All 16 pre-declared trials STATUS=COMPLETED, 29 folds each:

| world (coef) | model | H | mean IC | pooled t | role |
|---|---|---|---|---|---|
| 701 null | univar | 1 | 0.000110 | 0.08 | FP |
| 701 null | univar | 5 | 0.001357 | 0.94 | FP |
| 702 null | univar | 1 | 0.000506 | 0.36 | FP |
| 702 null | univar | 5 | 0.000576 | 0.40 | FP |
| 703 null | univar | 1 | −0.000101 | −0.07 | FP |
| 703 null | univar | 5 | 0.000672 | 0.48 | FP |
| 704 (0.002) | univar | 1 | 0.002723 | 1.89 | weak, reported |
| 705 (0.002) | univar | 1 | 0.003553 | 2.48 | weak, reported |
| 706 (0.005) | univar | 1 | 0.002161 | **1.51** | POWER — fails 1.96 |
| 706 (0.005) | univar | 5 | 0.003314 | 2.27 | power H5, reported |
| 706 (0.005) | ridge | 1 | −0.003829 | −2.68 | secondary |
| 706 (0.005) | ridge | 5 | −0.006011 | −4.14 | secondary |
| 707 (0.005) | univar | 1 | 0.004202 | **2.93** | POWER — passes |
| 707 (0.005) | univar | 5 | −0.001150 | −0.81 | power H5, reported |
| 707 (0.005) | ridge | 1 | 0.002493 | 1.71 | secondary |
| 707 (0.005) | ridge | 5 | 0.005197 | 3.78 | secondary |

- Pre-declared criteria (plan §9) and outcomes, verbatim from the log:

```
SEALED_GATE_VERDICT=FAIL FP_H1_POOLED_T=0.20988840080911897 FP_H1_FOLDS=3/87(max 8) FP_H5_POOLED_T=1.055869733405653 POWER_H1_T={'synth-v1-val-alpha-706': 1.509, 'synth-v1-val-alpha-707': 2.926}
SEALED_CHECK PASS fp_h1_pooled_abs_t_lte_2.5
SEALED_CHECK PASS fp_h1_fold_binomial_within_threshold
SEALED_CHECK PASS fp_h5_pooled_abs_t_lte_2.5
SEALED_CHECK FAIL power_h1_univariate_rejects_both
```

Driver exit code 4. Verdict recorded verbatim; no re-run was started.

**What the FAIL means (post-mortem, evidence not remediation).** The
false-positive arm — the core purpose of the seal — passed on all three
criteria: pooled null |t| = 0.21 (H1) and 1.06 (H5) against a 2.5 limit,
and 3 of 87 null fold-tests rejected against a max-allowed 8. The power
arm failed on one of its two worlds: 706 realized t = 1.51 where the
criterion required ≥ 1.96 (707 realized 2.93 and passed).

The root cause is the §7 power arithmetic, not the machinery. §7 assumed
realized per-session rank IC ≈ planted coefficient (0.005), predicting
t ≈ 3.7 per world. But workstream C's end-to-end alignment pin had
already measured the drift-wall attenuation: at coefficient 0.5 the
aligned family realizes rank IC 0.187 — 37% of the coefficient. §7
ignored that factor. The sealed worlds realized 43% (706) and 84% (707)
of the coefficient, i.e. ordinary draws from an attenuated ~0.4–0.85×
band, which maps to t ∈ [1.5, 3.1] — exactly what was observed. The
both-worlds criterion implicitly required ≥ 0.53× attenuation on both
draws; 706 drew 0.43×. In short: coefficient 0.005 was too small a
planting for a both-worlds-reject design once attenuation is accounted
for. The univariate machinery did detect the planted effect on 707
(t = 2.93) and directionally on 706 (mean IC 0.00216, ~20× the null
band, t = 1.51).

**Secondary observations (reported only, no claim rides on them).**
Single-world H5 statistics are inflated by the overlapping-label
autocorrelation established in dev trial D3 — 706 univariate H5 (t = 2.27)
exceeding its own H1 (1.51) is that inflation, not a discovery. The
4-feature ridge flipped sign on 706 (t = −2.68 H1, −4.14 H5) while
staying positive on 707 — the dev-phase dilution finding (D1 vs D4)
extends to sign instability at this effect size, reinforcing the §9
ruling that the univariate is the primary arm.

## 6. Acceptance mapping

1. worlds 706/707 pinned, WORLDS_OK=11 MISMATCH=0 (recompute log 11/11;
   post-commit spot verify 2/2) — §2.A
2. labels both horizons, red-first battery, purge test — §2.B
3. audit_panel zero rejections on generated panels; control neutrality
   — §2.C
4. pipeline refusals + determinism (cross-process) — §2.D
5. D1–D4 registered→completed, provenance triples matched end-to-end —
   §3
6. backtest: ledger conservation asserted on every run; execution
   ordering invariants — §2.G
7. sealed gate per §5
8. suite + mutations + clean clone + review at final head — §8
9. research_protocol.yaml byte-unchanged; synth/ byte-unchanged
   (generator pin proof); no real data; no real-market claim; short
   legs nowhere (Order schema long-only by construction)

## 7. Nonclaims

No real market data anywhere (generator + retained Cboe SAMPLE only).
Synthetic PnL validates machinery; no performance or discovery claim is
made or implied on any real market. The H=5 fold-level statistic is
declared invalid-by-design for FP purposes (overlap) and is not
evaluated. numpy determinism is scoped to the locked environment.

## 8. Process record (two implementers, one checkout)

Commits `892914d`–`84027cd` (workstreams E, D, G, runner integration,
dev driver) were produced by a second agent lane (a Codex TUI session
on this machine, started 09:18:22, interleaved with this session under
the owner's git identity). The owner, on discovery, parked the second
lane and ruled the sealed phase single-driver; all pre-gate rulings in
plan §9 are the owner's, recorded before the sealed run. Dev-trial
artifacts the second lane produced were verified (stamps, hashes,
registry states) before being relied on in §3.

## 9. Evidence invalidation

Any change to code, tests, protocol, dependencies, or synth/ after the
final head invalidates this packet; the world registry test fails
closed and the mutation harness must show zero of everything except
KILLED.
