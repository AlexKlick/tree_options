# M3 OD1 tripwire — root cause, re-priced arithmetic, owner decision

Status: **HALT per plan §7** (`DEV_TRIPWIRE=HALT`, exit 3, log
`/tmp/m3-dev-trials.log`, artifacts `artifacts/m3-options-dev/`). The
amendment window (workstream G) stays open until ruled. Nothing past
this point runs without the owner's coefficient decision.

## What ran (all machinery green)

| trial | arm/world | folds | positions | rho | sigma_IC | t |
|---|---|---|---|---|---|---|
| OD1 | A / 104 (c=0.002) | 29 | 19,561 | **0.846** | **0.351** | −0.15 (≈0, expected at c=0.002) |
| OD2 | B / 103 (null) | 29 | 16,736 | 0.341 | 0.454 | 9.12 (see note) |

Conservation held every session of every fold; settlements (expiry,
elected, action-terminal, silent-death terminal) all closed cleanly;
both trials COMPLETED and stamped. Two crashes were found and fixed on
the way (silent deaths; death-vs-election ordering) — both fail-closed
guards firing on real in-world structure, both with red-first
regressions.

Note on OD2's t=9.12: arm-B holds (~31 sessions) overlap across
stride-4 cohorts, so the disjoint-cohort t is NOT a valid statistic for
arm B — it is printed, never gated (criterion 6 gates arm A on nulls
701/702). OD1 (arm A, 4-session holds) is the valid estimator.

## Root cause of each tripwire violation

1. **rho = 0.846 vs prior 0.9** (2 SE at n=19,163 rejects any delta
   > 0.014 — the wire is extremely tight at this n). The prior assumed
   theta/spread "near-common cross-sectionally"; the measured value
   carries real dispersion in IV/strike distance across the selected
   book. 0.846 sits INSIDE plan §4's own stated band ("prior ≈
   0.8–0.9") and sets criterion 5's floor at 0.846 − 0.15 = 0.696.
   Verdict: prior refined, machinery sound.

2. **sigma_IC = 0.351 vs prior 0.16.** The prior assumed ~40 positions
   per cohort (1/√39 ≈ 0.16). Measured mean cohort size is **10.72**
   (19,561 positions / 1,785 cohorts): 1/√(10.72−1) ≈ 0.32 ≈ measured
   0.351. The thinning is the §9.2 filter doing its job in-world —
   `same_day_volume` FAILs 39,837 of 64,498 candidate evaluations (62%,
   the planted untraded_fraction=0.5 against the ≥100 floor), OI 2,225,
   spread 84, liquidity 369. Verdict: arithmetic consequence of
   measured book thinning, not a defect.

3. Autocorrelation 0.029 ≈ 0 — passed.

## The re-priced power arithmetic (§7, from measured values)

    mean cohort IC = 0.38 (H5 IC per unit c) x rho x att = 0.321 c att
    t = 0.321 c att x sqrt(447) / 0.351 = 19.3 c att

At the pre-declared c = 0.05: t ≈ **0.96 att ∈ [0.41, 0.96]** over the
attenuation band [0.43, 1.0] — underpowered against 1.96 at ANY draw.
Note the plan §7's printed claim "t ∈ [10.8, 25.1] at c=0.05" does not
follow from its own formula (t ≈ 50 c att gives [1.07, 2.49] even at
the ASSUMED sd=0.16/K=550 — marginal, not 10.8–25.1). The formula was
frozen; the printed conclusion was not reproducible. Measured thinning
moves the marginal case to a clear under-power.

Coefficient needed for t ≥ 1.96 at the PESSIMISTIC draw (att = 0.43):
c ≥ 0.24. For the campaign's margin rule (t_pessimistic ≥ ~4, a 2x
cushion): c ≥ 0.48.

## Second finding needing the same ruling — criterion 3, first clause

"`not_evaluable_floor`: ≥ 2% of candidate evaluations carry at least one
NOT_EVALUABLE rule" measures ZERO, structurally: the strategy evaluates
ONE pre-selected near-ATM strike per name (always quoted on the file),
so the zero-bid tails surface as same_day_volume FAILs and as execution
rejections instead — and those are large and live (OD1: 419 entry +
24,523 exit zero-bid/no-liquidity rejections, dwarfing the ≥ 100 floor).
Proposed amendment: keep the ≥100 execution-rejection floor and require
the filter-audit histogram to show the volume/untraded tail live (it
does: 62%), dropping the structurally-unsatisfiable NOT_EVALUABLE
percentage.

## Options for the 710/711 coefficient (everything else unchanged)

- **0.50** — t ≈ 19.3 × 0.5 × att = [4.1, 9.7]: passes at every draw
  with ~2x pessimistic margin. The planted family stays the same
  (linear momentum), just visibly strong.
- **0.30** — t ∈ [2.5, 5.8]: passes at every draw but the pessimistic
  margin is 1.3x (a bad draw leaves it close to 1.96).
- **keep 0.05** — the gate records an honest power FAIL (the gate-#1
  precedent: evidence, then a new lane needs a new ruling).

The fidelity/FP arms (criteria 1–5, 6 on nulls 701/702) need no power
and are unaffected by this choice.
