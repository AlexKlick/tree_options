# M4 G2 resolution — the $0 derived-quote workaround

- ruling (owner, 2026-08-21): **no money spent — find a workaround.** The
  sealed-window purchase lane (Massive Advanced / Cboe DataShop /
  ThetaData) is CLOSED. G2 resolves to a free-tier workaround lane.
- status of the premise: **live-proven the same day** (probe below).
- companion docs: `m4-vendor-decision-brief.md` (purchase rows closed),
  `m4-b-coverage-era.md` (the era feeding this), evidence §10/§11 of
  `m4-b-massive-structural.md`.

## 1. The blocked → workaround matrix

What the free tier withholds is quotes/greeks/OI. What it SERVES is the
PIT contract master, per-contract DAILY AGGREGATES (o/h/l/c/**vwap**/
**volume**/**trade count**), and equity spot. The workaround maps every
blocked capability onto what is served, with an explicit provenance
class for each field — that classification is what the G3 amendment
packet will ratify:

| blocked capability | $0 workaround | provenance class |
|---|---|---|
| executable price (INV-11 fills) | **daily VWAP of the option contract** — the realized volume-weighted average traded price; fills participation-capped by that day's volume; a zero-volume or bar-less day is UNFILLABLE (fail-closed, unchanged) | vendor fact |
| `abs_delta` (candidate filter) | **model-implied delta**: invert the repo's Black-Scholes pricer (`synth_options/greeks.bs_price`) on the VWAP premium to get IV, take `bs_abs_delta` under that IV | derived from vendor facts + disclosed model |
| open interest (liquidity filter) | **daily volume + trade count** as FLOW proxies (optional upgrade: OCC publishes free daily volume/OI downloads, ~24 months — page bot-blocks the fetcher; verify via browser before relying on it) | vendor fact (flow), NOT standing inventory |
| bid/ask spread band | **no substitute at $0.** Coarse intraday range (h−l)/vwap exists but is not a spread; the filter's spread term is DROPPED with disclosure, not approximated silently | nonclaim |

Two framing facts that make this defensible rather than a hack:

1. **Vendor greeks are also model outputs.** The M4-A Cboe sample's
   `delta_1545` was the vendor's Black-model computation on their quote
   snapshot. Our derived delta differs in whose model (ours, pinned in
   the repo, testable) and which input price (daily VWAP, not a 15:45
   quote) — not in kind. Provenance moves from "vendor model" to "our
   model, our disclosure."
2. **Bar sparsity is information, not noise.** An option prints a bar
   only on days it trades. The probe below shows an ATM SPY call with 17
   bar-days in ~35 sessions, volume 4→3,161. Untraded days are exactly
   the illiquidity the filter exists to reject; staleness rules (a
   filter input older than N sessions is NOT_EVALUABLE) encode this.

## 2. Live probe (2026-08-21, cache-absorbed, 0 new requests)

`O:SPY260417C00669000` (K=669, exp 2026-04-17; SPY spot 669.03 on
2026-03-16), daily bars 2026-01-26 → 2026-03-16:

| session | close | vwap | volume | trades |
|---|---|---|---|---|
| 2026-01-26 | 38.82 | 38.86 | 4 | 4 |
| 2026-02-17 | 29.19 | 29.13 | 6 | 6 |
| 2026-03-11 | 21.14 | 21.30 | 16 | 7 |
| 2026-03-12 | 17.20 | 18.02 | 1,512 | 230 |
| 2026-03-13 | 14.58 | 16.94 | 580 | 216 |
| 2026-03-16 | 16.05 | 16.28 | 3,161 | 332 |

17 bar-days, zero zero-volume bars, zero zero-closes, Decimal-exact
tokens preserved end to end. VWAP monotonically tracks the declining
ATM premium; volume clusters near the as_of — the shape the filter and
INV-11 need.

## 3. Honest limits (disclosed, not papered over)

- **VWAP is not bid/ask.** It is a full-day average price; fills at VWAP
  assume participation across the day. INV-11's executable-price
  semantics become "filled at the session's volume-weighted average
  traded price, capped by observed volume" — a WEAKER claim than
  quote-based fills, recorded as such.
- **Volume is not open interest.** Flow, not standing inventory; a
  heavily-traded-but-unwindable contract is indistinguishable here.
- **Black-Scholes on American contracts.** All 63,488 captured rows are
  American-exercise; BS ignores early-exercise premium. For the
  ATM±k band the bias is small and ONE-SIDED (ITM calls with dividend
  points are where it bites); if the backtest ever selects deep-ITM
  names, a binomial pricer upgrade is the fix — recorded as a known
  approximation with its failure region named.
- **Sparse bars** — no bar on a session means no trade; the derived
  fields for that (contract, session) are NOT_EVALUABLE, never
  carried forward silently.
- **No spread data at $0.** The filter's spread term is dropped with
  disclosure.

## 4. Build order (M4-C, the derived-quote lane)

1. `implied_vol` inversion (bisection on `bs_price` against a Decimal
   VWAP; refuse non-positive/degenerate premiums) + derived-greeks
   builder, Decimal-in-float-out at the model boundary only.
2. Bars era (sized: ATM±3 strikes × in-band monthly expiries × era
   slices — a concrete budget table lands with the lane plan; the
   probe's 17-bar shape suggests ~1 request per contract-LIFE, not per
   slice, which the sizing exploits).
3. `MassiveDerivedOverlay` implementing the `OptionPitSurface` seam
   (masters + bars + spot + derived greeks), publication wall unchanged
   (an EOD bar for session t is usable at t+1 09:00 ET — the M4-A wall
   applies verbatim).
4. Capability gate: `build_option_candidate_inputs` stays refusing
   until the G3 amendment packet ratifies the provenance semantics
   (model-derived delta replacing vendor delta is a protocol-visible
   change — the packet exists for exactly this).

## 5. What stays impossible at $0

Real bid/ask spreads, real open interest, vendor-computed greeks, and
intraday anything. Every claim this workaround supports carries its
provenance class; the sealed gate (G4) will gate a DERIVED-quote era,
labeled as one.
