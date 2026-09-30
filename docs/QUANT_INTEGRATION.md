# TREX quant integration

This branch integrates the finalized build kit into TREX research, execution,
supervised runtime, action-model and cockpit owners. It does not grant any
mandate, contact a broker, install a service, submit an order or enable live money.
Repository checks, external read-only account verification, an authorized paper
canary and exact fill economics are separate qualification boundaries.

## Source custody and baseline

- Supplied ZIP SHA-256: `849bc32499895d2a61f842b347749ed03bcbc4cc8058285ae7923824c6896f22`.
- Reviewed kit baseline: `f092e3efb30d50c377bd5dcbc9ea53f1705511eb`.
- Initially inspected clean canonical HEAD: `3fd315bafd54b3509319ffd58dcd929042fc80a0`.
- Integration rebased onto recorder merge: `b70bf2f59a3a6d0b64b1a3c67bf0322ecfeed3d4`.
- Initial lock SHA-256: `7de2811888cb2d75ed18047b0510558196e3618fd18203e713fbfeca0ba76d88`.
- System Python: 3.12.3; isolated uv Python: 3.12.13.
- Initial repository baseline: 5584 passed, 42 skipped, captured in
  `/tmp/trex-quant-baseline-pytest.log`. This precedes the recorder merge and the
  expanded default dev group. It is not the final integration result.
- Kit isolated validation: 20 hermetic tests passed, syntax and schema/apply
  checks completed. The standalone 7-test SDK harness is historical kit
  evidence; integration tests exercise TREX's real lifecycle instead.
- Original kit validation attempts failed from namespace shadowing and a missing
  isolated numpy dependency; those attempts remain in the `/tmp/trex-quant-kit-*`
  logs. Final isolated validation is
  `/tmp/trex-quant-kit-validation-complete-env.log`.

The canonical checkout was preserved. Its operational context and changing host
observations are recorded in [QUANT_OPERATIONAL_CONTEXT_20260929.md](QUANT_OPERATIONAL_CONTEXT_20260929.md).
No historical GO verdict is reused as current execution authority.

## Ownership and integration decisions

| Question | Decision and source evidence |
| --- | --- |
| Calendar for Massive daily bars | `data/massive_equities.py` requires TREX `time.calendar.SessionCalendar` and `time.sessions.session_close_instant`; no UTC-midnight fallback. Momentum/HQM bind exact session endpoints and calendar hash in `research/quant.py`. Missing endpoint prices are exclusions. |
| Historical value reconstruction | The Research Lab feature boundary should reconstruct ratios from filing-dated statements plus verified PIT prices/shares. `data/massive_equities.py::historical_financials` retrieves admissible statements; no complete share/enterprise-value reconstruction has been validated. `robust_value_5metric/v1` remains DATA_GATED. The latest-only ratios endpoint is explicitly refused. |
| Strategy/version persistence | `research/contracts.py::StrategyDefinition` owns definitions; `research/catalog/quant.py` is the single static catalog. `strategy_lab/catalog.py` and the original definition import are compatibility reexports. `research/quant.py::register_version` persists immutable versions/config/code/lock identities in the existing `research/runstate/store.py`. Frozen snapshots, experiments, comparisons and provenance use that same integrity-checked store and `EvidenceEnvelope`. |
| Equity funded ledger | `research/quant.py::execution_for_funded_replay` first calls `execution.evidence.assess_evidence`, then adapts admitted fill records into existing `research.comparison.funded.TradeExecution`. No separate equity ledger. Broker snapshots remain refused. |
| SnapTrade snapshot time | Only provider `time_updated` is used as broker snapshot time in `execution/snaptrade_adapter.py`. Placement, execution and receipt timestamps are not interchangeable. This mapping follows the generated contract; its behavior for an actual Alpaca Paper connection has not been externally validated. Missing times fail closed. |
| Stable fill and fee source | No source has been validated for stable fills, execution event times, fees and corrections. `SnapTradeProvider.activities()` preserves provider observations only; it is not an exact fill mapper. No PartialFill/CompleteFill is inferred from snapshots. |
| Fractional canary | Integer shares only. Fractional quantities are refused by parser, snapshot contract and supervised effect. Fractional support requires a separately reviewed execution-domain change. |
| Ownership fence | `trex/account_ownership.py` holds a process-lifetime flock on a hashed account alias and records epoch/pid. SnapTrade and `supervised_desk.main` share the root (`TREX_ACCOUNT_OWNERS`). IBKR defaults to managed account identity; an operator may bind `TREX_IBKR_ACCOUNT_ALIAS` only when it owns exactly one account. The same actual account must use the same alias across runtimes. Browser/LLM processes hold no execution lease. |

## Research behavior

Equal-weight, 12-1 momentum and HQM produce scores and proposed normalized
weights; clustering is deterministic and exploratory. Value, sentiment and
GARCH/intraday remain data-gated lanes. PIT facts cannot precede their event or
exceed a decision cutoff. Historical universe date is bound explicitly, metadata
is recursively immutable, conflicting price facts are refused, missing inputs
are exclusions and an empty eligible set produces no signal. A campaign proposal
binds a control/challenger comparison to common snapshots and calendar identity;
it is neither a scientific promotion verdict nor execution authority.

Evidence kinds distinguish deterministic replay and simulated execution from
broker paper. Frozen-input score/target comparisons are persisted without
inventing a NAV curve or realized performance. Exact execution attribution uses
the existing evidence assessor, including its fee and reconciliation refusals.

## Provider and execution boundary

`execution/snaptrade_provider.py` pins `snaptrade-python-sdk==13.0.28` after
inspection of the actual generated methods. Tests call the real generated
methods with only the urllib3 transport mocked. Retries are disabled. Generated
synchronous signatures omit timeout, so the shared SDK call boundary enforces a
10-second deadline. The SDK signs pre-serialized Decimal bodies incorrectly;
the local compatibility shim signs the native JSON body already serialized by
the SDK, preserving the exact wire numbers. It does not replace authentication
or HTTP transport. Integer quantity and float limit serialization must preserve
TREX values before effects.

The actual Account contract uses `is_paper` and `institution_name`. A binding
must be operator-labelled ALPACA-PAPER, and provider readback must affirm
`is_paper=True`, institution Alpaca and the exact account ID. Production/missing
paper flags fail closed. Credentials are read only from a private 0600 server
file, never from strategy configs, browser data or evidence envelopes.

Provider operations cover account details/balances/positions/orders, lookup,
quotes, activities and internal submit/cancel/replace calls. Only bounded submit
is currently wired through the supervised effect permit; cancel/replace remain
internal provider capabilities pending reviewed domain effect contracts. No
browser endpoint exposes them. Unknown/contradictory order facts fail closed;
provider order identity reuse is refused; cumulative readbacks never create
fills. `UncertaintyObserved` extends the execution domain for unknown outcomes
that do not faithfully mean timeout or disconnect, while those specific transport
facts retain `TimeoutObserved`/`DisconnectObserved`.

Primary contract references:
[SnapTrade generated Python SDK](https://github.com/passiv/snaptrade-sdks/tree/master/sdks/python),
[account details](https://docs.snaptrade.com/reference/Account%20Information/AccountInformation_getUserAccountDetails),
[order details](https://docs.snaptrade.com/reference/Account%20Information/AccountInformation_getUserAccountOrderDetail).
External contract behavior remains unverified here.

## Paper canary and recovery

The independent `trex.snaptrade_runtime` owns the lease and uses existing
`trex.supervised` mandates, permits, consume-before-effect outbox, append-only
execution journal, reconciliation and revocation. No scheduler, browser or LLM
owns submission. A canary requires explicit operator approval, exact alias and
epoch, verified paper account, a current whole-account snapshot, current
risk/reservation, a timestamped quote, one order, at most USD 100, mandate TTL at
most 900 seconds and permit TTL 15 seconds. Existing holdings/working orders
block this initial exercise. SDK quotes lack an authoritative event timestamp;
a verified timestamped quote source must be injected. No receipt timestamp is
substituted for it. Default canary calls refuse before any effect.

Restart loads durable claims/journals, reads provider state and reconciles first.
An orphan with an exact durable claim can restore the claim-time submit marker
and explicit uncertainty; absent/unreadable claims remain held. Missing order
lookups never prove not-submitted. Duplicate identical readbacks keep their first
receipt timestamp. Unknown/stale state blocks readiness. Account observations,
order identity bindings and graph edges are persisted. `close()` republishes
released ownership; independent CLI halt revokes authority without broker/LLM
availability. Halt does not pretend to cancel existing orders.

Read-only operator interface (credentials are absent in this session):

```bash
uv run python -m tree_options.trex.snaptrade_runtime \
  --config /private/snaptrade-paper.json --state /private/trex/snaptrade-paper read-only
```

Use `run` for the independent read/reconcile loop, `recover` for a recovery probe,
and `halt` to revoke a paper mandate. None of these commands submits an order.
No service has been installed or restarted by this implementation.

## Action model and cockpit

`action_graph/research-to-broker-paper.plan.json` substitutes the provider behind
the same governed effect guards as deterministic paper. It remains a validated
design fixture with execution_authorized=false; actual effects use the existing
supervised kernel rather than treating the JSON example as authority. Durable
runtime edges name proposal, authorization, risk reservation, permit, effect,
observation, reconciliation and evidence.

`GET /api/research/quant` projects the integrity-checked RunstateStore and runtime
artifacts; `#/quant` displays strategy/version, cutoff/universe, scores/ranks,
targets, exclusions, control comparison, mandate, risk, order/reconciliation and
evidence quality. Persisted campaign links connect research run, frozen snapshot
and target identity to an operational canary intent. The operational strategy is
named separately; incomplete broker economics remain refused for attribution.
The UI distinguishes BACKTEST, DETERMINISTIC REPLAY, SIMULATED EXECUTION, BROKER
PAPER and LIVE, and supplies no broker controls. Stale/invalid projections refuse
readiness. Generated static assets were built only in the isolated worktree;
they have not been deployed to the canonical/live cockpit.

## Validation custody

The default dev group now includes existing TREX/trex-web dependencies so the M0
mutation selectors exercise cockpit and supervised IBKR compatibility rather
than silently skipping optional imports. Existing M0 formatting/lint failures
were repaired mechanically. All 509 existing mutant IDs, owning selectors and
invariants were retained; 15 anchors were re-pinned by comparing equivalent
formatted mutant source, and 12 quant mutants were added. No gate was disabled,
loosened or removed. Full command logs are captured, including failed iterations.
The final M0 receipt and engineering handback belong in ignored `artifacts/`,
bound to the exact committed source HEAD; this document does not predict their
results.

## External qualification still required

1. Provision a private SnapTrade binding and positively identify the dedicated
   Alpaca Paper account through the read-only interface; validate entitlement,
   freshness, request IDs, account observations and shared alias ownership.
2. Verify Alpaca `time_updated` semantics and a timestamped current quote source.
3. Have the operator authorize the exact one-order, USD100, short-TTL paper
   mandate; demonstrate submit/readback/reconciliation and halt/restart under it.
4. Validate a stable execution source with fill identity, quantity/price/event
   time, fee and correction/cancel semantics before exact economics mapping.

Live money remains disabled. Historical value reconstruction and other data-gated
lanes remain explicit data dependencies, not completed experiments.
