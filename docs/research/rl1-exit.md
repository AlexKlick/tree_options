# RL-1 — exit checklist

RL-1 = "Catalog + historical comparisons + dollar accounting + baseline
differences + evidence drill-down" — the first milestone of the TREX
Research Lab handoff (see
`~/pop-deck-uploads/2026-09/TREX-Research-Lab-Design-and-Build-Handoff-29a9aa.md`).

This is the analog of `docs/production-candidate/W3-LANE-RECONCILIATION.md`
for the corrective branch.

## Final state

- **Head:** `5c54cef` (rc1 + R2 + R3 corrective pass landed on `origin/main`)
- **Source:** `src/tree_options/research/` (10 modules) + `src/tree_options/trex_web/research_view.py` + `scripts/research_gate.sh` (the catalog adapter is in-package at `research/catalog/sealed_round.py`)
- **Tests:** `tests/research/` (10 test files, 76 cases)
- **SPA:** `web/src/lib/types.ts` RL-1 contracts + `web/src/lib/api.ts` wrappers + `web/src/lib/router.ts` `#/research` + `web/src/components/ResearchPage.tsx` + `web/src/App.tsx` wiring
- **Workspace:** `~/.local/state/trex-research/<run-id>/runstate.sqlite3` (separate file from the desk evidence store; path-overlap guard refuses misconfig)

## Gate evidence

| Stage | Command | Result |
|---|---|---|
| Hermetic lane (research) | `pytest tests/research -q` | 76 passed, 0 failed, 0 skipped |
| Lint + types (research) | `ruff check src/tree_options/research src/tree_options/trex_web tests/research` + `mypy` (32 files) | All checks passed |
| Boundary guard (AST) | `grep -RIn 'tree_options.trex.{ibkr,monitor,gateway_watch,enter}' src/tree_options/research` | Zero matches |
| Path-overlap guard | `research.paths.assert_no_overlap_with_desk()` | Refuses `RESEARCH_*` env vars that resolve into `TREX_DESK_STATE/evidence` or `DESK_PAPER_DIR` |
| Research gate | `bash scripts/research_gate.sh` | rc=0; 76 passed; ruff + mypy clean |
| Desk safety gate (coexistence) | `bash scripts/desk_safety_gate.sh` | rc=0; 4954 passed, 36 skipped, 0 failed (3min 6s) |
| Web typecheck + vitest + build | `cd web && npm run check` | tsc clean; vitest 148 passed (+1 from ResearchPage); vite build + bundle check OK |

## Exit checklist

### 1. One verified benchmark + two eligible strategy versions render on a common declared basis

**Corrected 2026-09-25 (post-deploy audit against the real artifacts).**
The campaign's sealed artifacts contain **no plot-eligible family**. The
catalog on real data (6 scopes) is: `term-gate` WITHDRAWN (window
2024-10-01..2026-08-28), `jepa-filter` FAIL, `vrp-cond` sealed-but-not-
machine-reducible (DATA-GATED carrying the registered acceptance text),
and `exit-grid-2` / `pead-deep-2` / `tnull` never sealed. `vix_term` and
`hold-20` are the desk **incumbents** — they appear in the campaign only
as reference blocks inside other scopes' sealed-round.json, and enter
the catalog through the shadow-proxy adapter, which is a documented
honest-zero-rows stub in RL-1 (`comparison/engine.py::_shadow_executions`).

The SPA therefore renders the honest catalog: every family with
disposition + ineligibility reason, zero eligible candidates, and an
empty run-form selection. An earlier draft of this checklist claimed
vix_term/hold-20 as PASS rows per `REPORT.md:280-285` — that table
contains no such rows; the claim was fixture-true, real-data-false, and
is withdrawn. Wiring the incumbents' real series (desk shadow marks) is
the first unit of RL-2.

### 2. Every other catalog candidate shows an explicit ineligibility reason

Adapter rule: `plot_funded_account=false` whenever disposition ∈
{WITHDRAWN, DATA-GATED-NOT-RUN, NOT_EVALUABLE, NOT_EVALUABLE-SEALED,
NOT_CANDIDATE, INSUFFICIENT_N, INSUFFICIENT_COVERAGE,
DESCRIPTIVE-ONLY:NO-REGIME-SIGNAL}; `true` only for PASS or HOLD-STANDS.
`ineligibility_reason` carries the human string ("disposition=WITHDRAWN —
see REPORT.md §7 ..."). The engine rejects ineligible candidates from the
comparison diff (`paired_diff[s.candidate_id] not present`).

### 3. Every chart point traces to its input artifact (sha in `EvidenceEnvelope`)

`research.evidence.drawer.evidence_for_point(candidate, session)` returns
one `EvidenceEnvelope` per (candidate, session). The envelope carries:

- `exact_versions.strategy = candidate.family`
- `exact_versions.miner / playbook = <sha256 short prefix from sealed-round.json>`
- `source_artifacts = [(path, sha256)]` for every file the envelope touches
- `reproduction_command = 'python scripts/campaign/<scope>_sealed_run.py --trial <id>'`

For shadow-proxy / synthetic candidates, the drawer's diagnostics carry
`as_of_cutoff` (R2-03 knowledge-cutoff) + `mark_event_count` + the audit
window boundaries.

### 4. Existing correction tests + app routes remain intact

`desk_safety_gate.sh` exits rc=0 with the same passing test count it had
before RL-1 work started (4954 passed, 36 skipped). New routes mounted at
`/api/research/*`; existing `/api/desk/*`, `/api/market/*`, `/plan/*`,
`/api/plans/*`, `/api/discovery/*` untouched.

### 5. No path imports `tree_options.trex.ibkr`

Enforced by:

- `tests/research/test_boundaries.py::test_no_broker_imports_in_research`
  (AST check at unit-test level)
- `scripts/research_gate.sh` static check via `grep -RIn`
- Runtime guard `research.paths.assert_no_overlap_with_desk()` at
  `attach_research` time
- Per-file `from __future__` import discipline + `ruff check` policy

If anyone imports `tree_options.trex.{ibkr,monitor,gateway_watch,enter}`
from inside `tree_options.research`, the gate fails before merge.

## Operator-supervised (still out of scope)

- **SP-3 off-host backup anchor** — owner item; no destination supplied.
- **SP-5 monitor restart with reconciliation** — owner item; the live
  monitor still runs `bedced9` accounting until operator performs the
  supervised restart.
- **Broker credential provisioning to the research worker** —
  forbidden by RL §9.
- **PAPER_EXECUTION evidence kind** — DESK_PAPER_DIR integration lands in RL-2.
- **Sealed-window re-opening** — forbidden; the campaign's
  sealed-round.json is the only source.

## Out-of-scope for RL-1

- RL-2: reproducible scenario branching (contribution/allocation/cost
  scenarios). Routes `/api/research/scenarios` returns **410 Gone**
  with body `{"error": "scenarios_out_of_scope_for_rl1"}`.
- RL-3: calibrated outlook + study templates (rolling-origin
  evaluation, forecast distributions, CRPS diagnostics). Routes
  `/api/research/forecast` returns **410 Gone** with body
  `{"error": "forecast_out_of_scope_for_rl1"}`.
- E5 broker-paper execution.
- New charting library in `web/package.json`.
- New Python deps in `pyproject.toml`.
- GitHub Actions.
- Public exposure via `opencode-stack` ngrok paths.
- Vendor telemetry on (`MEM0_TELEMETRY=False` enforced by the gate).

## Files added (RL-1)

```
src/tree_options/research/
    __init__.py
    paths.py
    contracts.py
    catalog/
        __init__.py
        sealed_round.py
    comparison/
        __init__.py
        missingness.py
        pair.py
        drawdown.py
        funded.py
        engine.py
    evidence/
        __init__.py
        read_only_evidence.py
        drawer.py
    runstate/
        __init__.py
        spec_hash.py
        store.py
src/tree_options/trex_web/research_view.py
scripts/research_gate.sh

tests/research/
    __init__.py
    test_boundaries.py
    test_research_contracts.py
    test_catalog.py
    test_pair.py
    test_drawdown.py
    test_funded.py              (added in funded-stage — see STEP 3 record)
    test_engine.py
    test_drawer.py
    test_runstate.py
    test_routes.py

web/
    src/lib/types.ts            (RL-1 contracts appended)
    src/lib/api.ts              (research wrappers appended)
    src/lib/router.ts           ({view:'research'} + #/research)
    src/components/ResearchPage.tsx
    src/components/ResearchPage.test.tsx
    src/App.tsx                 (ResearchPage mounted on route.view === 'research')
```

## Receipts and run ids (this campaign)

- `~/.local/state/trex-w3-integration/r2/freeze-receipt.txt` — Steps 1–7
  timestamped. Run-anon workspace = `~/.local/state/trex-research/run-anon`
  (cleared at the end of the gate run).

## Next milestone

RL-2 — reproducible scenario branching, scenario ensemble, contribution /
allocation planner. The same `tree_options/research/` package gains a
`scenarios/` subpackage; the existing `scenarios` HTTP route flips from
410 Gone to a real handler. Plan pass begins after RL-1 lands and is
handed back to operator.
