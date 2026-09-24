# DESK-BT-001: modeled options expressions of the two direction signals (PRE-REGISTRATION)

Written 2026-09-23, before any option P&L was computed for any signal entry
and before the spread haircut was measured. Sealed by `DESK-BT-001.md.sha256`
in the commit that adds this file. Plan: options desk D7
(`~/.claude/plans/ok-those-charts-need-eager-bumblebee.md`), "DESK-BT-001".
Playbook: `data/desk/playbook/v1.toml` (sealed in the same commit).

**Every output of this study is labeled MODELED.** It prices history from
daily VWAP bars plus a haircut, not from fills. It can support or fail to
support an options expression; it can never promote, size or enable a row.
Forward sealed cards decide (at least 20 resolved per family, plan D7).

What was looked at before sealing: the protocol rows (PROTOCOL-XSMOM.md,
46 months; PROTOCOL-PEAD.md, 144 cards, 63 beats), which the desk's signal
code reproduces exactly (tests/unit/test_desk_protocol_golden.py); the
IVHIST-001 description of the on-disk option bars. No option bar was
priced for a signal entry, no placebo was drawn, no chain spread was
summarized.

## Question

Do the two surviving direction signals (XSMOM-TOP3, the no-skip ranking
sealed by the 2026-09-23 operator ruling; PEAD beats) earn money when
expressed with the short-dated option structures the on-disk bars allow,
after realistic costs, and more than the same structure on a date-matched
non-signal name?

## Data (all read-only)

- **Option bars:** the Polygon daily bars in `artifacts/massive-cache`
  (the IVHIST-001 cache and reader rules: monthly-traded expiries,
  standard roots only, duplicate bars that disagree are dropped). Bars
  exist for the original 29 names only; SMH, SOXX, XLE, XLV, XLF and GLD
  entries are NOT_EVALUABLE (no bars), TQQQ/SQQQ picks have no options
  expression (logged, never substituted). The capture holds ATM +/-3
  distinct strikes of monthlies picked at 30-60 DTE, calls and puts.
- **Spot:** the underlying's `adjusted=false` daily VWAP (the IVHIST-001
  spot). Forward F = S * exp(r * tau / 365), r = DTB3 as IVHIST-001, q = 0.
- **Panel:** `artifacts/paper-trades/ohlc-panel.json` (for the signals and
  the equity comparison), read under its shared lock; sha256 recorded.
- **Sealed earnings calendar:** `earnings-calendar.json`; sha256 recorded.
- **Chain store:** `artifacts/desk-store/chains/<D>/<SYM>.json.gz`, only
  its FIRST 20 recorded sessions, for the haircut.
- **Sessions:** the trex NYSE calendar (2025-01-09 is not a session).

## Entries

- **XSMOM:** every first session of a month from 2024-09-03 through
  2026-08-31 on which `desk.signals.xsmom_top3` fires; each of its top-3
  names is one entry.
- **PEAD:** every session in the same window on which
  `desk.signals.pead_beats` fires (move >= +1.5% on the first post-report
  session of a sealed-calendar report); each beat is one entry.
- The entry session D is the signal session; the entry price is D's VWAP.

## Structures (per entry: name X, session D)

- **Expiry:** the LATEST monthly expiry with 30 <= DTE <= 60 calendar days
  at D among those with bars on D. None: NOT_EVALUABLE.
- **ATM:** among strikes of that expiry with a call AND a put bar on D, the
  strike K0 nearest F (ties: the lower), only if |ln(K0/F)| <= 0.03; else
  NOT_EVALUABLE. Strike ranks count distinct strikes with bars on D.
- **S1 long ATM call:** buy the K0 call.
- **S2 call debit spread:** buy the K0 call, sell the call 2 ranks above K0.
- **S3 bull put credit spread:** sell the put 1 rank below K0, buy the put
  3 ranks below K0.
- **Exit session E:** the earlier of D + 20 NYSE sessions and the last
  session with DTE >= 7. Every leg is priced at its VWAP on E.
- A leg without a bar on D or on E makes the (entry, structure)
  NOT_EVALUABLE. No other strike, expiry or session is substituted and no
  price is carried forward. The count of such cases is reported (it is a
  selection on traded contracts; disclosed, not corrected).

## Pricing

- **Haircut h (per name):** over the first 20 recorded chain sessions, the
  contracts of the name with a monthly expiry at 30 <= DTE <= 60,
  |ln(K/F)| <= 0.10, bid > 0 and ask >= bid; per contract-session the
  half-spread fraction (ask - bid) / (ask + bid). h = its median. A name
  with fewer than 50 such contract-sessions uses the median pooled over
  all 29 names. Measured once, recorded with the chain manifests' sha256,
  never re-measured.
- **Fill:** a leg bought (to open or to close) pays VWAP * (1 + h); a leg
  sold receives VWAP * (1 - h). **Stress:** 2h.
- **Commission:** 0.65 USD per contract per leg, at entry and at exit.
- **Quantity:** 1 per leg (x100).
- **Return per trade:** r = P&L / max loss, where max loss is the debit
  paid including commissions (S1, S2), or width - credit received plus
  commissions (S3). The share of trades whose max loss exceeds the 500 USD
  per-trade limit is reported (information only; all evaluable trades
  count).

## Comparisons

- **Equity card (reported, decides nothing):** the same (X, D, E):
  close(E)/close(D) - 1 - 5 bp from the panel. Per variant: mean and
  per-trade Sharpe of the option r and of the equity return, and their
  correlation. A variant whose per-trade Sharpe is below the equity
  card's on the same entries is labeled `equity-dominated`.
- **Date-matched placebo:** for each evaluable signal (X, D, structure),
  the candidates are the names on D for which the same structure is
  evaluable and which did not fire the same signal on D. The placebo is
  candidate number int(sha256("DESK-BT-001|D|X|structure")[:16], 16) mod n
  of the candidates sorted by name. No candidate: the pair is dropped
  (counted).

## Variants and tests

Six variants, no more: {XSMOM, PEAD} x {S1, S2, S3}. Per variant, with T
evaluable signal trades:

- T < 20: NOT_EVALUABLE.
- **(a)** mean r > 0 at the base haircut.
- **(b)** paired placebo: d = r_signal - r_placebo per pair; average d
  within each entry session (the cluster); one-sided t-test that the mean
  of the k session means exceeds 0, Student t with k - 1 degrees of
  freedom; p < 0.05. k < 10: (b) NOT_EVALUABLE.
- **(c)** deflated Sharpe (Bailey and Lopez de Prado 2014): SR = mean(r) /
  sd(r) (sd with T - 1), skew g3 = m3 / m2^1.5, kurtosis g4 = m4 / m2^2
  (population moments). SR0 = sqrt(V) * ((1 - gamma) * PhiInv(1 - 1/N) +
  gamma * PhiInv(1 - 1/(N * e))), gamma = 0.5772156649, N = 6 always, V =
  the sample variance of SR across the evaluable variants. DSR = Phi((SR -
  SR0) * sqrt(T - 1) / sqrt(1 - g3 * SR + (g4 - 1) / 4 * SR^2)). Pass:
  DSR >= 0.95. Fewer than 3 evaluable variants: (c) NOT_EVALUABLE.
- **(d)** mean r > 0 at the stress haircut 2h.
- **Variant verdict:** PASS iff (a), (b), (c) and (d) all pass; FAIL if
  any fails; NOT_EVALUABLE if T < 20 or (b) or (c) is not evaluable.
- **Per signal:** SUPPORTED (MODELED) iff at least one of its three
  variants passes; otherwise NOT SUPPORTED (MODELED).

Every cell is reported, pass or fail. Nothing is re-gridded: the window,
structures, strike ranks, exit rule, haircut rule, placebo draw, tests
and bars stay as written. A new idea is a new pre-registration.

## Earliest run date and procedure

- The haircut needs 20 recorded chain sessions. The recorder's first
  session is 2026-09-22; if no session is missed the 20th is 2026-10-19
  (recorded by its early-morning retry), so the **earliest run date is
  2026-10-20**, later by one session for every session the recorder
  misses (the manifests decide).
- The run code (`src/tree_options/desk/bt001.py`, not yet written) is
  written, tested and committed after this seal and before the run; the
  run records the code commit, this file's sha256 and every input sha256.
  An ambiguity found while writing it is resolved in a separately sealed
  `DESK-BT-001-clarifications.md` BEFORE the run, and a clarification may
  only narrow a rule, never choose between outcomes.
- One scored run, under `host-work`, into
  `DESK_STORE/evaluations/DESK-BT-001.{json,md}`. A rerun is a
  reproducibility check to a temp path, never a new verdict.

## Consequences (fixed now)

- No verdict changes the playbook, a drift weight, a row or the rails
  automatically, and none promotes a family: forward cards decide.
- A signal NOT SUPPORTED (MODELED): its playbook rows keep trading in paper,
  marked "options expression not supported by the modeled history", and
  the operator is asked whether a v2 playbook sets their drift weight to
  0. SUPPORTED changes nothing either.
- `equity-dominated` variants are listed for the operator.

## Known limits (declared, not remedies)

- The modeled structures are 30-60 DTE; the playbook's signal rows use
  120-180 (R1), 45-90 (R2) and 60-120 (R3) DTE. The study speaks to the
  signals' short-dated expressions only.
- VWAP is a day average of trades, not a quote; the haircut is measured on
  2026 closing quotes and applied to 2024-2026 bars.
- The window (2024-09..2026-08) had no 2020-style crash; S3 is short
  premium.
- The XSMOM entries are 24 months x 3 names (fewer after exclusions); the
  PEAD beats about 60. T >= 20 may fail for some variants.
- Extended-hours contamination of the VWAP spot on after-close earnings
  days (IVHIST-001 observation) touches PEAD entry sessions' moneyness.
