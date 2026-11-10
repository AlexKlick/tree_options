# TREX MCP research gateway (candidate, no broker authority)

The `tree_options.trex_mcp` process exposes a small, read-only JSON-RPC/MCP
Streamable HTTP bridge to the **existing TREX cockpit market endpoints**.
It does not import `ib_async`, write to the supervised desk or open order
intent, access customer accounts/positions, or act as a new market-data feed.

## Exposed MCP tools

| Tool | What it does | Authority |
| --- | --- | --- |
| `trex_market_snapshot` | Gets a ticker's TREX discovery quote with original age | Read only |
| `trex_options_surface` | Returns the EOD/clock chain and delayed viewchain slices, quote provenance and explicit missing expiries | Read only |
| `trex_bearish_payoff` | Calculates *expiration* P&L for an explicitly priced long put or debit put spread | Pure research; no broker |

The payoff tool requires `spot`, `long_strike`, `net_debit`, `expiration`,
`strategy`, and `target_prices`. `net_debit` is per-share premium; contract
multiplier is 100. Optional `short_strike`, `contracts` (1-100) and
`max_risk_budget` let users compare payoff bounds. Premium assumptions are
**not verified quotes**. Returns at expiration exclude fees and volatility,
and are never advertised as returns achievable today.

The options surface uses `/api/market/{symbol}/options` with
`window=15&max_expiries=12`. TREX limits this to the nearest expiries and
strikes around ATM; not seeing a distant LEAPS contract **does not mean that
contract is unavailable**. The cache may be delayed or stale. Never present
its midpoint as an executable fill; preserve `session`, `fetched_at`,
`age_seconds`, `bid`, `ask`, `oi`, `volume` and any warnings.

## Architecture / custody

`ChatGPT or customer assistant -> OAuth resource /mcp -> trex_mcp ->
TENANT'S LOCAL trex_web GET /api/market/...`

The gateway is a **separate process** with a per-tenant **fixed loopback**
TREX read-only instance. There is no user-supplied URL, account identifier,
state path, broker credentials, tool-based refresh, job scheduling, or write
route. Authentication is mandatory for discovery/handshake and tool calls.

An external OAuth 2.1-compliant authorization server owns login, issuance,
refresh, revocation, and consent. The gateway uses TLS token introspection
for every MCP request (no positive cache) and requires all of:

- `active: true`, `sub` nonempty,
- `aud` contains exactly this gateway's public MCP resource URL,
- scope contains `trex:research:read`,
- a `trex_tenant_id` claim mapped to an operator-configured isolated
  loopback TREX instance.

No anonymous public research fallback. OAuth protected-resource metadata is
published at `/.well-known/oauth-protected-resource` and the `/mcp` suffix.
Never log access tokens; operate behind HTTPS reverse proxy, enforce request
and per-subject rate limits there, and disable proxy buffering/large bodies.
For ChatGPT, your authorization server must support the required client
registration and refresh flow. Its own discovery/configuration is separate
from this gateway; **there is no authorization server in this repository**.

### Operator-only environment example

Do not place real credentials in the repository or in a shell history. Put
these in a restricted environment file or secret manager, not in Git:

```dotenv
TREX_MCP_PUBLIC_URL=https://mcp.YOUR_DOMAIN.example/mcp
TREX_MCP_AUTHORIZATION_SERVER=https://login.YOUR_DOMAIN.example
TREX_MCP_INTROSPECTION_URL=https://login.YOUR_DOMAIN.example/oauth2/introspect
TREX_MCP_CLIENT_ID=trex-mcp-resource
TREX_MCP_CLIENT_SECRET=<secret from identity provider>
TREX_MCP_TENANTS_JSON={"pilot-a":"http://127.0.0.1:8090","pilot-b":"http://127.0.0.1:8091"}
# Optional. Default denies all browser Origin headers.
TREX_MCP_ALLOWED_ORIGINS=
TREX_MCP_BIND=127.0.0.1
TREX_MCP_PORT=8093
```

The real tenant IDs must come from issued/scoped OAuth claims and a controlled
registration process; they are never accepted from tool arguments. Different
customers must run isolated TREX app/state/broker environments. Do not point
multiple tenants to an owner instance.

### Local smoke and static gate

```bash
uv sync --locked --group trex-web
uv run --locked --group trex-web python -m tree_options.trex_mcp
uv run --locked --group trex-web pytest -q tests/unit/test_trex_mcp.py
uv run --locked --group trex-web ruff check src/tree_options/trex_mcp tests/unit/test_trex_mcp.py
# Project-wide mandatory owner gate, before merge / deployment:
bash scripts/m0_gate.sh
```

The service defaults to 127.0.0.1:8093. Reverse proxy only the `/mcp` POST
endpoint and OAuth metadata routes to this process over HTTPS. Do not proxy
`trex_web` order-capable endpoints or the entire existing host through this
service. The MCP server returns `application/json` for request-response tools;
no SSE/subscriptions, batch requests, server-initiated messages or session IDs.
It accepts the modern 2026-07-28 message metadata/header mode and older
2025-11-25 and 2025-06-18 handshake clients. Full multi-client interoperability
and certification still need end-to-end verification with a real ChatGPT app.

### Connect to ChatGPT

Once the HTTPS endpoint, identity provider and customer isolation are
operational, a permitted workspace administrator can add a custom MCP server
in **Plugins -> Add custom MCP server**, set the public URL from
`TREX_MCP_PUBLIC_URL`, configure OAuth, connect, then inspect the three
**read-only** tools. ChatGPT also supports Secure MCP Tunnel for private
instances, subject to product access. Do **not** tell users the integration
is live or approved for customer access before the product gates close.

## Release / customer blockers

TREX's `docs/product/CONTROL-ROADMAP-20261104.md` remains authoritative:
P0 custody and P3–P6 identity, operator acceptance and customer isolation are
unqualified. This candidate is a **developer integration**, not an installed
release. Before a customer pilot: complete source/release review; test OAuth
expiration/revocation/tenant swaps; confirm market-data redistribution rights;
review retention/consent; apply rate limits; run full M0 and actual MCP client
interop tests; verify auditability and stale/future/crossed quote refusal; and
keep all paper/live authority behind TREX's existing permits and operator.

Write tools for order drafting, mandate requests or broker execution are not
included. They would require separate versioned contracts, narrow capabilities,
per-entry customer consent, the supervisor's risk gate and replay-tested
reconciliation before being considered. No LLM-supplied numbers may become an
order by crossing this bridge directly.


## Wave 2 hardening candidate (2026-11-10)

See [`TREX_MCP_WAVE2_HARDENING.md`](TREX_MCP_WAVE2_HARDENING.md) for bounded-I/O,
shared HTTP pooling, per-worker admission, source-provenance controls,
fail-closed negative tests and the operator release gates. This is a
**separate review candidate**, not a production authorization or deployment.
