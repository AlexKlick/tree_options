# M3 — options schema/coverage spike (documentation only)

Scope: schema/coverage research from free samples; no production code, no
real-data ingest, no options claims beyond the samples. Date: 2026-08-18.
Sources are cited with access dates; facts are marked **[observed]** (from
the downloaded sample), **[documented]** (vendor page), or **[unverified]**
(could not be loaded from here — check at adapter time).

The downloaded sample and readme are RETAINED with hashes at
`~/m2-evidence/cboe-sample/` (zip sha256 `981be1aafe6970e7…`, CSV
`e45af427934177f9…`, readme `042804c20a146425…`; full list in its
SHA256SUMS.txt) so every [observed] count below is independently
recomputable.

## 1. Field inventory mapped to our schemas

**Cboe DataShop "Option EOD Summary"** ([product page], accessed
2026-08-18) — sample downloaded and inspected **[observed]**:
`UnderlyingOptionsEODCalcs_2023-08-25` (the Calcs variant; 34 columns,
one row per contract per day):

- identity: `underlying_symbol, quote_date, root, expiration, strike,
  option_type (C/P), delivery_code`
- option OHLC + `trade_volume, vwap, open_interest`
- TWO NBBO snapshots per contract: 15:45 ET (`bid_size_1545, bid_1545,
  ask_size_1545, ask_1545`) and EOD (`…_eod`), each with the underlying's
  own `underlying_bid/ask` at the same instant
- Calcs add-on (paid): `implied_underlying_price_1545,
  active_underlying_price_1545, implied_volatility_1545, delta/gamma/
  theta/vega/rho_1545`

Mapping onto existing schemas:

| Our type | Source fields | Gap |
|---|---|---|
| `OptionContract` (schemas/options.py) | root/expiration/strike/call_put + listing window inferred from first/last appearance | listing_start/end NOT delivered — must accumulate; `multiplier`/exercise_style assumed 100/american [unverified per-file] |
| `QuoteEvent` (schemas/market.py) | each snapshot → one QuoteEvent (execution_at = 15:45 / close) | none for EOD research; intraday NBBO streams need Databento-class feeds |
| `CandidateSnapshot` inputs (candidates/filters.py) | delta ← Calcs ONLY (paid add-on) or computed; OI ← `open_interest`; same-day volume ← `trade_volume`; spread ← bid/ask+sizes; underlying liquidity ← `underlying_bid/ask` | delta is the one filter input not in the base product; earnings-span flag not in ANY options product (separate source, stays deferred) |

**Databento OPRA** [documented at a high level; page is JS-rendered here —
verify before purchase]: definition/mbp-10/trades/statistics/ohlcv schemas
per publisher; usage-priced per GB with free trial credits. Relevant only
if intraday execution modeling is ever needed — out of scope for the
long-only EOD MVP.

## 2. Symbology vs DeliverableSpec

- Cboe delivers DECOMPOSED columns (root, expiration, strike, C/P) — no
  OCC symbol string parsing needed **[observed]**.
- Standard OCC symbol encoding (root + YYMMDD + C/P + 5-digit integer
  strike in thousandths, with the ≥1000-strike shift) **[unverified —
  optionsclearing.com refused connections from this host]**; only needed
  if a vendor delivers symbols instead of columns.
- Adjusted contracts: after corporate actions OCC appends/mangles the
  ROOT (e.g. `SPY1`); nonstandard deliverables are exactly our
  `standard_contract_flag=False` + `DeliverableSpec.corporate_action_id`
  path. The sample's `delivery_code` column exists but was EMPTY on all
  32,672 rows (sample contains only standard SPY/SPX/TSLA/VIX roots)
  **[observed]** → adjusted-deliverable coverage must be re-verified
  against a corporate-action-affected underlying at adapter time
  **[unverified]**.

## 3. PIT availability semantics

- Snapshots are stamped INTRADAY (15:45 ET and close) but the FILE is a
  daily product; historical purchase availability is **T+1** **[documented
  in sample readme]** → `available_at` (our convention) = a next-session
  retrieval instant; a 15:45 snapshot is knowable no earlier than T+1.
- The T+1 DATE is documented; the publication HOUR of day is not
  **[unverified]** — so this spike claims only "available on the next
  session", NOT a zero-hour leak window. At adapter time the actual file
  availability clock time must be recorded and the 10:00 ET entry window
  re-checked against it.
- Restatement policy unknown **[unverified]** → treat every purchased
  file as frozen-at-receipt (the M1 archive discipline: ingest the exact
  bytes, manifest them, never re-download over history).
- Two snapshots ≠ two revisions: 1545/EOD are distinct observations, both
  knowable at T+1 — model as two QuoteEvents per contract-day, not as a
  revision chain.

## 4. Coverage statistics (sample, 2023-08-25)

Sample is a DEMONSTRATION SUBSET (readme says so) — 4 underlyings,
32,672 rows **[observed]**:

| Metric | SPY | TSLA | ^SPX | ^VIX |
|---|---|---|---|---|
| contracts | 7,630 | 4,238 | 19,366 | 1,438 |
| expirations | 29 | — | 62 total across sample | — |
| strikes | 270 (120..720) | — | — | — |
| median 15:45 spread/mid | 1.87% | — | — | — |
| max open interest | 181,505 | — | — | — |
| contracts traded | 3,501 of 7,630 (46%) | — | — | — |
| zero-IV rows | 653 (deep/locked quotes) | — | — | — |

- Weekly (near-daily) expiry grid confirmed for SPY: 08-25, 08-28, 08-29,
  08-30, 08-31, 09-01, … **[observed]**.
- Zero-bid / zero-IV contracts exist and are numerous → the
  `NOT_EVALUABLE`/`as_tradable` rejection paths will carry real weight,
  exactly as the candidate filter was designed.

## 5. Storage and cost estimate + vendor shortlist

Storage **[estimate from observed ~90 bytes/row compressed]**: full-universe
EOD volume is commonly ~2–5M contract-rows/day **[assumption — verify with
one paid day or Databento credits before any purchase]** → ~180–450 MB/day
compressed, ~45–115 GB/year, ~500 GB–1 TB for 2012→2026. Plan for parquet
partitioning by year+underlying at adapter time.

Vendor shortlist (prices accessed 2026-08-18; checkout currencies change —
re-verify at decision time):

| Vendor | Tier | Notes |
|---|---|---|
| [ThetaData](https://www.thetadata.net/pricing) | free 30d EOD; Standard $80/mo; Pro $160–200/mo | cheapest research-grade EOD+quotes; free tier enough for adapter shakedown |
| [Polygon / massive](https://massive.com/pricing) | $29 / $79 / $199/mo | deepest history (20+ yr) at the top tier |
| [EODHD](https://eodhd.com/marketplace/unicornbay/options) | $29.99/mo | ~6k US underlyings but only ~2.5 years of options history |
| [Cboe DataShop](https://datashop.cboe.com/option-eod-summary) | per-selection historical purchase (checkout-priced); greeks = paid Calcs add-on; index underlyings need CGI license from $1k/mo | the sample's exact product; 2012→present; NBBO+underlying quotes included |
| [Databento](https://databento.com/options) | usage per GB, free trial credits | only if intraday depth is ever needed |

## 6. Implications for the v2 options-overlay generator

1. Emit TWO QuoteEvents per contract-day (15:45 + close) with sizes —
   feeds `select_quote`/`as_tradable` unchanged.
2. Strike grid: ~±3x spot in observed width for a mega-name (SPY 120..720
   at spot ≈ 440); v1 overlay can be narrower (±30%) with a fat expiry
   tail (weekly near + quarterly far, ~20–30 live expiries for big names).
3. Spread model: ~1–2% of mid at-the-money, wider on wings; zero-bid
   tails must EXIST so NOT_EVALUABLE is exercised on synthetic chains too.
4. OI/volume: concentrate near ATM/short tenor; ~half of contracts
   untraded on a given day.
5. Greeks: the generator KNOWS its implied process — plant IV per
   (underlying, expiry) and derive delta analytically so filter inputs
   stay internally consistent (do not import vendor greeks semantics).
6. `underlying_bid/ask` in every snapshot lets the candidate filter's
   underlying-liquidity rule run WITHOUT the parked equity vendor.
7. Earnings-span flag remains unsourceable from options data — stays a
   declared non-goal until an events source exists.

## Nonclaims

Sample is a 4-underlying demonstration subset on one session; full-market
row counts, delivery_code semantics, OCC string encoding, Databento
schema specifics, and every price are UNVERIFIED beyond the cited pages
on the access date. Nothing here licenses ingest of real options data —
that remains an owner-gated vendor decision.

[product page]: https://datashop.cboe.com/option-eod-summary
