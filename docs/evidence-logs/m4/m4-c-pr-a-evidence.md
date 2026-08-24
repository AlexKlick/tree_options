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
| `d61a0b0` | evidence: PR A verification record (round-1 gate 1,406 / 217 KILLED at this head) |
| `c17df86` / `f28c714` / `d1fc2b8` | R3: round-1 remediation (F1–F3+F8a/d runstate; F4–F6 amendment/bars; F7+F8a–d seal/docs) |
| `117c422` / `6c63e50` / `9202979` / `b92ff709` | R3 fixups: ruff drift; restore mutate `_run`; reanchor M185, supersede M214; drop M214 |
| `ac8b2a2` / `850f370` / `deb2ecd` / `bd92e9e` / `21eee40` | R4: round-2 runstate remediation (run-id escape; lock-all-mutators; heartbeat ordering; CLI exit codes 6/7/8; real argv_hash invariant) |
| `5a763c0` | R4: register M229 (replaces M214) + runbook exit codes 6/7/8 |
| `b7ebca5` / `d950a0f` / `e51a179` / `3e920f3` | R4: round-2 protocol/bars remediation (F4 derivation-time gate; F5 one-manifest binding; F6 execute-time census + identity cross-join; F7 test half) |
| `1553d91` | evidence: record both review waves (round-2 finding 8) |
| `5940b25` | test: unlink the round-2 symlink fixture — dangling scratch links crashed the harness copy (gate9 below) |
| `b82bfd9` | evidence: gate9 crash record + rows through 5940b25 (gate11 head) |
| `1efb697`/`604f8ea`/`f684147`/`2398669`/`cd7a29b` | R5-1: strict CensusFact.v parse; schema/report version pins; entry↔envelope semantic join; amendment output re-resolution + confinement; packet hashes attest consumed bytes |
| `5be2d3d`/`666bf0c`/`e363500`/`a272405`/`31c9d40`/`a195ea3` | R5-2: cross-store one-shot atomicity; work-manifest verified/hashed/consumed from ONE read; open() binds run id to identity; run.json-only store reports UNKNOWN (exit 3); checklist defers ERA_EXIT; runbook mismatch row → RECONCILIATION |
| `40da670`/`5ac0e04`/`674bd37`/`13e82f4`/`4fc4802` | R6: semantic demotion covers holiday pairs; output files refuse shared inodes (hard-link aliasing); report_version required, never defaulted; universe generator records the wrapper's absolute real path; execute narrows EVERY approval record, not the first |

## Review waves and remediation (2026-08-23)

Two independent adversarial reviews (Codex `gpt-5.6-sol`, detached,
exact-head, read-only; probes re-verified host-side before acceptance):

- **Round 1** (head `d61a0b0`, log `pr-a-codex-review.log`): VERDICT
  NO-GO — 8 findings (5×P1, 3×P2): runstate authority under `/tmp`;
  non-transactional stale-lease adoption + journal append; pin
  replacement; NOT_EVALUABLE facts as derivation operands; bars
  regeneration not proven; bars cross-run join absent; G4 duplicate
  guard trusting stored ids; unverified operator-artifact claims.
  Remediated in R3 (`c17df86`–`b92ff709`); gate at `b92ff709`:
  1,420 passed / **216** KILLED (M214 dropped — see round 2) /
  restoration TRUE (`pr-a-gate8.log`).
- **Round 2** (head `b92ff709`, log `pr-a-codex2.log`): VERDICT NO-GO —
  9 findings (4×P1, 4×P2, 1×P3): absolute `run_id` escapes the F1 root
  guard; fresh-acquire/release bypass the adoption lock (two live
  owners) + a vacuous `or True` test assertion; F4's emission-time
  confidence gate made the canonical census unusable by the amendment
  builder; F5 verified provenance against two different capture
  manifests; F6 still consumed authority with placeholder identity and
  a deleted census; F8d heartbeat mismatch ordered after the non-process
  ALIVE return; M214 removed without the promised M229 replacement and
  the forged-consumption test inserted a legitimate consumption first;
  PR #13 + this evidence doc stale at the pre-remediation head; F8a CLI
  usage/mapping inconsistencies. Remediated in R4 (`ac8b2a2`–`3e920f3`
  + this evidence commit), each fix red-first (agent logs
  `pra-r4-r4{1,2}-fix*-red.log`) and re-verified by the orchestrator
  (my independent runs + the reviewers' exact probes re-executed
  post-fix: `pra-r4-pr-a-*.log`; focused suites `pra-r4-r42-verify.log`
  109 dots / `pra-r4-r41-orchestrator-verify.log`).
- **M229 registration note** (round-2 finding 7): the mutant targets the
  stored-vs-recomputed arm (`if False:` on the disagreement check) — an
  arm-2-only mutant (trusting stored ids in the duplicate match) is a
  redundant guard that SURVIVES, because the corruption arm fires first
  for every forged shape (content_identity excludes code_sha, so the
  tampered payload still shares content identity and trips arm 1).
  Kill-proof narrative in commit `5a763c0`; population restored to 217.
- **Gate9 harness crash** (`1553d91`, `pr-a-gate9.log`, GATE_EXIT=1):
  failed BEFORE any mutant ran — the harness's disposable repo copy
  crashed in `shutil.copytree` on dangling symlinks left under
  `artifacts/runstate-tests/r2-*/` by the R4-1 symlink-escape test
  (pytest reclaims the `tmp_path` target at session end; copytree
  dereferences symlinks; 13 links had accumulated across R4 suite
  runs). Fixed in `5940b25` by unlinking the link in a `finally`
  (matching the `test_seal_ledger.py` ledger-root fixture idiom); the
  pre-fix scratch links were removed. Two gate launches were stopped by
  the orchestrator before any verdict (one at `1553d91` because the
  finding-8 evidence commit had to join the gated head; one at
  `5940b25` because this row did) — killed pre-verdict, no tree damage,
  re-launched at the final head only.
- **Round 3** (head `b82bfd9`, gate11 GREEN 1,445 / 217 KILLED /
  restoration TRUE / clean-clone identical tree, log
  `pr-a-codex3.log`): VERDICT NO-GO — 12 findings (5×P1, 6×P2, 1×P3);
  8/9 round-2 findings RESOLVED (finding 3 NOT_RESOLVED: lax
  `CensusFact.v: int | str` coerces `True`→1 / `1.0`→1 at parse,
  defeating the strict-int gate downstream — orchestrator-verified by
  direct model probe before remediation). New findings: census
  entry↔envelope semantic join absent; cross-store BARS one-shot race;
  work-manifest verify/hash TOCTOU; amendment output-root symlink
  escape; packet hashes from fresh reads; open() identity unbound;
  era_status crash on run.json-only store; schema versions unpinned;
  clean-clone count BLOCKED (the retained log's `-q` output has no
  count line); checklist ERA_EXIT fabrication; runbook mismatch-row
  contradiction. Remediated in R5 (`1efb697`…`a195ea3`, 11 commits,
  each red-first; agent logs `/tmp/r51-*`, `/tmp/r52*` + orchestrator
  re-runs; the R5-2 agent died at context overflow after findings 1–3
  and a continuation agent finished 4–6). Finding 10's re-evidence
  (clean-clone with an explicit count line) is produced at the R5 head
  after the final gate.
- **Round 4** (head `d2ce663`, gate12 GREEN 1,472 / 217 KILLED /
  restoration TRUE, log `pr-a-codex4.log`): VERDICT NO-GO — 6 findings
  (2×P1, 4×P2); 8/12 round-3 findings RESOLVED; all constraint checks
  PASS. Every finding orchestrator-verified against the exact code sites
  before remediation: holiday pairs escaped the COMPLETE-only semantic
  demotion (`SPOT_MISSING_HOLIDAY` is outside `INCOMPLETE_CLASSES`, so a
  foreign-envelope holiday pair exited 0); hard-link aliasing bypassed
  `_confine_output` (`resolve()` is path-containment; a shared inode
  truncated a tracked file through the "confined" path); an absent
  `report_version` defaulted to current and re-hashed clean; the R5
  clean-clone capture was itself truncated through `tail -5`
  (evidence-rule violation — a full-capture re-run is produced at the
  R6 head); the checklist universe regen check false-drifted on
  relative-vs-absolute wrapper spelling; `_matching_approval` honored
  only the FIRST approval for a protocol hash. Remediated in R6
  (`40da670`…`4fc4802`, 5 commits, each red-first; agent logs
  `/tmp/r61-f*-red.log` + orchestrator re-runs — full suite 1,479,
  exit 0). One agent-noted deviation: the builder had relied on the
  `report_version` default rather than setting it explicitly; the fix
  adds the explicit value (identical bytes, hashes unchanged).

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
   orchestrator re-checked CLI claims it reviewed but the round-1
   independent review (Codex gpt-5.6-sol, exact head d61a0b0, log
   `~/documents/tree_options-logs/pr-a-codex-review.log`) still found
   three claim/code mismatches (F8a/b/c) — fixed in this wave.

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
attempt, FAILED with the harness-scratch finding), r2-* / r3-sync (R3
wave), pr-a-gate3–gate8 (R3-era gates; gate8 = the round-2-reviewed head
`b92ff709`), pr-a-codex-review / pr-a-codex2 (both review transcripts),
pra-r4-* (R4 red/green agent logs, orchestrator verification runs, and
the reviewers' probes re-executed post-fix), and the final gate log for
the exact PR head (see PR body for its verdict line).
