# PR A evidence — M4 durable closeout and sealed-run authority

Campaign `TREE-M4C-M5P-M6C-001`, packet PR A. Branch
`m4/durable-closeout-preflight-20260823`, base `1f7c388` (tree
`5e9c170a3e847c4fb7ce2fde0c4eb4841bd8a8a7`).

Packet A1 (runstate), A2 (universe + census), A3 (amendment builder),
A4 (bars manifest + launcher), A5 (G4 seal), A6 (ops docs), plus the two
owner-selected riders (docs truth-up, G3 Ask C world-registry entries).

## Execution mode

Phase 0 (live-state reconciliation) was completed first:
`artifacts/reconciliation/20260823T1819Z/` (state.json + report; gate
GREEN on a `--no-local` clean clone of `1f7c388`: 899 passed,
178/178 KILLED, restoration TRUE — host log
`~/documents/tree_options-logs/phase0-gate.log`). Three flags recorded
for the owner: the 29-vs-30 universe discrepancy, the G3
bar-volume-derivation contradiction, and the 2026-08-21 spot-close gap
(cached DELAYED vendor response; re-running cannot heal it).

Implementation ran as a five-packet subagent fan-out (A2b census, A3
amendment, A5 seal, riders in parallel worktrees; A4 bars after A5's
ledger landed; A6 ops after all CLIs) with orchestrator verification and
cherry-pick integration. Every packet's report was checked against its
logs and the delivered code before integration; two material findings
from that verification are recorded below.

## Commits (base → head)

| commit | packet |
|---|---|
| `efb1189` / `b952d0c` / `0d1a88f` / `9d2dfb9` | A1 runstate (core, liveness, CLIs, mutants M180–M193) |
| `4f3bbb0` | M182 replacement + ruff-format drift fix (see below) |
| `1884a0c` | A2 part 1: declared universe manifest (29×105=3,045, `a13dd4eb…`) |
| `041509c` | A5 seal (identity, ledger, preflight, M213–M218) |
| `3bc198a` | A3 amendment builder (dry-run only, M200–M207) |
| `dcf3103` | A2 part 2: census builder (M194–M199) |
| `a2974c4` | census exit-rule review fix (see below) |
| `a45cf3f` | A4 bars manifest + launcher (M208–M212) |
| `7da1fca` | A6 ops (runbook, checklist, systemd template) |
| `c4505c4` | riders (docs truth-up + real_lanes registry entries) |
| `8274327` | mutation-harness scratch fix (see below) |
| `762ab9d` | runbook §4.1 correction (launcher records ERA_EXIT itself) |

## Verification evidence (host logs under `~/documents/tree_options-logs/`)

- **A1 mutation harness**, 192 mutants at `9d2dfb9`+A2-part-1 tree
  (`pra-mutate-1.log`): **191 KILLED, 1 SURVIVED, restoration TRUE**.
  The survivor (`M182-unknown-accepted-as-target`) was a redundant
  guard — the `LEGAL_EDGES` whitelist on the adjacent line already
  refuses every UNKNOWN edge, so gutting the explicit guard has no
  observable behavior. Replaced by `M182-legacy-era-undetected` (the
  `/proc` pre-journal era probe in `era_status`); manual kill-proof in
  `pra-m182-killproof.log`: exactly
  `test_legacy_prejournal_era_detected_exit_3` FAILED under the
  anchor→replacement, byte-exact restore verified.
- **Integration suites** (each run ONCE at each integration point):
  1,267 → 1,308 → 1,343 → 1,389 → 1,406 passed, exit 0
  (`pra-int-a5-suite.log`, `pra-int-a3-suite.log`,
  `pra-int-a2b-suite.log`, `pra-int-a4-suite.log`,
  `pra-final-content-suite.log`); mypy 97→107 files clean at each step;
  ruff format+check clean (230 files at final content head).
- **Clean-clone reproduction** (M3 §5c pattern): `git clone --no-local
  --branch …` → `uv sync --frozen` green → full suite **1,406 passed,
  identical count** (`pr-a-cleanclone-*.log`) → failure-injection
  focused suites 386 passed → all seven CLIs answer `--help` exit 0 →
  **tree hash identical** `609f5e2b60d2180f1932452c2605c2f8d3d22505`
  in both trees.
- **Packet mutant kill-proofs**: A2b all six (M194–M199) and A4 all
  five (M208–M212) individually kill-proven by their agents
  (mutate → purge `__pycache__` → owning test FAILs → byte-exact
  restore); A3 spot-verified M200/M202/M203; A5 anchors exact-once.
  The full harness settles all of them (see gate results in the PR
  body).

## Orchestrator verification findings (fixed before merge)

1. **Census exit rule** (`a2974c4`): as delivered, exit 0 required
   every pair COMPLETE — but `INCOMPLETE_CLASSES` deliberately excludes
   `SPOT_MISSING_HOLIDAY` (holiday Fridays have no close by
   definition), and the committed grid contains Good Fridays
   2025-04-18 and 2026-04-03. Exit 0 would have been unreachable for a
   perfect era census. Fixed: exit 0 = zero `INCOMPLETE_CLASSES` pairs
   + masters observed == expected; holiday test flipped to exit 0.
2. **Mutation-harness scratch** (`8274327`): the first full-gate
   attempt at `c4505c4` reported 10× HARNESS_ERROR (M208–M212,
   M214–M218) and restoration FAIL (`pr-a-gate.log`). Root cause: the
   harness's disposable copy lived under `/tmp`, and the A4/A5 ledger
   tests place scratch at repo-relative `artifacts/` paths precisely to
   honor the ledgers' mechanical `/tmp` refusal — inside the copy those
   paths resolve under `/tmp` and the (correct) refusal fails every
   owning test at baseline. Fixed by moving the harness scratch beside
   the repo (disclosed gate-machinery change; kill semantics untouched;
   the clean-clone suite passing outside `/tmp` is the direct evidence
   the harness copy now passes too).
3. Minor: the riders agent reported 7 new tests; the file collects 6
   (count off by one; content independently verified). The A6 agent
   died at a provider usage cap after finishing the runbook; the
   orchestrator verified every CLI claim in it against the argparse
   definitions and completed the remaining deliverables.

## Constraints honored

No final-holdout access; no 0.2.1 value chosen anywhere (the amendment
builder refuses to invent one; `owner_ratified_policy_value` is empty
in every census); no bars-era request; no G4 authority consumed (the
execute paths are CLI-refused; no APPROVAL/CONSUMPTION record exists
outside tests); no broker, no network orders; the live coverage era's
pre-existing process was never touched; no GitHub Actions; all
long-running authority paths refuse `/tmp` mechanically.

## Log inventory

All raw logs are retained host-side under `~/documents/tree_options-logs/`
(referenced by name above): phase0-gate, pra-mutate-1,
pra-m182-killproof, pra-int-*-suite, pra-final-content-suite,
pra-{a2b,a3,a4,a5,a6,riders}-*, pr-a-cleanclone-*, pr-a-gate (first
attempt, FAILED with the harness-scratch finding), and the final gate
log for the exact PR head (see PR body for its verdict line).
