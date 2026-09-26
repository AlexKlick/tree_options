# RL-1 — completion packet (2026-09-25)

RL-1 = "Catalog + historical comparisons + dollar accounting + baseline
differences + evidence drill-down" — the first milestone of the TREX
Research Lab handoff (see
`~/pop-deck-uploads/2026-09/TREX-Research-Lab-Design-and-Build-Handoff-29a9aa.md`).

This is the corrective return packet. The 2026-09-25 external audit
rejected RL-1 sign-off and pinpointed seven correctness defects; this
campaign is the red→green disposition for those findings plus the
product gap the audit named (no real comparison result rendered).
After this packet lands, the comparison workspace is honest and the
real-data historical exit remains pending its data blockers (the
audit's prescribed honest terminal state, not a regression).

## Final state (post-correction)

- **Branch:** `feat/rl1-completion` (worktree at
  `/home/alexk/documents/tree_options-worktrees/rl1-completion`).
- **Head:** to be filled in at land time.
- **Source:** `src/tree_options/research/` (10 modules + the new
  `__main__.py` for the read-only CLI + the new `catalog/synthetic.py`
  for the vertical-slice fixtures) + `src/tree_options/trex_web/research_view.py`.
- **Tests:** `tests/research/` (12 files; 156 cases; one dedicated
  `test_synthetic_slice.py`).
- **Workspace:** `~/.local/state/trex-research/run-anon/runstate.sqlite3`
  (separate from the desk evidence store; path-overlap guard refuses
  containment in either direction).
- **Synthetic fixture:** `data/research/fixtures/synthetic-v1.json`
  (sha-pinned, permanently labeled synthetic; machinery validation only,
  not investment evidence).

## Finding-by-finding disposition (the audit's RL1-01..07)

| ID | Headline | Disposition | Where |
|---|---|---|---|
| RL1-01 | "ending_value" is cash movements, not NAV | RED→GREEN: declared-calendar replay derives NAV = cash + Σ qty × last mark; realized derived from FIFO lots via `LedgerBook.apply`; independent `assert_conservation()` is the proof; fees applied once; no implicit borrowing (insufficient cash / underflow / off-calendar executions REFUSE with a machine-readable reason). | `comparison/funded.py`; oracles in `tests/research/test_funded.py` (10,000 / 10,510 / 10,008 / 10,500 / 9,500 + conservation + reject cases) |
| RL1-02 | Spec hashed but not implemented; nonempty results 500 | RED→GREEN: `resolve_plan(spec)` validates every control and pins calendar + contribution schedule + fee policy + cutoff (effect-or-refusal); engine passes the plan to adapters, clips observations to the declared window (exclusions counted), pairs over the declared calendar (no union-of-observed-dates fallback); `ComparisonResult.to_wire` is the single serialization boundary (ISO date keys, money strings). | `comparison/plan.py` (new); `comparison/engine.py`; oracles in `tests/research/test_plan.py` |
| RL1-03 | Duplicate POST 500; no validation; GET recomputes; status stuck queued | RED→GREEN: POST validates EVERYTHING before any write (body is a JSON object, finite positive capital, ISO dates, semantic plan, candidate/benchmark membership); immutable `spec` payload is EXACTLY the canonical hashed form (no server metadata inside); duplicate POST is idempotent (same run_id, 200); bounded worker claims queued, computes, publishes immutable content-bound result (engine sha + per-candidate input snapshot hashes + calendar sha); interrupted runs requeue at startup; pre-custody-format records are blocked (preserved, never erased). GET result reads the stored artifact, zero engine invocations per request (call-counting test). | `research_view.py`; `runstate/worker.py` (new); `runstate/spec_io.py` (new); oracles in `tests/research/test_routes.py` |
| RL1-04 | Store verifier unsound; path guard misses descendants | RED→GREEN: `put()` commits the audit head inside the same transaction as the append (legitimate append-after-verify no longer fails); `replace()` gives mutable run records a fully audited state history; `verify()` is read-only and actually verifies — payload rehash against claimed hashes, object/audit correspondence, chain vs committed head. Receipt wording: "local-consistency" (never an independently anchored authenticity claim). | `runstate/store.py` |
| RL1-05 | Drawer lacks point-specific provenance | RED→GREEN: timestamps normalized once (desk store's string timestamps no longer crash .isoformat); knowledge cutoff is the EXACT instant (08:00Z is no longer end-of-day); marks are scoped by declared deal association + requested session (another candidate's different-session mark can no longer land in this envelope; unassociated candidates count ZERO, never the whole store); synthetic envelopes never touch desk evidence; sealed artifacts resolve from the pinned repo root with a fresh-hash-vs-cataloged-hash CONFLICT warning (no silent rehash over stale metadata); every reproduction command resolves to `python -m tree_options.research inspect …` — a real read-only CLI, never a sealed executor. | `evidence/drawer.py`; `__main__.py` (new `tree_options.research` module); oracles in `tests/research/test_drawer.py` (incl. real `EvidenceStore` integration) |
| RL1-06 | Scientific verdict substituted for data capability | RED→GREEN: `ResearchCandidate` gains `funded_history` (`reconstructed`/`unavailable`) + reason as a DATA dimension independent of the verdict. The sealed-round adapter derives plot/capability from what the artifacts reconstruct (verdict preserved and displayed — PASS-without-data cannot plot; FAIL-with-data stays inspectable with its label). The engine's wholesale retrospective refusal is gone (registration is a displayed qualification, not a plotting veto). Drawdown reports OBSERVED recoveries for every registration (100 → 80 → 100 has a recovery date; the old code suppressed it for the retrospective label). | `contracts.py`; `catalog/sealed_round.py`; `comparison/engine.py`; `comparison/drawdown.py` |
| RL1-07 | Zero-interval drawer polling; stale completions can win | RED→GREEN (hotfixed first, deployed 2026-09-25, head `08a700a`): `intervalMs <= 0` ⇒ polling disabled (one-shot + explicit refresh); generation counter + non-overlap scheduling; interval ticks skip while a fetch is in flight; `EvidenceDrawer` keyed by candidate so one candidate's envelope never lingers under another's heading. | `web/src/hooks/usePoll.ts`; `web/src/components/ResearchPage.tsx`; `web/src/hooks/usePoll.test.ts` (5 new tests, 154 web total) |

## Comparison workspace (the product gap the audit named)

The page previously spooled a run and showed the queued ID. It now:

1. renders the catalog with the data-capability column split from disposition
   and a synthetic badge on every synthesized series;
2. submits a comparison spec (strategies + benchmark + capital + window +
   monthly contributions) and polls the bounded worker's status;
3. reads the IMMUTABLE stored result exactly once per run — never
   recomputes;
4. renders the comparison: summary tiles (ending value, contributed
   capital, gain, coverage with the gap count) FROM THE SAME RESULT
   payload, a multi-line dollar-value chart (existing SVG chart layer,
   gaps render as breaks), a numeric table by session, and a
   point-linked evidence drawer.

UI tests pin the end-to-end flow against the corrected wire contract
(ISO date keys, money strings, nav/gap nullable, `RunResultResponse`
with engine+input-snapshot+calendar+result shas).

## Exit checklist

### 1. One verified benchmark + two eligible strategy versions on a common declared basis

The synthetic vertical slice is GREEN on the permanent catalog fixture
(`data/research/fixtures/synthetic-v1.json`, sha-pinned). One
buy-and-hold benchmark and two strategy versions (momentum, drift)
have a COMPLETE funded history over 61 declared XNYS sessions. End-to-end
comparison via POST → worker → stored result → API → chart/table/tiles
renders the correct hand-oracles:

| Candidate | Final NAV (5-bp fees) | Total fees | Comment |
|---|---:|---:|---|
| synthetic-benchmark-v1 | **$10,883.20** | $4.80 | buy 24 @ 400.00, hold to 412.00 |
| synthetic-momentum-v1  | $10,628.04 | $11.96 | two-leg momentum across the Feb dip |
| synthetic-drift-v1     | $10,205.22 | $2.78  | adds on dips |

The historical-data exit (real benchmark, real strategy versions) is
PENDING — see the data blocker section below. The audit's prescription
for this exact state: "retain the working synthetic demonstration,
leave historical exit pending, do not silently reduce the target to an
empty all-rejected result."

### 2. Every catalog candidate shows an explicit data blocker when no funded history is reconstructable

`funded_history=unavailable` + `funded_history_reason` carry the honest
explanation (e.g. "no daily portfolio history is reconstructable from
sealed trials: capital, cashflow and valuation coverage are not recorded
by the sealed-round format"). `plot_funded_account` is False on every
sealed scope; the engine never invents rows it cannot produce.

### 3. Every chart point traces to its input artifact (sha in `EvidenceEnvelope`)

`research.evidence.drawer.evidence_for_point(candidate, session)` binds
each point to its actual candidate + session + deal association,
normalizes timestamps once, compares exact aware instants, separates
evidence kinds (synthetic never reads the desk store), pins artifact
paths to the repo root, refuses changed-source ambiguity with a warning,
and emits a REAL reproduction command:

```
python -m tree_options.research inspect --candidate <id> [--session YYYY-MM-DD]
```

(no sealed campaign executor is ever reopened to inspect a point).

### 4. Existing correction tests + app routes remain intact

The desk safety gate (full 4,800+ suite) and the research gate both
exit rc=0; the new tests live in `tests/research/`. New routes mounted
at `/api/research/*`; existing `/api/desk/*`, `/api/market/*`, `/plan/*`,
`/api/plans/*`, `/api/discovery/*` untouched.

### 5. No path imports `tree_options.trex.ibkr`

Enforced by:

- `tests/research/test_boundaries.py::test_no_broker_imports_in_research`
  (AST check at unit-test level)
- `scripts/research_gate.sh` static check via `grep -RIn`
- Runtime guard `research.paths.assert_no_overlap_with_desk()` at every
  entry point that opens a workspace (attach + `open_runstate_store`),
  with containment checked in BOTH directions against the resolved
  (symlink-following) workspace and against the desk tree roots
- `scripts/research_gate.sh` `pytest tests/research -q` enforces it

If anyone imports `tree_options.trex.{ibkr,monitor,gateway_watch,enter}`
from inside `tree_options.research`, the gate fails before merge.

## Data blockers (historical exit — pending, intentional)

Every campaign scope and the desk shadow-proxy path has a specific
blocker on reconstructable daily funded history:

| Path | Source | Blocker |
|---|---|---|
| sealed: jepa-filter | `sealed-round.json` | trials are per-trial dispatches, not daily portfolio history |
| sealed: term-gate    | `sealed-round.json` | WITHDRAWN verdict + same format blocker |
| sealed: vrp-cond     | `sealed-round.json` | DATA-GATED carrying the registered acceptance text |
| sealed: exit-grid-2, pead-deep-2, tnull | no sealed-round.json | scope never sealed |
| shadow-proxy: vix_term, hold-20 (incumbents) | `desk/shadows.py` EOD-deadline proxy marks | proxy valuation is not a filled execution; an adapter that defends a daily portfolio conversion to NAV is RL-2 work |

No patch to the audit's exit condition is implied. The synthetic slice
demonstrates the machinery; the historical-data comparison remains the
honest terminal state the audit prescribed.

## Gate evidence (fill in at land time)

| Stage | Command | Result |
|---|---|---|
| Research hermetic lane | `.venv/bin/python -m pytest tests/research -o addopts='' -q` | 156 passed |
| Lint (research) | `.venv/bin/python -m ruff check src/tree_options/research src/tree_options/trex_web tests/research` | clean |
| Types (research) | `.venv/bin/python -m mypy src/tree_options/research src/tree_options/trex_web` | clean |
| Boundary (AST) | `grep -RIn 'tree_options.trex.{ibkr,monitor,gateway_watch,enter}' src/tree_options/research` | zero matches |
| Path guard (containment, both directions) | `tests/research/test_boundaries.py` + new descendant/ancestor tests | refuse |
| Research gate | `bash scripts/research_gate.sh` | rc=0 |
| Desk safety gate (coexistence) | `bash scripts/desk_safety_gate.sh` | rc=0 |
| Web typecheck + vitest + build | `cd web && npm run check` | tsc clean; vitest 154 passed; vite build + bundle check OK |
| Authenticated `/api/research/candidates` probe (loopback) | `curl -fsS :8090/api/research/candidates` | 200; synthetic + sealed rows present |

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
  scenarios). `/api/research/scenarios` returns **410 Gone**
  (`{"error": "scenarios_out_of_scope_for_rl1"}`).
- RL-3: calibrated outlook + study templates (rolling-origin
  evaluation, forecast distributions, CRPS diagnostics).
  `/api/research/forecast` returns **410 Gone**
  (`{"error": "forecast_out_of_scope_for_rl1"}`).
- E5 broker-paper execution.
- New charting library in `web/package.json` (the SVG chart layer is
  reused).
- New Python deps in `pyproject.toml`.
- GitHub Actions.
- Public exposure via `opencode-stack` ngrok paths.
- Vendor telemetry on (`MEM0_TELEMETRY=False` enforced by the gate).

## No-execution statement

No orders, broker queries, monitor restarts, or sealed-study reruns
were performed in this campaign. The trex-web service restart on
deployment is the only environment action; the live monitor still
runs `bedced9` accounting until the operator performs SP-5.

## Next milestone

RL-2 — reproducible scenario branching (contribution/allocation/cost
scenarios) + the shadow-proxy adapter for incumbents (vix_term, hold-20)
that converts desk EOD proxy marks into a defended daily portfolio
history, plus a non-prod scheduled run to validate the worker's
requeue/interruption behavior under restart.
