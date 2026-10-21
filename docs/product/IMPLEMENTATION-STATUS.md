# TREX private product implementation

The owner-approved product plan is the controlling scope: IBKR options and
whole-share, long-only US equities in paper first; separate live qualification;
per-entry human approval; one existing supervised execution owner. Product
reliability never qualifies a strategy. Preserve the 29 virtual allocations,
closed campaigns, registrations, and the forward campaign's 60-session rule.

## Phase 0: baseline and custody

Baseline recorded 2026-10-14 in the private host directory
`/home/alexk/.local/state/trex-product-baseline-20261014`. It contains source/tree
identity, worktree inventory, service identity and sandbox properties, existing
canary instructions, evidence backups and their audit receipts, a research SQLite
backup and integrity result, supervised state copied under its existing lock,
and SHA-256 receipts. Private account and execution records remain outside Git.

At intake the clean main checkout was
`c0010040ac01bf1def9cded0cf049a91ef492604`, tree
`39186f5ca9841b5eb35f8f38c54880aab02d553d`. Development is isolated on
`feat/trex-product-foundation-20261014`. The web and premarket source is the pinned
`0ec5acb99856589559fdb0b0f7164ac3dc9bb092` release. The desk and discovery services
use main, not the web release; a working-directory identity is not an attestation
of every module loaded in a running process.

The existing desk process owns both the runtime and account locks. The current
canary is one open SPY November 20 760/758 put spread with recorded package entry
price 0.47. Its configured exit deadline is October 15. Read-only observations
showed a fresh connected heartbeat and corresponding broker leg updates. The
existing owner's scheduled recovery check on October 14 evening and exit checks
on October 15 remain pending. Do not interrupt, restart, halt, flatten, re-grant,
or replace that owner to install this candidate.

Desk evidence backup: 174 objects/events, audit verified. QSL evidence backup:
255 objects/events, audit verified. Research database integrity check: `ok`.
These are database backup checks, not a complete runtime restoration rehearsal.
The book/events/spec/request copies are explicitly observation copies: the owner
holds its lifetime lock, and no consistent multi-file runtime backup hook was
used. Historical permits and mandates in the archival copy MUST NOT be restored
as current authority. Broker reconciliation and a fresh owner epoch are required
before any resumed execution.

There were three claimed request/result pairs at intake, including the canary,
and no pending/sending outbox file was observed. This is local journal inventory;
it does not substitute for reconciliation with all broker orders.

### Unit and release inventory (2026-10-21, read-only)

`scripts/trex_inventory.py` maps every `trex-*`/`desk-*` user unit to the code it
runs; see [INVENTORY.md](INVENTORY.md). In the 2026-10-21 run, 22 of 28 services
ran from the serving checkout with `PYTHONPATH=src`. `trex-web`,
`trex-premarket` and staging ran release `0ec5acb`, and `desk-qsl` ran
`cc91b783`. `desk-evidence` and `desk-admission-preview` ran an untracked copy
(`desk-wave3-safety-rc2`) that has no recorded identity. The run also found 5
unreferenced release directories and 24 local branches with no upstream that are
not merged into `origin/main`. This is an observation, not a custody receipt. The
run did not check what a running process actually imported.

## Long-run artifact reconciliation

Both artifacts name run `20260929T094303Z`. Both retain the partial/untrusted,
never-promoted verdict. The served digest is the original historical artifact;
it must not be represented as the later corrected accounting.

| Artifact | SHA-256 | Meaning |
| --- | --- | --- |
| `artifacts/desk-store/evaluations/longrun/20260929T094303Z/digest.json` | `0570b72e91574bb841c3ff7b3dde694dd971c985385415a6033130c74958312b` | Served original; no provenance block |
| `~/.local/state/trex-longrun/v3-final-accounting-20261001/digest.json` | `248a63f98bc9ec23919a7cc65e0b3017dc71cff914ba9e7e67222694028df4da` | Later diagnostic re-score with provenance; protocol changed after observation |

The corrected artifact records code version `6bf369e`, outcome table hash
`6b0e95bea7c2340c2f8944195a0af8c2385abe59e3de79393a20d7d98368a0f8`,
and boards fingerprint
`3aa59f9a159040522f6b09f21bdfc0c5c17d39cc752e642fe40f1dcd2e9c9f90`.
Its accompanying report describes 1,912/1,913 boards, 240 historical sessions,
27 arms and no new model calls. Those descriptions were inspected, not recomputed
in this increment. Artifact identities were hashed locally. Neither artifact
establishes forward sample count, broker returns, or confirmatory profitability.
The experiment index must show the supersession relationship while preserving
both originals; this increment does not replace either artifact.

## Isolated Phase 1 repairs

- A shared broker-free `DeskSpec` contract lets exposure readers validate both
  bare legacy `LegStructure` and the entire versioned owner envelope. Unknown
  versions and malformed owner fields remain visible uncertainty.
- `python -m tree_options.desk qsl` is restored with session, store, dry-run and
  output arguments. Missing inputs retain exit 3; they cannot become success.
- `publish-evidence` builds health and scorecards from the same verified SQLite
  backup, atomically publishes a bounded JSON file, and records the audit and
  release. Web snapshot mode reads only that JSON. Missing, stale, future,
  malformed or corrupted snapshots return specific 503 blockers without a DB
  fallback. Default direct-read compatibility remains until snapshot deployment.
- Premarket code and unit templates are version controlled. Failed QSL checks
  produce `RESEARCH_INPUTS_BLOCKED` and exit 3 even if the web process responds.
  The runner never silently restarts a different web release.
- Release tooling binds source/head/tree, Python interpreter/packages, dependency
  locks, supported commands, and every frontend asset. Mutable state remains
  outside the release. The tooling prepares manifests and unit files; activation
  and rollback remain owner-coordinated deployment steps.
- Rendering now covers every unit that ran outside the serving checkout. There
  are drop-ins for `trex-web`, `trex-web-staging`, `desk-qsl`, `desk-evidence`
  and `desk-admission-preview`, plus the premarket unit. Each is pinned to one
  release and gated on that release's identity check, and each resets the
  inherited command lists. Render writes nothing unless every template passes,
  and it refuses commands the manifest does not list. Sandboxed templates no
  longer launch through `host-work`, which could not write its payload under
  `ProtectHome=read-only`. The staging drop-in pins code only and keeps
  staging's own state. None of this is installed. The ~22 units that run from
  the serving checkout have no release template yet.

At intake, real HTTP health and scorecards returned 200. The earlier reported
HTTP failure was not reproduced; snapshot work removes the filesystem dependency
without claiming that an observed outage was fixed on the installed service.

## Remaining gates

Phase 0 is OPEN: canary recovery/exit, complete owner-coordinated backup and
restore rehearsal, and complete experiment/source inventory remain outstanding.
The read-only unit/release inventory exists, but it does not close custody. Still
open: retiring the untracked `desk-wave3-safety-rc2` copy, deciding on the 5
unreferenced release directories, and triaging the 24 branches that have no
upstream.
Phase 1 is an isolated candidate, not an installed release. No unit has been
switched to a rendered drop-in.

Phase 2 now has an isolated experiment index, detail API and React destination;
see [EXPERIMENTS.md](EXPERIMENTS.md). Its nine-source publication preserves
replay/forward distinctions, closed verdicts, long-run accounting lineage and
virtual allocations. The equity pilot remains blocked on qualified inputs,
frozen registration and enforced resource budgets. This is not the Phase 2 exit gate.

Phases 3-6 remain OPEN: five-page
React workflows and onboarding; Family Apps signed command authorization;
durable per-trade browser approvals and entry pause; IBKR equity custody,
execution and cash accounting; ten consecutive NYSE paper sessions with ten
completed lifecycles per asset; authenticated phone/desktop and unassisted user
acceptance; independent loss-of-host alerts; separately configured live limits,
account qualification and explicit live activation. No live canary is authorized
by this implementation. Do not infer intended live capital from virtual funds.
