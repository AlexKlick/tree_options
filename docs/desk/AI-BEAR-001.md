# AI-BEAR-001: a regime-gated bearish program for the AI-bubble names (PRE-REGISTRATION)

Written 2026-11-09, before any trade was simulated or scored. Sealed by
`AI-BEAR-001.md.sha256` in the commit that adds this file. The parameters are
in `data/desk/aibear/AI-BEAR-001.params.json`, sha256
`072ab78199ba2d33dd63f639d6682e35e2d771ded2113b769497ee33559ecd53`, pinned in
code as `SEALED_PARAMS_SHA256`; the scorer refuses any other file.

- Plan: `~/.claude/plans/sequential-mapping-locket.md`, owner-approved 2026-11-09.
- Code: `src/tree_options/desk/aibear/{params,regime_engine,rules,pricing,backtest,history}.py`.
- Run: `python -m tree_options.desk.aibear backtest --out <dir>`.

The first scored run on these sealed parameters is the verdict of record. A
rerun may only reproduce it byte for byte (the same `results.json` sha256). It
may never replace it.

## Question

The owner wants to bet on an AI-bubble deflation that follows the 2000–02
dotcom path. That means three trades: a long-dated bearish core for the
multi-year decline, short-dated put spreads on the falls, and short-dated call
spreads on the relief rallies in between. All three are buy-side, defined-risk
debit verticals.

The research ledger already records shorts as **DEAD at every horizon**
(`artifacts/paper-trades/RESEARCH-LEDGER.md:81-82`). That test was
*unconditional* and ran on 2021-09..2026-09, which is mostly a bull sample.
The same ledger shows time-series momentum acting as downside protection in
both 2022 windows (`:56-61`). So the question here is a different one. **Does
gating the trades by a textbook regime state beat random timing, and does it
make money in bear markets without bleeding too much in bull markets?**

- **H-core:** core put spreads opened only while a name is out of its bull
  state have a positive net expectation. Failing that, they act as insurance:
  they pay in bear eras and lose at most half of what an always-on put
  ladder loses in bull eras.
- **H-falls:** put spreads opened on a failed rally inside a bear trend beat
  random timing.
- **H-relief:** call spreads opened on the first up close after a
  capitulation beat random timing.

## What was examined before sealing (and what was not)

Examined:

- the history files' row counts and first and last dates (below);
- the latest five regime states per name and the current market snapshot
  (2026-11-06, quoted in the plan);
- recorded CBOE chain quotes, for the cost and IV calibrations below.

Not examined: any simulated trade, any P&L, and any historical regime-state
series.

The author knows the general shape of 2000–02, 2008 and 2022, as anyone
does. That is why every threshold is a textbook definition rather than a
tuned value. It is also why this backtest cannot be promotion evidence on
its own (see "What this can and cannot show").

## Data (read-only; fetched 2026-11-09, fetch log sha256 `84d4e04b…70ed89`)

Store `artifacts/desk-store/history/` (`desk/aibear/history.py`), completed
sessions only:

| Series | Source | Rows | First | Last | file sha256 (prefix) |
|---|---|---:|---|---|---|
| NDX | FRED NASDAQ100 | 10294 | 1986-01-02 | 2026-11-06 | 3ebf07bfe9689068 |
| VXN | FRED VXNCLS | 6479 | 2001-02-02 | 2026-11-05 | fe5a5e4b12e055dc |
| SOX | Yahoo ^SOX | 8184 | 1994-05-04 | 2026-11-06 | 54659be5e1d5bd0c |
| CSCO | Yahoo adjclose | 9248 | 1990-02-16 | 2026-11-06 | 1a65f4a2948acedb |
| INTC | Yahoo adjclose | 9281 | 1990-01-02 | 2026-11-06 | 090c4a66719f871b |
| ORCL | Yahoo adjclose | 9281 | 1990-01-02 | 2026-11-06 | d0b3f441460c534d |
| QCOM | Yahoo adjclose | 8787 | 1991-12-13 | 2026-11-06 | 4d14a2fd9119ae84 |
| MSFT | Yahoo adjclose | 9281 | 1990-01-02 | 2026-11-06 | b70565a0547823c7 |
| AMD | Yahoo adjclose | 9281 | 1990-01-02 | 2026-11-06 | fcb7041abca4f898 |
| NVDA | Yahoo adjclose | 6992 | 1999-01-22 | 2026-11-06 | d9f9dca6de06611f |
| AVGO | Yahoo adjclose | 4341 | 2009-08-06 | 2026-11-06 | 95d3937e8f5a44e3 |
| TSLA | Yahoo adjclose | 4116 | 2010-06-29 | 2026-11-06 | c6c8b64b536528bc |
| META | Yahoo adjclose | 3639 | 2012-05-18 | 2026-11-06 | 73b4adeeb91874e2 |
| PLTR | Yahoo adjclose | 1534 | 2020-09-30 | 2026-11-06 | f3f80ef7c9b9ca46 |

Also read: `artifacts/desk-store/indices/VIX.csv` (CBOE, from 1990) for the
regime's VIX ratio and the pre-2001 IV bridge, and `indices/DTB3.csv` (FRED)
for the rate. The scorer records every input's full sha256 in `results.json`.

## Regime states (`regime_engine.py`; checked in this order)

| State | Definition |
|---|---|
| CAPITULATION | drawdown ≤ −20 % AND 20-session return ≤ −10 % AND VIX ≥ 1.25 × its median over the prior 60 readings |
| BEAR | close < 200-day MA AND the 200-day MA is below its value 20 sessions earlier AND drawdown ≤ −20 % |
| RELIEF | the BEAR conditions, but close > 50-day MA (a bear-market rally) |
| TOPPING | close < 200-day MA, OR drawdown ≤ −10 % with close < 50-day MA |
| BULL | everything else |

- Drawdown is measured from the highest close of the last 252 sessions.
- A state needs 252 observations first (`NOT_EVALUABLE` before that).
- Without a VIX reading on the date or within 5 calendar days before it,
  CAPITULATION cannot fire.
- A state at d reads nothing after d (future-poison test in
  `test_desk_aibear_regime.py`).

## Sleeves (`rules.py`)

| Sleeve | Structure | Instruments | Entry signal (close of t) | Exits |
|---|---|---|---|---|
| core | put debit vertical, ATM / 70 %, 270 DTE | NDX, SOX, CSCO, INTC, ORCL, QCOM, MSFT, AMD, NVDA, AVGO, TSLA, META, PLTR | state ∈ {TOPPING, BEAR, RELIEF} | take-profit at 0.80 × width; 10 consecutive BULL closes (then a 20-session cooldown); 90 DTE left (the "roll": re-entry needs a fresh signal) |
| falls | put debit vertical, ATM / 90 %, 45 DTE | NDX, SOX | state BEAR and the prior state RELIEF or TOPPING | take-profit at 0.70 × width; state RELIEF or BULL; 25 sessions held; 10 DTE left |
| relief | call debit vertical, ATM / 110 %, 45 DTE | NDX, SOX | prior state CAPITULATION and an up close | take-profit at 0.70 × width; close below the capitulation episode's lowest close; 20 sessions; 10 DTE left |

- **Timing:** every decision is made at the close of d from data on or
  before d, and executed at the close of d+1. That applies to entries and
  exits.
- **Positions:** one open position per sleeve and instrument. A position
  still open on 2026-11-06 is closed at that close (`open_at_end`).
- **Scoring window:** signals from 1997-01-02 to 2026-11-06.

## Pricing (synthetic; declared assumptions)

- **Model:** Black-Scholes (`synth_options/greeks.py` `bs_price`) on a spot
  normalized to 100 at entry, with no dividends and the DTB3 rate.
- **ATM IV of X on day d:**
  `VXN_eq(d) × 0.8655 × clip(rv60_X / rv60_NDX, 1, 3)^0.78` (ratio 1 for NDX).
  - `VXN_eq` is VXN, or before VXN's first value (2001-02-02), VIX × k_vxn.
    k_vxn is the median VXN/VIX over the first 252 shared sessions.
  - 0.8655 is the median QQQ iv30 / VXN, and 0.78 is the one exponent that
    sets the median actual/model IV to 1.0 across 8 names.
  - Both were fitted on 28 recorded chain sessions (2026-09-22..11-06).
    Per-name medians of actual/model are 0.91–1.11. Log sha256 `5ea48291…f3a02a`.
- **Skew:** leg IV = ATM IV × max(0.5, 1 + 0.5 × ln(S/K)). Flat IV is reported
  as a sensitivity and is never the verdict.
- **Costs:** each leg fill crosses a half-spread of max($0.02, pct × leg mid),
  with pct = 3 % for NDX, 8 % for SOX and 5 % for single names, plus
  $0.65 per leg fill.
  - The pcts come from the median half-spreads on 4 recorded sessions of
    these names (log sha256 `46f4b5a9…429892`). They are close to or above
    the long ATM legs' medians:

    | Leg | Measured median |
    |---|---|
    | QQQ 270-DTE ATM | 3.0 % |
    | SMH 270-DTE ATM | 4.0 % |
    | Single names, 270-DTE ATM | 1.0–2.8 % |
    | Single names, 45-DTE ATM | 0.8–4.8 % |

  - The further-OTM short legs measured wider, so their cost is understated
    for some names (declared bias 6 below):

    | Leg | Measured median |
    |---|---|
    | QQQ 70 % put | 7.4 % |
    | SMH 70 % put | 15 % |
    | SOXX 70 % put | 25 % |
    | Single names, 70 % put | 3–7 % |
- **Close-out:** a spread worth less than nothing to close is abandoned at zero.

**Declared biases** (each named here so that none can be discovered later
and argued away):

1. **Pre-2001 IV uses a constant from 2001–02.** k_vxn is measured on
   VXN's first year, so 1997-01..2001-02 is priced with a constant taken
   from slightly later data. That ratio was high in 2001 (≈2.1), so it most
   likely *over*prices 1997–98 options. That makes entries dearer for every
   sleeve.
2. **Survivorship.** The dotcom stand-ins (CSCO, INTC, ORCL, QCOM, MSFT,
   AMD, NVDA) all survived. Names that went to zero are absent, which
   understates the bear.
3. **Skew is assumed, not measured.** With β = 0.5, the 70 % put's IV is
   about 18 % above ATM. If the real skew is steeper, put spreads are
   cheaper than modeled and call spreads dearer.
4. **Closing prices only.** Execution at end-of-day closes, with no
   intraday stops.
5. **The IV model's fit is local.** It was fitted in a high-vol month
   (late 2026), and its fit in other eras is unmeasured.
6. **Short-leg costs are understated for some names.** NDX and the single
   names charge their OTM short leg less than measured. The short leg is the
   cheaper leg, so on a typical core spread this is about 1–2 % of the debit
   (NDX: about $0.10 on a ~$7 debit, both fills). For SOX, the 8 % on the
   long leg overstates its cost by more than the short leg understates its
   own, so SOX is net conservative.

## Baselines and statistics

- **Unit:** R, a trade's net P&L divided by its entry debit.
- **Clustering:** R is summed by calendar month of entry. Months are
  bootstrapped (`longrun.bootstrap_ci`, 10,000 draws, seed 20261109) and
  sign-flipped (`longrun.sign_flip_p`).
- **Random null:** per instrument, the same number of trades at random,
  non-overlapping signal days (any evaluable day), with the same structure
  and exits; 200 seeds. p_null = (1 + #{null total ≥ observed}) / 201,
  Holm-adjusted over {core, falls, relief}.
- **Always-on ladder (core only):** a core spread every 21 sessions whenever
  flat, with no regime stop. This is the unconditional short that the
  ledger found dead.
- **Eras** (assigned by entry date):
  - late-90s 1997–98 (bull);
  - dotcom 1999-01-04..2003-03-31 (bear);
  - 2003-04..2007-06 (bull);
  - GFC 2007-07..2009-12 (bear);
  - 2010–2019 (bull);
  - COVID 2020 (shock);
  - 2021-01..2021-10 (bull);
  - 2022 bear 2021-11..2023-03 (bear);
  - 2023-04..2026-11-06 (bull).

## Pass bar (per sleeve)

- **P1:** full-window total net R > 0 and its month-clustered 95 % CI lower
  bound > 0.
- **P2:** Holm-adjusted p_null < 0.05.
- **P3:** total net R > 0 in at least 2 of the 3 bear eras (dotcom, GFC,
  2022).
- **P4 (core only):** core R per bull-era year ≥ 0.5 × the ladder's (when
  the ladder loses), or ≥ 0.

Verdict: **PASS** = every applicable check. **INSURANCE** = all but P1: it
times well, pays in bears and bleeds at most half in bulls, but is not
positive over the full window. **FAIL** otherwise.

The bull-era bleed is a headline number, not a footnote. If the bubble keeps
inflating, the bull-era bleed is what the owner pays.

## What this can and cannot show

- **It can refute.** A sleeve that fails P2 or P3 here has no case.
- **A PASS or INSURANCE is not adoption.**
  - The pricing is synthetic, and the author knows the history.
  - Adoption (playbook v3 rows, Phase 4) needs ≥ 20 forward shadow cards
    per sleeve. They are priced from real recorded chains by the Phase 2
    monitor and scored after the fact.
  - It also needs an owner ruling.
- **Until then, paper trades in these sleeves are owner-initiated tickets
  (Phase 3).** They are the owner's discretion, not a promoted strategy.
  Live trading is not authorized.
