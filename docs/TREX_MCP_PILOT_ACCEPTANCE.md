# TREX MCP — pilot acceptance and merge sequence

**Scope:** Read-only research interface. No paper/live orders, account/position
inspection, user-supplied upstream URL, or deployment performed by this PR.

## Branch dependencies

1. OAuth / upstream deadlines — requires merged #77.
2. Customer-aware bounded request quotas — stacks on (1); subject and tenant
   budgets remain process-local and require **distributed ingress limits**.
3. Bearish option comparison — stacks on (2); existing terminal math is reused,
   and differing expirations are disclosed, never ranked as one-day outcomes.
4. Pilot local gate, release service *template*, and operator checklist — stacks
   on (3); no service is installed or enabled by repository commits.

## Local gate before merging

With the normal TREX test lock, full checkout, and locked Python environment:

```bash
uv sync --locked --group trex-web
uv run python scripts/trex_mcp_pilot_gate.py
uv run python scripts/trex_mcp_pilot_gate.py --full
uv run ruff check src/tree_options/trex_mcp tests/unit/test_trex_mcp* scripts/trex_mcp_pilot_gate.py
uv run mypy src/tree_options/trex_mcp
```

`--full` runs `scripts/m0_gate.sh`, including its mutation suite; do not
interpret a local green receipt as network, broker, or security certification.
The gate emits `LOCAL_CHECKS_PASS_NOT_CUSTOMER_READY`, even when M0 succeeds.
No CI is installed or implied (the repo's official release gate is local).

## External gates — all mandatory before inviting customers

- [ ] P0 custody/reconciliation, P3 environment separation, P4 paper acceptance,
      P5 restricted owner live gates as applicable, and P6 invited-user controls
      independently verified under `docs/product/CONTROL-ROADMAP-20261104.md`.
- [ ] HTTPS front door with exact `Origin` allowlist; OAuth 2.1 provider, scopes,
      tenant/subject audience binding, short TTL, revocation, introspection and
      protected-resource metadata validated with **real** ChatGPT and independent
      clients (including backwards compatibility). Do not use injected test auth.
- [ ] Rate limits at TLS ingress for IP, subject and tenant across all replicas;
      cap bodies, connections, streaming duration, and close idle connections.
- [ ] Tenant runs one isolated TREX web/state service on its own loopback port;
      access control disallows broker/command and other tenants' instances.
- [ ] Market-data distribution/use rights, tenant consent, retention/redaction,
      production alert destination, backups and deletion verified.
- [ ] Red-team: revoked/expired token, tenant swap, invalid OAuth claims,
      slow response, oversized compressed response, missing LEAPS,
      stale/crossed quotes, timing/loopback path normalization, worker failure.
- [ ] Full reproducible gate, build/release SHA and deployed source identity
      attached to GitHub coordination issue. At least one end-to-end customer
      pilot signoff before general release.

The proposed `deploy/product/trex-mcp-pilot.service.in` is intentionally
non-runnable until `@RELEASE_DIR@`, `@SECRET_ENV_FILE@` and `@PYTHON@` have
been substituted and reviewed by the authorized operator. Run only behind an
approved HTTPS proxy, with `TREX_MCP_BIND=127.0.0.1` and a dedicated port.
Do not expose TREX's existing `trex_web` service directly to MCP customers.

## No inference of trade authority

A buyer's analysis of TSLA long-dated puts is not an order, a funded risk
reservation, a broker quote, an empirically validated edge, or a mandate. The
model may only request research math and recorded quotes; the existing TREX
supervisor retains exclusive ownership of broker execution and recovery.
