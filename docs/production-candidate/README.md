# TREX desk-wave3-safety-rc1

**Release status: shadow-only integration candidate. The full canonical host gate and independent review are still required. This is not a completed E5/E6 autonomous trading runtime.**

The source is based on the supplied `tree_options-main.zip`, corresponding to upstream `ab493d7b0758ebb04611d7b5f842a47049b2e77f`. It incorporates the plans, constraints and failure reports from all three supplied Claude Code transcripts. The four reported Wave 3 commits were not retrievable through the connected GitHub API; this candidate independently implements reviewed portions rather than claiming a byte-for-byte branch merge.

Start with `INTEGRATION_REVIEW.md` for findings and the remaining execution work. `verification/VERIFICATION.json` records the actual tests, environment differences and unchecked gates. `capabilities.json` describes the new commands' effects for agent callers.

## What runs here

The new desk commands consume the existing sealed `trex.deal/1` queue, write a separate transactional evidence database, expose descriptive end-of-day shadow outcomes, and record **blocked admission previews**. The new desk path does not submit/cancel orders, write executable spec files, modify a broker book, activate the selection rule, notify external services, or re-score research.

**This is not a global kill switch for TREX. Existing legacy execution commands remain separate and capable of broker activity when explicitly run in their configured environment.** The candidate includes narrow, regression-tested legacy accounting changes; merely displaying this evidence page does not disable a running legacy monitor.

| Surface | Delivered status |
|---|---|
| Queue consumer | Strict IDs, dates, policy hashes, Decimal money, quantity, leg and maximum-loss consistency |
| D7 evidence | Transactional candidate retention, deduplicated episodes, exact-session marks, censoring, audit verification and backups |
| D7 scorecards | Descriptive deadline-EOD proxy outcomes; assumed commissions separated; no promotion authority |
| E6 desk-enter | Shadow admission preview only; `--armed` explicitly refused |
| Legacy accounting | Same-quantity price revisions on still-tracked entry and exit orders |
| Assignment-risk engine | Pure optional heuristic with tests; live inputs not wired |
| D9 cockpit | Read-only `/desk/evidence`, `/api/desk/health`, `/api/desk/scorecards` on the existing application |
| E5, D8, DESK-BT-001 | Not implemented by this candidate |

## Isolated checkout and dependency gate

Do not extract over the live checkout, switch its branch, share its writable virtual environment, or install this package into the live monitor environment. Use a separate checkout/release directory and a separate `.venv`.

For a worktree based on the stated upstream commit, review and apply the accompanying patch there. If the four missing branches become available, compare their changes first; do not stack them blindly on this independently implemented candidate. The source archive has no upstream Git history; the patch is the preferred integration artifact for a real repository.

From the isolated candidate checkout, with working package/Python download access:

```bash
# This deliberately installs ONLY into the isolated candidate's own environment.
# Do not point UV_PROJECT_ENVIRONMENT at the live/shared .venv.
unset UV_PROJECT_ENVIRONMENT
uv sync --locked --group dev --group trex-web
./scripts/desk_safety_gate.sh
```

The gate checks the import root, the repository's `.python-version`, and critical dependency versions against `uv.lock`, then runs ruff, mypy and the full pytest suite. It never installs/syncs dependencies, starts services or places orders itself. Keep the existing local-gates authority; this release adds no hosted GitHub Actions workflow.

In the authoring environment the canonical installation failed on a Python-download DNS error. The available interpreter was Python 3.13.5, not the requested 3.12, and several installed dependency versions differed from the lock. Hypothesis, ruff and mypy were unavailable. The successful focused test command below therefore **does not substitute for this gate**:

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 PYTHONPATH=src \
python -m pytest tests/desk_safety \
  tests/unit/test_trex_engine.py tests/unit/test_trex_desk_engine.py \
  tests/unit/test_trex_enter.py tests/unit/test_trex_monitor.py \
  tests/unit/test_desk_enforcement.py tests/unit/test_desk_playbook.py \
  tests/unit/test_desk_selection.py tests/unit/test_desk_miner.py \
  tests/unit/test_trex_web*.py \
  --confcutdir=tests/desk_safety -o addopts='' --maxfail=1 -q
```

`--confcutdir` avoids the unavailable root Hypothesis fixture layer only for this focused run. Do not carry that flag into the canonical release gate. Full-suite/lint/type-check results are unverified, not waived.

## Local evidence smoke, without broker access

After the host gate and review, point the candidate at read-only copies of the actual queue and chain store for the initial smoke. The existing research `DESK_PAPER_DIR` is **not** an execution directory. The new preview reads stop files from `TREX_DESK_RUN_DIR` or an explicit `--run-dir`.

```bash
export PYTHONPATH="$PWD/src"
export TREX_DESK_STATE="$HOME/.local/state/trex-desk-candidate"
export TREX_DESK_QUEUE="/absolute/path/to/copied-queue"
export DESK_STORE="/absolute/path/to/copied-desk-store"

# Dry run evaluates an in-memory copy; it does not publish evidence.
.venv/bin/python -m tree_options.desk shadows --dry-run

# Local evidence only. Missing inputs may return exit 3 with a JSON explanation.
.venv/bin/python -m tree_options.desk shadows
.venv/bin/python -m tree_options.desk verify-evidence
.venv/bin/python -m tree_options.desk scorecards
.venv/bin/python -m tree_options.desk desk-health
.venv/bin/python -m tree_options.desk desk-enter --dry-run
```

These commands use an aware clock and the existing session calendar. Admission previews operate only during a valid session's 09:50–11:30 ET entry window, using its correctly dated preceding-session queue. Outside that interval the preview reports `outside_entry_window`; it does not fake an admission. Supplying `--session` cannot launder an expired queue into a current entry.

Exit 0 means the inspection/preview completed, **not** that trading is allowed. Exit 3 indicates pending/missing input or degraded readiness; inspect the JSON. Exit 1 is a contract, custody, database, filesystem or unsupported-arming error. Health explicitly reports `execution_enabled: false` for the new desk path in every state.

## Storage, provenance and recovery

Default active database: `<TREX_DESK_STATE>/evidence/desk.sqlite3`. Use a local filesystem, not NFS/SMB. Existing `book.json`, original queues, chain files, research registries, protocol files and legacy D7 JSON directories are not overwritten.

The store uses SQLite WAL, full synchronous commits, an object table with unique content keys and a linked audit table. A replay is idempotent; the same key with different content is a conflict. Object publication and its event commit together. Whole-database snapshots use SQLite's backup API, verification before publication, an exclusive destination name and filesystem synchronization.

```bash
.venv/bin/python -m tree_options.desk backup-evidence \
  --out /absolute/private/backup/desk-unique-snapshot.sqlite3
.venv/bin/python -m tree_options.desk verify-evidence \
  --database /absolute/private/backup/desk-unique-snapshot.sqlite3
```

Never copy only the active main SQLite file while ignoring WAL state. Keep snapshots and audit-head receipts off-host. A local hash chain does not authenticate market data, stop a privileged database rewrite, or detect restoration of an old, internally consistent snapshot without an external anchor.

Queues are ingested in ascending order. Discovering a previously absent queue older than already-ingested queues requires a **new evidence store**, not silently replacing a weekly representative after seeing outcomes. Preserve both stores and explain the backfill. Existing early draft `desk-shadow/1` JSON is not auto-imported: reconstruct from original queues and original chain vintages, keeping that reconstruction labeled.

Recorded episodes carry queue/policy hashes and registration timing. Quotes received after an explicit historical observation cutoff cannot leak into that view. A historical reconstruction is nevertheless not proof that an operator possessed the data before trading; registration/capture provenance remains visible.

Deadline outcomes require the deadline session's valid chain and all required legs. An earlier quote is not a substitute. Missing/conflicted deadline data is censored and excluded from resolved P&L; open and censored denominators remain visible. Later recovery of a valid exact-date observation can resolve a censored case on a later evaluation without rewriting prior events.

## Meaning of the numbers

The modeled entry is the miner's hypothetical fill. The exit is an adverse-side **deadline-EOD proxy**, not a simulation of the complete intraday stop/touch/breach/assignment policy and not an observed broker execution. Gross P&L, assumed round-trip commissions and modeled net P&L are separate. The current commission assumption is inherited from the existing rails, not a newly verified brokerage fee schedule; other fees, slippage and execution constraints may be missing.

Families are separated by row, tier and both policy hashes. One episode per underlying/family/week reduces repeated counting, while all candidate records are retained. The 20-resolved count is displayed as a sample floor only. This release never promotes, pauses or retires a strategy, and does not claim a significance test from that count.

## Cockpit and agent integration

When the existing FastAPI cockpit is run from this candidate, open `/desk/evidence`. It reads the configured desk-state evidence database through new GET-only routes. The existing React cockpit/navigation is not replaced. The page has no trading controls, uses text-only DOM insertion, and returns a restrictive content-security policy.

Keep the cockpit behind the existing private/loopback access perimeter. This change does not introduce a new remote authentication scheme and is not approval to expose portfolio/research data publicly. Broker connection health is explicitly unverified by these endpoints.

An optional visual smoke can be reproduced with Playwright and Chromium already installed:

```bash
PYTHONPATH=src:. python tests/desk_safety/browser_smoke.py --out /tmp/trex-preview
```

It renders actual page HTML against synthetic fixture responses at desktop/mobile widths. It does not exercise browser HTTP navigation or CSP enforcement; those remain host checks. TestClient tests cover route behavior separately.

## Deployment templates: review only until host verification

`deploy/desk-evidence/` contains new names rather than reusing the unverified lane's `desk-shadow` units. They pin source and interpreter to `%h/.local/share/trex/releases/desk-wave3-safety-rc1`, set `HOME`, `UV_NO_SYNC` and `PYTHONPATH`, and restrict write paths/network address families. Resource limits are proposed conservative bounds, **not measured production sizing**.

Before installation, prepare that isolated release and its locked `.venv`, create the evidence directory with private ownership, verify the real store/state paths, and test user-systemd namespace support. Run `systemd-analyze verify` on the host and one manual evidence cycle; confirm journal exit codes and JSON readiness, not just a green oneshot. The authoring run validated timer calendar syntax only. No unit has been installed or enabled by this work.

A same-name service already present on the host must be compared before replacement. Do not stop the existing position-guarding monitor as part of installing evidence timers. Deployment/restart of the narrow legacy accounting fix is a separate operator action requiring a broker/book reconciliation plan.

## Rollback

Stop only newly installed evidence/preview timers and allow their oneshot to finish. Preserve the evidence database and a verified snapshot; do not delete observations to make a gate green. Revert the isolated candidate checkout or restore its previous immutable release. Do not alter the live legacy book or sealed research records. Reverting legacy monitor code with open positions requires its own supervised reconciliation; this read-only evidence rollback is not a broker rollback procedure.
