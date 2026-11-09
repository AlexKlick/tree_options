# AI-BEAR-001 results: all three sleeves FAIL (verdict of record)

- **Run:** 2026-11-09 14:15 MDT, commit `4c30374` (the sealing commit), parameters
  sha256 `072ab781…ecd53`, pre-registration `docs/desk/AI-BEAR-001.md`.
- **Output:** `~/.local/state/trex-aibear-20261109/run-001/`:
  - `results.json`, sha256 `887fa4a734b9f95c73f9424ac0bf444f93f82bcd9912f749a7633ff825488a04`;
  - `trades.jsonl`, sha256 `ffeb1c0471e188133333d2ef5237c1d4bd28254feb3605b9cc56a7aa05069230`.
- **Reproduction:** `run-001-repro` (same commit, same inputs); see
  "Reproduction" below.
- **Window and units:** signals 1997-01-02..2026-11-06. R is net of the
  sealed costs (one unit risked per trade), and CIs are clustered by
  entry month.

## Verdicts

| Sleeve | Trades | Months | Total R | 95 % CI | Mean R | Hit | Null mean | p_null (Holm) | Bear eras > 0 | Verdict |
|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---|
| core | 767 | 288 | −211.41 | [−256.06, −167.12] | −0.28 | 17.6 % | −139.65 | 1.0 | 0 / 3 | **FAIL** |
| falls | 97 | 59 | −17.91 | [−29.54, −5.47] | −0.18 | 26.8 % | −21.72 | 0.51 | 1 / 3 | **FAIL** |
| relief | 101 | 39 | −32.36 | [−46.78, −19.81] | −0.32 | 17.8 % | −5.33 | 1.0 | 0 / 3 | **FAIL** |

- P1, P2 and P3 fail for every sleeve. For core, P4 fails too: −9.16 R per
  bull-era year, against −9.55 for the always-on ladder.
- Sensitivity with flat IV and no skew, for information only: core −246.33,
  falls −18.64, relief −31.65. The direction is unchanged.

## By era (total R, n)

| Era | Kind | core | falls | relief | ladder (core, no regime) |
|---|---|---:|---:|---:|---:|
| late-90s 1997–98 | bull | −13.32 (40) | −3.67 (9) | −1.71 (4) | −16.90 (33) |
| dotcom 1999-01..2003-03 | bear | −8.59 (104) | −7.08 (39) | −12.65 (32) | −12.14 (86) |
| 2003-04..2007-06 | bull | −24.38 (87) | +1.79 (5) | −0.78 (1) | −25.40 (79) |
| GFC 2007-07..2009-12 | bear | −3.20 (57) | −2.49 (22) | −9.00 (23) | +1.04 (51) |
| 2010–2019 | bull | −103.66 (263) | −6.31 (9) | −2.04 (8) | −115.01 (236) |
| COVID 2020 | shock | −10.06 (27) | — | −1.37 (9) | −17.73 (25) |
| 2021-01..10 | bull | −4.20 (19) | — | — | −6.53 (19) |
| 2022 bear 2021-11..2023-03 | bear | −0.59 (41) | +0.18 (12) | −4.61 (14) | +3.03 (38) |
| 2023-04..2026-11-06 | bull | −43.41 (129) | −0.32 (1) | −0.20 (10) | −33.29 (101) |

## Why the sleeves lose (read from `trades.jsonl`; descriptive, not part of the verdict)

### Core

- **Its regime gate does no better than no gate.** The core's 767 trades
  lose about as much as the always-on ladder's 668 (−211 vs −223 R) and
  more than random timing. 593 of 767 trades end by the regime stop (10
  BULL closes) at −0.2..−0.6 R each.
  - A 5 % dip below the 200-day MA reads as TOPPING. In a bull market
    those dips mostly recover, so the gate keeps buying puts after a drop,
    with IV already up, right before the bounce. The ledger's short-horizon
    reversal finding says the same.
- **Wins are capped.** The take-profit at 0.80 × the 30-point width caps a
  winner near +1.5 R, and only 65 trades reached it.
- **Even the hindsight-perfect window barely pays.** Inside the actual
  NDX peak-to-trough window (2000-03-10..2002-10-09), the 68 core
  entries made +5.49 R in total (hit 46 %, +0.08 R per trade).
  - Bear-market rallies that lift closes back over the 200-day MA flip
    the state to BULL and stop the position out before the next leg down.
  - The window's 24 core regime-stop exits cluster in Jun–Aug 2000 (11),
    Apr–May 2001 (3), Oct–Nov 2001 (4) and Jan–Mar 2002 (3).
- **Bull years pay for it.** Every bull year costs −1.7 to −16 R.

### Falls

- Close to random timing (−17.9 R vs a null mean of −21.7 R; raw
  p_null 0.17, Holm 0.51), and negative in absolute terms.
- Most exits (64 of 97) are the RELIEF/BULL stop: rallies inside bear
  markets are frequent and fast.

### Relief

- **Much worse than random timing.** 77 of 101 trades are stopped by a
  close below the capitulation low.
- In real bears (2000–02, 2008, 2022) capitulations come in waves: the
  first up close after one is usually not the bottom.

## What this means for the program (per the pre-registration)

- **These three sealed rules have no case.** They fail P2 and P3, which is
  the refutation the pre-registration says this backtest can deliver.
- **Phase 4 (automated v3 playbook rows) is closed for them.** R8a/R8b stay
  dormant.
- **The ledger entry stands as "shorts DEAD".** It now has a second
  member: regime-gated debit spreads, with three sleeves tested from 1997
  to 2026.
- **A redesign (AI-BEAR-002) cannot be judged on this same history.** The
  1997–2026 path has now been seen, so any new rule tuned on it is in-sample
  by construction. The honest test for a redesign is forward-only (Phase 2
  shadow cards on real chains), or a period not yet examined.
- **The result does not say the AI bubble won't deflate.** It says that
  these mechanical debit-spread rules lost money across the past 30 years,
  even through the dotcom crash. The losses come from the bear-market
  rallies that whipsaw them and from the bull years in between.

## Reproduction

`run-001-repro` was run on the same commit and inputs (rc 0, 24 s). It
reproduced both files byte for byte:

- `results.json` `887fa4a734b9f95c…` (both runs);
- `trades.jsonl` `ffeb1c0471e18813…` (both runs).

The per-year table and the peak-to-trough window figures are in
`run-001/by-year.txt`.
