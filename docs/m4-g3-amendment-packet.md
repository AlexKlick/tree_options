# M4 G3 — protocol/world amendment packet (DRAFT for owner ratification)

- status: **DRAFT** — every ask is grounded in landed, gated code (PRs #7–#10);
  two sections are marked PENDING-era until the structural coverage era
  completes (~2026-08-24). Nothing in this packet is applied until the
  owner ratifies; `research_protocol.yaml` stays 0.1.0 and the world
  registries stay untouched until then.
- entry criteria met: manifest/verify pair + input-hash lineage (PR #8),
  derived-quote capability with provenance stamps (PR #10, gate 843
  passed / 169 KILLED, re-proven on merged main ce22792).

## Ask A — surface schema: a VWAP-based quote kind (the M4-C finding)

`OptionQuoteSnapshot` / `QuoteEvent` / `OptionChainEntry` /
`OptionDayFile` carry REQUIRED non-optional `bid`/`ask`/`open_interest`.
A free-tier daily aggregate (VWAP + volume + trade count) therefore
cannot be encoded without fabricating a two-sided market — which PR #10
refuses to do (overlay raises the not-in-file signal; derived reads live
on their own stamped surface).

**Ask**: extend the quote schema with a vwap-quote kind — either
optional quote sides (`bid: Decimal | None`) with a required
`quote_kind: Literal["two_sided", "vwap"]`, or a parallel
`VwapQuoteEvent`. Synthetic and Cboe lanes stay byte-compatible
(two_sided). INV-11 fill semantics per kind: two_sided fills as today;
vwap fills at the session VWAP, participation-capped by observed volume,
zero-volume day = unfillable (fail-closed, unchanged).

## Ask B — derived-greeks provenance class

`abs_delta` in the candidate filter may be **model-implied**: IV
inverted from the bar's Decimal VWAP on the repo's own Black-Scholes
(`massive_derived`, assumptions `derived-pricing/1` = the synthetic
world's flat 0.03/0.0, versioned), delta computed under that IV. Vendor
greeks were themselves model outputs (Cboe `delta_1545`); the change is
whose model and which input price — recorded per candidate as
`provenance="model-derived-from-vwap"` + assumptions version. Known
approximation, failure region named: BS on American exercise (deep-ITM
calls with dividend points); a binomial pricer is the upgrade path if
the filter ever selects deep-ITM.

## Ask C — world registry entries + provider/schema tokens

Register the real lanes: `massive-polygon-free/1` (structural masters +
bars), `massive-derived-free/1` (derived overlay); schema tokens
`m4-massive/1`, `m4b-manifest/1`, `m4-massive-derived/1`. All are
content-hash bound (manifest build/verify with disk reconciliation).

## Ask D — liquidity semantics

Volume + trade count are **flow**, not open interest. The candidate
filter's liquidity term becomes a volume-flow threshold (value TBD from
era distributions); the spread term is **dropped with disclosure** (no
$0 substitute exists — not approximated). ~24-month free OCC daily
volume/OI downloads are a possible real-OI upgrade, unverified (their
page bot-blocks fetchers; browser verification pending).

## Ask E — holdout declaration (PENDING-era)

Per the standing M1 correction, the holdout is declared only after real
coverage inspection. The era completes ~2026-08-24; this section then
proposes the holdout window from the observed coverage grid (105 Fridays
× 30 names, oldest four banked before the 2026-08-23 entitlement roll).
**PENDING-era: the concrete window proposal.**

## Not in this packet

No research claims; no backtest run; the G4 sealed-gate plan is a
separate artifact after ratification; protocol version mechanics
(0.1.0 → 0.2.0 amendment) applied only on owner GO.

## PENDING-era sections checklist

- [ ] Ask E holdout window proposal (era §4 numbers)
- [ ] Ask D volume-flow threshold value (era bar-volume distributions)
- [ ] Coverage statement for the G4 plan (completeness census)
