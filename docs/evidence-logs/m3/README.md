# M3 retained evidence logs

The 2026-08-20 /tmp wipes (two separate incidents, external actor)
deleted live evidence logs mid-campaign — including
`/tmp/m3-sealed-gate.log`, the one-shot sealed gate's own console log.
This directory retains what survived, per the M2-proper lesson
("retain every gate/mutation log INTO THE REPO immediately").

The gate's verdict is durable independent of the lost log: it is
STAMPED in `artifacts/m3-options-sealed/sealed-gate-summary.json`
(gitignored like all trial artifacts, but on-disk) and the outcome
lines were recorded verbatim in
`docs/m3-sealed-gate-criterion4-decision.md` at root-causing time:

    SEALED_GATE_VERDICT=FAIL
    SEALED_CHECK FAIL synth-v1-val-null-701|B: position SYN-0023 open past expiration 2019-11-01
    SEALED_CHECK FAIL synth-v1-val-null-702|B: position SYN-0037 open past expiration 2019-11-01
    SEALED_CHECK FAIL synth-v1-val-alpha-710|B: position SYN-0020 open past expiration 2019-11-01
    SEALED_CHECK FAIL synth-v1-val-alpha-711|B: position SYN-0057 open past expiration 2019-11-01
    GATE_EXIT=4

plus the eight SEALED_TRIAL=... COMPLETED lines (29 folds each,
conservation 2001/2001 sessions) quoted in the same memo and the
evidence packet.

Contents:

- `m3-verdict-correction.log` — the owner-ruled option-C corrected
  verdict recomputation: CORRECTED_VERDICT=PASS, exit 0.
- `m3-crit4-verification.log` — the corrected-criterion verification
  against the immutable artifacts (0 violations / 21,895 open arm-B
  positions; fold geometry 29/29 per trial) + the stamp head-mix audit.
- `m3-mutation2.log` / `m3-mutation2.json` — mutation run 2 (126
  KILLED / 2 INVALID / 5 SURVIVED; the restoration-suite anomaly).
- `m3-restore-repro2.log` — the full-suite clean-worktree replica that
  PASSED (the restoration anomaly did not reproduce outside the
  harness).
- `m3-killcheck-baseline.log` / `m3-round2-tests.log` — the round-2
  kill-strengthening baselines.
- `m3-mutation3.log` / `m3-mutation3.json` / `m3-mutation3.md` — mutation
  run 3 at `8479f1f`: **133/133 KILLED** (round-2 strengthening held;
  zero SURVIVED, zero INVALID). The restoration suite failed a third
  time on `test_run_options_trial_end_to_end` — root-caused (see
  commit `bc8b12c`): the harness copytree excludes `.git`, and WS-F
  stamping fail-closes with `DirtyWorktreeError("not a usable git
  repository")` in a git-less tree. Deterministic, not load; the
  retained worktree had no `.git` at all.
- `m3-fix-validation.log` — the fix validated on the exact failure site:
  the retained run-3 worktree + a synthetic baseline commit → the e2e
  trial test passes (TEST_EXIT=0). Full re-run (run 4) is NOT yet done: it executes inside the Phase-5 final gate (m0_gate.sh) at the code-final head — until that gate records 133/133 KILLED with a passing restoration suite, exact-head mutation closure is PENDING. The 8479f1f result is an old-head datapoint. Originally queued after the
  clean-clone gate completes; the final gate re-proves restoration.

| `m3-verdict-correction2-rejected.log` | 2026-08-20 | Hardened correction driver re-run at 82ad4cc — refused the GENUINE stamped artifacts (exit 2, all 8 `INPUT_REJECTED ... config_hash`): the r1 P1-5 check compared trial config_hash to the gate summary's, an invariant that is false of the ruled run. Red demonstration for the r1.1 fix (569783b). |
| `m3-fullsuite-p1-fixes.log` | 2026-08-20 | Full pytest suite at the 5-P1-fix head (pre-r1.1): 529/529 passed, SUITE_EXIT=0. |
| `m3-verdict-correction3-pass.log` | 2026-08-20 | Hardened correction driver re-run after the r1.1 fix (c9f7087): validation passed, all 4 worlds 29 folds, criterion 4 = 0 violations / 0 unmapped across 21,895 open arm-B positions, CORRECTED_VERDICT=PASS, exit 0. |
| `m3-review-r1.log` | 2026-08-20 | Bounded Codex review round 1 at c88d238 (pinned worktree, read-only): VERDICT NO-GO, 5 P1 + 1 P2. Full transcript retained. |
| `m3-review-r2.log` | 2026-08-20 | Bounded Codex review round 2 at 4209b82 (pinned worktree, read-only): VERDICT NO-GO, 4 P1 + 1 P2. Full transcript retained. |
| `m3-fullsuite-p2-fixes.log` | 2026-08-20 | Full pytest suite at the 4-P1-fix head (df2131d): all passed, SUITE_EXIT=0. |
| `m3-verdict-correction4-pass.log` | 2026-08-20 | Hardened correction re-stamp at 8de3670 after the r2 fixes (per-world criterion-3 floor): CORRECTED_VERDICT=PASS, exit 0 — same measurements, criterion 4 = 0 violations / 0 unmapped across 21,895 open arm-B positions. A first attempt on a dirty tree refused (DirtyWorktreeError, fail-closed). |
| `m3-review-r3.log` | 2026-08-20 | Bounded Codex review round 3 at 45ca6f5 (pinned worktree, read-only): VERDICT NO-GO, 3 P1 + 1 P2 — round cap reached. Full transcript retained. |
| `m3-fullsuite-p3-fixes.log` | 2026-08-20 | Full pytest suite at the 3-r3-fix head: all 538 passed, SUITE_EXIT=0. |
| `m3-verdict-correction5-pass.log` | 2026-08-20 | Correction re-stamp after the r3 fixes (world-bound validation): CORRECTED_VERDICT=PASS, exit 0 — same measurements. |
