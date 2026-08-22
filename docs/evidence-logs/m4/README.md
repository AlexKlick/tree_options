# M4 retained evidence logs

- `m4-a-shakedown.md` / `.json` — M4-A integration shakedown (2026-08-21):
  adapter parse of the retained Cboe demo per underlying, UNMODIFIED
  `OptionPitSurface` smoke queries, real-data manifest round-trip, coverage
  inspector tables, and the G2 findings carry-forward.
- `m4-a-shakedown-run.log` — full one-off driver capture (verbatim counts).
- `m4-coverage-{SPY,TSLA,SPX}.json` — inspector machine JSON per underlying
  (source sha `1732795c0b38…`, session 2023-08-25).
- `m4-mutation-run.log` — M4 integration mutation run, full capture
  (141/141 KILLED incl. the new M135-M142; restoration full-suite pass;
  machine table also at `artifacts/m4-mutations.{json,md}`, the generated
  artifacts dir).
- `m4-b-massive-structural.md` / `.json` — M4-B **live** Massive (Polygon)
  free-tier structural pull (2026-08-21): 44 live requests of a 45 budget,
  SPY + TSLA at 4 `as_of` dates over the entitled 2-year window, plus daily
  bars for 5 ATM contracts. Carries the cross-workstream reconciliation
  (WS-D1 client ↔ WS-D2 inspector), the truncation ledger, and the
  structural findings. **Read §2.1 first: the four SPY captures are
  truncated nearest-expiry prefixes, so only the TSLA series supports
  universe/tenor/lifecycle claims.** The `.json` is the inspector's report
  with bulk per-ticker maps elided to counts (§6) — derived numbers only.
  Raw vendor payloads stay gitignored under `artifacts/`.

See docs/m4-real-data-plan.md (G1/G2 gates; nonclaims §4 carry forward:
one-session demo subset — schema/coverage evidence only).
