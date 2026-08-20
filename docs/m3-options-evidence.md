# M3 options evidence packet — synth options, American exercise, two-arm sealed validation

Status: FINAL record for MU-2. The one-shot sealed gate ran 2026-08-20
07:29–12:54 and returned **FAIL (exit 4)** on criterion 4 — root-caused
to a defect in the *gate driver's own bound* (world last session vs the
owning fold's evaluated end), not in the trial machinery. Owner ruling C
(2026-08-20): the verdict is a pure function of the immutable stamped
artifacts, so the corrected verdict was recomputed from those same
artifacts — **PASS** — with the original FAIL recorded verbatim
(`docs/m3-sealed-gate-criterion4-decision.md`). The clean-clone
determinism proof re-executes the gate once (§5c). Plan of record:
`docs/m3-options-plan.md`; tripwire halt + owner rulings in
`docs/m3-od1-tripwire-decision.md`.

## 1. Coordinates

- Branch `m3/options-20260819`, base = MU-1 merge commit `96e4f6e`
  (PR #5; MU-1 = plan + WS-A/B/C: `e1e1b49` → `db20461` → `6b782ac` →
  `998db86` → `2755e57` → review rounds `df22377` → `485cefc` →
  `4c9eefe`).
- MU-2 commits, by workstream:
  - `29b6505` WS-D candidate→order strategy + terminal settlement kind
  - `8a231d3` WS-E options backtest (arms A/B, per-session loop,
    settlements, marks, conservation)
  - `6836ab7` WS-F trial runner + dev calibration driver (OD1–OD3, §7
    tripwire)
  - `2494b7b` silent-death terminal settlements
    (bankruptcy/delisting/coverage lapse)
  - `152c18c` death-claims-first settlement ordering + direction-aligned
    OD statistics (dev trials 103/104 executed here, stamped)
  - `1f18839` OD1 tripwire decision memo (halt + root cause + re-priced
    arithmetic) — the campaign's mid-flight owner interruption
  - `537c7b5` WS-G validation-world amendment: worlds 710/711 @ 0.50 +
    options overlays 701/702/710/711 pinned (owner-ruled)
  - `3bbb461` WS-H sealed gate driver + criterion stamps + mutants
    M108–M134
  - `b1c9b45` mutation kill-strengthening round 1 (10 SURVIVED → owners)
  - `6212d90` criterion-4 root-cause memo + owner-ruled verdict
    correction driver (option C)
  - `958c0fc` correction driver world/arm iteration fix (corrected
    verdict stamped here)
  - `8479f1f` mutation kill-strengthening round 2 (5 SURVIVED → KILLED,
    each kill verified locally before re-running)
  - `1993a06` surviving evidence logs retained in-repo after two
    external /tmp wipes (`docs/evidence-logs/m3/`)
  - `bc8b12c` mutation-harness fix: the restoration suite fail-closed in
    the git-less harness copy (deterministic root cause of the 3×
    restoration failure; validated on the retained failure site)
  - `85e2435` mutation run 3 + harness-fix validation logs retained
- `src/` is UNCHANGED since `3bbb461` — every later commit touches only
  tests/docs/scripts (`git diff 3bbb461..HEAD -- src/` is empty). The
  sealed trials' stamp head-mix (710 A+B @ `3bbb461`, rest @ `b1c9b45`)
  is therefore src-identical; recorded in the corrected verdict's
  `stamp_audit`.

## 2. Workstream record (D–H)

- **D** — candidate→order strategy: top-K by signal with fee-inclusive
  affordability (`budget ≥ cost + fee minimum`), calendar-day expiry
  targeting with nearest-available tie-break, volume applicability,
  election-window visibility (10:00 quotes only), dividend-branch
  election. Sizing divides the fee-inclusive divisor, not raw premium.
- **E** — options backtest: per-session decision loop, terminal
  settlement kinds (expiry cash-settle vs exercise), marks at prior-file
  EOD bid, `FillAudit` (decision/quote/execution sessions and instants)
  recorded at the single `execute_fill` choke point, ledger conservation
  asserted every session, force-close on the fold's evaluated end.
- **F** — trial runner: per-fold fresh $1M, walk-forward folds from the
  protocol, payloads carry `n_sessions_evaluated`,
  `conservation_checks`, `world_last_session`, pooled raw stride-4
  cohort ICs. Dev calibration OD1–OD3 ran on worlds 103/104 (stamped at
  `152c18c`) and tripped the §7 power tripwire → halt → owner ruling.
- **G** — amendment window (the plan's one pre-declared window, §3.G):
  worlds 710/711 (linear momentum, coefficient 0.50, seeds 710/711,
  500 securities, validation pool) appended to the world registry;
  options overlays for 701/702/710/711 appended (8 overlays total).
  Both registries carry amendment notes; tests pin the exact 15-world
  and 8-overlay compositions including pools and frozen coefficients.
- **H** — sealed gate `scripts/run_m3_sealed_gate.py`: one-shot, 8
  trials = {701, 702, 710, 711} × {A, B}, fixed clock, verifies both
  code pins and both lanes' expected hashes before running; verdict
  computed only from stamped payload files; exit 4 on FAIL.

## 3. OD1–OD3 dev record (honest, per the tripwire memo)

- Dev calibration measured the options-vehicle attenuation at ≈ 0.26–0.30
  — BELOW the equity-lane band floor (0.43) used by the §7 pre-registered
  arithmetic. The tripwire halted the campaign rather than proceed
  underpowered (`docs/m3-od1-tripwire-decision.md`).
- Root cause (memo §"near-common" note): arm-B holding periods (~31
  sessions) overlap across the 1,785 dev cohorts, so effective
  independent cohorts shrink by ≈ 1/√(10.72−1) ≈ 0.32 — measured book
  thinning, not a machinery defect.
- Owner ruling 1: coefficient **0.50** for validation worlds 710/711
  (re-priced: t = 19.3·c·att ⇒ t ∈ [4.1, 9.7] at the band draws). The
  0.05 coefficient would have been underpowered at the measured draw
  (t ∈ [0.41, 0.96]).
- Owner ruling 2: criterion 3 amended to `rejection_paths_live` — the
  structurally-zero ≥2% NOT_EVALUABLE clause dropped (half-spread ∝ mid
  makes negative bids impossible by construction), the ≥100 zero-bid
  execution-rejection floor and live volume-tail histogram evidence
  kept. Recorded into plan §3.G/§4 BEFORE anything was pinned.

## 4. Sealed gate run (2026-08-20, one-shot)

8/8 trials COMPLETED (5.5 h wall). Recorded verdict, verbatim:

    SEALED_GATE_VERDICT=FAIL
    SEALED_CHECK FAIL synth-v1-val-null-701|B: position SYN-0023 open past expiration 2019-11-01
    SEALED_CHECK FAIL synth-v1-val-null-702|B: position SYN-0037 open past expiration 2019-11-01
    SEALED_CHECK FAIL synth-v1-val-alpha-710|B: position SYN-0020 open past expiration 2019-11-01
    SEALED_CHECK FAIL synth-v1-val-alpha-711|B: position SYN-0057 open past expiration 2019-11-01
    GATE_EXIT=4

Criteria 1/2/3/5/6/7 PASSED on the run; criterion 4 failed on all four
worlds' arm B. Root cause (memo `docs/m3-sealed-gate-criterion4-decision.md`):
the gate compared open arm-B positions' `contract_expiration` against
`world_last_session` — but each fold's backtest ends at that fold's
evaluated end (nth_after(last test session, 1) + 6 sessions, clamped),
which is EARLIER than the world's end for every fold but the last. Every
fold-tail arm-B position therefore tripped the check (5,719 in 701|B
alone). The trial machinery was verified clean on the immutable
artifacts: re-derived fold geometry matched stamped `n_folds` (29/29 per
trial), and the corrected criterion (expiration > OWNING fold's
evaluated end) showed **0 violations across all 21,895 open arm-B
positions**.

## 5. Corrected verdict (owner ruling C, same artifacts)

Ruling: the verdict is a pure function of the stamped payloads; fold
geometry is deterministically re-derivable (registry world → bar
sessions → protocol walk-forward splits) and was verified against the
stamps. No trials re-run, no second trial registry; the FAIL stays
recorded verbatim. `scripts/run_m3_sealed_verdict_correction.py` (at
`958c0fc`) re-evaluated all 7 criteria and stamped
`artifacts/m3-options-sealed/sealed-gate-corrected-verdict.json`:
**PASS**, `original_failures` preserved, `stamp_audit` recording the
head-mix above. Corrected run's measurements:

| measure | value | bound |
|---|---|---|
| pooled power t (891 stride-4 cohorts) | **3.864** | ≥ 1.96 |
| per-world power t (710 / 711) | 2.547 / 2.929 | reported |
| null fidelity mean ρ (arm A) | 0.8434 | ≥ 0.696 |
| arm-B fidelity ρ (expected ≈ 0.35) | 0.347–0.396 | reported |
| false-positive \|t\| (701 / 702) | 0.257 / −1.112 | < 2.5 |
| conservation checks passed | 2001/2001 sessions × 8 trials | all |
| zero-bid execution rejections (arm B range) | 324–27,697 | ≥ 100 |
| volume-tail fail fraction | 7.8% | ≥ 5% |

Honest caveat, recorded: per-world power t (2.547/2.929) fell BELOW the
OD1 memo's predicted [4.1, 9.7] — the implied attenuation at the sealed
draw is ≈ 0.26–0.30, consistent with the dev tripwire's measurement,
i.e. real options-vehicle theta/spread drag, not gate noise. Pooled
across both alpha worlds the detection clears 1.96 with margin.

### 5b. Mutation campaign

133 mutants (M01–M134; M108–M134 added this campaign). Run 1 at the
WS-H head: 119 KILLED / 4 INVALID / 10 SURVIVED. Round 1 (`b1c9b45`)
re-pointed the 4 INVALID owners and strengthened/anchored survivors;
run 2: 126 KILLED / 2 INVALID / 5 SURVIVED. Round 2 (`8479f1f`) fixed
all remaining, each kill verified locally (apply-mutant → run-owner →
restore) before the suite re-ran: M113 (zero-bid RATE ≥ 15% assertion +
bid tick-floor anchor), M125 (fee-MINIMUM knife edge: budget 100.80
buys nothing), M127 (nearest-target KEY anchor), M129 (`exists_on`
guard + pinned `CONTRACT_NOT_LISTED` code), M131 (same `signals` for
both runs), M109/M124 owners re-pointed at the tests that kill.
Anchoring lesson (recorded for future campaigns): many defensive guards
are UNREACHABLE on this generator — half-spread ∝ mid makes negative
bids and locked markets impossible; `exists_on` dominates `expired_on`
for standard contracts — so anchors must target the live enforcement
site, not the defensive line.

Run 3 (at `8479f1f`, the pre-PR confirmation): **133/133 KILLED** — the
round-2 strengthening held with zero SURVIVED and zero INVALID
(`docs/evidence-logs/m3/m3-mutation3.{log,json,md}`). The restoration
suite failed a third time on `test_run_options_trial_end_to_end`, and
the retained worktree finally settled the anomaly: the harness copytree
excludes `.git`, and WS-F stamping (`build_stamp`) fail-closes with
`DirtyWorktreeError("not a usable git repository")` in a git-less tree
— deterministic, not load (the two "isolated replicas" passed only
because a `.git` existed there). Fix `bc8b12c`: the harness initializes
a synthetic baseline repository in the copy (init + add -A + commit,
before `uv sync`); validated on the exact failure site — the retained
run-3 worktree plus a synthetic commit runs the e2e trial test green
(`docs/evidence-logs/m3/m3-fix-validation.log`). Run 4 at the final
head re-proves restoration end-to-end inside the harness (§5c row
below).

### 5c. Clean-clone determinism proof (ruling C's one re-execution)

Fresh `git clone --branch m3/options-20260819 --no-hardlinks` of the
local repo at head `8479f1f`, `uv sync --frozen`, then the sealed gate
re-executed end-to-end. Comparison is git_sha-normalized for the
recorded head-mix: trial payloads, `config_hash`,
`dataset_manifest_hash`, and both lanes' expected hashes must be
IDENTICAL; only `stamp.git_sha` may differ.
<!-- CLEANCLONE_RESULTS -->

## 6. Evidence log retention

`docs/evidence-logs/m3/` (committed `1993a06`) holds the surviving logs
after two external /tmp wipes: the corrected-verdict run
(`m3-verdict-correction.log`), the criterion-4 verification
(`m3-crit4-verification.log`), mutation run 2 (`m3-mutation2.{log,json}`),
the restoration-anomaly replica (`m3-restore-repro2.log`), and the
round-2 kill baselines. The wiped gate log's verdict lines are quoted
verbatim in `docs/m3-sealed-gate-criterion4-decision.md` and §4 above.
Run 3 and the harness-fix validation are retained alongside them
(`m3-mutation3.{log,json,md}`, `m3-fix-validation.log`, committed
`85e2435`). Runs completing after that commit (run 4, the final gate,
the clean-clone comparison) are appended to the same directory and
indexed in its README as they finish.

## 7. Non-claims / deferred

- Per-world power t below the OD1 predicted band (§5) — attenuation is
  the vehicle's, measured honestly; no claim of equity-lane efficiency.
- Arm-B stride-4 cohort t is printed, never gated (§4 criterion 2 uses
  pooled arm-A cohorts only, per plan).
- No real-data claims of any kind: all worlds synthetic (v1 era),
  `research_protocol.yaml` and `synth/` byte-frozen throughout.
- MU-2 does not touch the M0–M2 equity lane results; overlays are a
  separate registry file with separate tests.
- Cleanup (owner-gated, post-merge): worktree removals, local-main ff
  to `96e4f6e`, stale m0/m1 branch deletion.

## 8. Process record

- **/tmp evidence wipes (×2, external actor)**, 2026-08-20 ~13:05–13:50
  and again mid-afternoon: every live `/tmp/m3-*` log deleted while runs
  were in flight, including the sealed gate's own console log. Remedy:
  all evidence retained in-repo under `docs/evidence-logs/m3/`
  immediately on completion (commit `1993a06` onward); the stamped
  `sealed-gate-summary.json` and the memo's verbatim quote preserve the
  lost log's content.
- **Second implementer lane, interrupted**: a concurrent agent session
  took over the lane mid-closeout (commits `958c0fc`–`85e2435` era),
  executing the same playbook in parallel with this packet's driver.
  The owner ruled a single driver; the second lane was stopped and its
  completed work adopted verbatim (mirrors the M2-proper §8
  two-implementer disclosure).
- **Restoration-suite saga**: three in-harness failures with two false
  "cannot reproduce" diagnoses (load hypothesis) before the retained
  worktree exposed the deterministic git-less-copy root cause (`bc8b12c`).
  Lesson recorded: an in-harness-only failure with clean isolated
  replicas means the HARNESS environment differs — retain and inspect
  the worktree before theorizing about load.
- **Exit-code masking**: `&&` after `cmd | tail` swallowed a nonzero
  ruff exit during the strengthening rounds (formatting slipped into
  two commits); caught and corrected before the final gate — all later
  pipelines capture exits explicitly.

## 9. Entry criteria unlocked

Per plan §6: options-era machinery (overlay generator, PIT surface,
American exercise/settlement, two-arm backtest, sealed two-lane
validation) is now proven on synthetic data — the next milestone's
entry criteria (real-data option surfaces, if ruled) are satisfiable
from this base.

## 10. Evidence invalidation

Any change to code, tests, protocol, dependencies, or `synth/` after
the code-final head invalidates this packet; docs-only commits do not.
The world and overlay registries fail closed on regeneration drift, and
the mutation harness must show zero of everything except KILLED with a
passing restoration suite at the final head.
