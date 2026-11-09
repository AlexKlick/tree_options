# AI-BEAR-002: the forward-only redesign of the AI-bubble sleeves (PRE-REGISTRATION)

Written 2026-11-09, after AI-BEAR-001 failed and before AI-BEAR-002's first
signal session (2026-11-10). Sealed by `AI-BEAR-002.md.sha256` in the commit
that adds this file.

- **Parameters:** `data/desk/aibear/AI-BEAR-002.params.json`, sha256 pinned in
  code as `SEALED_FORWARD_PARAMS_SHA256`. The monitor refuses any other file,
  and `book.json` records the digest it was written under.
- **Code:** `src/tree_options/desk/aibear/{monitor,rules,regime_engine}.py`.
- **Runner:** `deploy/desk/desk-aibear-monitor.{service,timer}`, 07:15 ET,
  Tuesday to Saturday.

## Why forward-only

AI-BEAR-001 scored the three sleeves on 1997–2026 and all three failed
(`docs/desk/AI-BEAR-001-results.md`). That history has now been seen, along
with *why* the sleeves failed. A redesign aimed at those failure modes is
tuned on that history by construction, so no score on 1997–2026 could tell
skill from fitting. The only honest test is data that did not exist when the
rules were written: sessions from 2026-11-10 on, priced from the real
recorded CBOE chains.

## The redesign (what changed, and the failure each change answers)

| Sleeve | AI-BEAR-001 rule | AI-BEAR-001 failure | AI-BEAR-002 rule |
|---|---|---|---|
| core2 | put spread ATM/70 %, 270 DTE; entry in TOPPING, BEAR or RELIEF; take-profit at 0.80 × width; stop after 10 BULL closes; exit at 90 DTE left | entered on every 5 % dip; 593 of 767 trades stopped at −0.2..−0.6 R; winners capped near +1.5 R | put spread **ATM/60 %, 365 DTE**; entry only on a **failed bear rally** (a BEAR close right after a RELIEF close); take-profit at **0.90 × width**; stop only on a **trend reversal** (state BULL while the 200-day MA is rising); exit at **120 DTE left** |
| falls | put spread ATM/90 %, 45 DTE, on the first BEAR close after RELIEF or TOPPING | close to random timing | **unchanged**: a pure out-of-sample replication |
| relief2 | call spread ATM/110 %, 45 DTE, on the first up close after a CAPITULATION | 77 of 101 trades stopped below the capitulation low (they bought too early) | same structure; entry on the **first RELIEF close** (a close back over the 50-day MA inside a bear) **within 20 sessions of a CAPITULATION**; stop below the lowest CAPITULATION close in that window |

- **Regime states:** identical to AI-BEAR-001 (`docs/desk/AI-BEAR-001.md`),
  computed from the Polygon panel's closes (`ohlc-panel.json`) and CBOE VIX,
  on or before the session.
- **Instruments:**
  - core2 runs on QQQ, SMH, INTC, QCOM, MSFT, AMD, NVDA, AVGO, TSLA, META
    and PLTR. These are AI-BEAR-001's core names that have recorded chains:
    NDX → QQQ, SOX → SMH; CSCO and ORCL are dropped.
  - falls and relief2 run on QQQ and SMH.
  - ETF closes stand in for the index closes, a declared difference.

## Cards (what is recorded and how it is priced)

- **Signal:** at the close of session D (data on or before D), from D =
  2026-11-10 on. One live rule card per sleeve and symbol.
- **Fill:** at the next session's EOD chain.
  - Expiry: the listed expiry nearest the target DTE inside the window
    (core2 270–450 days, the others 30–60).
  - Strikes: the listed strike nearest spot × moneyness, with spot = the
    chain's underlying close.
  - Prices: buy the long at its **ask**, sell the short at its **bid**, plus
    $0.65 per leg.
  - A leg without a positive bid and ask makes the card `unfillable`. It is
    recorded and counted, but excluded from R.
- **Marks:** each session, at the mid of both legs. The exit rule is decided
  on the session's close with the same `rules.exit_reason` as the backtest.
- **Exit:** at the next session's chain: sell the long at its **bid**, buy
  the short at its **ask**, less $0.65 per leg. A spread worth less than
  nothing is abandoned at zero. Missing quotes wait up to 3 sessions, then
  the card closes at intrinsic value from the panel close.
- **Controls (the forward null):** for each sleeve, symbol and session, a
  control card with the same structure and exits is opened with probability
  0.05. The draw is seeded by `sha256("AI-BEAR-002:<sleeve>:<sym>:<D>")`,
  so it is reproducible, and there is at most one live control card per
  sleeve and symbol.
- **Unit:** R = (exit proceeds − entry debit) / entry debit.

## Forward pass bar (per sleeve)

The bar is evaluated once a sleeve has at least **20 closed rule cards**;
the verdict is PENDING before that.

- **F1:** the total net R of the closed rule cards is > 0.
- **F2:** the month-clustered one-sided sign-flip p is < 0.10. Months are by
  fill session, via `longrun.sign_flip_p`, 10,000 draws, seed 20261109.
- **F3:** the mean R is above the same sleeve's closed control cards'
  mean R.

**FORWARD-PASS** = F1, F2 and F3. **FORWARD-FAIL** otherwise. The α is 0.10
rather than 0.05 because 20 cards is a small sample; this is declared here,
before any card exists.

A FORWARD-PASS is the first evidence that could support an owner ruling on
playbook v3 rows (Phase 4). It is not that ruling.

## The advisory LLM read

- **What it is:** each run, the local Qwen3.8-27B (:18000; fallback
  glm-5.3-flash) reads up to 12 Google News headlines per name for TSLA,
  NVDA, AVGO, AMD, PLTR, META, MSFT, QQQ, SMH and SOXX. The headlines are
  framed in the prompt as untrusted data.
- **What it returns, per name:** a stress score (0–100), **p_down10_20d**
  (the probability of a close ≥ 10 % lower 20 sessions later), up to 3
  catalysts, a rebound veto and a rationale.
- **What it can do:** nothing. **No card reads it.**
- **How it is judged:** forward only, by the Brier score of p_down10_20d
  against the realized outcome, compared with the base rate (skill =
  1 − Brier / Brier_base_rate). Historical LLM backtests are excluded,
  because the model knows how 2000, 2008 and 2022 ended.
- **What is recorded:** headlines are fetched at run time (about 07:15 ET
  on D+1, pre-market), and the prompt sha256 is kept with each read.

## What was seen before sealing

- AI-BEAR-001's full results.
- The 2026-09-22..11-06 chain sessions (for the cost and IV calibrations).
- The 2026-11-06 states.
- One dry-run LLM read (2026-11-09, not recorded as a forward read).
- The market closes of 2026-11-09, which is why the first signal session is
  2026-11-10.
