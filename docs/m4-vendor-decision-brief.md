# M4 G2 — vendor decision brief (real-data purchase gate)

Prepared for the owner ruling at gate G2 of `docs/m4-real-data-plan.md`
§2. Everything below is grounded in the M4-A shakedown run
(`docs/evidence-logs/m4/m4-a-shakedown.md`, one session 2023-08-25,
32,672 rows, adversarially verified) and pricing pages re-verified
2026-08-21. Per plan §4 these are schema/coverage facts — no research
claims.

## 1. What the shakedown established

The adapter ingests the Cboe "Option EOD Summary" product end-to-end
into the M3 machinery: 75 new tests, full gate **615 passed** under
`-W error`, mutation **141/141 KILLED** (8 new seam mutants M135–M142),
ruff/mypy/compileall clean; `OptionPitSurface` runs UNMODIFIED on real
parsed data with the T+1 wall, candidate snapshot, and manifest
round-trip all live-verified. Coverage on the demo session:

| underlying | mapped | contracts | expiries | ATM spread median | notes |
|---|---|---|---|---|---|
| SPY | 7,599 | 7,630 | 29 | 1.17% | p90 13.33% |
| TSLA | 4,075 | 4,238 | 19 | 1.67% | p90 3.54% |
| ^SPX | 16,277 | 16,394 | 50 | 1.29% | +2,972 SPXW collisions refused |
| ^VIX | refused | — | — | — | no bid/ask in either variant |

ATM spread medians land inside the spike §6 prior band (1–2% of mid) —
the M3 spread model is calibrated correctly against real quotes.

## 2. Findings that constrain the purchase

1. **^SPX/SPXW root duality** (P3, verified): the canonical id
   (underlying, expiry, strike, C/P) cannot distinguish SPX (AM-settle)
   from SPXW (PM-settle); ~15% of ^SPX rows this session collide and
   the two rows are *different instruments* (different quotes, delta,
   OI). Any sealed era containing index options must either configure
   the vendor to a single root family or extend the contract id with
   the root. An **equity-only universe sidesteps this entirely**.
2. **^VIX is unusable** for the quote-based filter (no bid/ask at all,
   confirmed against the readme) — exclude from any universe.
3. **Index underlyings require the cgi_or_historical variant** (no_cgi
   zeroes their underlying quotes; the adapter refuses rather than
   ingest zeros). Equity names are unaffected.
4. **Zero-greeks rows** (deep-ITM, license-flat): counted, kept in the
   contract master, excluded from candidates (NOT_EVALUABLE) — handled.
5. **`underlying_20d_median_dollar_volume` is unavailable** from this
   product (declared 0-sentinel): the candidate filter's liquidity
   threshold must be 0 for real data, or the universe restricted at
   purchase time to names that pass it by construction (the equity-only
   top-N restriction does this — same justification as the M3 §9.2
   liquidity rule).
6. **Holidays**: the publication clock skips weekends only; a holiday
   approximation is disclosed (verify agent probed Labor Day). A
   multi-session era should add the repo calendar before any sealed
   run.

## 3. Scale extrapolation (estimates, plan §5 of the spike)

- Observed: ~4k–16k contract-rows per underlying-day for liquid names
  (index heavier than equity).
- Equity-only top-100 universe ≈ 100 × ~7k ≈ 0.7M contract-rows/day ≈
  ~60 MB/day at ~90 B/row compressed → **~15 GB/year, ~100 GB for a
  2016→2026 window**. Top-30 trims that ~3×.
- The synthetic design-down grid (M3 plan §7: ~840 quote
  rows/underlying-day) bounds what the *backtest touches*, not what
  ingest stores — storage is a purchase/adapter-partitioning concern
  (parquet by year+underlying per the spike).

## 4. Vendor options (prices re-verified 2026-08-21; supersede the
spike table on two rows)

| Vendor | Cost | History | Fit |
|---|---|---|---|
| **Polygon Options Basic** | **$0/mo** | 2 yr EOD | free multi-session coverage era; 5 calls/min rate limit (a top-30 × 2 yr pull ≈ 15k requests ≈ ~2 days of throttled pulling); different schema → small adapter variant |
| ThetaData Standard | $80/mo, cancel anytime | 8 yr | cheapest deep history; **one-month bulk download ≈ $80 total** for a fixed window; 12 yr at Pro $160 |
| Cboe DataShop | one-time, per-selection (checkout-priced) | 2012→present | **schema-identical to the adapter just proven**; per-selection aligns with an equity top-N universe; historical purchases include index values (cgi) regardless of license |
| Polygon Advanced | $199/mo | 5+ yr | subscription for what is a one-time historical need — dominated unless quotes matter later |
| EODHD / Databento | — | ~2.5 yr / per GB | too shallow / intraday-only (EOD design) |

Note: ThetaData's free options tier (spike row) **no longer appears on
their pricing page** ($40/$80/$160 tiers only); Polygon's $0 options
tier is new since the spike. Both checked 2026-08-21.

## 5. Recommendation (staged, per the ruled lane)

1. **Now, $0**: owner creates a Polygon account + API key (key file
   under `~/.config/tree_options/`, never committed) → the free 2-year
   EOD tier powers a real multi-session coverage era (~500 sessions,
   equity top-30 to start) — real spread/OI/ladder stats over time,
   holiday handling, and the first look at multi-year root stability
   before any money moves.
2. **At G2 proper** (after the free coverage run): choose the sealed
   window source —
   - **Cboe DataShop one-time** if checkout pricing for an equity-only
     top-N × (2016 or 2018)→2026 selection is reasonable: zero adapter
     risk (proven schema), one-time cost, fixed window matches the
     sealed-era philosophy;
   - **ThetaData Standard one month ($80)** if Cboe's checkout price is
     high or per-name selection is too coarse: cheapest deep history,
     cost of a schema adapter variant + a bulk-download month;
   - equity-only universe either way (dodges items 1–3 and 5 above);
     index options become a separate later ruling if ever wanted.
3. The purchase itself is owner-executed at both steps (spike
   nonclaims carry forward: nothing in this repo licenses a purchase).

## 6. Owner asks (summary)

- Create the Polygon free-tier API key and drop it at
  `~/.config/tree_options/polygon.key` (or export its path) to start
  the free coverage era.
- Rule G2 (vendor + universe + window) when the coverage numbers are
  in — or now, if the framing above is enough.
