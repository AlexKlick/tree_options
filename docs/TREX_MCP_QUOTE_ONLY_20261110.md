# TREX MCP quote-only read path — TDD candidate (2026-11-10)

## First-principles bottleneck

`trex_market_snapshot` previously called `GET /api/market/{symbol}`,
which assembles quote, daily bars and up to twelve news items. The MCP
tool needs only the quote and its original source timestamp. Repeated
requests therefore paid for `MarketCache.get_envelope("bars")` and
`MarketCache.get_envelope("news")` plus JSON decoding/chart projection
even though MCP discards the results.

## Design

- New `GET /api/market/{symbol}/quote` returns only `now`, `symbol`,
  `quote`, and `quote_age_seconds`, using the **same** market.json
  snapshot priority and TTL-cache fallback as the full cockpit route.
- A shared broker-free `read_market_quote` helper eliminates duplicated
  selection logic and an avoidable `exists()` filesystem check.
- Both endpoints use that helper; the full endpoint retains bars/news and
  unchanged response fields. A missing snapshot or quote never creates a
  price, request spool entry, or external provider request.
- Symbols on the new route are restricted to 1–6 ASCII letters with an
  optional internal class period. No path traversal or caller-selected
  upstream URL. The MCP read allowlist explicitly includes `/quote`;
  its sole market-snapshot tool now requests this path.
- Authentication still requires fresh OAuth introspection on **every**
  MCP request. Tenant->loopback mapping, source timestamps, output
  whitelisting, and non-executable trading boundary remain unchanged.

## Red/green evidence and scale claims

- Red: new `market_quote` helper import was absent.
- Green: **21/21** isolated pure quote-selection tests, including
  snapshot/cache precedence, corrupted/missing snapshots, symbol safety,
  and not retaining invalid fallback records.
- A partial MCP source mirror with quote-only GET changes passed
  **125/125 targeted tests** including existing OAuth, tenant, options
  and byte-budget checks. This mirror predates some later commits and
  is NOT an exact-head full-repository receipt.
- Full-checkout integration tests were committed for parity with the
  existing quote endpoint, no bars/news envelope calls, cache fallback,
  absent quote, and symbol rejection. Those tests have NOT run here.
- Expected reduction is **two bar/news envelope reads** and their
  associated decoding/projection work per quote-only request when the
  original route would have attempted both. This is a **code-path
  reduction**, not a measured production CPU/latency or throughput gain.
  No cross-tenant or cross-request authorization cache is introduced.

## Exact-head production gates

Run from the PR head, under the existing TREX local gate lock:

```sh
uv sync --locked
uv run ruff check src tests scripts
uv run mypy
uv run pytest tests/unit/test_trex_market_quote_reader.py \
  tests/unit/test_trex_web_quote_only.py \
  tests/unit/test_trex_mcp.py \
  tests/unit/test_trex_mcp_network_contract.py
bash scripts/m0_gate.sh
```

Capture full pytest, mutation kill ledger, frontend checks if relevant,
exact source SHA, dependency locks, and a performance profile with
identical cold/warm data. Customer activation remains blocked by
#83–#86, including licensing and real OAuth/ChatGPT interoperability.
The quote-only MCP path **does not** grant paper/live order authority.

Follow-on work under #89: revision-keyed immutable options read model
and incrementally materialized performance statistics, only after
reproducible cold/warm, p50/p95, and CPU allocation baselines.
