# RL-3 — completion packet (2026-09-26/27)

RL-3 = "calibrated outlook and study templates": a quantile-forecast
evaluation lane over the research store's execution-bound run custody.
A forecast run EVALUATES distributional forecasts of an index level h
observed sessions ahead on a rolling monthly-origin grid, publishes a
content-bound receipt whose every number re-derives from a per-origin
ledger, and surfaces the latest-origin forward quantiles as an
explicitly-beyond-data fan. It never claims a validated product:
`calibration_status` is always `not_claimed`, coverage is displayed
with its n and uncertainty, and listed-but-disabled horizons are
labeled illustrative everywhere they appear.

This packet closes the whole campaign: **RL-3a** (machinery, synthetic
lane, API/CLI; branch `feat/rl3-forecast`, 15 commits `23f3f9b..a1abcb5`
inclusive, merged to main as `c4035f6`) and **RL-3b** (wave 2:
index-lane enablement verification, the SPA Outlook tab +
ForecastFanChart, this packet; branch `feat/rl3-outlook`, commits
`f53415a..<this head>`).

## What landed

- **Engine** (`src/tree_options/research/forecast/`): `contracts.py`
  (spec, run-id v4 over spec + series bytes + BOTH calendar shas +
  engine sha; tally identity; ledger rows), `refusal_codes.py`,
  `spec_io.py`, `metrics.py` (pinball per τ, `grid_quantile_score`
  = 2×mean pinball over the declared grid — named a GRID score, never
  CRPS; Wilson 95%; moving-block bootstrap; matched-cohort skill;
  DM pass-through with origin-unit lag), `sources.py` (registry:
  synthetic h=5; `index:VIX` h=5/20 enabled, 63/126 listed
  illustrative; single-read loaders hashing and parsing the same
  bytes; bytes-only authority identity), `harness.py` (rolling-origin
  grid, three models: `rw_full` baseline / `rw_window` / `ar1_direct`,
  finite guards demoting non-finite output to FAILED origins),
  `engine.py` (`evaluate_forecast` re-checks registry/horizon/history;
  refusal retains tally + ledgers; `model_notes` study block;
  degraded-metrics fallback for aggregate overflow).
- **Worker** (`runstate/worker.py`): third dispatch kind `forecast`
  with execution-bound identity — engine/calendar checks before the
  series load, grid-authority divergence outranks series drift, the
  run id is RE-DERIVED and any inconsistency refuses
  `research.forecast.identity_mismatch`; unknown kinds fail loudly.
- **API** (`trex_web/research_view.py`): `GET /api/research/forecast`
  (registry + interval semantics + freshness-qualified receipts:
  fresh only when series/engine/both calendars all match the current
  world; refused attempts surface separately) and `POST` (pre-write
  400s incl. `horizon_not_enabled`; single-read submission binding;
  idempotent on the full execution identity). The RL-1 410 stub is
  retired.
- **CLI**: `python -m tree_options.research inspect --forecast
  <run_id>` — read-only parity with the API result route.
- **SPA** (RL-3b): `lib/types.ts` forecast wire mirror, `lib/api.ts`
  client, `lib/forecast.ts` pure helpers, `ResearchOutlook.tsx` (third
  tab `outlook`), `ForecastFanChart.tsx` (SVG quantile bars at the
  horizon offset from the last close; unavailable models are explicit
  gaps; numeric formatting, never USD).
- **Harnesses**: `scripts/research/forecast_mutation_pass.py`
  (checkpoint B, Python scope) and
  `scripts/research/spa_mutation_pass.py` (checkpoint C, UI honesty
  gates).

## Handoff §10 acceptance matrix — every row names its enforcement

The exit sentence: "each enabled predictive horizon has a traceable
evaluation receipt and model/data scope; uncensored failures and
excluded origins are counted; comparison to a baseline is visible; the
UI correctly explains interval semantics."

| Row | Enforcement (test that fails if the gate is removed) | Live evidence |
|---|---|---|
| Every enabled horizon has a traceable receipt + scope | `test_forecast_routes.py::test_202_then_200_idempotent_same_run_id` (execution-bound id); `test_registry_shape_and_disabled_horizon_copy` (enabled exactly (5,20) on index, (5,) synthetic); `test_forecast_worker.py::test_completed_run_publishes_content_bound_receipt` (receipt sha == canonical hash of the wire, recomputed in-test); SPA `run-keying` test (a receipt renders under its own run only) | scratch instance: index h=5 AND h=20 receipts published fresh (below) |
| Uncensored failures/excluded counted | `test_forecast_harness.py` ledger identity `total = evaluated + excluded + failed`; stub-None → `n_failed=1`; unordered → FAILED `quantile_ordering`; NaN → FAILED `non_finite`; `test_forecast_engine.py` refusal retains ledgers; SPA receipt test asserts `target_beyond_data ×1` and `non_finite ×1` render | h=20 receipt: 90 evaluated / 13 `insufficient_history` / 1 `target_beyond_data`, `failed_by_model` all 0, reasons visible |
| Baseline comparison visible | engine `ValueError` on ≠1 baseline, pinned from BOTH sides (`test_exactly_one_baseline_required` for two baselines, `test_zero_baselines_also_refuse` for omission — a `> 1` mutation of the guard dies); `test_forecast_metrics.py` matched-cohort skill + DM oracles (hand-computed `d=[1,2,3,4]` LRV/stat/p; negation reverses); SPA: baseline row labeled, skill/DM cell carries direction + lag units, null skills show their reason | h=20: `rw_full` baseline 83/90 coverage; `ar1_direct` skill +0.0774 (DM p=0.2123 — honestly not significant); `rw_window` skill −0.0748 |
| UI explains interval semantics | `INTERVAL_SEMANTICS` pinned by the routes registry test; SPA `renders the interval-semantics text` test asserts the FULL registry string renders VERBATIM in `outlook-semantics`; the receipt test asserts the fan caption's beyond-data and not-confidence-intervals phrases | metadata serves the full semantics string; the Outlook tab renders it verbatim |
| Long horizons blocked or labeled illustrative | `test_horizon_63_is_rejected_not_just_hidden` + `test_index_lane_rejects_disabled_horizons_pre_write` (400 pre-write, nothing persisted); SPA: disabled options unselectable with the exact registry copy (mutation-killed twice: M-C1, M-C5) | scratch POST h=63 → 400 `research.forecast.horizon_not_enabled`, listed [5,20,63,126] |

## Honesty rules and where each is enforced

- **No fan without a receipt**: `hasReceipt` admits only a completed
  run's result wire (unit oracle incl. the discriminating case of a
  noncompleted envelope carrying receipt-shaped data); the receipt
  block re-gates on schema; refusal/queued/failed render blockers.
  Mutation-killed (M-C2; combined M-C3b) and pinned by the no-receipt
  SPA test.
- **Coverage always with n**: `coverageLine` renders `hits/n` first
  (mutation-killed M-C4); Wilson + bootstrap bounds and the binomial
  caveat render with it.
- **Calibration never claimed**: `calibration_status` renders exactly
  as recorded (`not_claimed`); no copy says calibrate/calibrated —
  mutation-killed (M-C7); the empty state reads "run evaluation".
- **Run-keying**: superseded POSTs are discarded by a submission
  generation; the polled status is trusted only when the record names
  the current run (pinned by the polled-status identity gate test:
  B's status held pending while A's completed record stays cached →
  B's result fetch cannot fire early); a late result response for a
  superseded run is dropped at the effect AND at render (redundant
  locks — either alone survives by design, the combination is
  mutation-killed, M-C6b).
- **Immutable receipts are not reinterpreted**: the fan chart uses the
  receipt's own `quantile_grid`, never the live metadata's.
- **Floats, not strings**: this surface carries statistics and index
  levels, never money (desk precedent, `desk/har.py`). Non-finite
  values are structurally impossible (`canonical` allow_nan=False +
  FAILED-origin guards + SPA `fmtLevel` dashes).

## Review loop (14 codex passes through checkpoint D, all findings dispositioned)

| Checkpoint | Receipt(s) | Outcome |
|---|---|---|
| A: plan design (×2 rounds) | `rl3-a-design-review.log`, `-r2.log` | both rounds ended **NO-GO**; the plan was revised to v3 (closure-corrected grid, DM lag in origin units, grid score not CRPS, compressed-origin declaration) and implemented in that form — convergence was established at checkpoint B, not by a later design-round GO (no separate acceptance receipt exists; recorded as an implementation disposition) |
| B: wave-1 code | `rl3-b-review.log` | NO-GO (2 P1 + 3 P2 + 5 surviving mutants) → fixed `10cab93` |
| B′ + sol cold read | `rl3-b-prime-review.log`, `rl3-sol-review.log` | both NO-GO (submission re-read, authority parse-in-identity, run-id schema bump) → fixed `fbd3d36`, `aa06c01` |
| B verification | `rl3-b2-verify.log`, `rl3-sol2-verify.log` | sol 7/7 CLOSED; astra 2 residuals → closed `46aa5ff` (bytes-only identity, grid-authority guard) |
| Final | `rl3-astra-final.log`, `rl3-sol-final.log` | astra GO-for-gates; sol one refusal-ordering P2 → fixed `2241951` |
| C: SPA honesty (×3 rounds) | `rl3-c-review.log`, `rl3-c2-review.log`, `rl3-c3-review.log` | NO-GO (1 P1 + 6 P2) → all CLOSED by `b4cbbdd` + `0282282` + `949e7e4` (submission generation, status identity gate, same-id resubmit, receipt's own grid, by-slot fan endpoints, failed-run errors, self-consistent fixtures with independent oracles, mutation credit requires a completed failing run) |
| D: exit-packet audit | `rl3-d-final-audit.log`, `rl3-d2-audit.log` | NO-GO (1 P1 deploy-build omission + 6 P2 + 1 P3) → all dispositioned (see below) |

Second reviewer `gpt-6-sol` was added per owner instruction for the
wave-1 work; checkpoints C ran on `gpt-6-astra` (the standing
reviewer). Bounded remediation ran one round beyond the plan's ≤2 on
checkpoint C (three fix rounds) because the final residual — mutation
credit without a completed failing run — was an evidence-quality
defect in the new harness, not product code; the deviation is recorded
here rather than silently absorbed.

## Mutation passes

- **Checkpoint B (Python scope)**: 15 KILLED / 0 SURVIVED
  (`forecast_mutation_pass.py`; the original 10 + 5 review-named
  survivors, all killed by hand-derived oracles).
- **Checkpoint C (SPA honesty gates)**: 7 KILLED / 0 SURVIVED / 2
  justified-survivors / 9 total (`spa_mutation_pass.py`). M-C3 and M-C6
  break single locks of deliberately redundant pairs; the combined
  mutants M-C3b and M-C6b are both KILLED. KILLED requires a completed
  vitest run whose summary shows real test failures — rc alone is not
  credit — with full per-mutant logs retained.

## Gate evidence (log receipts under `tree_options-logs/`)

| Gate | Log | Result |
|---|---|---|
| `scripts/research_gate.sh` | `rl3b-research-gate2.log` | rc=0; **349 passed**; ruff + mypy clean |
| web check (tsc + vitest + build + bundle) | `rl3b-web-check4.log` | rc=0; **36 files / 185 tests** passed; single relative chunk `index-CFGpcFAu.js` |
| `scripts/desk_safety_gate.sh` (host-test) | `rl3b-desk-gate.log` | rc=0; **5219 passed / 7 skipped** (run at `0628e2c`; after that head the campaign changed SPA production code, SPA tests, the SPA harness, one engine TEST, and docs — but NO production Python under `src/`, which remains byte-identical through this head; the engine-test addition is covered by the research-gate rerun above) |
| `forecast_mutation_pass.py` | `rl3b-py-mutation.log` | **15 KILLED / 0 SURVIVED / 0 other** — re-executed on the wave-2 tree; `src/` is byte-identical to the wave-1 head `a1abcb5` (wave 2 added no production Python), so this run is the same source binding |
| `spa_mutation_pass.py` | `rl3b-spa-mutation.log` | **7 KILLED / 0 SURVIVED / 2 justified-survivor / 9 total** at the wave-2 tree |

Tests added by the campaign: `tests/research/test_forecast_*.py`
(9 files / 136 test functions / 145 parametrized cases) + forecast
lifecycle/worker/route coverage; SPA `lib/forecast.test.ts` (18) +
`ResearchOutlook.test.tsx` (10).

## Live verification (scratch instance, branch code, :8091)

Workspace `RESEARCH_WORKSPACE_DIR=$(mktemp -d)`; the production
`run-anon` store was never written. Durable receipts under
`tree_options-logs/`:

| Probe | Receipt | Result |
|---|---|---|
| POST `index:VIX` h=20, eval from 2018-02-01 | `rl3b-h20-result.json` | 202 → completed receipt: 104 grid origins → 90 evaluated / 13 `insufficient_history` / 1 `target_beyond_data`; baseline coverage 83/90, Wilson [0.848, 0.962], bootstrap [0.856, 0.978]; `ar1_direct` skill +0.0774 (DM p=0.2123); forward fan from origin 2026-09-25 (last close 14.87), all three models `ok`; `beyond_data: true`, `target_session: null` |
| POST `index:VIX` h=5 | `rl3b-h5-result.json` | 202 → completed: 92 evaluated / 12 `insufficient_history`; coverage 82/92, Wilson [0.811, 0.940]; fans ok |
| POST h=63 | `rl3b-h63.json` | 400 `research.forecast.horizon_not_enabled` pre-write; `listed [5, 20, 63, 126]`; nothing persisted |
| GET metadata after the h=20 receipt | `rl3b-metadata-fresh.json` | index h=20 `fresh: true` with receipt `3a968516…` bound (the same execution identity as the h=20 POST — deterministic); 63/126 `illustrative_only` with the exact status copy; synthetic lane separate (a fresh scratch workspace has no h=5 receipt until one is run — the earlier both-fresh observation was transcript-only and is superseded by this artifact) |
| CLI parity (`inspect --forecast`) | `rl3b-cli-parity.log` | rc=0; run_id, engine/result/calendar/session-authority shas ALL equal to the API GET (asserted programmatically in-session) |
| Synthetic-lane write proof (wave 1) | RL-3a session receipts (`rl3-{b-prime-review,…}` era; scratch server log not retained — observed in-session: full receipt + typed `insufficient_origins` refusal with ledgers retained + freshness flip) | machinery validated on the frozen fixture |

The plan's verification target said "index run with
`origins.evaluated ≈ 100`"; the observed 90 (h=20) and 92 (h=5) follow
from the live data (104 month-start origins in scope minus the 12–13
early origins below `min_history` and the beyond-data tail) and are
the honest numbers.

## Checkpoint D dispositions

- **D1 (P1) deploy-build omission** — FIXED: the post-deploy checklist
  below now REQUIRES building the SPA in the serving checkout before
  the restart (static output is gitignored; merging alone cannot
  transfer the bundle) and recording the served asset identity.
- **D2 verbatim semantics/caption** — FIXED: the SPA test now asserts
  the FULL registry `interval_semantics` string renders verbatim, and
  the fan caption's beyond-data + not-confidence-intervals phrases are
  asserted.
- **D3 completed-run restriction** — FIXED: `hasReceipt` unit tests add
  the discriminating case (a `running`/`failed` envelope carrying
  receipt-shaped `result` data → false).
- **D4 named-test gaps** — FIXED: `test_zero_baselines_also_refuse`
  pins the `!= 1` guard against a `> 1` mutation; the new
  polled-status identity gate test holds B's status response pending
  while A's completed record stays cached and asserts B's result fetch
  does NOT fire until B's own status arrives.
- **D5 gate provenance** — FIXED: the table cites the durable log
  receipts; the Python mutation pass was RE-EXECUTED on the wave-2
  tree (src/ byte-identical to `a1abcb5`) for a clean binding.
- **D6 checkpoint-A wording** — FIXED above (both design rounds ended
  NO-GO; v3 was implemented and converged at checkpoint B).
- **D7 receipt gaps** — FIXED: all wave-2 scratch artifacts are now
  durable under `tree_options-logs/`; the wave-1 synthetic scratch
  log was not retained and is labeled as observed-in-session; the
  no-execution statement below is labeled an operator attestation.
- **D8 counts** — FIXED: 15 wave-1 commits, 12 codex passes, 135 test
  functions / 144 parametrized cases, and the refreshed gate totals.

## Transparency notes

- Wave-1 commit `b303840` briefly landed with a failing test; the
  follow-up `4918563` corrected it in the immediately following commit
  of the same push window (the paired-cohort oracle asserted the
  cohort, not the sign). Recorded here, not hidden.
- The run-id schema was bumped `forecast-run/1 → /2` pre-merge when
  the identity gained the session-authority binding.
- `_ENGINE_MODULES` grew during review remediation (worker,
  diagnostics, desk indices self-hash): the engine sha changed for
  EVERY lane. This is expected and documented — a rerun is a NEW run;
  old receipts stay truthful under their own identity and surface as
  stale in metadata.
- The RL-1-era string `forecast_out_of_scope_for_rl1` is retired with
  the 410 stub.
- The live desk-store VIX snapshot was staged READ-ONLY into the
  worktree for the scratch proof (gitignored path; the desk store
  itself untouched; no monitor/broker interaction).

## Post-deploy checklist (executed at deployment; the :8090 restart)

1. [ ] Merge `feat/rl3-outlook` → main (normal merge), push.
2. [ ] **BUILD the SPA in the serving checkout** (`web` check or the
   build step) — static output is gitignored, so the serving
   checkout's bundle must be regenerated or the restart would serve
   the pre-RL-3b SPA (checkpoint D, P1).
3. [ ] `systemctl --user restart trex-web.service`.
4. [ ] `GET :8090/api/research/forecast` → 200
   `research-forecast-metadata/1`.
5. [ ] The SERVED bundle (the asset filename in the served
   `index.html`) is the new chunk and contains
   `research-tab-outlook` / `outlook-receipt` / `forecast-fan-svg`
   testids — record the served asset identity.
6. [ ] Tailnet `/trex/` → 403 operator gate (route live).

## Operator-supervised / out-of-scope (carried)

- Broker-paper execution evidence (E5) and DESK_PAPER_DIR integration
  remain future work; `paper_execution` stays a reserved contract
  value.
- No new pyproject dependencies (numpy-only); no GitHub Actions; no
  public exposure; `MEM0_TELEMETRY=False` enforced.
- Shadow-table incumbents (`vix_term`, `hold-20`) remain honestly
  `unavailable` pending desk-side coverage (unchanged from RL-2).

## No-execution statement (operator attestation)

No orders, broker queries, monitor restarts, or sealed-study reruns
were performed. The only environment action in RL-3b is the planned
`trex-web.service` restart at deployment. The production research
store `~/.local/state/trex-research/run-anon/runstate.sqlite3` was
never written by the campaign: all write-path proofs ran on scratch
instances with isolated workspaces, now removed. These are agent
attestations over the session's own actions (no independent receipt
can exist for an absence); the scratch receipts above are the
positive evidence for what DID run.

## Next milestone

RL-4 per the handoff sequence. Candidates already visible from this
campaign's receipts: the AR(1)-direct model's h=20 skill is positive
but statistically insignificant (DM p≈0.21 on 90 origins) — a natural
question for a future study template is whether ANY simple quantile
model beats the random walk on this grid with the paired-cohort
discipline applied honestly, not whether this one does.
