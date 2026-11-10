# TREX MCP exact-expiration projection — November 10, 2026

## Why

The MCP `trex_options_surface` tool previously passed a validated requested
date into its local result filter, but **not** to TREX's options endpoint.
The endpoint then returned only the nearest twelve expirations. A September
2027 or January 2028 option could be present in the source and absent from
MCP solely due to truncation.

## New read-only contract

`GET /api/market/{sym}/options?expiration=YYYY-MM-DD` optionally selects
the exact expiration in both recorded, columnar EOD/clock captures (ISO keys)
and delayed cached viewchains (compact YYYYMMDD keys). Without `expiration`,
the original nearest-N behavior and response schema remain unchanged.
With `expiration`, the expiry lookup bypasses the nearest-expiry cap,
using dictionary membership instead of sorting nearby expirations.

MCP forwards only a validated, canonical expiration to its fixed,
tenant-local, read-only TREX GET. Invalid dates fail before I/O. API-invalid
dates receive 422. No new broker, order, command, token, account, provider
write, model inference, or external refresh capability is introduced.

**Important limits:** The selected expiry remains ATM-windowed to the
existing `window` rungs, so deep-OTM strikes may still be absent. A missing
row means `not observed in this bounded snapshot`; it does not establish
that the contract is unlisted, illiquid, executable, or data-licensed.
The MCP response remains non-executable and retains original timestamps and
freshness controls. The historical source is not rewritten.

## TDD

Red-first:
- MCP did not forward expiration in the read GET params.
- Selector did not exist and could not look up an exact date.
Green in the local MCP-only partial source mirror (2026-11-10):
- 110 targeted tests passed, including existing MCP tests, selector tests,
  and early invalid-date rejection.
- The pilot runner returned `LOCAL_CHECKS_PASS_NOT_CUSTOMER_READY`.
- New full-checkout integration tests in
  `tests/unit/test_trex_web_symbol_views.py` check observed far-dated
  expiries beyond twelve, live compact-date lookups, unchanged default,
  absent expiry, and invalid queries.

## Mandatory exact-head release checks

Run `uv sync --locked`, `uv run ruff check src tests scripts`,
`uv run mypy`, `uv run pytest tests/unit/test_trex_web_symbol_views.py
tests/unit/test_trex_mcp.py tests/unit/test_trex_mcp_expiration_forward.py
tests/unit/test_trex_exact_expiry_selection.py`, and then
`bash scripts/m0_gate.sh` with the mutation ledger on a full TREX checkout.
These full-repository tests were **not run** in the partial source mirror.
Confirm no unlicensed redistribution (#86) or customer activation (#83–#85).

Future independent phase: bounded exact-strike projection and immutable
per-session/symbol read model, after measuring decompression/JSON parse CPU,
I/O and latency against the full-store cold/warm baseline (issue #89).
