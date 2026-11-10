# TREX MCP provenance guard — TDD wave 3

Date: 2026-11-10. Base: recovery PR #87 (restores #79–#82 to canonical main).
Scope: **read-only** market and options projections only. No order authority,
new quote source, broker import, account access, background task or public deployment.

## Threat model

The TREX discovery/desk cache is a *data source*, not an authoritative clock.
Its `now`, `quote_age_seconds`, recorded-session date and `age_seconds` can be
missing, stale, inconsistent or malformed. A provider-relative check by itself
can accept a future quote when both provider `now` and `source_as_of` are in
the future. A reported `age_seconds=0` does not demonstrate a live quote.

## Projection contract

- Validate a supplied TREX `now` as an offset-aware timestamp, no more than
  60 seconds ahead of the independent gateway's UTC clock. Missing is permitted
  but cannot certify a provider-relative age.
- Validate `source_as_of` and `live.fetched_at` against the gateway and TREX
  clocks; no naive timestamp, no future-dated source beyond the bounded skew.
- Validate `recorded.session` against **America/New_York** gateway and provider
  calendar dates, not UTC date or untrusted `now` alone.
- Parse each supplied provider clock at most once per response; validate only
  O(1) timestamps outside the already bounded O(n) option-slice filter.
- Preserve the existing `quote_age_seconds` and `live.age_seconds` **as reported
  values** for backward compatibility, without relabeling them as verified.
- Add `gateway_checked_at`, `gateway_quote_age_seconds` and
  `reported_age_consistent`; mark `freshness_known=false` when reported age
  contradicts source chronology, or when source timestamp is absent.
- For live option slices, add `gateway_age_seconds` and `cache_age_verified`.
  Compute `cache_expired` against independently derived age, not provider-
  reported age; if fetch timestamp is absent, expiry is **unknown**, not false.
- Keep all market output nonexecutable. No fallback quote, invented timestamp,
  automatic execution or session subscription.

## Exact TDD gate in isolated source bundle

```bash
PYTHONPATH=src pytest -q tests/unit/test_trex_mcp*.py
python -m compileall -q src/tree_options/trex_mcp
```

The 11 initial provenance tests failed **red** before implementation (missing
`gateway_at` contract). Two more tests cover NY date rollover and real MCP
endpoint rejection. With the proposed implementation **103/103 targeted tests
passed** on the isolated 2026-11-10 candidate. No performance improvement is
claimed from a microbenchmark; the design uses one clock parse per response.

## Remaining blocking gates

- Recovery PR #87 must be merged and release identity rechecked before this
  child PR can merge to main.
- Run the exact-head full repository M0, mutation, Ruff, mypy and frontend gate
  under TREX host lock. Ruff and mypy were not available in the isolated runner.
- Qualify actual OAuth provider / revocation, ChatGPT client interoperability,
  customer and ingress isolation, market-data rights, paper acceptance and owner
  activation separately. A green local test suite is **not** production readiness.
