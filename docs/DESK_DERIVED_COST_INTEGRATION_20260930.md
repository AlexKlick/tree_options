# Derived cost research integration

This integration adapts PR48 source `dec366635025404af77ad879d605f70c90269748`
to candidate `67ab82103411f5d9c25b5230b18040300f565fb5` in an isolated
worktree. It adds an optional cost sensitivity model; it does not change the
flat research control or authorize an execution. No corpus rerating or
external provider validation was performed for this integration.

The peer reports pooled IWM/QQQ/SPY EOD quote marginals from September 2026,
filtered to 7–60 DTE, positive volume/open interest and absolute delta at most
0.70. The supplied 18,783-row total is reported peer evidence, not an
independently revalidated corpus receipt. Five full-spread delta marginals
and three rounded DTE ratios define fifteen **derived** cells. These cells
were not measured jointly. Applying this calibration to another historical
period is a modeling assumption and provides no historical decision-clock
quote, liquidity, or fill evidence. Withdrawn aggregate rerating figures
are not carried into this integration.

`desk.cost.SpreadCostModel` divides the full quote by two once, then charges
each package leg for both modeled opening and closing fills plus the declared
commission. A cheap/expensive two-leg package costs $23.60 in the shortest DTE
band: `(.020 / 2 + .190 / 2) * 100 * 2 + .65 * 4`. This is a hand arithmetic
oracle, not an observed execution price. Unknown symbols, absent/nonfinite or
out-of-domain deltas, unsupported DTE, and incomplete packages have no price.
One refused leg refuses the complete package.

Existing `desk.outcomes.CostModel` remains the default modeled control.
`candidate_outcome(..., costs=SpreadCostModel())` and `outcome_table` accept
the optional derived model. The command line selects it explicitly:

```bash
uv run python -m tree_options.desk outcome-table \
  --bundle /private/frozen-minute-bundle.json \
  --out /private/derived-outcomes.jsonl --cost-model derived
```

Current IAG minute-close candidates contain no delta. They therefore return
`status="no_price"`, `pricing_status="NO_PRICE"`, `gross=null`, `net=null`
when this optional model is selected and no explicit cost inputs are supplied.
The CLI summary counts these refusals, excludes them from performance means,
and reports `DATA_GATED` whenever pricing is incomplete. A successful pricing
result reports `PRICED_SIMULATION` and `cost_model="derived-spread/1"`.
Cost provenance always records `joint_cells_measured=false`,
`corpus_revalidated=false`, `describes_fill_clock=false`,
`exact_execution_economics=false` and `execution_authorized=false`.

The candidate adapter recognizes an explicit `cost_legs` collection containing
both actual long/short OCC tickers. Each row supplies `ticker`, `symbol`,
`abs_delta`, integer `dte`, `source_session`, aware `source_timestamp_et`, aware
`available_at`, and boolean `is_eod_snapshot`. No missing source flag becomes
EOD by default. Source event time must precede availability, and both must be
at or before the decision. DTE must match the actual ISO expiry and decision
date through the existing calendar-days helper. These fields classify the
delta input; they do not turn the separate EOD cost calibration into an
intraday quote. The integration supplies no historical delta producer or
minute-bundle enrichment. A future producer must bind these inputs to frozen
research manifests before a historical modeled-cost run can become usable.

`NoPriceLedger.record_outcome` is called after selecting an arm's candidate,
including after an outcome cache hit. Its identity comprises arm, snapshot,
candidate and exit horizon. Identical repeats are idempotent; conflicting
facts under one identity are refused. `total` counts distinct candidate/horizon
facts, while `snapshots` separately identifies unique board coverage. Menu
probes must not attribute refusals to an arm. The longrun integration owns
selected-decision attribution, source-engine hashing, and promotion gating;
this cost lane changes no longrun or skill source.

## Validation

- Initial regression against peer pricing/current outcomes: **24 failed,
  10 passed**, `/tmp/trex-derived-cost-red.log`.
- Explicit CLI regression before implementation: **1 failed, 36 passed**,
  `/tmp/trex-derived-cost-cli-red.log`.
- Final five-module focused run with warnings as errors: **107 passed**,
  `/tmp/trex-derived-cost-green-final.log` (derived cost, existing outcomes,
  IAG, hindsight and enforcement).
- Scoped Ruff: all checks passed,
  `/tmp/trex-derived-cost-ruff-final.log`.
- Scoped mypy: no issues in two source files,
  `/tmp/trex-derived-cost-mypy-final.log`.

Full repository, integrated mutation and M0 qualification belong to the final
combined head. Focused results do not establish pricing coverage, corpus
revalidation, broker account readiness, exact economics, or deployment.
