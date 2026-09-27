# TREX governed action integration status

Source packet: `TREX-Governed-Adaptive-Action-Model-20260926-9472c2.zip`,
SHA-256 `826bae1fb666224e2eb5d378df3e33c7c7fb6939b4ff6a5554eeb267c0bfe78b`.
The extracted packet is preserved in this directory with its own checksums.
It was written against `c4035f6`; this worktree started at `6347c3c`, which
also includes the RL-3b Outlook work. The packet's static fixtures are not
runtime operation registrations or an authority source.

## Installed in this worktree

- `tree_options.action_graph.proposal` checks the packet's synthetic plan
  against its schema, fixture operation registry, hashes, dependency DAG and
  paper-effect guard declarations. The package copies the schema and examples
  so an installed cockpit can serve the exact fixture.
- `GET /api/action-model/example` returns only the checked synthetic plan and
  a receipt that says execution is unauthorized. There is no action-model
  POST, grant, dispatch or broker import on this route.
- The Action model cockpit page links the proposed steps and inspector, and
  shows the existing account *snapshot* separately from the synthetic plan.
- `CapitalProfile` separates intended deployable capital from paper account
  equity, and `review_candidate` fails closed on missing explicit loss limits,
  objective-specific outcome evidence, strategy scope, reconciled exposure, package risk,
  assignment plan and broker margin. An empty blocker list means reviewable only.
- `review_canary` screens a manually specified, one-package operational paper
  canary against fresh account and quote facts, legacy-flat status, exit
  readiness and the authored capital limits. It does not issue a permit.
- Historical replay now records per-row eligibility; a separate frozen
  portfolio scenario applies modeled $5,000/$300/$1,500 overlap limits and
  publishes read-only summaries to the cockpit. It cannot test the $300
  realized daily stop from daily bars.

## Existing owner paths to reuse

| Concern | Current owner | Required change before governed paper entry |
|---|---|---|
| Login and paper account observation | `deploy/trex/docker-compose.yml`, `trex.gateway_watch`, `trex.ibkr.IbkrTrex.account_snapshot` | Bind every decision to a fresh broker-reported account ID and verify paper environment at dispatch. Do not create a second login loop. |
| Exit protection and ownership | `trex.monitor`, `trex.exit_watch`, `trex.enter` | Prove exclusive owner/epoch at the send boundary; retain protection independent of the planner. |
| Desk selection | `desk.miner`, `desk.rails`, `desk.enter` | Convert validated queue rows to exact trade intents only after strategy, margin, account, quote, and reconciliation contracts are complete. `desk.enter` currently refuses non-shadow execution. |
| Existing execution facts | `execution.records`, `execution.lifecycle`, `execution.paper` | Keep their intent, attempt and broker-observation IDs distinct; implement a durable outbox and an IBKR adapter around the same facts. The current `execution.paper` is deterministic simulation, not IBKR paper account execution. |
| Volatility forecasting and modeled trade outcomes | `desk.har`, `desk.evaluate`, `desk.pricing`, `desk.scorecards` | `FORECAST-001` passed a pre-registered realized-variance forecast comparison. That does not verify a trade win probability or live execution quality. Link its exact result and the trade-level scorecard to proposals without turning either into a grant. |
| Research result and chart | `research_view`, `ResearchPage`, RL-3b Outlook | Project immutable source/result IDs into the action graph. RL-3b evaluates index-level quantile forecasts and claims no calibration; it is not the desk's volatility-trade win-rate oracle. |

## Paper path still required

1. Author a capital profile with separate account balance, intended strategy
   budget, objective, horizon, exact per-trade/open/day loss caps, allowed
   strategy versions and instrument scope. The future capital target is a
   budget *input*, not a reason to size from the paper account's buying power.
   Operator-stated numbers remain proposed inputs until a complete profile
   and mandate are recorded outside the source tree. The operator favors
   repeatable small wins in a volatility trading algorithm, with occasional
   exceptional payoff at limited risk. The minimum supported trade win rate,
   exception reward/risk tiers, day loss cap, horizon, exact strategy versions
   and eligible instruments remain unspecified. A volatility forecast's QLIKE
   result cannot be substituted for a measured trade win rate. Reward/risk
   can reduce allowable trade loss below the hard cap but never raise it.
2. Add a trusted mandate service with authenticated operator decision,
   expiry/revocation, policy revisions and account/owner epochs. A profile
   label or GUI approval badge is insufficient.
3. Persist intent, risk reservation and outbox before any external send. The
   broker owner must consume a single-use permit against current account,
   owner, policy, price/contract and risk snapshots at the send boundary.
4. Reconcile unknown submission, partial fills, corrections, cancels and
   assignment risk before retries or new exposure. A timeout cannot create
   a second logical effect.
5. Pass deterministic broker fault cases, account/ownership recovery and
   GUI/ledger parity checks. Then conduct one separately reviewed supervised
   IBKR paper cycle on a concrete order and report the broker receipts.
6. Only after that, evaluate adaptive candidate selection against a frozen
   baseline with complete version/review history. Paper fills alone do not
   establish a profitable strategy or suitability for a future live account.

No current code in this worktree enables governed paper entry. The existing
legacy `trex.enter` behavior is outside this action-model change.

## External risk references

IBKR states paper fills and complex orders can differ from production:
https://www.ibkrguides.com/brokerportal/aboutpapertradingaccounts.htm .
FINRA explains that options spreads have complex risks, including assignment:
https://www.finra.org/investors/investing/investment-products/options .
