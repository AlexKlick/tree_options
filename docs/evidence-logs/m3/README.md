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
| `m3-review-r4.log` | 2026-08-20 | Bounded Codex review round 4 at 74dfd3a (owner-ruled final round, narrow charter — the three r3 fixes + touched-file regression): **VERDICT: GO, no P1/P2 findings**. Code-final head = 74dfd3a. |
| `m3-final-gate.log` | 2026-08-20 | Phase 5 final full gate at the code-final checkout c9e8430 (LAST_CODE=2e73d82): GATE_EXIT=0 — ruff/mypy/compileall clean, pytest -W error 538 passed, mutation 133/133 KILLED (0 SURVIVED, 0 INVALID), restoration full-suite pass TRUE (the bc8b12c harness fix proven at the exact head), uv build + wheel smoke ok, head/clean-tree assertions held. Launched concurrently with the old-head clean-clone datapoint (recorded in the log header). |
| `m3-cleanclone.log` + `old-cleanclone-work/` | 2026-08-20 | Preliminary old-head clean-clone datapoint at 8479f1f (pre-fix code, pre-hardening driver — superseded by cleanclone2): 8/8 IDENTICAL, ARTIFACTS_IDENTICAL=1, clone-side correction PASS. |
| `m3-final-gate2.log` | 2026-08-20 | Full gate re-run at the code-final head 355f116: all phases green — 540 passed, mutation 133/133 KILLED, restoration TRUE, wheel ok — but GATE_EXIT=3: the exact-head EXIT trap refused the mid-run docs commit e21e20a (the trap fired as designed; the run's phase results remain valid evidence, the exit is honestly recorded). Remedy: gate3 below. |
| `m3-sealed2-gate.log` | 2026-08-21 | The re-registered one-shot sealed gate (mismatch-procedure option (a)) into `artifacts/m3-options-sealed2/`, all 8 payloads stamped `e21e20a` (src-identical to code-final 355f116): 8/8 trials COMPLETED, SEALED2_GATE_EXIT=4 verbatim (the two ruled criterion-4 failures at null-701\|B and alpha-711\|B with the wrong bound deliberately unchanged; no other criterion failed), per-world SEALED_WORLD_OK hashes quoted in evidence §5c. The lane's inline correction refused the new head set as designed (SEALED2_CORRECTION_EXIT=2). |
| `m3-verdict-correction6-pass.log` | 2026-08-21 | The owed corrected verdict for the re-registered run, `--expected-heads e21e20a…`: CORRECTED_VERDICT=PASS, exit 0 — criterion 4 = 0 unmapped / 0 past-fold-end across all four worlds' open arm-B positions, fold geometry 29/29 per trial. |
| `m3-cleanclone2-gate.log` + `cleanclone2-work/` | 2026-08-21 | The binding clean-clone determinism proof at the code-final head: fresh `--no-local` clone of 355f116 ran the full gate in-clone (CLONE_PYTEST_EXIT=0, clone-side CORRECTED_VERDICT=PASS) and auto-compared against the re-registered main run: 8/8 IDENTICAL + ARTIFACTS_IDENTICAL=1 (git_sha-normalized; only stamp.git_sha differs, by design). Evidence §5c. |
| `m3-final-gate3-run.sh` / `m3-gate3-inner.sh` | 2026-08-21 | The gate3 entrypoint + inner script as executed: `m0_gate.sh` with three mechanical deviations (header comment; mutation outputs renamed to `artifacts/m3-gate3-mutations.{json,md}`; the repo-root cd made absolute, required by the /tmp placement). Gate3 re-ran the full gate at the docs-only final head on main after the PR #6 merge (0e010f7). |
| `m3-final-gate3-misfire.log` | 2026-08-21 | Gate3's FIRST launch (01:47:47) refused at entry: the un-patched /tmp copy still had m0_gate.sh's relative `cd "$(dirname "$0")/.."`, which from /tmp resolves outside the repo → "fatal: not a git repository" → the dirty-check fail-closed (GATE3_INNER_EXIT=2). Zero phases executed; head and tree untouched. Retained verbatim; the relaunch below is authoritative. |
| `m3-final-gate3.log` | 2026-08-21 | Gate3, relaunched 01:54:21 at GATE3_HEAD=025b07c after the launcher fix: **GATE3_INNER_EXIT=0** — ruff/mypy/compileall clean, pytest -W error 540 passed, mutation 133/133 KILLED (zero SURVIVED/INVALID), restoration full-suite pass TRUE, wheel smoke ok (tree_options 0.1.0 protocol 0.1.0), zero REFUSED, head-unchanged and clean-tree assertions held. The final-head gate closure. |
