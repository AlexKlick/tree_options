# AI-BEAR-002 AMENDMENT 2: standard contracts only (PRE-REGISTRATION)

Written 2026-11-09 (evening), before AI-BEAR-002's first signal session
(the 2026-11-10 close). No forward card exists. Sealed by
`AI-BEAR-002-AMENDMENT-2.md.sha256` in the commit that adds this file.

**Amends:** AI-BEAR-002 as amended by `docs/desk/AI-BEAR-002-AMENDMENT-1.md`
(sha256 `af7e1633…`). Neither earlier document's bytes are edited. The
parameter file is unchanged: `AI-BEAR-002-A1.params.json`, sha256
`1ec43b1b4f2f42e2aaf6b9251dd302e0ef6ccdda4e4ffbf45b432fea3ea9f685`.

## Why

- **The defect.** Since the 2026-11-05 session, the recorded CBOE EOD chains
  for NVDA and TSLA also list contracts under the roots `B1VDA` and
  `B1SLA`.
  - They sit at the standard expiries and strikes, and they come first in
    the file.
  - Each has open interest 0 and volume 0, and a price unrelated to the
    stock. For example, on 2026-11-09 `B1VDA261224P00170000` was quoted
    0.24/0.54 while `NVDA261224P00170000` was quoted 9.05/9.50.
  - The strike rule breaks ties by file order, so a card on NVDA or TSLA
    would have filled, marked and exited on the junk contract.
- **Scope.** Across the 319 recorded chains for the 11 forward names
  (2026-09-22..2026-11-09), 1,080 such rows exist, all on NVDA and TSLA.
- **Found** in a read-only scan on 2026-11-09 that priced put spreads from
  the same chains.

## Amended rule

1. **Only standard contracts are quoted.**
   - A chain row counts only if its OCC symbol is the chain's underlying
     symbol followed by YYMMDD, the right, and the strike × 1000 in 8 digits
     (e.g. `NVDA261224P00170000`).
   - Every other row is dropped when the chain is loaded. Strike selection,
     fills, marks and exits never see it.
   - The number dropped is kept on the loaded chain (`Chain.nonstandard`).

## Not changed

Everything else stays as AMENDMENT 1 left it:

- regime states;
- sleeve entry and exit rules;
- structures, and the strike and expiry rules;
- fill and exit price sides;
- the control context, probability and seed;
- the 20-card bar and α = 0.10;
- the first signal session (2026-11-10).
