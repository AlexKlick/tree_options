# M4-C — Massive (Polygon) free tier: the $0 derived-quote lane

- date: 2026-08-21, branch `m4/derived-20260821` (from `main` at `d597060`)
- triggered by: the **G2 $0 ruling of 2026-08-21** — the owner ruled the
  quote-bearing purchase lane closed at $0 (ruling recorded the same day on
  the sibling coverage-era lane; no purchase happened). The question this
  lane answers: what can be DERIVED, honestly, from what the free tier
  already serves?
- what the free tier serves this lane: per-contract DAILY AGGREGATES with a
  real VWAP, volume and transaction count, plus the point-in-time contract
  master and the underlying close (spot proxy — DECLARED INPUT, never a
  quote).
- what it still does not serve: bid/ask, open interest, greeks — unchanged
  (§2).
- pieces: `src/tree_options/data/massive_derived.py` (the model),
  `src/tree_options/data/massive_overlay.py` (the PIT read surface),
  `src/tree_options/time/monthlies.py` (the monthly-expiry rule), and the
  bridge's `--bars-mode atm-grid` bar-selection strategy
  (`scripts/capture_massive_structural.py`, runbook §6.1).

## 1. What landed

**`massive_derived.py` — the model.** `implied_vol` is bisection on the
repo's OWN Black-Scholes pricer — `tree_options.synth_options.greeks.bs_price`,
the M3 synthetic-era, stdlib-only pricer — inverted on a VWAP premium;
`derived_abs_delta` then evaluates the shared analytic `bs_abs_delta` at the
solved iv. The pricer is shared with the synthetic world ON PURPOSE: derived
and synthetic greeks stay convention-identical by construction instead of
importing a second pricing stack that can drift. `PricingAssumptions`
(defaults `risk_free=0.03`, `dividend_yield=0.0`, `model="black-scholes-1"`,
`version="derived-pricing/1"`) mirrors the synthetic world's flat
`OptionsOverlaySpec` defaults — one flat value per overlay for every
underlying and expiry — and that equality is asserted by test against the
loaded spec. Assumptions are DECLARED, not observed: no rates feed exists on
this tier, and every derived output names the assumption set it was computed
under. FOUR refusal classes, all `MassiveDerivationError` naming the bound
and the numbers — never a silent clamp to a bracket edge: a non-positive
premium; a premium below the nearly-zero-vol price (under discounted
intrinsic — arbitrage or degenerate data); a premium above the upper vol
bound; solver non-convergence. Floats are BY DESIGN inside this one module
(bisection iterates on prices): it is the lane's single sanctioned float
island, and callers convert a Decimal VWAP to float EXACTLY ONCE at the call
site.

**`massive_overlay.py` — the surface.** `MassiveDerivedOverlay` implements
the read surface `tree_options.data.options_pit.OptionPitSurface` consumes,
duck-typed like the Cboe overlay, so the UNMODIFIED M3 PIT machinery
constructs and answers publication/eligibility/contract/ladder/expiry
queries against it. The publication wall is REUSED, not reimplemented:
`publication_of` IS `tree_options.data.cboe_eod.publication_instant` — the
shared T+1 09:00 America/New_York wall — so a decision at close(t) sees
session t−1's bars, never session t's, and every row carries
`exchange_timestamp` = 16:00 ET close of the bar session with
`received_timestamp` = that wall (validated `exchange <= received`). VWAP is
Decimal end-to-end: the vendor's exact token IS the premium; the Decimal
VWAP, spot and strike cross to float exactly once at the solver call site
and the two results are pinned back to `Decimal(repr(x))` at the record
boundary — no price field is ever built from, rounded through, or
re-serialized via a float. Vendor facts (premium, volume, transactions) and
the derived block (`DerivedPricing`) live on separate fields of
`MassiveDerivedQuote` so they can never be conflated, and every derived
record stamps `model`, `assumptions_version` and
`provenance="model-derived-from-vwap"`. `load_derived_surface` is parse-side
only — captures from disk, no client, no network, no key — and a present
capture manifest is verified fail-closed before anything loads. Provider
token `massive-derived-free/1`, schema `m4-massive-derived/1`.

**`monthlies.py` — the rule.** `is_monthly_expiry` is the third-Friday rule
(a Friday in days 15–21 — the unique Friday that can be the third), living
in the sanctioned `time/` home because `weekday()` arithmetic outside
`time/` is banned by the AST lint; callers import it rather than reopening
the arithmetic. Its approximation is NAMED in the docstring: when the
exchange closes on a third Friday (Good Friday, rare) the traded expiry
moves, typically to the Thursday before — the predicate answers the
calendar question only.

**The bridge's `--bars-mode atm-grid`** (landed by the parallel bridge
agent): the bar-selection strategy that feeds this lane — an ATM ± band
strike grid per in-band expiry per master, monthly-filterable, deduped per
contract life. Full profile and cost model in runbook §6.1.

## 2. THE BID/ASK FINDING (disclosed, not papered over)

The M3 quote-bearing shapes have NO null mechanism for the quote sides:
`OptionQuoteSnapshot`, `QuoteEvent`, `OptionChainEntry` and `OptionDayFile`
all carry REQUIRED non-negative `bid`/`ask` (and `open_interest`) fields —
and this tier carries none of them (probed 2026-08-21: the snapshot endpoint
answers HTTP 200 `NOT_AUTHORIZED`). A VWAP-only bar therefore CANNOT be
encoded as an M3 chain entry without fabricating a two-sided market:
`bid=ask=vwap` would be a fabricated locked market, and the capability
record already says a bar close is not an executable price.

The overlay encodes this honestly through the surface's own mechanisms,
never fabrication:

- `entry_for` raises `ValueError` — the surface's not-in-file signal — so
  the unmodified `OptionPitSurface.candidate_snapshot` answers its
  documented all-None NOT_EVALUABLE path (`abs_delta=None`, `bid=None`, …).
  That is the filter's NOT_EVALUABLE discipline, the same encoding the Cboe
  lane uses for not-delta-bearing rows: exclusion is the only fail-closed
  encoding.
- `day_file` / `quote_history` raise `MassiveCapabilityError` naming the
  tier: those reads REQUIRE a file-level underlying bid/ask pair and quote
  sides that no free-tier endpoint serves, and the spot proxy is DECLARED
  INPUT, not a quote — it must not be laundered into one.
- Derived reads live on this overlay's own surface: `derived_quote`,
  `derived_quotes_for`, `derived_stats`.

**G3 PACKET IMPLICATION — the lane's headline ask.** The amendment window
must extend the surface schema with a VWAP-based quote kind (or optional
quote sides) before any candidate wiring. This is a protocol-visible change
— new shapes, not new values poured into old shapes — and is exactly what
the packet exists for. Until it lands, this module deliberately provides
surface reads only: there is no derived candidate-inputs builder here, and
`tree_options.data.massive_options.build_option_candidate_inputs` keeps
raising unconditionally so the lane cannot be wired into candidates by
accident.

## 3. Counted-not-fatal taxonomy

A derivation problem is recorded, counted in `derived_stats()`, and named —
never raised, never silently dropped (the M4-A zero-greeks discipline): the
overlay is always loadable evidence of exactly what the tier could and could
not derive. Census identities: `cells == derived_ok + stale + no_bar +
refused` and `bars == derived_ok + stale + refused`.

Per-(contract, session) cell classes:

| class | meaning |
|---|---|
| `stale` | a bar more than `staleness_sessions` (default 5) overlay sessions behind the capture frontier provides NO derived quote — a stale VWAP is never carried forward |
| `no_bar` | a session with no bar is no trade (NOT "price unchanged") |
| refused: zero-volume bar | no trades — the VWAP token is not a trade price |
| refused: bar after expiration | the contract's life is over |
| refused: no spot proxy | the underlying has no declared close that session; the lane refuses rather than guess a spot |
| refused: `MassiveDerivationError` | premium under intrinsic or outside the vol bracket — the four refusal classes of §1 |

Master-row classes — each NAMED in `refused_master_contracts` (the
row-accounting discipline: never a silent drop):

| refusal | why |
|---|---|
| nonstandard deliverable | `shares_per_contract != 100`; the `OptionContract` schema demands a `corporate_action_id` this tier cannot know |
| off-cent-grid strike | the canonical contract id cannot round-trip it |
| non-OCC ticker (or a ticker disagreeing with its own typed row) | vendor corruption — refuse rather than pick a side |
| identity restatement across as_ofs | underlying/expiry/strike/side/style/shares disagree — refused rather than pick |
| canonical-id collision | two tickers mapping to one canonical id (the M4-A SPX/SPXW pattern): first kept, the collision refused and audited |

Bar series no master owns are named in `unmatched_option_tickers` and
excluded from the census — counted, never silently dropped.

## 4. Test inventory

Counts read from the files and confirmed by collection at this branch state
(the gate is the authority at the final head):

| file | cases | composition |
|---|---|---|
| `tests/unit/test_massive_derived.py` | 17 | 11 functions; the round-trip test parametrized strike (3) × call_put (2) = 6, non-positive premium (2) |
| `tests/unit/test_massive_overlay.py` | 32 | 32 functions, no parametrize |
| `tests/unit/test_monthlies.py` | 19 | 8 functions; the window invariant parametrized over the 12 third Fridays of 2026 |
| `tests/unit/test_capture_massive_structural.py` | 53 | 35 pre-existing + 18 new (atm-grid selection, flag validation, wire routing) |

Full-suite inventory at this branch state: **843 test items across 48
files** (collection count; the gate runs them under `-W error`).

Mutants: M159+ cover this lane, registered in `scripts/mutate.py` by the
parallel agent (the registry ended at M158 on `main`). Count: 12 (M159–M170, harness total 169).

Gate at the final head: to be recorded. Authority runs at 80eaeb1..63a99f8: pytest -W error 843 passed; mutations 169/169 KILLED, restoration TRUE (/tmp/m4c-mutations.json). The single gate (m0_gate.sh, log /tmp/m4c-gate.log) runs at the final head; any deviation is disclosed before merge.

## 5. Nonclaims

1. **No candidate wiring.** `build_option_candidate_inputs` in
   `massive_options` still raises unconditionally and no derived builder
   exists; nothing here feeds the M3 candidate filter or any backtest fill.
2. **No research claims.** Nothing here feeds trials, criteria, or
   leaderboard numbers; this is evidence of capability, not of performance.
3. **BS-on-American is an approximation with a named failure region.** The
   M4-B sample is 100% American-style, the shared pricer is European
   Black-Scholes, and the flat `dividend_yield=0.0` is a declared assumption
   — the approximation degrades exactly where early exercise has value:
   deep-ITM contracts on dividend-paying underlyings.
4. **Volume is flow, not inventory.** Bar volume is contracts traded that
   session; open interest (inventory) does not exist on this tier and is
   never inferred from volume.
5. **No spread data at $0.** No bid or ask exists anywhere in this lane, so
   spread/midpoint filters remain NOT_EVALUABLE (§2).
6. **Protocol and registry untouched.** `research_protocol.yaml` stays
   frozen, no world-registry entries were added, and the DTE-band constants
   remain restated in the bridge pending the G3 amendment window.
