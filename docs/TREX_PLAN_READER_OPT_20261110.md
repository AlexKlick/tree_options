# TREX cockpit single-pass plan TOML reader — TDD candidate

## Observation

Before this change, `list_plans()` parsed each valid plan and then invoked
`load_plan_view(id, ...)`, which re-resolved IDs by scanning and reparsing
the plan files. On the ordinary case of N uniquely identified, date-named
TOMLs, this causes O(N²) validation parses. The operation is repeated by
cockpit plan/stats requests. This is a code-supported complexity finding,
not a measured speedup on the production host.

## Refactor

- Parse each sorted candidate TOML once during the list pass.
- Count IDs once. If an ID is unique and no literal `<id>.toml` filename
  conflicts with its validated source path, supply the validated TradePlan
  directly to `load_plan_view()`. Book, events, dates, heartbeat and risk
  projections are still built using the existing code.
- If IDs collide or the legacy literal-name precedence could choose a
  different source, use the existing resolving path unchanged.
- Direct calls to `load_plan_view(id, ...)` retain their original resolution
  contract. No change to broker or execution-state write paths.
- Malformed TOMLs are still skipped in listings, and no plan is cached across
  requests or users. This avoids stale plan authority after file changes.

## Red-first TDD

A new pure `requires_legacy_resolution` function is tested with normal
date-named plans, matching filenames, duplicate IDs, and conflicting
literal-name aliases: import failed before implementation, then **5 passed**
in the isolated test runner.

New full-checkout tests (`test_trex_web_plan_parse_budget.py`) assert a
20-plan list performs 20 TOML parses, not a quadratic number, and that
duplicate IDs and literal-name conflicts preserve the old resolution.
Those integration tests were committed but **not run here**, because the
complete TREX checkout and host gate are unavailable.

## Release gates

Run `uv sync --locked`, `uv run ruff check src tests scripts`,
`uv run mypy`, `uv run pytest tests/unit/test_trex_plan_resolution.py
tests/unit/test_trex_web_plan_parse_budget.py tests/unit/test_trex_web.py`,
and the full `bash scripts/m0_gate.sh` / mutation ledger at the exact
candidate SHA. Benchmark p50/p95 and `load_plan` call count at 1, 20,
100 plans against identical fixtures. This change is cockpit-only,
read-only and **does not qualify customer trading, execution, or OAuth**.
