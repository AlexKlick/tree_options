# M4 retained evidence logs

- `g4-sealed-event-reconciliation.md` — the consumed one-shot M4-G4 sealed
  event of 2026-08-31 (APPROVAL `468e2cf8…` / CONSUMPTION `bb6a616b…`,
  `sealed_run_id 9b945db3…`, verdict UNKNOWN, 0 trials): the crash census
  (8 of 2,871 spot-proxy closes carry a third decimal; zero strikes over
  2dp in 10,049,160 master rows), the boundary fix (cent quantization at
  the `BarRecord` seam reusing `PRICE_TICK`, custody-stamped, the shared
  2dp `Price` type untouched), the `shares_per_contract` 84/232 side
  observation, and the forward path (new head → new packet → NEW
  pre-declared gate; the consumed pair stays history, never re-run).
- `m4-protocol-021-ratification.md` — protocol 0.2.1 ratification package
  (2026-08-26, branch `m4/protocol-021-20260826`): the four fixed owner
  decisions (flow=100 owner_deviation bound to census `43b0b040…`;
  holdout window A, 13 dates scoped to lane-2 folds; repo-generated
  calendar; criterion-4 strict per-lane class map), the scoped builder
  admission (owner_deviation-only against the exit-5 census — derivation
  and the EXACT-fact gate stay refused), the amendment packet at
  `artifacts/amendment/43b0b040ea3c/` with hashes, and every full command
  capture path.
- `pr13-audit-remediation.md` — exact-head source verification for the four
  merge blockers reported against PR #13 head `f95b99a`: canonical run
  identity, checkout-independent universe identity, component-wise run-state
  custody, and typed held-input G4 authority. The tested source head is
  `314a5f4`; the record keeps live/vendor/approval execution out of scope.
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
- `m4-b-coverage-census.md` / `.json` — coverage-era closeout census
  (2026-08-26, code `0c13e8f`): 29×105 = 3,045 expected == observed
  masters, rows 10,049,160 declared == parsed, pairs COMPLETE 2,871 /
  SPOT_MISSING_HOLIDAY 145 (expected; 5 holiday Fridays × 29) /
  **SPOT_MISSING_SESSION 29 = the exit-5 driver** (2026-08-21, all
  underlyings — vendor spot-close gap; owner escalation, census never
  re-run) / TRUNCATED, ERROR, MISSING all 0. The `.json` is the census
  with the 145 uniform holiday findings elided to per-date counts; the
  29 session findings are retained verbatim. Values taxonomy leaves
  `flow_min_session_volume` (G3 contradiction quoted verbatim) and
  `final_holdout_window` to the owner — nothing is ratified here.

See docs/m4-real-data-plan.md (G1/G2 gates; nonclaims §4 carry forward:
one-session demo subset — schema/coverage evidence only).
