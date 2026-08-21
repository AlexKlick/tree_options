# M3 sealed gate — criterion 4 root cause and owner decision

Status: the one-shot sealed gate RAN and recorded **FAIL** (exit 4, log
`/tmp/m3-sealed-gate.log`, registry `artifacts/m3-options-sealed.db`,
artifacts `artifacts/m3-options-sealed/` — all immutable). This memo is
the root-cause record and the disposition request, mirroring the OD1
tripwire memo and the M2-proper gate-#1 precedent.

## Owner ruling (2026-08-20)

**Option C — corrected verdict recomputed from the immutable
artifacts.** The verdict is a pure function of the stamped payloads and
the function had a bug: `scripts/run_m3_sealed_verdict_correction.py`
re-evaluates all seven criteria against the SAME sealed artifacts with
the corrected criterion-4 bound (owning-fold end, not world end), and
stamps `sealed-gate-corrected-verdict.json` alongside them. No trials
re-run, no second trial registry; the original FAIL stays recorded
verbatim in the log and summary. The clean-clone determinism proof
re-executes the gate once in a clone (git_sha-normalized for the
recorded head mix).

## What the gate recorded

All 8 trials (4 worlds × 2 arms) COMPLETED, 29 folds each, conservation
asserted at every one of the 2,001 evaluated sessions per trial. The
verdict's only failures — four lines, one per world, all arm B:

    position SYN-00xx open past expiration 2019-11-01

Criteria 1, 2, 3, 5, 6, 7 all PASSED on the stamped payloads, including
the two the campaign was built for:

- `power_transfer`: pooled stride-4 cohort t = **3.864** over 891
  disjoint cohorts (710: 2.547, 711: 2.929) vs the 1.96 floor.
- `vehicle_fidelity_nulls`: mean null rho **0.8434** vs the 0.696 floor;
  FP nulls |t| = 0.257 / −1.112 vs the 2.5 bound.
- `rejection_paths_live` (amended form): 324–27,697 zero-bid rejections
  per trial (floor 100); volume tail 7.8% (floor 5%).

## Root cause — the gate's criterion-4 bound, not the machinery

The pre-declared criterion: "arm B: every position ends in exactly one
settlement (expiry or early exercise) or a forced close; no position
open past expiration." The gate implemented the last clause as
`exit_kind is None and contract_expiration <= world_last_session` —
comparing every open position's expiry against the WORLD's last session
(2026-12-31).

But the trials run per-fold with fresh cash and a CLAMPED window: each
fold's backtest ends ~7 sessions after its last test session. An arm-B
position entered near a fold's end legitimately remains open at that
fold's end whenever its contract expires after the fold's evaluated
window — the expiry settlement only fires when the loop actually
reaches the expiration session. 5,719 such positions exist in 701|B
alone; every one trips the wrong bound. The check breaks at the first
hit per trial, which is why exactly one position per world surfaced
(all expiring 2019-11-01 — fold 0's tail; the fold geometry is shared
across worlds, so the same Friday expiry surfaces in each).

## Verification on the immutable artifacts (the corrected criterion)

`/tmp/m3-crit4-verification.log`: fold geometry re-derived exactly as
the gate derived it (frozen protocol + calendar + registry world —
29/29 folds per trial, every position mapped), and the corrected
criterion checked — "open ⇒ contract_expiration > the owning fold's
last evaluated session":

    synth-v1-val-null-701|B:  open=5719  violations=0
    synth-v1-val-null-702|B:  open=5401  violations=0
    synth-v1-val-alpha-710|B: open=5139  violations=0
    synth-v1-val-alpha-711|B: open=5636  violations=0

**0 violations across all 21,895 open arm-B positions.** The machinery
satisfies the criterion the gate mis-encoded, on the very artifacts the
gate produced.

Also recorded there — the stamp audit: trials 2–8 stamped at `b1c9b45`;
trial 1 (710|A) stamped at `3bbb461` (the kill-strengthening commit
landed mid-run; `git diff 3bbb461 b1c9b45 -- src/` is EMPTY — the two
heads differ only in tests and the mutation manifest).

## Disposition options

- **A — corrected gate #2 (the M2-proper §10 precedent).** Fix the
  criterion-4 bound in `run_m3_sealed_gate.py`, pre-declare, and re-run
  a NEW sealed gate over the same 8 configs into fresh registry/
  artifacts. The re-run is deterministic (FIXED_CLOCK, identical
  inputs) so it reproduces these payloads byte-for-byte; cost ≈ 4.5 h.
  The FAIL's registry stays immutable alongside it.
- **B — record FAIL as final.** The evidence packet records the verdict
  verbatim plus this root cause; the campaign closes on a FAIL caused
  by a gate-verdict bug with machinery verified clean on the same
  artifacts.
- **C — corrected verdict recomputed from the immutable artifacts
  (recommended).** The verdict is a pure function of the stamped
  payloads; the function had a bug. Commit the corrected verdict step
  (same fold-end derivation verified above) as an owner-ruled
  recomputation against the SAME sealed artifacts — no trials re-run,
  no second trial registry, the FAIL stays in the log verbatim. The
  clean-clone determinism proof then re-runs the gate once in a clone
  and compares (git_sha-normalized for the recorded head mix), which is
  the only full re-execution either way.
