# Dedicated Alpaca Paper setup and read-only qualification

The read-only setup and qualification commands provision a private local
template and qualify observations of a dedicated Alpaca Paper account. They
grant no mandate, consume no effect permit, and submit, cancel, replace or
manually refresh no broker orders or state. They do not create brokerage
connections. Commercial SnapTrade user registration
is an operator provisioning step; Personal API access does not register users.

## Provider sequence: qualify IBKR first

The user selected **both providers, with IBKR qualification first**. Reuse the
existing supervised IBKR paper owner and its broker session. The qualification
hook reads account balances, positions and open orders on that owner's serialized
thread and persists a 30-second read-only receipt. It does not create another
IBKR client, grant a mandate, alter orders or authorize the quant workspace to
execute equities.

At the next reviewed startup of the existing supervised desk owner, retain its
normal plan/configuration arguments and add:

```text
--qualification-receipt /home/alexk/.local/state/trex/ibkr-paper-qualification
--qualification-account <exact-paper-account-ID-configured-locally>
```

Set `TREX_IBKR_ACCOUNT_ALIAS=ibkr-paper-primary` in that owner's reviewed local
configuration and retain the shared account ownership root. The qualification
output must be separate from the live desk, supervised execution and account
ownership state directories. Do not start a second owner or reconfigure a live
service merely to generate a receipt. This implementation installs/restarts no
service; the hook runs once when an explicitly configured owner starts.

Add the private IBKR catalog entry below. The account ID stays in the mode-0600
server catalog; browser projections expose only the alias, provider and safe
qualification status:

```json
{
  "accounts": [
    {
      "provider": "ibkr",
      "account_alias": "ibkr-paper-primary",
      "account_id": "<exact-paper-account-ID-configured-locally>",
      "state_root": "/home/alexk/.local/state/trex/ibkr-paper-qualification"
    }
  ]
}
```

`state_root/read-only-qualification.json` must positively match the private
account ID hash, alias, output root, owner epoch, paper environment and ownership
assessment. Old, future-dated or contradictory receipts fail closed. A valid
receipt is labeled `QUALIFIED_AT_ASSESSMENT`, rather than asserting a continuing
lease from a historical read. The new quant workspace's IBKR rows explicitly
report `tradeable=false` and `equity_execution_ready=false`: they describe this
workspace's unsupported equity execution path, not the capabilities of the
existing supervised options desk.

IBKR aliases may fund virtual sleeves and receive research/deployment proposals
for review. Their proposals cannot be staged or routed into the SnapTrade equity
canary. Halting an IBKR review proposal changes only the workspace proposal and
its safe funding reservation; it does not halt, revoke or alter the established
IBKR options runtime. An actual governed IBKR equity adapter remains a separate
implementation boundary. No new IBKR execution authorization is created here.

SnapTrade/Alpaca Paper remains the alternate provider. Add its existing catalog
entry alongside IBKR when provisioned, preserving distinct aliases and state
roots. Only the SnapTrade operational canary currently has a supported quant
workspace execution engine, and all of its independent qualifications and exact
operator approval remain necessary. Follow the Personal setup below if you
choose to provision that alternate account.

## Alternate SnapTrade Personal setup

For your own account, use a **SnapTrade Personal API key**. Create a Personal
SnapTrade account, enable two-factor authentication, generate its API key and
connect a dedicated **Alpaca Paper** account through the Dashboard or its
Connection Portal. The [current Personal quickstart](https://docs.snaptrade.com/docs/getting-started)
requires only `clientId` and `consumerKey`: do not register a separate user or
send `userId`/`userSecret`. Personal OAuth is currently read-only and cannot
replace the Personal API-key path for this trading runtime.

Commercial applications serving their own end users use a Commercial API key
plus per-user registration and four credentials. TREX supports both explicitly;
a Personal operator does not need to follow the Commercial demo or purchase a
Commercial integration to satisfy this code's credential schema.

Keep the dedicated Alpaca Paper account separate from the supervised IBKR
options desk and from any live account. Prefer a read-only connection for this phase.
Connection creation and, only for Commercial mode, user registration are
operator provisioning steps; TREX only reads an already connected account.

Create the blank template, without contacting SnapTrade:

```bash
uv run python -m tree_options.trex.snaptrade_qualification \
  --config /home/alexk/.config/trex/snaptrade-paper.json \
  --auth-mode personal init-binding
```

The command exclusively creates a mode-0600 file and refuses to overwrite any
existing file. Newly created parent directories are mode 0700. Populate the file
locally with `credentials.auth_mode` set to `personal`, `client_id` and
`consumer_key`, and the exact provider `binding.account_id` and
`paper_confirmed_by`. The Personal credentials object must omit `user_id` and
`user_secret` entirely. If an existing blank Commercial template was created in
a prior phase, edit it locally to select Personal mode and remove those two
fields; rerunning initialization never overwrites it. For Commercial mode use
`--auth-mode commercial`, retain `user_id`/`user_secret`, and populate all four
credential values. Omitting `auth_mode` retains legacy Commercial semantics and
its existing credential fingerprint unchanged. Keep the
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

## Research and paper workspace

The cockpit workspace creates durable research assignments, a funding plan and
reviewable deployment proposals. It does not start a broker session or grant a
mandate through HTTP. Controls are disabled by default and require a direct
loopback connection with matching browser Origin when enabled. Existing remote
read-only cockpit access does not gain mutation authority from forwarding
headers.

The requested funding plan is exactly **19 sleeves of USD 50,000 and 10 sleeves
of USD 5,000**, totaling **USD 1,000,000**. These are virtual experiment funding
reservations, not 29 independently verified brokerage accounts or fill ledgers.
A single account has shared broker positions and cash. Each proposed deployment
reserves its maximum gross notional against its sleeve atomically; repeat
requests cannot double-reserve, and separate plans cannot duplicate the same
million-dollar budget. An unsubmitted proposal can release its reservation on
halt. A claimed, observed or uncertain effect retains its reservation until a
separately implemented reconciliation-based release proves the obligation ended.
Exact per-sleeve external P&L is unavailable without authoritative attributable
fills, prices, fees and corrections.

### What the operator needs to do

1. First qualify the existing IBKR paper account using the owner hook above.
   To additionally provision the alternate SnapTrade path, create or select a
   dedicated Alpaca **Paper** account. For the proposed full
   funding plan, provide USD 1,000,000 of paper buying power/cash through Alpaca's
   paper-account controls. TREX records the intended plan separately and does
   not reset or change the broker balance. Broker cash must actually support
   each approved effect; an allocation plan does not establish broker funding.
2. For your own accounts, create a SnapTrade Personal account, enable 2FA,
   generate a Personal API key and connect the **Alpaca Paper** institution.
   The [current getting-started guide](https://docs.snaptrade.com/docs/getting-started)
   explains the Personal path and its two credentials without separate user
   registration. Select `trade-if-available` in the Portal when you are ready
   for operator-approved paper execution; its default read access only qualifies
   observations. A provider-reported trade permission is still checked locally.
   Applications managing accounts for other end users instead select Commercial
   mode and register each user; the interactive demo describes that separate path. Alpaca's
   [paper-account guide](https://docs.alpaca.markets/us/v1.4.2/docs/paper-trading)
   distinguishes paper accounts and paper API credentials. Do not select the
   live Alpaca integration.
3. Populate the private binding described above locally. Personal SnapTrade
   credentials are its `client_id`/`consumer_key` pair; Commercial mode also
   requires `user_id`/`user_secret`. Both are distinct from Alpaca's own paper
   API key pair. Alpaca credentials belong in its connection flow; TREX reads
   the selected SnapTrade credentials and exact connected provider account ID
   from its private binding. Keep all values
   out of chat and out of the cockpit.
4. Create a private catalog file, mode 0600, for the web projection and local
   owner. It contains local paths, not credential values:

```json
{
  "accounts": [
    {
      "account_alias": "alpaca-paper-canary",
      "binding_file": "/home/alexk/.config/trex/snaptrade-paper.json",
      "state_root": "/home/alexk/.local/state/trex/snaptrade-paper"
    }
  ]
}
```

Use `/home/alexk/.config/trex/paper-accounts.json` for this example. Each alias has
its own persistent state root; an alias/state root cannot be duplicated in the
catalog. The existing provider account ownership fence additionally excludes a
second alias/runtime for the same actual SnapTrade account. All runtimes must
share the explicit ownership root. Add additional dedicated account bindings
locally when needed, then bind sleeves to aliases in the workspace.

5. From the reviewed checkout, run offline configuration validation and then
   explicitly run the read-only qualification command above. This command
   contacts the provider but cannot submit orders. Share only the resulting safe
   blockers or the **path** to the private binding, never its contents.
6. Supply a real-time Massive stock-quote entitlement through the existing
   private Massive/Polygon API-key binding. The operational canary reads the
   last NBBO without cache and requires authoritative exchange/SIP timestamps
   within the existing 15-second window. Delayed quotes, stale quotes, absent
   fields or missing entitlement block the effect. A screenshot or locally
   stamped price cannot satisfy this boundary.

### Cockpit environment

The server reads `TREX_PAPER_CATALOG` and `TREX_PAPER_WORKSPACE`. Set them to the
private catalog and durable workspace directories. `TREX_WORKSPACE_CONTROLS=1`
enables local owner proposal controls. This does not authorize broker effects.
Run this only from an integrated/reviewed checkout; the current live service is
not changed by code or these instructions:

```bash
TREX_WORKSPACE_CONTROLS=1 \
TREX_PAPER_CATALOG=/home/alexk/.config/trex/paper-accounts.json \
TREX_PAPER_WORKSPACE=/home/alexk/.local/state/trex/paper-workspace \
uv run --group trex-web python -m tree_options.trex_web \
  --host 127.0.0.1 --port 8091
```

The UI/API show aliases and safe blocker codes only. Private config paths,
provider account IDs, credentials, raw provider responses and account receipts
are not browser payloads. `GET /api/paper/setup` supplies the same ordered
qualification checklist. Create the fixed allocation plan, bind sleeves and
submit a deployment proposal with a stable idempotency key. General research
strategies remain `REVIEW_REQUIRED`: historical data, strategy version and
persistent portfolio execution qualification still need implementation. No
HTTP approve/execute endpoint is added.

### Explicit bounded canary and local owner

For the first operational canary, create a proposal for
`operational-canary/1`, one order, notional at most USD 100 and TTL at most 900
seconds. `research_job_id` may be null for this operational check; assigning a
sleeve preserves funding/provenance attribution. This is a **one-share BUY/open
long LIMIT** check, not an automated roundtrip or strategy trading campaign.
Selling, fractional quantities and additional orders are not supported here.
The account must have no current positions or working orders under the existing
isolated canary risk policy.

Review the exact symbol and limit locally, then stage the immutable approval:

```bash
uv run --group broker-paper python -m tree_options.trex.paper_workspace \
  --workspace /home/alexk/.local/state/trex/paper-workspace \
  --catalog /home/alexk/.config/trex/paper-accounts.json \
  --ownership-root /home/alexk/.local/state/trex-account-owners \
  stage-canary --deployment-id <id-from-proposal> \
  --symbol <reviewed-symbol> --limit <reviewed-limit-at-most-100> \
  --operator-approved
```

Staging approves that exact order and proposal hash but creates no mandate and
makes no provider call. Changing its symbol, price or account requires a fresh
proposal and approval. The existing supervised paper mandate is minted only
inside the independently owned local execution invocation after account recovery:

```bash
uv run --group broker-paper python -m tree_options.trex.paper_workspace \
  --workspace /home/alexk/.local/state/trex/paper-workspace \
  --catalog /home/alexk/.config/trex/paper-accounts.json \
  --ownership-root /home/alexk/.local/state/trex-account-owners \
  execute-canary --account-alias alpaca-paper-canary \
  --deployment-id <id-from-approved-proposal> --quote-provider massive
```

**This command is an actual broker-paper effect request.** The operator must
approve the exact order first. Implementation tests never invoke it against a
real provider. Without `--quote-provider massive` the command refuses before
claiming the deployment. A fresh paper account, trade-enabled connection,
exclusive owner, timestamped quote, cash/risk checks, mandate and effect permit
all remain required at the actual send boundary. A delayed provider call can
consume the TTL and cause refusal. The persistent claim is made before effects;
interruptions and ambiguous timeouts never authorize a retry.

Local `recover --account-alias ...` loads existing execution state and reads and
reconciles external state without submitting. Repeating `execute-canary` after
its claim refuses rather than treating a failed transport as permission to send
again. Account/readback ambiguity blocks additional effects. There is no current
operator shortcut to reset an uncertain deployment into `STAGED`.

Local `halt --deployment-id ...` and the guarded cockpit halt immediately persist
the established account `HALT` kill-file without waiting for workspace or broker
effect locks. All subsequent mandate grants and effect preflights refuse. They
also attempt a nonblocking mandate revocation; `mandate_revocation_pending=true`
means an in-flight owner still holds that lock, while the durable halt barrier is
already active. An already transmitted broker request cannot be undone. Neither cancels
or liquidates existing broker orders/positions. They retain uncertain/observed
funding reservations; reconcile and review those obligations separately. Halt
works without an LLM or browser session. Qualification receipts release their
owners when done and are explicitly labeled observations at an instant, not an
ongoing runtime lease.

This implementation does not connect an account, install services, grant a real
mandate or submit a paper order on the operator's behalf. Exact external fill
and fee evidence remains unvalidated, and live-money execution stays disabled.
