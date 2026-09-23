# FORECAST-001: pooled log-HAR realized-variance forecasts (PRE-REGISTRATION)

Written 2026-09-23, before any forecast was fitted or scored. Sealed by
`FORECAST-001.md.sha256` in the commit that adds this file. Plan: options desk
D4 (`~/.claude/plans/ok-those-charts-need-eager-bumblebee.md`). Code:
`src/tree_options/desk/{rv,har,stats,evaluate}.py`.

Nothing in the panel was examined for this study before sealing. The only
checks were its name list, its first and last sessions, and the documented
META vendor hole.

## Question

Does a pooled log-HAR model with earnings handling forecast realized variance
better than the naive 22-day realized variance, out of sample and strictly
point in time? The answer decides which vol forecast the desk uses for VRP
and for the cheap/fair/rich condition.

## Data (read-only)

- **Panel:** `artifacts/paper-trades/ohlc-panel.json`.
  - Split-adjusted OHLCV from 2021-09-13, not dividend-adjusted.
  - Read under a SHARED flock on `ohlc-panel.json.lock`; its sha256 is
    recorded in the results.
  - Names: the desk's 35-name chain universe, which is the panel minus TQQQ
    and SQQQ.
- **Sessions:** the NYSE calendar
  (`tree_options.trex.clock.session_calendar()`).
- **Earnings:** `artifacts/paper-trades/earnings-calendar.json`, sealed
  (sha256 `916569ad1f81bcf8a074fb8594fc1de26290210eff79a1e92fef7131888c0383`
  at sealing; the value at run time is recorded).
  - **Reporters:** the 26 names with at least one date.
  - **ETFs:** SPY, QQQ, IWM, SMH, SOXX, XLE, XLV, XLF and GLD, which have no
    reports.
  - **Coverage:** the calendar covers report dates from **2024-09-01** only.
    Before that, reporter earnings are UNOBSERVED; see the coverage rule
    below.
- **IV history:** IVHIST-001 (`DESK_STORE/iv-history/vwap_atm.json`), used
  only for names that IVHIST-001 labels `ok`.
- **Cutoff:** the earliest of the 35 names' last panel sessions at run time,
  recorded in the results.

## Definitions

- **Variance proxy**, all logs natural, t-1 the previous NYSE session:

      v_t = ln(O_t/C_{t-1})^2 + 0.5*ln(H_t/L_t)^2 - (2*ln2 - 1)*ln(C_t/O_t)^2

  v_t is missing if either bar is absent, if any price is <= 0 or if
  v_t <= 0.
- **Event pair** of a report dated D: s(D) is the first session on or after
  D, and E(D) = {s(D), the session after s(D)}. Report timing (before the
  open or after the close) is unknown, so the pair covers both.
- **Cleaned proxy** (regressors only): v*_s = v_s off event pairs. On a
  session in any E(D), v*_s = mean(v*_{s-5..s-1}), applied recursively.
- **Regressors at origin t:**
  - x_d = ln v*_t
  - x_w = ln mean(v*_{t-4..t})
  - x_m = ln mean(v*_{t-21..t})
- **Target:** RV_{t,h} = sum_{j=1..h} v_{t+j}, using RAW v, and
  y = ln(RV_{t,h}/h).
- **Earnings count:** N_earn(t,h) = #{D : E(D) intersects (t, t+h]}.
  Calendar dates inside the forward window are treated as known at t. This
  is a schedule assumption: dates are usually confirmed 2 to 5 weeks ahead,
  and quarterly cadence places the rest within days. A misdated event only
  moves the count across a window edge.
- **Row validity:** a row (name, t, h) exists only when every v it needs is
  present: v_{t-21..t+h}, plus the 5 sessions before any event session it
  cleans. The META vendor hole (2022-01-28..2022-06-09) drops the rows that
  touch it.

## Model (per horizon h in {5, 20, 63, 126})

    y = b0_i + bd*x_d + bw*x_w + bm*x_m + be*N_earn + e

- **Estimation:** pooled OLS with one intercept per name (no common
  intercept).
- **Coverage rule.** This is a forced adaptation, fixed now: before 2024-09
  the sealed calendar observes no reporter earnings.
  - A reporter row is **covered** iff its forward window starts on or after
    2024-09-01 (that is, t+1 >= 2024-09-03). Covered rows use the observed
    N_earn.
  - Uncovered reporter rows use N_earn = h/63, the mean count of a quarterly
    reporter (Berkson imputation). Their regressors stay uncleaned because
    the events are unknown.
  - ETF rows always use N_earn = 0.
  - be is estimated only when the estimation sample holds at least 100
    covered reporter rows with N_earn >= 1. Otherwise the N_earn column is
    dropped (be = 0) for that fit.
- **Bias correction:** F^HAR_{t,h} = h * exp(yhat + s^2/2), where
  s^2 = SSR/(n - k) of the fit.
- **Refit schedule:** monthly, expanding window, strictly point in time.
  - At the first session of each month M from 2024-09 on, fit on every row
    with t + h <= L_M, where L_M is the last session before M.
  - Forecasts for origins in month M use that fit and read panel data dated
    <= t only.
  - The future-poison test pins this: altering any data after t must not
    change the forecast at t.

## Benchmarks (raw v)

- **RV22:** F = h * mean(v_{t-21..t}). This is "the 22-day naive".
- **EWMA(0.94):** sigma2_t = 0.94*sigma2_{t-1} + 0.06*v_t, seeded with the
  mean of the first 22 v of each contiguous run of present v. A gap restarts
  the run. F = h * sigma2_t.
- **IV^2:** F = iv30_t^2 * c(t, t+h)/365, where c is the calendar days
  between session t and session t+h. This applies to IVHIST-001 `ok` names on
  sessions with an evaluable iv30. The 30-day IV is used unchanged at every
  h; this is declared as a misspecified comparator away from h = 20.

## Scoring

- **Test sample:** origins t from 2024-09-03 through the last t with
  t + h <= cutoff. Each comparison uses the rows where both forecasts exist.
- **Losses:**
  - QLIKE L = RV/F - ln(RV/F) - 1 (primary)
  - MSE L = (RV - F)^2 (secondary)
- **Diebold-Mariano test:**
  - d_{i,t} = L(benchmark) - L(HAR), and dbar_t is the cross-sectional mean
    over the names present at t.
  - DM = mean(dbar) / sqrt(Omega/T), where Omega is the Newey-West
    (Bartlett) long-run variance with lag h-1.
  - One-sided p = 1 - Phi(DM). H1: HAR has the lower loss.
- **All cells reported:** 4 horizons x 3 benchmarks (RV22, EWMA, IV^2) x
  2 losses. Each cell gives n dates, n rows, both mean losses, DM and p.
- **Also reported:** per-name mean QLIKE (HAR vs RV22), and per fit n, s^2,
  bd/bw/bm/be and whether be was estimated.

## Pass bar (primary)

**PASS** iff pooled HAR beats RV22 on QLIKE with one-sided p < 0.05 at
**both** h = 20 and h = 63. Anything else is FAIL. The other cells are
reported and decide nothing.

## IV blend (encompassing)

The plan says "on train", but no IV history exists before 2024-08-26, so the
training window cannot hold it. Adaptation, fixed now:
- **Blend-train:** IV-ok names, origins t >= 2024-09-03 with
  t + h <= 2025-08-29.
- **Regression:** pooled OLS

      ln RV_{t,h} = a + c1*ln F^HAR + c2*ln F^IV + u

  with Driscoll-Kraay standard errors (date-summed scores, Bartlett, lag
  h-1).
- **Decision:** the blend is **allowed** at h iff c2 > 0 with one-sided
  p < 0.05. With no IV-ok names the blend is NOT_EVALUABLE, which means not
  allowed.
- **If allowed:** F^B = exp(a + c1*ln F^HAR + c2*ln F^IV + s_u^2/2), with the
  coefficients frozen from blend-train. It is scored against HAR on origins
  t >= 2025-09-02 with t + h <= cutoff (QLIKE DM). This is reported for
  information only.

## Failure handling (fixed now)

- **On FAIL:**
  - The desk's forecast input for VRP and cheap/fair/rich is RV22, labeled
    `forecast_source: rv22`.
  - HAR outputs are shown as `unvalidated`.
  - There is no re-gridding of lags, horizons, proxy, earnings handling,
    windows, losses or bar. A successor FORECAST-002 needs its own sealed
    pre-registration.
- **On PASS:** HAR at h = 20 is the desk's forecast input
  (`forecast_source: har`).
- **Forward monitoring:** each month the newly realized windows are scored
  with the same code on a forward-only sample.
  - Demotion: after >= 6 forward months, if HAR is worse than RV22 on QLIKE
    at h = 20 with one-sided p < 0.05, the source becomes RV22.
  - Forward results never promote a FAIL.

## Known limitations (declared)

- **Earnings handling:**
  - Pre-coverage earnings are unobserved: the training regressors carry
    uncleaned earnings spikes, which attenuate the regressors, and N_earn is
    imputed there.
  - Late-July and August 2024 reports fall before coverage, so the regressor
    windows in September 2024 may contain them uncleaned.
  - Report timing is unknown, hence the event pair.
- **Data:**
  - No dividend adjustment: ex-dividend overnight gaps inflate v slightly
    for every forecaster.
  - The test window holds no 2020-style crash.
- **Estimation:** OLS runs on overlapping rows. Only the point estimates are
  used; inference is by DM.
