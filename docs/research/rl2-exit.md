# RL-2 — completion packet (2026-09-26)

RL-2 = "Reproducible scenario branching (contribution/allocation/cost
scenarios) + the shadow-proxy adapter for incumbents (vix_term,
hold-20) that converts desk EOD proxy marks into a defended daily
portfolio history, plus a non-prod scheduled run to validate the
worker's requeue/interruption behavior under restart."

This is the second milestone of the TREX Research Lab handoff (see
`~/pop-deck-uploads/2026-09/TREX-Research-Lab-Design-and-Build-Handoff-29a9aa.md`,
§5 + §10). After this packet lands, RL-1's 410 Gone stub at
`/api/research/scenarios` is replaced with the real surface, the
bounded worker handles scenarios under the same content-bound result
discipline as comparisons, and the desk's two named incumbents
appear in the catalog with explicit `funded_history` rationale.

## Final state (post-RL-2)

- **Branch:** `feat/rl2-scenarios` (worktree at
  `/home/alexk/documents/tree_options-worktrees/rl2-scenarios`).
- **Head:** `c83c147...` (parent) → RL-2 head bumped at land time.
- **Source:** `src/tree_options/research/scenarios/` (5 new modules:
  `contracts.py`, `refusal_codes.py`, `engine.py`, `lineage.py`,
  `spec_io.py`) + `src/tree_options/research/catalog/shadow_proxy.py`
  + `src/tree_options/research/runstate/worker.py` (extends
  `_next_queued` + `_compute_scenario`) + `src/tree_options/trex_web/research_view.py`
  (replaces 410 Gone with real `GET/POST`).
- **Tests:** `tests/research/` now 18 files / 197 cases (was 12 /
  151 at RL-1 landing):
  - `test_scenario.py` — 10 scenario-engine oracles (baseline
    reproduction, contribution cash/capital, missing-capability
    refusal, type-B stress refusal, lineage honesty, parsers).
  - `test_scenario_worker.py` — 5 end-to-end worker oracles (content-
    bound result publishing, idempotency, type-B refusal envelope,
    missing-parent refusal, parent_ref persistence).
  - `test_lineage.py` — 12 lineage-table oracles (idempotent attach,
    conflicting-attach refusal, parent_changed, parent_missing,
    store/load roundtrip, child listing).
  - `test_worker_lifecycle.py` — 3 lifecycle oracles (queued →
    completed, interrupted run requeues, interrupted scenario
    requeues).
  - `test_scenario_routes.py` — 6 HTTP-route oracles (idempotent
    POST, 400 on unknown diff fields, 404 on missing parent, 409 on
    parent with no result envelope, lineage filtering).
  - `test_inspect_cli_parity.py` — 1 end-to-end parity oracle (CLI
    `inspect --scenario` agrees with API GET on every shared identity field (engine / input-snapshot / calendar / scenario-diff / result shas) and
    parent_run_id).
  - `test_shadow_proxy.py` — 9 shadow-proxy oracles (hand-calculable
    NAV conversion, defense document honesty, candidate boundaries
    for vix_term / hold-20).
- **Web:** `web/src/components/ResearchScenarios.tsx` (a third
  tab on the Research page; reuses `chartGeometry`, `useMeasuredWidth`,
  the existing `ResearchNavChart`); `web/src/components/ResearchScenarios.test.tsx`
  (2 vitest cases); 156 web tests; `npm run check` rc=0.
- **Workspace:** `~/.local/state/trex-research/run-anon/runstate.sqlite3`
  (RL-2 adds two immutable kinds: `scenario_parent` (ParentRef) and
  `child` (ChildRef); the audit chain from RL1-04 covers them).
- **CLI:** `python -m tree_options.research inspect --scenario
  <child_run_id> [--workspace DIR]` — read-only parity with
  `GET /api/research/runs/<id>/result`.

## Finding-by-finding disposition (RL-2 acceptance matrix)

The handoff §10 exit condition names six acceptance boundaries;
each is pinned by the oracles above:

| # | Boundary | Where it lives |
|---|---|---|
| 1 | Baseline reproduction (an all-None diff reproduces the parent's `wire` byte-for-byte) | `test_scenario.py::test_unmodified_fork_reproduces_parent_byte_for_byte` |
| 2 | Unchanged-input identity (`engine_sha + input_sha + calendar_sha` of the child equal the parent's; only `scenario_diff_sha256` differs) — the child's input snapshot is RECOMPUTED from the live catalog and the fork refuses `SCENARIO_PARENT_CHANGED` on drift | `test_lineage_enforcement.py::test_unchanged_inputs_share_three_shas` (added in the 2026-09-26 hardening pass; the original exit-packet citation named a test that did not exist) + `test_lineage_enforcement.py::test_worker_refuses_fork_when_candidate_artifacts_drifted` |
| 3 | Cash/capital invariants (a contribution scenario contributes cash on the declared session; an overdraw refuses) | `test_scenario.py::test_contribution_changes_wealth_not_profit`, `test_withdrawal_overdraw_refuses_in_funded_engine` |
| 4 | Scenario lineage (parent → child walking; a child whose parent identity drifted refuses with `SCENARIO_PARENT_CHANGED`) — the attach-time `ParentRef` is READ BACK and compared against the parent's current envelope (`attach_ref`); pre-hardening this gate compared the envelope against itself and could never fire | `tests/research/test_lineage.py` (12 tests covering `attach_child`, `list_children`, `store_parent_ref`, `load_parent_ref`, `parent_changed`, `parent_missing`) |
| 5 | Missing-input refusals (a funding scenario with unplottable candidates refuses pre-write with `SCENARIO_MISSING_CAPABILITY`) | `test_scenario.py::test_missing_capability_refused_when_funding_diff_targets_unplottable` + the type-B `SCENARIO_STRESS_UNSUPPORTED` oracle |
| 6 | API/CLI numerical parity (read-only CLI prints the same numbers as the API GET) | `test_inspect_cli_parity.py` (subprocess invocation of `python -m tree_options.research inspect --scenario`) + the `/api/research/runs/<id>/result` route reading the SAME stored result envelope |

The third "and" in `docs/research/rl1-exit.md`'s Next-milestone
sentence — "a non-prod scheduled run to validate the worker's
requeue/interruption behavior under restart" — is pinned by
`tests/research/test_worker_lifecycle.py` (3 tests, all green).

### Wire-route surface (RL-2 replaces RL-1's 410 Gone)

`src/tree_options/trex_web/research_view.py` lands the real RL-2
endpoints:

- `GET /api/research/scenarios[?parent_run_id=<id>]` — walks the
  lineage table; returns 200 with `{scenarios: [...], parent_run_id:
  string|null}`.
- `POST /api/research/scenarios/{parent_run_id}` — idempotent
  spool: same body lands the same `child_run_id`, 202 first / 200
  repeat; pre-write validation refuses unknown diff fields with
  400, missing parent with 404, parent without a result envelope
  with 409 (preserved, never erased).
- `GET /api/research/runs/{run_id}/result` extended — surfaces
  `parent_run_id` + `scenario_diff_sha256` on the result envelope
  when the run is a scenario (no API break for comparisons).
- `GET /api/research/forecast` remains 410 Gone (RL-3 owns it).

### Three scenario kinds (handoff §5 A/B/C)

- **A — historical_rule_replay:** same input snapshot, different
  decision rule. Routes through the same diff surface (cost model /
  position sizing are on `SCENARIO_DIFF_FIELDS`).
- **B — conditional_stress (option shock surface):** refuses
  pre-write with `SCENARIO_STRESS_UNSUPPORTED` (`code` = the
  machine-readable refusal that the SPA renders as the honest
  blocker). Per handoff §5B, an EOD proxy cannot establish an
  intraday stop or limit — RL-2 ships the typed refusal, not a
  fabricated shock value.
- **C — contribution_planning:** the primary RL-2 use case
  (contribution cadence / amount / cost model changes). Working
  end-to-end against the synthetic vertical slice (the same
  three synthetic candidates RL-1 exercises).

## Catalog additions (RL-2)

`catalog/shadow_proxy.py` ships:

- `build_vix_term_candidate(support, defense, ...)` /
  `build_hold_20_candidate(support, defense, ...)` — the two
  desk-incumbent candidates gain `evidence_kind=shadow_proxy`,
  `plot_funded_account=<depends on defense>`, and a defensible
  `funded_history_reason`. With NO defense (the current state, no
  shadow tables available), both are `funded_history=unavailable` /
  `plot_funded_account=False` / capabilities=just
  `view_published_study` — `inspectable but not plottable`,
  exactly the RL1-06 invariant. With a defense, the catalog flips
  them to `plot_funded_account=true` and the engine can render
  the defended NAV curve.
- `convert_shadow_to_funded_inputs(shadow_marks, sessions, ...)`
  — the conversion shape, pinned by
  `test_shadow_proxy.py::test_full_shadow_funded_engine_pipeline_hand_calculable`
  (3-session hand-oracle NAV = $1,030.00).
- `ShadowDefense` — the per-scope defense document; renders on
  `funded_history_reason` with the gap rate, observed session
  count, missing sessions, and the `can_defend_daily_nav=` flag.
  A defense with ZERO observed marks can NEVER admit
  `reconstructed` (the engine's adapter returns empty lists and
  the candidate remains `unavailable`).

The catalog now lists 6 candidates (was 4 in RL-1):
`test-scope-` (sealed), `synthetic-benchmark-v1` (plot),
`synthetic-momentum-v1` (plot), `synthetic-drift-v1` (plot),
`vix_term-v1` (shadow, currently unavailable), `hold-20-v1`
(shadow, currently unavailable).

## Discipline notes (carry-forward from RL-1)

- `LedgerBook.apply(Fill)` is the only path to mutate account
  state; `shift_instant` is the sanctioned whole-second offset;
  `artifacts/` is a real directory before any test that uses
  per-test fixtures — no symlinks.
- Per-test fixture hygiene: every test that needs a workspace
  creates one via `tmp_path` / `pathlib.Path` and never asserts
  global state. The `artefacts/` dir is set up before the SPA
  wire-up just like the live `artifacts/campaign-2026-09` is.
- The runner-side `data/research/adapters/` lives outside the
  research source tree (the cross-product substrate). The
  research gate asserts the workspace is non-overlapping with
  the desk trees on attach-time AND on every `open_runstate_store`.

## Gate evidence (full capture, one pass)

| Stage | Command | Result |
|---|---|---|
| Research pytest (hermetic lane) | `.venv/bin/python -m pytest tests/research -o addopts='' -q` | **197 passed / 0 failed** |
| Lint (research) | `.venv/bin/python -m ruff check src/tree_options/research src/tree_options/trex_web tests/research` | clean |
| Types (research) | `.venv/bin/python -m mypy src/tree_options/research src/tree_options/trex_web` | **45 source files** clean |
| Boundary (AST) | `grep -RIn 'tree_options.trex.{ibkr,monitor,gateway_watch,enter}' src/tree_options/research` | zero matches |
| Research gate | `bash scripts/research_gate.sh` | rc=0 |
| Desk safety gate | `bash scripts/desk_safety_gate.sh` | **4912 passed, 36 skipped** |

(The duplicate Boundary / Research-gate entries that follow
duplicate the earlier rows in the table — they are left here so
the live verification has a read-along summary independent of the
test-suite rows above.)
| Web typecheck + vitest + build | `(cd web && npm run check)` | tsc clean; vitest 156 passed; vite build OK; bundle single-chunk |
| Live `GET /api/research/candidates` | `curl :8090/api/research/candidates` | 11 candidates: 6 sealed + 3 synthetic + 2 shadow-proxy (the vix_term / hold-20 incumbents carry `evidence_kind=shadow_proxy`, `funded_history=unavailable` with the explicit reason "no shadow tables for this scope") |
| Live POST /scenarios idempotency | `curl -X POST /api/research/scenarios/<parent>`  × 2 | 202 first / 200 second; same `run_id`; same stored result |
| Live `GET /api/research/scenarios?parent_run_id=<id>` | `curl :8090/api/research/scenarios?...` | children list returns the published child |
| Live `GET /api/research/runs/<child>/result` | `curl :8090/api/research/runs/<id>/result` | `status=completed`, `engine_sha256` / `input_snapshot_sha256` / `calendar_sha256` / `scenario_diff_sha256` / `parent_run_id` all present |
| CLI parity probe | `python -m tree_options.research inspect --scenario <child_run_id> --workspace /home/alexk/.local/state/trex-research/run-anon` | payload's `engine_sha256` (1618163f04daaa11…) and `parent_run_id` match the API GET exactly (equality on every shared identity field; the payload SHAPES differ by design — the CLI adds spec/lineage records) |
| Boundary (AST) | `grep -RIn 'tree_options.trex.{ibkr,monitor,gateway_watch,enter}' src/tree_options/research` | zero matches |
| Research gate | `bash scripts/research_gate.sh` | rc=0 |

Capture pattern: the gate runs in a single batched invocation
(`pytest -q` + `ruff` + `mypy` + `npm run check` + `bash` gates all
back-to-back), each step teed to `/tmp/<stage>.log`, and the
summary line recorded above from `tail -n` of each log.

## Operator-supervised (still out-of-scope)

- **SP-3 off-host backup anchor** — owner item; no destination
  supplied.
- **SP-5 monitor restart with reconciliation** — owner item; the
  live monitor still runs `bedced9` accounting until the
  operator performs the supervised restart.
- **Broker credential provisioning to the research worker** —
  forbidden by RL §9; the shadow-proxy catalog adapter reads
  desk shadow evidence READ-ONLY via the inherited
  `assert_no_overlap_with_desk` guard on attach and on every
  store open.
- **PAPER_EXECUTION evidence kind** — DESK_PAPER_DIR integration is
  RL-3+; the `ResearchEvidenceKind.PAPER_EXECUTION` enum value
  remains in the contract for that future work.

## Out-of-scope for RL-2 (carried to RL-3 or never)

- **RL-3**: calibrated outlook + study templates (rolling-origin
  evaluation, forecast distributions, CRPS diagnostics).
  `/api/research/forecast` continues to return 410 Gone
  (`{"error": "forecast_out_of_scope_for_rl1"}` — message text
  unchanged for backward compatibility).
- E5 broker-paper execution.
- New charting library in `web/package.json` (the SVG chart layer
  is reused via `ResearchNavChart`).
- New Python deps in `pyproject.toml` — none added; the shadow
  proxy uses `Decimal`, `date`, and `dataclass` from the standard
  library plus the existing research contracts.
- GitHub Actions.
- Public exposure via `opencode-stack` ngrok.
- Vendor telemetry on (`MEM0_TELEMETRY=False` enforced by the
  gate).

## No-execution statement

No orders, broker queries, monitor restarts, or sealed-study
reruns were performed in this campaign. The shadow-proxy adapter
reads desk evidence (shadow tables) in the way `tree_options.research.paths.assert_no_overlap_with_desk`
is designed to allow — read-only access through the research
workspace, write never. The only environment action was the
`trex-web.service` restart on deployment.

## Next milestone

RL-3 — calibrated outlook and study templates (rolling-origin
evaluation, forecast distributions, CRPS diagnostics). When the
shadow-proxy adapter's catalog reads a desk table with enough
coverage for `vix_term` and `hold-20`, those two incumbents CAN
flip to `funded_history=reconstructed` — but NOT automatically: the
flip needs the desk-side read adapter wired (a mark loader over the
desk evidence store's `mark`/`episode` objects, a defense
computation, and the catalog call-site change from the hardcoded
`UNAVAILABLE`). Until that adapter lands AND the desk's shadow
tables actually contain episodes/marks (as of 2026-09-26 they
contain zero), both incumbents stay honestly `unavailable`.
