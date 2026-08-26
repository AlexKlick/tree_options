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
| `06f3e9e`/`58c9e17`/`7c15944`/`7211e0a`/`689a53e` | R7: custody-held output writes (temp + os.replace, fstat nlink==1); builder refuses a non-WHOLE census (masters_observed == expected); O_NOFOLLOW on the ledger name in BOTH ledgers (dangling-symlink O_CREAT follow closed); regeneration re-hashes every read against the manifest pin; census.md states the real exit-0 rule incl. holidays |
| `ee419cb`/`59e9d7f`/`0972d19`/`2c3db7b`/`f19bce0`/`f74a9a6` | R8: unpredictable mkstemp temp + published-inode verification; output paths that are themselves symlinks refused even in-root; ledger ROOT taken into custody O_DIRECTORY\|O_NOFOLLOW in BOTH ledgers (dir_fd-relative, ENOTDIR mapped); regeneration completeness (pinned-minus-read must be empty); census re-hashes every capture file at read time via a read-once shim — plus the protected-file correction restoring `inspect_structural_coverage.py` to its base blob |
| `4888270` | gate16 M199 SURVIVED kill-restoration: owner test gains an unlisted-file phase only manifest verification refuses (the R8 re-hash had masked the deleted-master scenario; mutant re-killed by hand-applied kill-proof, mutate.py untouched) |
| `cdf67a8`/`337e34a`/`576f120`/`5f1b4df` | R9: publish verification = final-name + byte custody (lstat-regular, O_NOFOLLOW re-open, byte compare); ledger roots walked COMPONENT-WISE from / with dir_fd + O_NOFOLLOW per component (both ledgers, create branch mkdir(dir_fd=)); capture-manifest bytes-once (raw= on the loader, census provenance + BARS binding consume the verified byte set, drift guard read refuses); census refuses pinned-but-unreferenced masters |
| `9e7a39b`/`db8e050`/`d469753`/`265146a`/`9bc08b5`/`493fed9` | R10: amendment bytes-once (proof step parses the rendered text, emitted hashes rendered bytes) + final-effect sweep at packet attestation; manifest drift guard MOVED to the final effect (census before emission, BARS at the binding); both ledgers re-verify the ledger NAME maps to the locked inode post-fsync under flock (RECONCILIATION, never success); runstate journal O_NOFOLLOW name custody on read + append against a dir-fd-held store dir; census emitter custody writes (CensusEmitRefused → exit 4); amendment output PARENT held under a component-wise custody walk, every write dir_fd-relative |
| `004ae49`/`38be17b`/`f30329d`/`7ed168a`/`3fb0bd1`/`314a5f4`/`91e6148` | EXTERNAL audit lane (owner-ruled into PR A 2026-08-25): canonical run identity (RunIdentityCore + NonCanonicalRunIdError at create/open, M230); checkout-independent universe identity (logical repo-relative source ids, M231–M233); FULL runstate store under a shared no-follow custody module (`runstate/custody.py`, M234+); typed held-input G4 authority (VerifiedSealedInputs, effect-boundary joins, M244–M267); disclosed gate-copy exclusion (`artifacts/`+`dist/` from the disposable mutation copy, owning regression test, 314a5f4); evidence doc `pr13-audit-remediation.md` |
| `908afa6`/`5ca9adb`/`2c86396`/`00c1593` | R11-A (owner-ruled consolidation wave): durable name→inode binding — companion identity record custody-written at ledger/journal creation, every open verifies name↔bound inode (a byte-clone at the canonical name can never be consumed; unbound non-empty and vanished-bound both refuse); `custody.write_all` looped write is the ONLY authority-record write path (both ledgers + journal); `atomic_write(expected=…)` identity-conditional replace — stale adoption refuses a replacement live owner; `unlink_held_name` = rename-to-unpredictable-temp → verify renamed identity → unlink the TEMP only (a successor at the old name is never deleted) |
| `744e1f3`/`881bdcd`/`71cf8ee`/`87b175a`/`cfac494`/`4431f14` | R11-B (same wave): amendment holds ONE custody fd across all four emits with the final sweep AT packet return over all four names through a fresh component-wise re-walk whose dir-fstat identity must equal the held fd (out-of-root relocation refused); census emission carries the manifest gate at the write moment against the sha RECORDED in the census body (MassiveManifestError → exit 2, nothing published) and publishes the three outputs as one all-or-nothing set (temps → vet → rename-set → verify at return; refusal unlinks temps and drops the digest dir, retry is clean); g4-seal runner identity = registry-resolved (`RUNNER_REGISTRY` + `runner_implementation_sha256` bound into the self-hashed packet at approval, current code hash cross-joined before consumption — execute takes NO runner parameter, a foreign callable with the approved literal is not authority); verified-inputs exit re-scans the held directory and requires exact entry-set equality with the snapshot the verifier consumed |
| `5887acf`/`fc615e8`/`6ec70ec`/`81296a4` | R12 (owner ruling: fix the real bugs + declare the threat model): release restores a swapped entry at the canonical name (with a restoration re-verify) BEFORE the mismatch refusal — the refusal path never empties `owner.json`, so no fresh exclusive acquire while a successor is live; runner identity binds the CALLABLE — (version, qualified name, source-file sha256, config digest) in the packet, all four re-derived from the registry entry at execution, `register_runner` requires a validated 64-hex `config_digest` (same-file foreign runner and differently-configured instances refused); census refusal cleanup verify-then-deletes the digest dir (held-identity match or loud refusal — a substituted directory is left intact); the emit-set catch gains raw `OSError` and any failure after the first rename rolls back identity-checked this-run-published names (all-or-nothing incl. the untyped path, retry clean) |
| `2722b32`/`79fe077`/`fd151af` | R13: DUAL-TREE ledger anchor — the ledger identity is recorded in a SECOND tree (a dedicated custody-written identity record under the runstate store namespace, `runstate/seal-ledger-anchor/<sha256(root)[:16]>.identity.json`, mapping documented in ledger.py; journal.py/store.py unmodified), every open verifies BOTH the beside-the-file companion AND the runstate anchor, so an OFFLINE co-replacement of ledger.jsonl + its companion with a self-consistent approval-only pair refuses at the next open; in-scope extensions: a surviving anchor with a vanished ledger/root refuses (the second tree's memory that authority existed), the anchor binds at first append (creation crash window closed), and the anchor pins the companion's byte digest; runner config digest is RECOMPUTED from the live implementation at the effect boundary via a registration-time `config_digest_fn` (mutated-after-approval configuration → ApprovalInvalidError before consumption; the stored digest comparison also retained; placed after `runner = registered.implementation` to preserve M266's anchor — after every identity check, before any authority is spent); census OUTPUT-EXISTS recovery semantics — byte-identical complete set → idempotent exit 0, strict-subset residue incl. this emit path's own-pattern stale temps → custody roll-forward (exit 0 whole / exit 5 incomplete — the documented never-report-partial-as-whole contract preserved), foreign content → refusal — plus `fsync(out_fd)` after the rename set |
| `b587bd5`/`36a73e3`/`ef84991` | R14 (owner ruling: fix the 3 concrete + declare the trust roots): the anchor record (format 2) gains the ledger's COMMITTED EXTENT — `ledger_size` + `committed_tail_sha256`, written at creation over the empty extent and advanced after each append's fsync + name check by an identity-conditional custody replacement, checked at every read and append — same-inode prefix rollback and a valid re-chained in-place rewrite padded to the anchored size both refuse as corruption, while the benign crash case (append durable, anchor update interrupted → valid chain extension) opens and re-anchors; the PARENT of every first-use namespace directory is fsynced (custody `open_directory` create branch + the ledger-root walk — an interrupted first append can no longer lose the namespace entries on reboot); a fresh census publication fsyncs `args.out_root` through a component-wise no-follow walk so the digest entry is durably committed (refusal = exit 4). Disclosed: ANCHOR_FORMAT 1→2 (a pre-R14 anchor is now a malformed-anchor reconciliation refusal — every ledger in the suite is created fresh; no durable pre-R14 ledger exists); one pre-existing anchor test's contract updated (extents now advance per append by design); the F3/F5 owning tests trace mkdir/fsync ordering by real directory identity alongside the structural walk |

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
- **Gate13 MUTATION_DRIFT** (`pr-a-gate13.log`, GATE_EXIT=1 at
  `9bc503d`): 1,479 passed / 216 KILLED / restoration TRUE — the sole
  failure was M209, whose anchor the R6 finding-6 fix had rewritten.
  Re-anchored in `6ba33d8` to the new all-approvals narrowing line
  (replacement drops the narrowing); kill-proofed manually — under the
  mutant the owner test FAILED with the runner invoked and a
  consumption record bound (`/tmp/m209-killproof.log`), byte-exact
  restore, owner suite green (`/tmp/m209-restore.log`). A first
  gate13 launch was killed mid-harness by the harness task reaper
  (no verdict; log showed the suite green at the point of death) and
  was relaunched detached-setsid to completion.

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

- **Round 5** (head `3e20215`, gate14 GREEN 1,479 / 217 KILLED /
  restoration TRUE, log `pr-a-codex5.log`): VERDICT NO-GO — 5 findings
  (4×P1, 1×P2); round-4 5/6 RESOLVED (the hard-link fix's TOCTOU the
  exception); the full-capture clean-clone accepted; all constraint
  checks PASS. Every finding orchestrator-verified in source before
  remediation: the inode check and write_text were separate operations
  (a link planted between them truncated a tracked file); the builder
  accepted a census its producer had classified non-whole
  (masters_observed != expected); a DANGLING child-file symlink at the
  ledger name was followed by O_CREAT in BOTH ledgers (authority under
  /tmp); BARS regeneration verified masters then re-read bytes; census.md
  stated the obsolete every-pair-COMPLETE exit rule. Remediated in R7
  (`06f3e9e`…`689a53e`, 5 commits, each red-first; agent logs
  `/tmp/r71-f*-red.log`; the finding-1 RED genuinely truncated
  research_protocol.yaml before the fix — restored, md5 verified). Full
  suite 1,488 (agent + orchestrator runs identical, 0 failures).

- **Round 6** (head `f400522`, gate15 GREEN 1,488 / 217 KILLED /
  restoration TRUE, log `pr-a-codex6.log`): VERDICT NO-GO — 5 findings,
  all P1 (three round-5 remediation boundaries still bypassable + two new
  producer/output-integrity gaps); round-5 5/5 RESOLVED; all constraint
  checks PASS. Every finding orchestrator-verified in source before
  remediation: `_write_exclusive`'s temp name was pid-predictable and the
  publish unverified (rename-plant race); `_confine_output` accepted an
  in-root symlink aliasing two own artifacts; the ledger ROOT was
  re-resolved between validation and mkdir/open in BOTH ledgers
  (dir-symlink swap lands authority under the target); BARS enumeration
  re-hashed only PRESENT masters (a pinned master deleted pre-enumeration
  was silently absent); the census producer re-read the spot proxy and
  masters after manifest verification (swap upgrades a sealed
  SPOT_MISSING_SESSION into COMPLETE). All one family: filesystem-custody
  TOCTOU. Remediated in R8 (`ee419cb`…`f74a9a6`, 6 commits, each
  red-first; agent logs `/tmp/r81-f*-red.log`). Orchestrator verification
  caught one violation before freeze: the finding-5 commit had carved its
  `raw=` seam into PROTECTED `scripts/inspect_structural_coverage.py`
  (blob `b755f947` ≠ base `c7bd4115`) — reworked through a read-once
  `_PinnedCaptureFile` shim so the frozen loaders parse pinned bytes under
  their base signatures (`f74a9a6`; all three protected blobs re-verified
  identical to base). Full suite 1,495 (orchestrator run identical to the
  agent's, 0 failures). Gate16 at `0427161` then failed on exactly one
  mutant: M199 SURVIVED — R8's finding-5 re-hash independently refuses the
  owner test's deleted-master scenario, masking the verify-gutting mutant
  (invariant enforced twice, but the owner test must still fail under the
  mutant). Restored in `4888270` with an unlisted-file phase only
  manifest verification can refuse (hand-applied kill-proof
  `/tmp/m199-killproof.log`: phase 1 exit 2 via re-hash, phase 2 exit 0 →
  assertion fails); gate17 is the exact-head record for this head.

- **Round 7** (head `a6eb384`, gate17 GREEN 1,495 / 217 KILLED /
  restoration TRUE, log `pr-a-codex7.log`): VERDICT NO-GO — 4 findings,
  all P1; round-6 5/5 RESOLVED; all constraint checks PASS (the reviewer
  independently re-verified the three protected blobs, accepted gate17,
  the M199 kill restoration, and the f74a9a6 correction). Every finding
  orchestrator-verified in source before remediation: the post-publish
  check used `os.stat` (FOLLOWS a planted symlink) and `emitted` read
  through it too — rename-to-`.held` + link + in-place rewrite kept the
  inode while substituting content; root custody was a single open, so
  `O_NOFOLLOW` bound only the FINAL component and a symlinked
  intermediate ancestor was followed (both ledgers);
  `capture_manifest.json` was verified once then RE-READ — census
  provenance and the BARS work-manifest binding hashed a swapped second
  read; the manifest pins every on-disk master in `files[]` but the
  census iterated `manifest.masters` only, silently ignoring a
  pinned-but-unreferenced master at exit 0. All four the same
  custody family, one level deeper. Remediated in R9
  (`cdf67a8`…`5f1b4df`, 4 commits, each red-first; agent logs
  `/tmp/r9-f{1,2,3,4}-*.log`). F3 carried one interpretive call,
  disclosed in its commit: bytes are THREADED from the single verified
  read into the artifact (provenance/binding name exactly those bytes)
  plus a guard re-read whose only effect is refusal on drift — pure
  threading could not refuse a swap landing after the verified read;
  census exit 2 chosen (manifest-tamper family). Full suite 1,502
  (agent + orchestrator runs identical, 0 failures; +7 tests).

- **Round 8** (head `f95b99a`, gate18 GREEN 1,502 / 217 KILLED /
  restoration TRUE, log `pr-a-codex8.log`): VERDICT NO-GO — 6 findings,
  all P1; round-7 disposition 2/4 RESOLVED (component walk, census
  completeness), 2 held NOT_RESOLVED as deeper variants (the R9 fixes
  put custody inside the helper and guards before the work — the
  reviewer proved the window moves to AFTER the helper returns and
  AFTER the guard); all constraint checks PASS. All six
  orchestrator-verified in source: the amendment builder re-read its
  published outputs by PATHNAME after `_write_exclusive` released
  custody (proof step + `emitted`); the manifest drift guards preceded
  derivation/emission (census) and the binding (BARS); the ledgers
  opened+flocked the inode but never re-verified the NAME post-fsync
  (rename+clone breaks the one-shot domain across two executions);
  runstate's /tmp refusal covered the store root but not the
  `journal.jsonl` child; the census emitter wrote through final names
  with plain `write_text` (a planted link truncates a protected-path
  file); amendment's helpers re-resolved intermediate components after
  confinement. Remediated in R10 (`9e7a39b`…`493fed9`, 6 commits, each
  red-first; agent logs `/tmp/r10-f{1..6}-red.log`). F4's fix landed
  wholly in journal.py (the finding located the defect there; store.py
  untouched). CensusEmitRefused maps to exit 4 (emission/refusal
  family, pinned in test + docstring). Full suite 1,510 (agent +
  orchestrator runs identical, 0 failures; +8 tests).

- **External audit lane integrated (2026-08-25, owner ruling: keep in
  PR A).** An independent external audit of PR #13 at `f95b99a`
  (`tree_options_pr13_audit_and_next_issues-1d5a47.md`) found four
  merge blockers — G4 preflight accepting untyped files, runstate
  following filesystem links, deterministic run identity documented but
  not enforced, and host-checkout-dependent universe identity — the
  same custody/identity family the review rounds converge on. A
  parallel Codex lane remediated all four on this branch
  (`004ae49`…`91e6148`, 7 commits, +~5k lines incl. the shared
  `runstate/custody.py` module and 38 new mutants M230–M267; registry
  now 255). Its own record is `pr13-audit-remediation.md`; its
  exact-head gate at `314a5f4` (255/255 KILLED, restoration TRUE,
  exit 0, full-capture log sha256 `e90e9d5c…` — orchestrator-verified
  byte-identical, preserved as `pr-a-gate20-external.log`) and its
  clean-worktree reproduction (1,585 passed) were accepted after
  independent verification: protected blobs identical to base, 0
  anchor drift across all 255 cb2b2eb→91e6148, full suite 1,585
  green. The lane's two non-passing gate attempts were honestly
  recorded and NOT promoted; the gate-copy exclusion (314a5f4) is a
  disclosed, narrowly-scoped harness change with an owning regression
  test (same class as the 8274327 precedent). **Gate19 REFUSED, not
  failed:** every stage was green against the frozen `cb2b2eb` copy
  (1,510 / 217 KILLED / restoration TRUE) but the exit trap correctly
  refused certification because the external lane's commits moved the
  head mid-gate (cb2b2eb→004ae49 at 15:02). gate21 is this
  integration's exact-head record.

- **Round 9** (head `88e6630`, gate21 GREEN 1,585 / 255 KILLED /
  restoration TRUE, log `pr-a-codex9.log`): VERDICT NO-GO — 10 findings
  (8×P1, 2×P2); round-8 disposition 3/6 RESOLVED (journal child-name
  /tmp vector, census emit symlink truncation, plus the R7-F4 census
  completeness carry-over), 3 NOT_RESOLVED as deeper variants (the
  sweep/guard/name-check all still land BEFORE the true final effect:
  packet publication, render/emit, unlock/close/return); all constraint
  checks PASS (reviewer re-verified the protected blobs and accepted
  gate21, gate20-external at its own head, and the gate19 REFUSED
  handling). All ten orchestrator-verified in source at the cited
  lines. Six in the orchestrator lanes: amendment custody fd closed
  before packet publication; census/BARS guards before render/hash;
  ledger/journal name→inode check then an unguarded success window;
  amendment parent fd never re-bound to the confined root at packet
  return; a NEW class — both ledgers did one unchecked `os.write`
  (positive short count = torn prefix acknowledged as success);
  census partial emission unretryable. Four in the external lane's
  code (disclosed to the reviewer as never-reviewed parallel
  remediation, reviewed with full rigor): stale adoption overwrites a
  replacement live owner; release's verify→unlink window unlinks a
  successor; the approved runner was a caller-asserted version string;
  the Massive directory snapshot could go stale between scan and
  verification. **Owner ruling 2026-08-25 (AskUserQuestion):
  CONSOLIDATION R11** — the per-site pattern converges per round but
  the reviewer sweeps every remaining window each round (4→6→10
  findings); the structural alternative is one custody boundary where
  the final effect carries its own verification. R11 executed as two
  parallel agents on disjoint files (A: custody.py + ledgers + journal
  + lease; B: amendment + census + verified_inputs + g4_seal), each
  red-first (agent logs `/tmp/r11a-f{3,5,6,7}-red.log`,
  `/tmp/r11b-f{1,2census,4,8,9,10}-red.log`). Orchestrator
  verification: changed files exactly the 17 briefed; protected blobs
  + mutate.py + pyproject byte-identical; AST-extracted all 255
  anchors, 0 drift; independent full suite 1,603 passed exit 0; hand
  kill-proof that M260's rewritten owner test still kills its mutant
  (FAILED under the applied mutant, byte-exact restore — the M199
  masking lesson applied proactively).

- **Round 10** (head `cc0eb12`, gate22 GREEN 1,603 / 255 KILLED /
  restoration TRUE, clean-clone full gate GREEN incl. seven CLI
  checks, log `pr-a-codex10.log`): VERDICT NO-GO — 10 findings
  (8×P1, 2×P2); round-9 disposition 1/10 RESOLVED (the looped
  write), 9 NOT_RESOLVED; all constraint checks PASS. All ten
  orchestrator-verified in source. Qualitatively different from
  prior rounds: the findings are not missed sites but the NEW R11
  mechanisms themselves carrying the same verify→act shape one
  level up — the companion identity record is replaceable together
  with the ledger (a self-consistent clone+binding bundle
  verifies); the identity-conditional replace still returns before
  an unconditional os.replace; release's rename-aside empties the
  canonical name before the mismatch refusal; runner identity is
  the whole source-file hash (same-file foreign callable or a
  configured instance passes); the amendment sweep is four
  sequential per-member checks then return; the confinement
  equality check precedes the artifact checks; both manifest guards
  still precede writes/hash; census closes the custody fd then
  rmtree's by blind pathname; sequential set renames let a raw
  OSError escape the typed catches leaving a partial set; the exit
  rescan is itself a snapshot. **Owner ruling 2026-08-25
  (AskUserQuestion): fix the REAL bugs + declare the threat
  model.** Four findings are defects independent of racing (F3
  release destroys the canonical name; F4 runner identity
  granularity; F8 delete-by-blind-pathname; F9 untyped exception +
  no rollback) — remediated in R12 (`5887acf`/`fc615e8`/`6ec70ec`/
  `81296a4`, each red-first; agent logs
  `/tmp/r12-f{3,4,8,9}-red.log`). Six findings are
  userspace-irreducible TOCTOU windows against a hypothetical
  concurrent local writer. **Threat model (owner ruling
  2026-08-25): PR A's authority model assumes a cooperative
  single-operator host. In scope for review: sequential
  correctness, durability, crash-window safety, identity binding,
  and any defect reachable WITHOUT a concurrent writer. Out of
  scope: interleavings that require an adversarial process
  concurrently writing the repo tree or artifacts/ between any two
  syscalls — userspace cannot close those windows in the limit,
  and this machine's operator context does not include such an
  attacker. Findings in the out-of-scope class will be recorded as
  boundary notes, not defects.** Orchestrator verification of R12:
  8 files exactly as briefed; protected blobs + mutate.py +
  pyproject byte-identical; AST 0/255 drift (M260's anchor line
  preserved verbatim inside the new four-way check); independent
  full suite 1,612 passed exit 0 (+9 tests).

- **Round 11** (head `e5fed92`, gate23 GREEN 1,612 / 255 KILLED /
  restoration TRUE, clean-clone full gate GREEN incl. seven CLI
  checks, log `pr-a-codex11.log`): VERDICT NO-GO — **3 findings
  (2×P1, 1×P2); the owner's threat-model boundary WORKED**: round-10
  disposition 4 RESOLVED (all four R12 fixes, restoration and
  escalation tests accepted), 5 OUT-OF-SCOPE boundary notes (the
  reviewer's own consistency pass confirmed each genuinely requires
  an interleaving concurrent writer), 1 NOT_RESOLVED — promoted to
  in-scope, correctly, because the companion co-replacement attack
  is OFFLINE (sequential between invocations). All constraint
  checks PASS (protected blobs; protocol 0.2.0/flow null with all
  0.2.1 references confirmed dry-run/test material; no holdout —
  still AWAITING_OWNER_DECLARATION; no broker; no G4 execution;
  /tmp mechanically refused). Findings, all orchestrator-verified:
  (1) the companion identity record is co-replaceable offline with
  the authority file — it lives beside what it guards and adds no
  security over it; (2) the runner config digest is STORED not
  recomputed — a mutable configured runner mutated after approval
  executes (the orchestrator's own "re-derives all four" claim was
  false for configuration — caught by the reviewer, accepted); (3)
  census publication is neither crash-atomic (SIGKILL residue →
  forever OUTPUT-EXISTS refusal) nor durably acknowledged (no
  directory fsync after the rename set). **Owner ruling 2026-08-25:
  dispatch R13 (all three).** Remediated in R13 (`2722b32` dual-tree
  anchor / `79fe077` live digest recompute / `fd151af` crash
  recovery + dir fsync), each red-first (agent logs
  `/tmp/r13-f{1,2,3}-red.log`; the F1 RED reproduced the exact
  offline co-replacement returning the approval-only clone view;
  the F2 RED executed the dangerous-mode mutated runner; the F3
  REDs hit the forever-refusal `assert 4 == 0`). Disclosed
  deviations: the anchor is a dedicated identity record under the
  runstate store namespace (NOT a journal record — the journal
  schema wants per-run incarnation fields; journal.py/store.py
  unmodified); the anchor also covers vanished-ledger refusal,
  first-append binding, and companion-digest pinning; the digest
  recompute sits after implementation resolution to preserve
  M266's anchor; recovery exits 5 (not 0) when the re-run census is
  itself incomplete — the documented never-report-partial-as-whole
  contract; this emit path's own-pattern stale temps are classified
  as crash residue and rolled forward. Orchestrator verification:
  8 files exactly as briefed; protected blobs + mutate.py +
  pyproject byte-identical; AST 0/255 drift; independent full
  suite 1,620 passed exit 0 (+8 tests).

- **Round 12** (head `5e729bf`, gate24 GREEN 1,620 / 255 KILLED /
  restoration TRUE, clean-clone full gate GREEN incl. seven CLI
  checks, log `pr-a-codex12.log`): VERDICT NO-GO — 5 findings
  (4×P1, 1×P2); round-11 disposition 0/3 RESOLVED; all constraint
  checks PASS. All five orchestrator-verified in source. THREE
  CONCRETE: (F1) same-inode prefix rollback — truncate the ledger
  to its approval prefix keeping the inode; the anchor bound
  inode + companion digest but not the committed extent, and
  replay accepted any valid chain prefix, so the consumption
  silently vanished and the approval re-spent; (F3) first-use
  namespace creations (ledger root, anchor-tree components) were
  never parent-fsynced — a reboot could lose both entries and the
  empty view would silently forget an acknowledged consumption;
  (F5) the census digest-directory entry in out_root was never
  parent-fsynced. TWO BOUNDARY-CLASS: (F2) forging ALL THREE
  records (ledger + companion + anchor) offline as a
  self-consistent set passes both-tree verification — the anchor
  is public derivations under the same operator-writable parent
  and is indistinguishable from a fresh install without an
  owner-held external anchor; (F4) a caller-supplied
  `config_digest_fn` can attest falsely (constant or
  side-effecting callback) — a defect of first-party registration
  code, not an attack. The reviewer's evidence gaps explicitly
  invited the declarations. **Owner ruling 2026-08-25 (fix the 3
  concrete + declare the trust roots). TRUST ROOTS (extending the
  threat model): (1) Authority namespace — the trust root is the
  owner's exclusive write access to the tree as a whole; an
  offline replacement of the ENTIRE authority namespace (ledger +
  companion + anchor together) is fresh-install-equivalent and OUT
  OF SCOPE; distinguishing it from a legitimate fresh install
  requires an owner-held external anchor, which is a NAMED FUTURE
  LANE, not PR A scope. (2) Registration layer — the
  runner-registration surface is first-party trusted code; a
  `config_digest_fn` that does not faithfully digest its
  implementation is a defect of that trusted code, not an attack
  surface (same trust class as the compiler).** Remediated in R14
  (`b587bd5` committed-extent anchoring / `36a73e3` parent fsyncs
  / `ef84991` census out_root fsync), each red-first (agent logs
  `/tmp/r14-f{1,3,5}-red.log`; the F1 RED accepted the
  rolled-back approval-only prefix, the padded in-place rewrite,
  and lacked the extent fields; the F3/F5 REDs traced that no
  parent fsync followed the creations). Orchestrator
  verification: 5 files exactly as briefed; protected blobs +
  mutate.py + pyproject byte-identical; AST 0/255 drift;
  independent full suite 1,625 passed exit 0 (+5 tests).

- **2026-08-25 R15 (round-13 remediation, owner ruling: CLASS fix —
  both families).** Round 13 at `07e6812` (pr-a-codex13.log): NO-GO —
  7 findings (4×P1, 2×P2, 1×P3), ALL orchestrator-verified in source;
  round-12 disposition F2/F4 → trust-root BOUNDARY NOTES (the ruling
  held), F1/F3/F5 → NOT_RESOLVED via deeper variants. Pattern: two
  families, both instance-fix-not-class-fix. **Family A — the
  committed extent exists ONLY on the seal ledger AND its larger
  branch skips the prefix proof:** (F1) `_check_anchor_extent`
  accepted any larger valid re-chain — approval + padded
  RECONCILIATION_NOTE beyond anchored size N hides a removed
  consumption, re-spend; (F4) BARS has NO extent binding — same-inode
  truncation to the approval line re-spends the one-shot launch
  authority; (F5) the runstate journal likewise. **Family B —
  parent-fsync covered only 2 create branches, not restart-closed:**
  (F2) a crash-residue component takes the existing-open branch
  unrepaired; (F3) the BARS walk never parent-fsynced; (F6) census
  out_root fsync gated `if fresh_publication` (recovery skips the
  repair) and a freshly created out_root's entry in ITS parent is
  never committed; (F7) amendment publication has zero directory
  fsyncs before the packet attests. **Remediated in R15 as ONE class
  mechanism each:** `custody.check_committed_extent` (three branches;
  the LARGER branch PROVES the pinned extent as the prefix —
  record-boundary check at the extent edge + replay of ONLY the
  pinned bytes + tail equality, fail-closed) adopted by the seal
  (anchor AND companion, cross-checked: `5418466`/`6b205da`) and the
  journal (`00fcafa`); `NameBinding` format 2 carries
  extent_size/committed_tail_sha256 with identity-conditional
  advance after each append's fsync + name check; durable traversals
  (parent fsync on BOTH branches, restart-closed repair-on-open) on
  the seal/bars/runstate walks (`bfcf54f`/`56864a0`/`00fcafa`) and
  every attesting publication path commits the created hierarchy +
  out_root unconditionally (census `80246cd`, amendment `44d736d`).
  Each red-first (agent logs `/tmp/r15a-f{1,2,3,4}-red.log`,
  `/tmp/r15b-f{6,7}-red.log`, `/tmp/r15c-f5-red.log`). Disclosed
  deviations: NAME_BINDING_FORMAT 1→2 (no durable pre-R15 companion
  exists); tail-damage tolerance rescoped — a damaged line INSIDE
  the committed extent is an in-place-rewrite refusal, tolerance
  remains only for damage beyond the extent (seal + journal tests
  reworked to the landed semantics, within-extent refusal pinned as
  new tests); two owning tests updated to the format-2 record shape;
  the seal/bars/journal clone-swap tests re-armed by directory
  identity (durable walks add earlier fsyncs, count-based arming no
  longer identified the window); the BARS durable walk kept inline
  in its local loop (its own error family + absent-root read
  semantics); lease/heartbeat walks not converted (operate inside
  the already-durable store; reviewer had already classified their
  creation fsyncs as covered). Orchestrator verification: exact file
  scope per agent (A custody/ledger/bars, B census/amendment, C
  journal/store); protected blobs + mutate.py byte-identical; AST
  0/255 drift across ALL 255 anchors in 63 files; independent full
  suite 1,642 passed exit 0 (+17 tests: 7 A + 5 B + 5 C).

- **2026-08-25 R16 (round-14 remediation, owner ruling: apply the
  landed repair-on-open principle to the two PUBLICATION walks).**
  Round 14 at `1cebde0` (pr-a-codex14.log): NO-GO — 2 findings
  (1×P2, 1×P3), both verified in source; round-13 disposition 5/7
  RESOLVED (the whole extent family + authority-walk durability —
  the class ruling WORKED; trajectory 5→4→6→10→10→3→5→7→2); all
  R15 deviations ACCEPTED incl. the M185 re-seam; all constraints
  PASS. The 2: census publication attests over uncommitted outer
  crash-residue ancestors (freshness decided by digest-dir
  existence; ancestor commits only in the fresh branch; recovery
  fsyncs out_root alone) and amendment returns packets over the
  same (absent-only ancestor snapshot → empty on pre-existing
  residue). **Remediated in R16 as one contract** (mirroring
  custody.open_directory(durable=True)): `_commit_output_chain` —
  a durable no-follow walk of the out_root chain that fsyncs the
  PARENT of every traversed component, created-now or
  pre-existing, outermost-first, as the last filesystem act
  before any attestation — census `08b516b` (the `if emitted:`
  commit; exit-4 refusal, nothing attests), amendment `dc2e838`
  (pre-packet-return, OutputRefusedError family). Red-first
  (/tmp/r16-f{1,2}-red.log): roll-forward + exit-5 retries over
  pre-existing residue, and the amendment retry seeding out_root
  ITSELF uncommitted — each tracing every residue parent's fsync
  (st_dev/st_ino) against the attestation event; red assertions
  "the pre-existing outer ancestor's entry in ITS parent is never
  committed" / "the parent output root's entry in the artifacts
  root is never committed before the packet attests". Disclosed:
  the R15 creation-time commits stay (subsumed by the full-chain
  pass); the F1 red log was recaptured after a harness-fixture
  TypeError (final log shows the intended failures). Orchestrator
  verification: exact file scope; protected blobs + mutate.py
  byte-identical; AST 0/255 drift; independent full suite 1,645
  passed exit 0 (+3 tests).

- **2026-08-26 R17 (round-15 remediation, owner ruling: R17 micro-fix).**
  Round 15 at `3127fb0` (pr-a-codex15.log, 265k tokens): NO-GO —
  EXACTLY ONE finding (1×P2), verified in source by the orchestrator
  1/1; findings trajectory 5→4→6→10→10→3→5→7→2→1; round-14
  disposition BOTH RESOLVED; all constraints PASS; both R16 deviations
  accepted (the subsumed creation-time commits + the red-log
  recapture); boundary-consistency all CLOSED except one row. The
  finding: census IDEMPOTENT recovery can attest uncommitted
  member-name entries — the digest-dir fsync lives ONLY in the emitter
  (`_emit_census_set`: renames then `os.fsync(out_fd)`); a crash
  after the last rename but before that fsync leaves a complete
  byte-identical set with no stale temps; the retry classifies
  IDEMPOTENT (:997; the :994 fsync is stale-temps-only) and
  `emitted = publish is None` (:1652) SKIPS the emitter; the R16
  `_commit_output_chain` commits the digest dir's ENTRY in out_root
  but not the three entries INSIDE it → attest exit 0/5 → a later
  reboot may lose the acknowledged member names. **Remediated in
  `5f501b2`**: `_commit_digest_directory_entries(out_dir)` — opens
  the digest directory `O_RDONLY|O_DIRECTORY|O_NOFOLLOW` and fsyncs
  its fd — called in the `if emitted:` block AFTER
  `_commit_output_chain` (outermost-first per the file's own
  parent-before-children convention; the new fsync is the final
  filesystem act before the summary), own `except OSError → EMISSION
  REFUSED / exit 4`, nothing attests; the emitter's own post-rename
  fsync untouched (fresh/roll-forward keep their earlier durability).
  Red-first (/tmp/r17-f1-red.log): the reviewer's exact probe — all
  three members seeded byte-identical + file-fsynced, the digest dir
  deliberately never fsynced; both tests failed on the unfixed tree
  on the intended assertion ("the retry attests the byte-identical
  prior publication without ever fsyncing the digest directory
  itself"). 2 new tests (exit-0 + exit-5 idempotent retries), fsync
  matched by real (st_dev, st_ino) against the first summary print
  via the R15/R16 ordered-event harness. Deviations disclosed:
  `held_dir_identity` is None on the idempotent path so the helper
  re-opens by path (as the fix spec directs). Orchestrator
  verification: exact file scope; protected blobs + mutate.py +
  amendment.py (RESOLVED surface) byte-identical; AST 0/255 drift;
  independent full suite 1,647 passed exit 0 (+2 tests).
