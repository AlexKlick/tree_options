# TREX MCP Wave 2 — bounded-I/O, provenance, and load-shedding candidate

Date: 2026-11-10. Dependency: merged #76. Status: **review candidate, not a customer release**.

## First-principles boundary

TREX market/option research is not a broker. This service can read only
`/api/market/{SYMBOL}` and `/api/market/{SYMBOL}/options` on one tenant's
fixed loopback `trex_web` instance. No account, portfolio, command, refresh,
trading, or user-selected URL exists in its MCP tools. Treat OAuth's authenticated
subject + audience + scope + tenant as authorization data, not model-generated inputs.
Never treat a cached midpoint or scenario premium as a tradeable price.

## TDD hardening shipped in this candidate

| Surface | Invariant | Implementation |
| --- | --- | --- |
| Inbound body | Bound reads even without `Content-Length` | Incremental ASGI request stream; 65,536 decoded request bytes max; 413 on excess |
| Untrusted JSON | No `NaN`, `Infinity`, excessive recursive arrays or invalid IDs | Strict JSON constant parsing; recursion error mapped to JSON-RPC parse rejection; primitive ID validation |
| Authorization | Validate every call without caching a positive grant | Per-request OAuth introspection; audience/scope/tenant/sub/active, plus `exp` and `nbf` consistency checks |
| Token | Reject oversized bearer headers before introspection | 4,096-character limit |
| Tenant registry | One loopback port cannot silently serve multiple customers | Unique port check even across localhost aliases |
| Upstream OAuth JSON | Prevent giant introspection replies or decompression bombs | At most 16,384 decoded bytes; fail closed |
| Upstream TREX JSON | Refuse giant option trees and decompression bombs | At most 1,048,576 decoded bytes; non-200/invalid JSON rejected |
| Pooled transport | Reuse connections while retaining per-request introspection | Single app-lifespan HTTPX async pool; 24 max connections, 12 idle keepalives, `trust_env=False`, no redirect following |
| Work admission | Keep a burst from exhausting unbounded coroutine/broker-adjacent resources | Per-worker 16-in-flight semaphore, 100 ms wait, 429 with `Retry-After: 1` |
| Quote provenance | No misleading ticker, future quote, NaN, crossed bid/ask or fabricated midpoint | Check outer/nested symbols; reject timestamps later than TREX observation; nullable finite numeric fields; invalid two-sided quotes have null midpoint |
| Output control | One provider response must not spill arbitrary content into a customer AI context | Field allowlists; ≤8 warnings × 200 chars; ≤80 option rows; linear pass + explicit truncation |

These are **defense-in-depth**. TLS termination and authenticated per-subject / per-IP
rate limits must be provided at the reverse proxy or API gateway, across all workers.
The in-process semaphore is not a distributed limiter. Also cap request body size
*before* forwarding to Uvicorn at the ingress; ASGI cannot prevent a front proxy
from buffering oversized uploads. Do not turn off upstream TLS validation.

## Failure contracts

- `401`: missing, malformed, overlong, expired, or unauthorized bearer token.
- `403`: disallowed `Origin` or token tenant not in the configured registry.
- `413`: inbound MCP body exceeds budget, even when streamed/chunked.
- `429` + `Retry-After`: local worker is saturated; no tool side effect.
- `503`: OAuth introspection unavailable; never use cached/reused authority.
- MCP `isError=true`: rejected or unavailable market read; never replace a quote
  with previous close or other guessed value.
- Missing expiry in a bounded chain slice never means the listed option does not exist.
- Crossed quotes remain observable with `quote_quality=crossed_quote` and
  `mid=null`; none of these quotes is execution-authorized.

## Validation evidence

Run in an isolated package directory with `fastapi`, `httpx`, and `pytest`
installed; no broker connection or host state was accessed:

```sh
PYTHONPATH=src pytest -q tests/unit/test_trex_mcp.py tests/unit/test_trex_mcp_hardening.py
python -m compileall -q src/tree_options/trex_mcp
```

Observed on 2026-11-10: **42 passed**, compileall passed. Red-first tests failed
before implementation for missing network bounds, client reuse, token limits,
invalid quote state, namespace pollution, exhaustion and payload bombs.

An illustrative in-memory projection of 768 synthetic chain rows, of which
384 were put rows, returned 80 bounded records in ~1.83 ms/call over 500
calls, with ~78.5 KiB peak traced Python allocations in this environment.
This **excludes** JSON decode, transport, TLS and OAuth introspection and is
*not* a service latency SLO or a before/after speedup claim. Connection pooling
reduces setup churn without relaxing per-request authorization.

**Not run here:** the repository-wide M0 gate, ruff, mypy, mutation suite,
frontend gates, OAuth provider integration, authenticated ChatGPT client,
reconciliation, 10-session paper acceptance, or measured production latency.
Do not merge or deploy as production without those receipts, release identity,
and the owner-controlled TREX gates in
`docs/product/CONTROL-ROADMAP-20261104.md`.

## Next ordered production gates

1. Gate this exact PR head with local `uv sync --locked`, ruff, mypy, full
   `scripts/m0_gate.sh`, and mutation kill ledger under the TREX test lock.
2. Integrate with an actual OAuth 2.1 provider; test revoked/expired tokens,
   delayed introspection, audience mix-ups, tenant swaps and CORS-origin behavior.
3. Configure dedicated HTTPS hostname, ingress request/identity quotas,
   customer-specific Unix service + TREX state, off-host alerts and backups.
4. Verify contract compatibility with a real ChatGPT connector and another
   MCP client; exercise delayed and missing option chains without invented quotes.
5. Obtain market data redistribution permission and implement customer
   onboarding/consent/audit retention before any invitee pilot. **No orders**.
