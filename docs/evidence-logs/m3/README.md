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
