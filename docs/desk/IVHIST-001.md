# IVHIST-001: VWAP-derived 30-day ATM IV history vs CBOE vol indices (PRE-REGISTRATION)

Written 2026-09-23, before any implied vol was computed from the cache and
before any comparison with an index. Sealed by `IVHIST-001.md.sha256` in the
commit that adds this file. Plan: options desk D2
(`~/.claude/plans/ok-those-charts-need-eager-bumblebee.md`). Code:
`src/tree_options/desk/ivhist.py`.

What was looked at before sealing: which underlyings have option bars on
disk, the date span of the cached bars, and a coverage count for 5 names
(SPY, QQQ, AAPL, KO, XOM) of how many sessions have a monthly expiry with a
near-ATM call and put on each side of 30 days. That count was about 55% of
sessions bracketed, 25% with only a longer expiry and 15% with only a shorter
one. No IV was solved and no index value other than the CSV's last row was
read.

## Question

Can the free Polygon option daily bars (VWAP per contract per session)
reproduce a 30-day implied volatility that tracks CBOE's own vol indices well
enough to serve as the desk's IV history? That history feeds the IV rank and
percentile, the IV-squared benchmark in FORECAST-001 and the playbook's
cheap/fair/rich condition.

## Data (all read-only)

- **Option bars:** `artifacts/massive-cache/*.json` (key-redacted Polygon
  bodies, file names are hashes). Each file is identified by its `ticker`
  field. `O:<ROOT><YYMMDD><C|P><strike*1000>` is an option bar series. A ROOT
  with digits (for example `XOM1`, an adjusted deliverable) is excluded. Files
  holding the same ticker are merged by session; a session whose duplicates
  disagree is dropped for that contract. Bodies are decoded with
  `massive_client.loads_exact` and typed by `massive_options.parse_daily_bars`.
  Bars exist for the original 29 names only. There are none for SMH, SOXX,
  XLE, XLV, XLF, GLD, TQQQ or SQQQ.
- **Unadjusted spot:** the underlying's Polygon `adjusted=false` daily bars
  from the same cache. The research panel is split-adjusted and is not used
  here. Spot on session D is the underlying's daily VWAP on D. The option
  price is itself a VWAP, so this is the most synchronous pairing on disk. If
  cached files disagree on D, D is NOT_EVALUABLE for that name.
- **Rate:** FRED DTB3 snapshot
  (`DTB3.csv`, sha256 `8e9cfa22d49da3141e2d44d34e9acb5967614a3ff444947213d7b1b558385dcc`,
  fetched 2026-09-23). r(D) = DTB3 on the latest observation dated on or
  before D, divided by 100, used as the continuous rate. The error from
  skipping the discount-to-continuous conversion is under 15 bp. At the ATM
  call/put average it is negligible.
- **Dividends:** q = 0 for every name (declared). Averaging the call and put
  IV at one strike cancels the first-order dividend error and the
  first-order early-exercise error.
- **Benchmarks:** CBOE daily index history CSVs
  (`cdn.cboe.com/api/global/us_indices/daily_prices/<X>_History.csv`,
  snapshot 2026-09-23). The index value is the CLOSE column, or the only
  value column for GVZ. sha256:
  - VIX `c1fa255f4e659d217f8cf244310a126517dc8f832568afe6be8184f46ca6dae7`
  - VXN `9eec3e55fc98dcb4271415ac7ee1f71c6cd52d5915d83cd45f49ed17c4d18e18`
  - RVX `58b00b16a1f4eb29140b7fd06735350eb94592028eb0c1b6f50c03cd310b1e37`
  - VXAPL `8bd039b4022cedebeffa138a6e806c0b8b8795722a1aed85fe8538b8210b7e5b`
  - VXAZN `fb04c6d76d2bffe3332560650645ce2921fd9713f2a5b6227a821f69f6a22eb3`
  - VXGOG `7c8344211027e6d542bc504e2bb0d6a8230337040c8b1c09b704260cc5944b42`
  - GVZ `102c7a3ba143303bfcaff722f5e2f99e649100c636c7f2b0652de48c1d54b0d0`
- **Sessions:** NYSE sessions 2024-08-26..2026-09-03 inclusive, from the trex
  calendar.

## Method: iv30 per (name, session D)

1. **Eligible expiries.** Monthly expiries only: the third Friday, or the
   last session before it when that Friday is not a session. Calendar DTE
   tau = days from D to the expiry, with 8 <= tau <= 90.
2. **Per expiry.**
   - Strikes K with both a call bar and a put bar on D, VWAP > 0.
   - Forward F = S * exp(r * tau / 365) (q = 0). Keep |ln(K/F)| <= 0.10.
   - For each strike, solve IV_C and IV_P with `massive_derived.implied_vol`
     (bisection on the hash-pinned `bs_price`, bounds 1e-4..5.0, price
     tolerance 1e-10). A strike counts only if both solve.
   - iv_K = (IV_C + IV_P) / 2.
   - ATM IV = linear interpolation in m = ln(K/F) between the nearest usable
     strike with m <= 0 and the nearest with m >= 0.
   - If only one side exists, use the nearest strike's iv_K when |m| <= 0.03.
     Otherwise the expiry is not evaluable.
3. **30-day constant maturity.**
   - When evaluable expiries bracket 30 days (nearest tau1 <= 30 <= tau2),
     interpolate total variance linearly in tau: w = iv^2 * tau,
     iv30 = sqrt(w30 / 30). Method `interpolated`.
   - Otherwise take flat IV from the evaluable expiry nearest 30 with
     15 <= tau <= 60. Method `extrapolated`.
   - Otherwise the session is NOT_EVALUABLE, with a reason.
4. **Output.** `DESK_STORE/iv-history/vwap_atm.json` holds, per name per
   session: iv30, method, spot, rate, the expiries used (tau, ATM IV, strikes
   used), or NOT_EVALUABLE with its reason.

## Benchmark pairs

- VIX ~ SPY
- VXN ~ QQQ
- RVX ~ IWM
- VXAPL ~ AAPL
- VXAZN ~ AMZN
- VXGOG ~ GOOGL
- GVZ ~ GLD

GVZ ~ GLD is **NOT_EVALUABLE by construction**: there are no GLD option bars
on disk. It is declared here, before the run.

## Metrics and pass bar (per pair)

- The sample is every session where both our iv30 and the index close exist.
- The difference is b_D = 100 * iv30_D - index_D, in vol points.
- **Primary metrics:**
  - n
  - median(b)
  - Pearson correlation of the levels (100 * iv30, index)
- **PASS** iff all three hold:
  - n >= 250
  - |median(b)| <= 2.0
  - correlation >= 0.85
- n < 250 means the pair is NOT_EVALUABLE.
- **Reported for information only, deciding nothing:**
  - mean bias
  - median absolute difference
  - correlation of daily changes
  - the same primary metrics on `interpolated` days only
  - the share of `extrapolated` days

## Verdict and labels (fixed now)

- A **benchmarked name** is labeled `ok` if its pair passes, otherwise
  `low-fidelity`.
- **Unbenchmarked single stocks** are the 23 other names with bars. They
  generalize from the single-stock benchmarks only: `ok` iff VXAPL, VXAZN and
  VXGOG all pass, else `low-fidelity`. The index-ETF pairs never promote a
  single stock. No unbenchmarked ETF has bars.
- **Names without bars** (SMH, SOXX, XLE, XLV, XLF, GLD, TQQQ, SQQQ) are
  labeled `not-evaluable`: no history.
- **IVHIST-001 overall:**
  - PASS when all 6 evaluable pairs pass
  - PARTIAL when some pass
  - FAIL when none pass

## What happens on failure

A `low-fidelity` history is still written and may be displayed, for example
as an IV rank with the label shown. It is:
- excluded from the IV-squared benchmark and from the IV blend in
  FORECAST-001
- excluded from any rule that thresholds IV rank or IV level

This holds until a successor pre-registration (IVHIST-002) passes. There is
no re-gridding after results: the strike band, DTE bounds, spot choice,
averaging, extrapolation rule, rate and pass bar stay as written. A new idea
is a new pre-registration.

## Known risks (declared, not remedies)

- **Variance swap vs ATM.** The CBOE indices are model-free 30-day
  variance-swap rates that integrate the wings. An ATM IV sits below them by
  construction, most for index ETFs with a steep put skew. The bar is the
  operator's and is applied unchanged. A FAIL on bias alone is reported as
  exactly that.
- **Timing.** VWAP is a day average; the index is the 16:15 ET close.
- **Proxy mismatches.** SPY stands in for SPX and QQQ for NDX (VXN). These
  are American equity options priced with a European model.
- **Thin prints.** A contract's VWAP can be one print.
