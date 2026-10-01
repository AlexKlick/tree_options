# Repeatable theory campaigns

This phase adds an equity research campaign to TREX's existing catalog, Research
Lab store, trial registry, GEPA Pareto mechanics and funded FIFO ledger. The
options desk remains the execution owner for its accounts. Research runs produce
proposals requiring review; every execution and live-money flag remains false.

## What can be used now

`research.quant_campaign` evaluates supported equity configurations on frozen,
common chronological periods. It records each attempted configuration before
its outcome, charges declared costs, persists results and provenance, and resumes
deterministic computations without consuming another configuration slot.

The current objective is **independent next-session open-to-close experiments**.
Each period starts with the same cash, sizes whole shares, pays five basis points
per side plus registered slippage, and closes its inventory at that session's
calendar close. It reuses `comparison.funded.run_funded_account` and the existing
FIFO ledger and conservation oracle. A mean of these independent returns is not
a compounded wallet curve or a monthly momentum strategy's holding-period return.
Endpoint loss is not intraday drawdown. Aggregate turnover is a mean per period;
fees are a total across periods.

Equal weight, 12-minus-1 momentum and HQM can enter the bounded configuration
search. Value, sentiment and intraday/GARCH data gates remain intact. This phase
does not assert a newly discovered profitable strategy.

## Research graph and selection

The persisted graph is executable research provenance, following:

```text
freeze full input/spec/calendar/protocol/engine/proposer identities
 -> register configuration in the existing trial registry
 -> PIT score/target -> modeled next-open executions -> FIFO conservation
 -> training metrics -> bounded reflective configuration proposals
 -> freeze candidate pool -> common validation -> freeze winner
 -> sealed holdout against the same control -> review proposal
```

The LLM receives training metrics and diagnostics only. It cannot change strategy
code, the eligible catalog, data gates, fees, capital policy, the order boundary,
the registered objective or the holdout. Callback feedback is detached from the
mechanically evaluated configurations. Validation never feeds reflection; no
holdout outcome selects the winner. All labels must end strictly before the next
decision cutoff, including across split boundaries. Incomplete candidate coverage
cannot rank on its successful subset. An incomplete control blocks selection.

The existing `desk.gepa.pareto_front` is reused with return, negative endpoint loss
and turnover objectives. The registered scalarization selects among the frontier:
mean net return minus risk penalty times endpoint loss minus turnover penalty
times mean turnover. Defaults are 0.25 and 0.001; coefficients are bounded at
1000. The budget is 2–32 distinct configurations, including control, with at most
four reflection generations. Failed/incomplete configurations retain their slot.

GEPA's reflective evaluator/proposer approach and separate test-set concept are
described in the [official GEPA API](https://gepa-ai.github.io/gepa/api/optimize_anything/optimize_anything/)
and [original research paper](https://arxiv.org/abs/2507.19457). TREX implements a
bounded GEPA-style configuration seam through its existing owners; it does not
add a GEPA SDK dependency or claim that agent-benchmark gains imply trading alpha.

This graph describes the workflow and evidence lineage. Factor/correlation
network strategies are a separate research hypothesis requiring qualified input
panels and a registered experiment; a provenance graph alone creates no alpha.

## Offline acceptance example

From a clean committed checkout, the following commands produce **synthetic
machinery evidence**, including a concentration challenger and equal-weight
control. No provider is called.

```bash
uv sync --locked
uv run python -m tree_options.research.quant_campaign_io make-fixture \
  --output /tmp/trex-quant-example.json \
  --calendar data/calendar/trex/nyse_sessions_2018_01_02_2028_12_29.json \
  --calendar-checksum data/calendar/trex/nyse_sessions_2018_01_02_2028_12_29.sha256

host-test uv run python -m tree_options.research.quant_campaign_io run \
  --input /tmp/trex-quant-example.json \
  --workspace artifacts/theory-example \
  --calendar data/calendar/trex/nyse_sessions_2018_01_02_2028_12_29.json \
  --calendar-checksum data/calendar/trex/nyse_sessions_2018_01_02_2028_12_29.sha256

uv run python -m tree_options.research.quant_campaign_io inspect \
  --workspace artifacts/theory-example
```

The fixture writer refuses to overwrite. Choose a new filename and workspace for
a genuinely new experiment. Never delete old receipts to make a rerun fit.

`make-fixture` documents the actual input shape. For real research, provide
`data_class: user_supplied_unqualified` with historical universe/source hashes,
PIT observations (`available_at >= event_at`, both at or before the decision),
explicit next-session outcome prices, exact code SHA and dependency-lock hash,
and declared train/validation/holdout periods. Numeric monetary values are Decimal
strings. Duplicate JSON keys, unknown fields, derived-semantic overrides, boolean
budgets and nonfinite numbers refuse loading. Preserve original vendor manifests;
source-hash presence is not independent source qualification.

## Explicit model-assisted search

Append `--reflect-glm53` to `run` to authorize up to the registered number of
server-side research calls through the existing TREX Z.ai transport. The model
is explicitly **glm-5.3**, with no Flash substitution or provider fallback.
Existing private server environment bindings supply the credential. Never put
credential values in the input JSON or browser. The response must report the
same model and a stable response ID; contradictory duplicate envelope fields
are refused. Only the model names, response ID and response digest are persisted.

A reflection claim is persisted before the call. Timeout, missing response,
malformed output or interrupted publication leaves an uncertain claim. Resume
does **not** call the provider again. Inspect the retained claim before starting
a new campaign with a fresh unexposed holdout. Published reflection outputs are
reused deterministically on restart. The exact prompt/decoder boundary is covered
by the campaign engine source identity.

The default CLI performs no LLM calls. The Python proposer seam supports hermetic
or explicitly controlled proposal sources under a pinned identity. It is a
trusted server integration seam; it does not sandbox arbitrary Python callbacks.

## Persistence and cockpit

One campaign owns one research workspace under an exclusive process lock. The
operator can touch `<workspace>/STOP` to halt progression at a candidate,
reflection or holdout boundary independently of the browser or LLM. Removing it
and repeating the same command resumes from durable artifacts.
The existing `RunstateStore` verifies content/audit integrity before resume. The
binding includes the full spec, all period data, calendar content, frozen
protocol, relevant engine modules and proposer identity. Changed input or code
requires a new workspace; incomplete terminal holdout results stay immutable.
No revised data is inserted under an old evaluation identity.

`GET /api/research/quant` and `#/quant` project persisted campaign aggregates,
control comparison and graph edges. Set `RESEARCH_WORKSPACE_DIR` to the selected
campaign workspace when starting a separate cockpit instance. GET does not run
the optimizer, own a broker session or grant a mandate. Source APIs and component
tests are distinct from browser/deployment proof. The existing running cockpit
is not automatically redirected to this branch.

All campaigns here are exploratory retrospective research. Changing seeds,
penalties or hypotheses after reading outcomes is another searched configuration,
not an independent confirmation. Reusing an exposed holdout invalidates that
claim even in a new workspace. Trial budgets are software commitments within the
workspace; they do not establish independent preregistration, custody of human
knowledge, or a global budget across unrelated workspaces.

## Options continuation and operational migration

The existing `desk.longrun` remains the paired options replay owner. This phase
binds full board row/context bytes, policy identities, source/config metadata and
loaded bundle/outcome-table bytes before reusing receipts.
Shipped scoring, outcome, rule and policy helper source bytes and Python/NumPy
runtime versions are also bound;
changed engine code requires a new run directory. Contradictory duplicate
outcome identities refuse loading. Legacy plans without the custody marker are
preserved and refused for adoption; use a new run directory after code promotion.
Direct Python custom-rule callers must explicitly bind their rule parameters in
metadata; callable closure identity is not inferred.

Whole board row IDs containing `+` retain their single-row meaning. Package
interpretation is bound to the frozen board membership; conflicting cached
interpretations refuse adoption. Pair completion requires both exit times to be
known aware instants. A missing exit remains unknown; contradictory or malformed
timestamps refuse ordering instead of falling back to a lexical comparison.

Do not replace a running long-run process's files or upgrade the supervised
options desk to demonstrate these checks. This branch is isolated. Its source
changes, fixtures, model-review calls, external account verification and runtime
deployment are separate evidence lanes.

## Next production research boundary

Before claiming market strategy performance, qualify a PIT equity dataset with
historical listings/delistings, corporate-action-consistent raw/adjusted prices,
publication timing and outcome coverage. Then add a separately registered
multi-session funded rebalance schedule, cost sensitivity, purged walk-forward
folds and statistical/multiple-search controls on fresh holdout windows. The
existing ledger is the accounting owner. Do not extrapolate the one-session
experiment into a compounded monthly wallet.

Options campaigns can use the existing paired long-run evaluator after their
minute data, context timing, option existence and outcome-table sources are
qualified. Keep its historical evidence and banned strategy-family constraints.
Graph factor hypotheses should enter the same experiment/evidence path.

The dedicated Alpaca Paper account is still unconnected. Follow
[SNAPTRADE_PAPER_SETUP.md](SNAPTRADE_PAPER_SETUP.md) for the separate read-only
qualification gate. Neither research selection nor GEPA output enables the
one-order canary, exact external fill economics or live money.
