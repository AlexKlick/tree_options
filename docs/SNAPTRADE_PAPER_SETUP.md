# Dedicated Alpaca Paper setup and read-only qualification

This phase provisions a private local template and qualifies observations of a
dedicated Alpaca Paper account. It grants no mandate, consumes no effect permit,
and submits, cancels, replaces or manually refreshes no broker orders or state.
It does not register SnapTrade users or create brokerage connections.

## Operator setup

Connect a dedicated **Alpaca Paper** account through your SnapTrade customer
dashboard/connection portal. Keep it separate from the supervised IBKR options
desk and from any live account. Prefer a read-only connection for this phase.
SnapTrade registration and connection creation are operator provisioning steps;
the TREX qualification command only reads an already connected account.

Create the blank template, without contacting SnapTrade:

```bash
uv run python -m tree_options.trex.snaptrade_qualification \
  --config /home/alexk/.config/trex/snaptrade-paper.json init-binding
```

The command exclusively creates a mode-0600 file and refuses to overwrite any
existing file. Newly created parent directories are mode 0700. Populate the file
locally with `credentials.client_id`, `consumer_key`, `user_id`, `user_secret`,
and the exact provider `binding.account_id` and `paper_confirmed_by`. Keep the
canonical alias `alpaca-paper-canary`, `brokerage_slug` `ALPACA-PAPER`,
`environment` `broker_paper`, and `live_money` false. Empty placeholders are
invalid. No credential values belong in chat, Git, action graphs or UI payloads.

Validate the private file offline:

```bash
uv run python -m tree_options.trex.snaptrade_qualification \
  --config /home/alexk/.config/trex/snaptrade-paper.json check-config
```

This validates file ownership, regular-file identity, private permissions and
structure. Symlinks and nonregular files are refused. It builds no SDK client
and cannot verify the external account or connection.

## Read-only qualification

Use explicit state and shared ownership roots, avoiding ambient HOME-dependent
paths in agent shells:

```bash
uv run python -m tree_options.trex.snaptrade_qualification \
  --config /home/alexk/.config/trex/snaptrade-paper.json \
  --state /home/alexk/.local/state/trex/snaptrade-paper \
  --ownership-root /home/alexk/.local/state/trex-account-owners qualify
```

The existing runtime's `read-only` command delegates to the same qualification.
Exit 0 means the observations qualified at the recorded instant; exit 2 means
blocked. Missing configuration produces an explicit blocked receipt without
provider contact. A readable cached response is not sufficient.

Qualification reads account details, the matching connection detail, balances,
positions and recent orders (`state=all`, `days=7`). It requires:

- Exact account identity, affirmative provider `is_paper=true`, Alpaca institution
  and matching `ALPACA-PAPER` connection.
- The account's connection explicitly enabled (`disabled=false`), permission
  `read` or `trade`, and both freshness modes `realtime`.
- Affirmative holdings availability, completed initial sync, timezone-aware sync
  timestamp, and all observation timestamps within the existing 30-second policy.
- Complete shaped account facts, finite cash/position quantities, coherent
  cumulative order quantities, known order states and identities.
- Provider request IDs, exclusive alias **and provider account** locks, and clean
  reconciliation of existing TREX journals before progression.

Current source checks are deliberately conservative: real-time reads do not
prove that an old scheduled holdings-sync timestamp is fresh. An old timestamp
remains blocked pending validation of actual Alpaca Paper behavior; do not
rewrite it to the local receipt time or silently relax the threshold.

SnapTrade's [connection contract](https://docs.snaptrade.com/reference/Connections/Connections_detailBrokerageAuthorization)
defines independent institution and SnapTrade freshness modes. Either delayed
mode means cached/delayed data. Its [orders contract](https://docs.snaptrade.com/reference/Account%20Information/AccountInformation_getUserAccountOrders)
also permits disabled connections to return cached orders. [Manual refresh](https://docs.snaptrade.com/docs/syncing)
can incur charges and change provider state; this phase never invokes it.

## Receipts and ownership

`read-only-qualification.json` records the latest attempt. Content-addressed
historical receipts live under `qualification/`. They contain request IDs,
digests, row counts, capture/sync times, findings, owner epoch, resolved state and
ownership roots, and a hash of the provider account identity. Raw provider
metadata and credentials are excluded. Account snapshot persistence also keeps
observation references rather than arbitrary response bodies.

The receipt expires 30 seconds after its **oldest** observation. A one-shot
qualification holds both locks during assessment and releases them before
returning. `owner_held_at_assessment` proves only that interval;
`owner_released_after_assessment` explicitly denies an ongoing lease. Every
runtime must use the same shared ownership root; different roots cannot provide
cross-runtime exclusion. Different aliases for the same SnapTrade account are
fenced by the second lock on the provider account ID.

The state directory also has a process-lifetime lease and a persistent account
binding, checked before recovery. Another account cannot share or later rebind
that directory. Populated legacy execution roots without a verified binding are
refused; they require a separately reviewed migration. Passing an already active
runtime object to the qualification helper is refused without releasing it.
Release publication happens while ownership is held; failed
contenders do not overwrite an active runtime's projection. Historical receipt
filenames hash their exact canonical bytes, identical writes preserve the file,
and conflicting bytes cause refusal before the latest pointer changes.

A receipt is not execution authority, a fill ledger, or proof of exact external
economics. `orders_authorized`, `exact_economics`, and `live_money` remain false.
Positions retain source quantities for validation; no fractional OrderIntent is
created. Existing holdings/working orders still block the isolated canary's risk
preflight. A read-only connection cannot cross the canary effect boundary.

## Remaining external dependencies

As of this phase, the operator reports the account is not connected. External
qualification is blocked on operator provisioning and private binding. No real
provider requests, orders, mandate grants, runtime installs or restarts have
been exercised by the implementation agent.

After a connected account positively qualifies, the next implementation boundary
is an authoritative timestamped quote source and an explicitly approved exact
one-order paper mandate. Stable fill identities, prices/event times, fees and
correction semantics remain a separate unvalidated evidence dependency.
