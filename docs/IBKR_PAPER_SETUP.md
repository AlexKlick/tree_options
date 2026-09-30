# IBKR Paper: read-only account qualification

This path qualifies account observations through the **existing supervised desk owner**.
It does not connect a second IBKR client, grant a mandate, submit/cancel/replace an order,
activate a service, or implement equity execution. The repository implementation and fake
SDK tests are complete; no real IBKR qualification was run during this work.

## Authoritative ownership and provider contracts

- `trex/supervised_desk.py` creates client 83 once, acquires `DeskRuntime`'s process lock,
  holds the account-alias `AccountOwnership` fence, and writes the desk epoch/PID to
  `owner.json`. Supervised entries and protective exits share this owner/session.
- `trex/supervised_ibkr.py:IbkrSupervisedBroker.paper_blockers()` is reused unchanged:
  connected session, paper gateway port, supervised client ID, managed account and
  existing IBKR paper-account prefix rule. A qualification requires a non-account-number
  alias and exactly one managed account, matching the existing startup alias contract.
- `IbkrTrex.account_snapshot()` reads cached values, locally stamps their observation
  time and can combine tags across multiple accounts. It is useful for its existing
  consumers but is deliberately insufficient for this stronger qualification gate.
- `trex/ibkr_paper_qualification.py:qualify_read_only()` accepts the current
  `SupervisedDesk` and its **already-held** account fence. It verifies session object
  identity, process lock, owner PID/client/epoch and account fence before and after reads.
  It never acquires/releases these leases or starts another runtime.

The installed `ib_async` methods were inspected and exercised with actual SDK wrapper
callbacks and completion futures against fake client methods. `reqAllOpenOrdersAsync`
updates the existing trade cache without clearing it or binding foreign orders;
`reqPositionsAsync` waits for `positionEnd`; `reqCompletedOrdersAsync(apiOnly=False)`
waits for `completedOrdersEnd`. Completed orders cover the current gateway session;
this is not proof of all prior-day history. Concurrent existing requests with these
SDK completion keys refuse qualification instead of overwriting their futures.

The SDK's public `accountSummaryEvent` discards the request ID. The qualifier therefore
uses a unique, temporary summary request and temporarily intercepts the SDK wrapper's
actual `accountSummary(reqId, ...)` callback on the **owner's serialized thread**. Every
callback is delegated to its original handler; only rows carrying this request's ID
enter qualification. The prior callback is restored in `finally`, and only this new
summary subscription is canceled. Existing owner subscriptions and caches remain intact.

Each read is bounded to five seconds and must prove request completion. Balances require
fresh request-bound finite USD values for net liquidation, cash and buying power.
Positions/orders are selected for the exact configured account. Unknown order states,
contradictory quantities, duplicate identities and previously known working orders
missing from the response fail closed. Canceled terminal snapshots with incompatible
quantity fields can remain BLOCKED; no missing cancellation semantics are invented.
Fractional position observations may be represented as decimal strings, but this does
not make the existing options execution effect contract support fractional equity orders.

## Operator setup: configure the existing owner, not another process

1. Identify the currently deployed supervised desk checkout, its owner state root,
   account fence root and paper gateway configuration from the owning service's private
   configuration. Preserve the explicit state roots: ambient agent HOME can resolve
   different files. Do not infer freshness from old drill-check transcripts or copies of
   another runtime's `account.json`.
2. In the existing owner's private configuration, select one managed IBKR Paper account
   and a non-secret alias such as `ibkr-paper-primary`. The existing
   `TREX_IBKR_ACCOUNT_ALIAS` must match this alias and `TREX_ACCOUNT_OWNERS` must retain
   the shared fence root used by every execution provider. Multiple managed accounts
   require a deliberately reviewed selection/ownership contract before this alias path
   can qualify; do not change an account binding silently.
3. Choose a private output directory **outside** the desk, supervised and fence roots,
   for example `/home/alexk/.local/state/trex-ibkr-qualification`. Neither an ancestor nor
   a descendant of those live owner roots is accepted. The configured Paper Workspace
   catalog's `state_root` points to this receipt directory, not the live desk directory.
4. At a separately authorized owner deployment/normal startup, append these arguments
   to that existing owner's `run` command (both are required):

   ```text
   --qualification-receipt /home/alexk/.local/state/trex-ibkr-qualification
   --qualification-account <exact private IBKR Paper account ID>
   ```

   This hook runs once after ownership and epoch establishment and before the existing
   desk loop. Default startup behavior is unchanged. It neither grants fresh entry
   authority nor changes the exit owner's behavior. A qualification failure is logged
   by exception class/verdict only and cannot take the protective exit loop down.
   **Do not launch a parallel owner or restart a running owner merely to refresh a badge.**
5. Alternatively, an explicitly authorized future owner control handler can call
   `supervised_desk.qualify_owned_account(desk, held_fences, ...)` on the same serialized
   owner thread. This release does not add an IPC request handler, recurring scheduler
   or browser-controlled IBKR session. No such control hook is externally exercised.

The one-shot receipt expires within **30 seconds of the oldest completed read**.
It is an initial setup observation, not continuous health or a continuing ownership lease.
After expiry, an application must display expired/blocked until a new authorized owner
assessment exists. An expired receipt must never be restamped by a copier. A recurring
owner-controlled read path is a separate implementation boundary; repeated restarts are
not its substitute.

## Durable receipt and projection contract

Output: `read-only-qualification.json`; immutable content-addressed history:
`qualification/ibkr/<canonical-content-sha256>.json`. Identical historical content is
idempotent; conflicting bytes at the same identity refuse before the latest receipt
changes. Persistence uses its own output lock and never writes owner state.

Schema: `trex.ibkr.read-only-qualification/v1`, provider `ibkr`. Relevant fields:

- `account_alias`, hashed `provider_account_sha256`, `owner_epoch`;
- local `read_started_at`, final `assessed_at`, oldest-read-bound `expires_at`;
- `verdict`, `paper_verified`, `ownership_verified_at_assessment`, `findings`;
- per-operation `request_id`, local `captured_at`, content `digest`, `row_count` and
  `broker_event_at: null` for balances, positions, open orders and completed orders;
- `orders_authorized: false`, `equity_execution_ready: false`, `exact_economics: false`
  and `live_money: false`, always.

Account numbers, raw balances/order references and raw provider error text are absent
from the receipt. Private output/owner paths are provenance for server-side checks and
must not enter the public workspace DTO. The private catalog may contain
`provider: "ibkr"`, `account_alias`, exact `account_id`, and receipt `state_root`;
the public projection verifies the expected account hash and omits account ID/paths.

A QUALIFIED receipt proves selected account observations completed while the existing
owner and alias fence were held. It does not prove complete historical reconciliation,
fill prices/fees, current portfolio risk permission or equity execution capability.
Balances and order snapshots have **local completion times**, not authoritative broker
business-event times. Exact economics stays false; no fill ledger is inferred.
IBKR equity experiments need a separately reviewed equity effect/domain/provider path
before any paper execution, preserving the existing options owner and exit lifecycle.
SnapTrade remains a separate supported provider with its own dedicated account gate.

## Validation receipts

All test output was fully captured under `host-test`; no live session or provider call
occurred. The fake harness uses actual installed IB/SDK callbacks and futures.

| Capture | Exact result |
|---|---|
| `/tmp/trex-ibkr-qualification-red.log` | 6 failed, 17 passed, 1 warning: foreign callbacks, assessment time and overlapping output regressions |
| `/tmp/trex-ibkr-qualification-hook-red.log` | 2 failed, 2 passed, 23 deselected: alias ambiguity and missing owner hook |
| `/tmp/trex-ibkr-qualification-green-v3.log` | 159 passed, 0 failed, 0 skipped, `-Werror`, 0.82 seconds |
| `/tmp/trex-ibkr-qualification-composed-red.log` | 1 failed, 28 deselected: actual writer's four observations refused by old three-observation reader |
| `/tmp/trex-ibkr-qualification-identity-red.log` | 3 failed, 29 deselected: raw account alias persistence and missing row account identity |
| `/tmp/trex-ibkr-qualification-casefold-red.log` | 1 failed, 1 passed, 31 deselected: lowercase account identifier alias |
| `/tmp/trex-ibkr-qualification-final-v5.log` | 192 passed, 0 failed, 0 skipped, `-Werror`, 1.78 seconds |
| `/tmp/trex-ibkr-qualification-ruff-final.log` | All checks passed |
| `/tmp/trex-ibkr-qualification-mypy-final.log` | Success: no issues found in 2 source files |

The final focused suite includes qualification, supervised desk, desk runtime,
drill-check and Paper Workspace. A composed regression projects the actual owner
receipt through the private catalog, checks QUALIFIED_AT_ASSESSMENT, and verifies
tradeable/equity_execution_ready/owner_held remain false with no private account IDs
or state paths in the public output. Full repository/M0/mutation gates and final commit custody belong to the
parent integration. External account observation, deployed owner hook and equity
execution remain unverified.
