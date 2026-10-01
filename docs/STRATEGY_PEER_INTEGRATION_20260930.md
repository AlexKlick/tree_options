# Peer strategy integration — 2026-09-30

The integrated strategies remain **NOT_PROMOTABLE** research. QSL produces modeled
shadow fills, and VIXfloor consumes historical outcome proxies. Neither module
confers execution authority, exact external fill economics, or live-money permission.
No broker call, operational grant, service installation, timer activation or large
corpus evaluation was performed during this integration.

## Source reconciliation

Integration worktree: `research-paper-workspace-20260930`, starting at
`95b618b1949db211e464e056e61958712d70a735`. Peer branches were based on older
`b70bf2f`; their source and tests were inspected through `git show` and adapted into
this current worktree without merging/resetting their branches.

- `feat/strategy-vixfloor@1672e0c`: `desk/vixfloor.py` and unit tests.
- `feat/strategy-qsl@0941b85`: `desk/qsl.py`, safety tests, additive desk CLI and sealed
  QSL preregistration. Current CLI commands and existing longrun interfaces remain owners.
- The earlier candidate incorrectly said `b757009` earnings-after-next was an
  ancestor of its base. It was absent from that candidate. The full repository
  integration merges main at `b757009` with candidate `cbf8911`, preserving the
  actual earnings pair API, surface/refusal exposure, ideas and draft rendering,
  and its tests. The peer INDEX statement that it was unmerged is historical.
- Recorder-integrity and account-truth changes are preserved. No sealed playbook,
  execution allowlist, existing live runtime, or its mandate was changed.

`docs/desk/QSL-PREREGISTRATION.md` and its sidecar retain the peer bytes and SHA-256
`390606d99a3169b085d77e58ed5c02d7529da24c36969ce01b60b62bdef6abc3`.
Its original language about crossing economics describes model assumptions, not
independently observed broker fills. The implementation's evidence classification
and this reconciliation explicitly retain that distinction.

## Historical evidence and negative outcomes

The supplied artifact root is `/home/alexk/.local/state/trex-strategy-20260930`.
Hashes below identify the files inspected now. They do not establish that original
run inputs had these bytes: the original result JSON identifies paths, seeds and
counts but lacks the new exact-input hash binding.

| Artifact | SHA-256 at inspection |
|---|---|
| `INDEX.md` | `eaac7e8b221eb8798e9e951e7df72f92611f196c27f61d68df6d3579bc580a7f` |
| `data-map.md` | `cfd81d5d7d88ec5c4647f4cfdd334e856e01a938bf6034296532a298f23b26ee` |
| `mining-report.md` | `f641b71068f9e9f1977b10de3c5689b7d6f5f147a153c7ea07525dc2c5509587` |
| `strategy-specs.md` | `1ecda303fc41d45fa854a8543071782ba92f78c97154908958e9f2689b8ea504` |
| `qsl-setup.md` | `533a4525115b5221fbe8491f798971e286a4db99d6253ddd526f427684c51790` |
| `vixfloor-results.md` | `8b15df213da12d107e0caf988674ad8a05e1e27805bb44d7b9e96f670b3cf480` |
| `vixfloor-results.json` | `0bcd1a25f7d2b12043a19456018fadee29228412eb788eb3b2133b14d113be0a` |
| `vixfloor-results-long.json` | `921e05e9697554a7fac7fb05321cb6d1f6fdb891e9836d985ef4dad24ef035d8` |

Primary VIXfloor corpus: 688 boards / 86 sessions, 2026-05-26 through 2026-09-25.
Its low/high arms have 176/100 evaluable entries against preregistered floors of
300/120. Both are **NOT_EVALUABLE**; nominal inherited survivor performance cannot
satisfy missing sample floors. The secondary low-minus-high contrast is $4.03.

The sibling feasibility corpus has 1,913 boards / 240 sessions, 2025-09-29 through
2026-09-25. Its 310/516 evaluable entries meet floors, but the low arm's half-split
changes sign. Low-arm mean changes from $84.24 on the primary to $33.14 on the sibling;
high-arm sibling mean is $77.20. Low-minus-high is -$44.06. The sibling JSON records
**one-sided** permutation `share_ge=0.9455`; `share_abs_ge=0.112` is a distinct
absolute-tail statistic. The INDEX's undifferentiated “share 0.112” must not become
a one-sided confirmation claim. Overall verdict remains **NOT_PROMOTABLE**.

The peer's stratified bootstrap collapsed repeated sampled sessions into boolean
membership and thus dropped bootstrap multiplicity. The hand oracle `[0,0,1]`
exposes -9 for the proper resample versus -11 for the collapsed sample. The corrected
source uses integer index arrays and labels statistics
`session-bootstrap-multiplicity/2`. Historical bootstrap intervals are superseded;
a separately bound rerun is required for corrected intervals. Neither supplied JSON
was overwritten or recomputed here. Other historical numbers above remain attributed
to the peer reports, not current-head evaluation receipts.

Peer QSL reports claim 30 gate passes, 22 adopted scratch episodes, 48 marks and four
censored observations over five recorder sessions. These are historical modeled
shadow-lane results, not externally executed fills. The reported 5,090 passed / 36
skipped suite cannot be independently recovered from the retained quiet progress log;
it is kept as a peer claim, separate from the current exact-count receipts below.

## Current correctness and custody boundaries

VIXfloor now reuses the current v2 outcome plugin and rejects conflicting duplicate
outcome identities. Its outcome cache is board-bound. Board IDs are unique; VIX
closes must be finite and positive, and contradictory same-date closes are refused.
`inputs.source_hashes` hashes the actual bytes parsed for boards, VIX and the outcome
table. `cost_semantics_verified=false` retains the reported flat $14.60 round-trip
cost as a declared, unverified table assumption. The prior-session VIX close excludes
future session dates; historical publication/revision availability remains unqualified.

QSL still uses the existing surface, desk queue grammar and shadow ledger. It records
`evidence_kind="SIMULATED EXECUTION"` with `execution_authorized=false`,
`exact_external_economics=false`, and `live_money=false`. Its deterministic crossing
price is a simulated entry assumption; EOD marks do not supply stable external fill
identities, actual execution times, or authoritative commissions.

Chain headers must bind the expected symbol/session, aware event and fetched timestamps,
and a valid raw-source hash. Both `event_at <= available_at` and
`available_at <= next-session 09:30 ET` are required. Runtime observation time is a
second upper bound: a future fetch cannot be used before it is observed. Invalid or
unavailable chains produce explicit exclusions. Rows retain event/availability times
and `chain_document_sha256` for the complete parsed document, independently of the
header's declared raw-source hash. For example, the actual 09-22 SPY document's
09-23 16:38 ET fetch was unavailable for the declared 09-23 09:30 ET decision and is
now excluded. Old five-session acceptance counts therefore do not qualify this source.
No retrospective rerun was used to invent replacement counts.

The peer's warm-up/clock dates are projections: five measured sessions, assumed uptime,
future qualified arrivals, sample floors and sealed HAR requirements all matter.
The estimated ~2027-02-02 promotion date and ~2026-12-03 interim date are neither a
clock start nor a readiness receipt. New availability exclusions can reduce accrual.
Current strict HAR gates and unknown after-next events remain intact. SPEC 4 event
study and SPEC 5 diagnostic are still unbuilt, apart from gates explicitly carried by QSL.

## Runtime handoff and distinct order budgets

QSL defaults resolve from `desk.paths.state_root()` to `queue-qsl` and
`evidence/qsl.sqlite3`. Overrides are `TREX_DESK_QSL_QUEUE`, `TREX_DESK_QSL_DB`,
`TREX_DESK_STATE`, and `DESK_STORE`. Peer result JSON uses schema
`desk-vixfloor-eval/1` with `inputs`, `arms`, `family`, `secondary_contrast` and
`digests`; QSL queue JSON includes a top `qsl` block and `admissible` rows with their
own `qsl` proof blocks. Durable projections must preserve these classifications.

Both producers now attach `engine`, containing actual module-byte SHA-256 mappings,
the existing `longrun_engine_identity()` fingerprint and a canonical manifest digest.
VIXfloor binds its evaluator source plus the shared scorer; QSL additionally binds
surface/IV/Greek calculations, rate parsing, queue/session helpers and TREX plan types.
The shared scorer binds its shipped helpers and Python/NumPy versions. The static
`CODE` label is a module name, not the source digest. This is custody of deployed source
under the immutable-code assumption, not inferred closure or resident-bytecode identity.
Previously retained historical JSON does not acquire these fields retroactively.

DTB3 selection now inspects the latest eligible observation even when its close is empty.
Malformed/nonfinite rates and annual decimal rates outside the broad input-sanity range
[-1, 1] explicitly return not-ready; a bad latest observation cannot fall back to a valid
older rate, zero, or a fabricated replacement. The original one-session lag remains.

`deploy/desk/desk-qsl.service` and `.timer` are inactive repository templates derived
from the documented peer handoff and current desk service conventions, not copied
from untracked host units. They isolate modeled shadow adoption into the QSL database,
pin explicit roots, and schedule 19:15/10:30 ET. Before installation, an operator must
select the deployed checkout and state root, ensure it contains this command, and
review the forward sampling policy. No template was installed or enabled here.

Historical `drill-check` verdicts are not current operational proof. The live desk
root in the pasted transcript was `~/.local/state/trex/desk-paper`; the default CLI
root differed. Ambient agent HOME also redirected watcher defaults. This integration
performed no live probe; operator diagnostics must pass explicit roots and inspect
which roots the CLI actually reads.

Source-grounded budgets are separate:

- IBKR options `_grant_hint()` uses the existing `daily_grant` policy. Its cap of three
  is not a universal literal mandate. `supervised.issue_permit()` consumes the budget
  on authorized ENTRY intents. Protective exits are owned by `desk_runtime`; an expired
  mandate does not block them. Entry repricing is refused. Thus “three = entry + exit +
  reprice” is not the current domain's meaning. G4 now refuses an exhausted entry budget
  rather than reporting GO with zero remaining. This gate writes no HALT and does not
  intervene in exit ownership.
- SnapTrade's `broker_paper` mandate independently requires exactly one order,
  `operational-canary/1`, at most $100 gross notional, and at most 900 seconds TTL.
  These controls and explicit operator authorization were not altered or exercised.

## Local validation receipts

All test commands ran through `host-test` with full captures. These establish fixture
and repository-source behavior only, not provider, live runtime, deployment, corpus
re-evaluation or promotion readiness.

| Receipt | Exact result |
|---|---|
| `/tmp/trex-peer-strategies-baseline.log` | 36 passed |
| `/tmp/trex-peer-strategies-red.log` | 8 failed, 36 passed (availability, evidence, bootstrap, duplicate identity regressions) |
| `/tmp/trex-peer-drill-budget-red.log` | 2 failed, 38 deselected (initial fixture serialized wrong alias; not the semantic oracle) |
| `/tmp/trex-peer-drill-budget-red-v2.log` | 1 failed, 1 passed, 38 deselected (actual exhausted-budget false GO) |
| `/tmp/trex-peer-strategies-green-v4.log` | 171 passed, 0 failed, 0 skipped, 3.74 seconds |
| `/tmp/trex-peer-strategies-ruff-v4.log` | All checks passed for scoped source/tests |
| `/tmp/trex-peer-strategies-mypy-v4.log` | Success: no issues found in 3 source files |
| `/tmp/trex-peer-custody-red.log` | 9 failed, 50 deselected (invalid rate and missing internal engine custody) |
| `/tmp/trex-peer-custody-green.log` | 180 passed, 0 failed, 0 skipped, 3.41 seconds |
| `/tmp/trex-peer-custody-ruff.log` | All checks passed for scoped source/tests |
| `/tmp/trex-peer-custody-mypy.log` | Success: no issues found in 3 source files |

The final focused suite covered QSL, VIXfloor, longrun, desk enforcement and drill-check.
The parent integration owns full-suite/M0/mutation validation and final commit custody.
