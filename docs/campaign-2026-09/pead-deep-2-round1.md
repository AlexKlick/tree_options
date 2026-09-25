# PEAD-DEEP-2 — round 1 results (INNER FOLDS ONLY)

Slot `pead-deep-2`, scope `c09-pd2` (24 configs, cap 32), menu order 2.
Executor round 1, 2026-09-24. Runner:
`scripts/campaign/pead-deep-2_run.py` (this branch). Nothing is adopted,
nothing seals a card, the RESEARCH-LEDGER is not touched (consolidator owns
it). Verdict vocabulary: the slot's pre-declared set only.

## 1. Binding (verified before anything ran; REFUSED on drift)

- Menu v3 `docs/theory/campaign-2026-09-registration.json` sha256
  `4aca21014f6efa640d97bf2abd3fdbb912af5a7b9acb68f65b842f924379b10a`
  (sidecar-verified); slot doc `slots/pead-deep-2.md` sha256
  `8626ee08cde0e9bc111f8899687aaa4aed23ebd7baef7142f15625132940f832`
  (menu `dataset_pinning` pin). Protocol raw sha equals the menu's
  `protocol_hash`; canonical re-stamped per INV-14.
- T-NULL gate: `artifacts/campaign-2026-09/tnull/calibration-v3.json`
  stamps THIS menu sha and its slot verdict is CALIBRATED — family scoring
  unfrozen per `rules.sequencing` (amendment v3).
- Inputs (all sha-pinned to the menu): `ohlc-panel.json` (read under the
  shared flock), `earnings-calendar.json`, `earnings-timing.json`,
  `VIX.csv`, sealed NYSE calendar (2025-01-09 phantom excluded in the
  walker; file byte-identical).
- Geometry re-verified from the sealed calendar: era 2024-09-05..2026-09-23
  = 514 sessions; inner walk = sessions 1..406 (through 2026-04-20;
  406+40+5 = 451 < 452 = era-ordinal of 2026-06-25); sealed window =
  final 63 sessions. **The sealed window was NEVER scored, read for
  tuning, or plotted**: the session walk stops at 2026-04-20 and the
  runner hard-asserts no computed trade row carries an entry or exit
  session >= 2026-06-25 (the deepest inner h40 label exits ~2026-06-19,
  inside the purge gap).
- INV-13: all 24 config ids were registered in the trials sqlite
  (`artifacts/campaign-2026-09/pead-deep-2.db`, scope `c09-pd2`) as
  REGISTERED with NO outcome BEFORE any outcome was computed or viewed;
  execution was one-shot per trial (REGISTERED -> RUNNING -> COMPLETED,
  per-config artifact as `metrics_uri`); one scored run per cell; no
  re-gridding. Registry state after the run: 24/24 COMPLETED, 24 outcome
  rows claimed.

## 2. What ran (exactly the registered grid)

Firing rule is EXACTLY `pead_beats()` (`src/tree_options/desk/signals.py`
— +1.5% threshold, clean-back 5, hole/prior-gap guards) over the sealed
calendar. Entry at close(s), exit at close(s+h) on calendar ordinals with
the house hold filter (name must carry every session in (s, s+h]);
Decimal closes); net = gross − 5bp (15bp disclosed); day-clustered t
(ddof=1) per `iter002.py stats_of`. 24 cells: B (unconditioned, h10/20/40),
M (within-beat median split, 6 cells), L (fixed bands +1.5–3/3–6/>=6%,
h20), V (prior-session VIX tercile, 9 cells), R (SPY RV20(t−1) tercile,
h20).

Inner population (the strata-edge population, INV-07): **50 beat events**
over 406 entry sessions (2024-09-05..2026-04-20). Disclosures: 0 events
missing VIX, 0 missing RV20, 0 holds dropped incomplete (beyond-panel or
missing session) at every hold; the iter002 `window_clean` cross-check
diverges from the house hold filter on 0 rows. Strata edges frozen from
this span: m-median = +3.2490%; VIX terciles 16.56 / 20.64; RV20 terciles
0.0070650 / 0.0088939. Partition checks passed (M, V, L, R each partition
the arm's usable rows exactly).

## 3. Cell bar (menu v2 drift-relative amendment; B READ, never recomputed)

`B(pead-deep-2 window, event shape)` from `calibration-v3.json`:
`B_net_day_clustered_mean` = +0.010079 (primary leg, like-for-like with
the iter002 day-clustered mean), `B_net_per_trade_mean` = +0.023181
(per-trade sensitivity leg, disclosed). The pead-deep-2/event null cell is
below the v3 NOT_EVALUABLE floor (12 complete trades < 20), so B(W, shape)
is the standing drift gate — no tripwire prior exists for this window.
Legs: mean − B > 0; day-clustered t >= 2; n_days >= 30; n >= 20;
conditional (stratum − PD2-B same hold) > 0. INSUFFICIENT_N when n or
n_days is below the minima (never read as pass or fail).

## 4. Results (per-config artifacts under
`artifacts/campaign-2026-09/pead-deep-2/trials/`; summary at
`artifacts/campaign-2026-09/pead-deep-2/round1-summary.json`)

| config | n | days | mean (dc) | t | per-trade | mean−B (dc) | mean−B (pt, sens.) | cond vs B | verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| PD2-B-h10 | 50 | 42 | +1.4140% | 1.54 | +1.6478% | +0.4061% | −0.6703% | 0 (ref) | NULL |
| PD2-B-h20 | 50 | 42 | +1.9636% | 1.12 | +1.8597% | +0.9558% | −0.4584% | 0 (ref) | NULL |
| PD2-B-h40 | 50 | 42 | +3.7093% | 1.34 | +3.3552% | +2.7014% | +1.0371% | 0 (ref) | NULL |
| PD2-M-lo-h10 | 25 | 23 | +2.0247% | 1.46 | +1.9420% | — | — | +0.6107% | INSUFFICIENT_N |
| PD2-M-hi-h10 | 25 | 22 | +0.9671% | 0.71 | +1.3535% | — | — | −0.4469% | INSUFFICIENT_N |
| PD2-M-lo-h20 | 25 | 23 | +2.7610% | 1.29 | +2.0545% | — | — | +0.7974% | INSUFFICIENT_N |
| PD2-M-hi-h20 | 25 | 22 | +1.3621% | 0.49 | +1.6650% | — | — | −0.6015% | INSUFFICIENT_N |
| PD2-M-lo-h40 | 25 | 23 | +2.8243% | 0.86 | +1.9964% | — | — | −0.8850% | INSUFFICIENT_N |
| PD2-M-hi-h40 | 25 | 22 | +4.5634% | 1.08 | +4.7140% | — | — | +0.8541% | INSUFFICIENT_N |
| PD2-L-mod | 23 | 21 | +3.2965% | 1.43 | +2.4819% | — | — | +1.3329% | INSUFFICIENT_N |
| PD2-L-str | 14 | 14 | +1.7441% | 0.58 | +1.7441% | — | — | −0.2195% | INSUFFICIENT_N |
| PD2-L-ext | 13 | 12 | +0.5611% | 0.14 | +0.8834% | — | — | −1.4025% | INSUFFICIENT_N |
| PD2-V-lo-h10 | 16 | 15 | +0.6118% | 0.46 | +0.5410% | — | — | −0.8022% | INSUFFICIENT_N |
| PD2-V-mid-h10 | 17 | 14 | +0.8162% | 0.50 | +1.2246% | — | — | −0.5978% | INSUFFICIENT_N |
| PD2-V-hi-h10 | 17 | 13 | +2.9834% | 1.61 | +3.1126% | — | — | +1.5694% | INSUFFICIENT_N |
| PD2-V-lo-h20 | 16 | 15 | −0.6161% | −0.37 | −0.8107% | — | — | −2.5797% | INSUFFICIENT_N |
| PD2-V-mid-h20 | 17 | 14 | +1.5041% | 0.44 | +1.0618% | — | — | −0.4595% | INSUFFICIENT_N |
| PD2-V-hi-h20 | 17 | 13 | +5.4350% | 1.41 | +5.1710% | — | — | +3.4714% | INSUFFICIENT_N |
| PD2-V-lo-h40 | 16 | 15 | −4.1782% | −1.13 | −4.1692% | — | — | −7.8875% | INSUFFICIENT_N |
| PD2-V-mid-h40 | 17 | 14 | +5.8068% | 1.01 | +4.4924% | — | — | +2.0975% | INSUFFICIENT_N |
| PD2-V-hi-h40 | 17 | 13 | +10.5512% | 2.50 | +9.2998% | — | — | +6.8419% | INSUFFICIENT_N |
| PD2-R-lo | 15 | 13 | +3.2596% | 1.14 | +2.7550% | — | — | +1.2960% | INSUFFICIENT_N |
| PD2-R-mid | 17 | 15 | −0.3333% | −0.18 | +0.2563% | — | — | −2.2969% | INSUFFICIENT_N |
| PD2-R-hi | 18 | 14 | +3.2212% | 0.76 | +2.6280% | — | — | +1.2576% | INSUFFICIENT_N |

Conditional margins for INSUFFICIENT_N rows are descriptive columns
derived from the stamped means (they are not verdict legs — the floors
bind first). "—" marks legs not evaluated below the floors.

**Verdict counts: CANDIDATE 0 · NULL 3 · INSUFFICIENT_N 21.**

- The pre-declared dominant risk (thin n) is exactly what happened: every
  conditioned stratum sits below the n >= 20 AND days >= 30 floors
  (strata hold n 13–25, days 12–23). INSUFFICIENT_N is never read as pass
  or fail.
- The three PD2-B reference cells are NULL: t = 1.54 / 1.12 / 1.34 (< 2)
  and conditional ≡ 0 by construction (a reference cannot beat itself;
  mechanical application of the pre-declared bar, so no PD2-B cell can
  ever be CANDIDATE). Their mean − B leg passes on the primary
  day-clustered basis at all three holds; on the disclosed per-trade
  sensitivity basis h10/h20 would fail and h40 passes — noted for the
  consolidator, not a second verdict.
- No family verdict (STRATUM-CONFIRMED / FAMILY-NULL / INSUFFICIENT_N at
  the family level) and no terminal PROMOTE-AS-GATE / CLOSE: those belong
  to the sealed round, which round 1 did not touch.

**Inner-fold best (descriptive only, no adoption).** The summary's
mechanical pick among non-INSUFFICIENT_N cells is `PD2-B-h10`
(conditional 0.0000%; ties broken by t) — degenerate, since only the
three reference cells clear the floors. Among the CONDITIONED cells the
maximum conditional margin is **`PD2-V-hi-h40`: conditional +6.8419% vs
PD2-B-h40** (mean +10.5512%, day-clustered t 2.50, n 17, days 13) —
INSUFFICIENT_N, never read as pass or fail. The V arm shows a lo < mid <
hi ordering at h20/h40 with the hi tercile the only stratum reaching
t >= 2 anywhere; recorded as the round-1 observation the sealed round
will test once, not as evidence.

## 5. Timing arm (forward-only; 0 backtest cells)

Standing verdict: **INSUFFICIENT_COVERAGE** (no verdict until BOTH
PD2-T-bmo and PD2-T-amc hold >= 12 valid timed cards; can never gate a
card). Current in-universe coverage from `earnings-timing.json`: 22 timed
next reports (6 bmo, 2 amc, 14 unknown), earliest 2026-09-24; historical
timing coverage is zero, exactly as registered.

## 6. Withdrawals and data gates

- `pead-deep-2`: **0 withdrawn configs.** The declared-and-dropped
  candidates (per-name IV conditioning — IVHIST-001 low-fidelity gate;
  options-expression arm — long-dated bars capture in flight, ETA
  ~2026-09-27) were dropped AT REGISTRATION and were never registered
  configs. No proxy was improvised.
- Campaign-level gated arms recorded for the round bookkeeping (owned by
  their own slots' executors, not run here): vrp-cond scope O (`c09-vrp-o`)
  WITHDRAWN without running; exit-grid-2 scope C (`c09-eg2-c`)
  DATA-GATED-NOT-RUN.

## 7. Multiplicity and adoption posture

24 cells in one scope; anything surviving the cell bar must also survive
the sealed window and the census base-rate deflation; DSR at N = 24 is
reported for any eventual PROMOTE-AS-GATE (none this round). A surviving
gate is a SUCCESSOR CANDIDATE only, evaluated after the sealed PEAD rule's
own 20-card review (kill-switch inheritance: if the base rule dies, these
families die with it). Round 1 nominates nothing.

## 8. Artifacts

- Registry: `/home/alexk/documents/tree_options/artifacts/campaign-2026-09/pead-deep-2.db`
  (24 trials, scope `c09-pd2`, cap 32, all COMPLETED).
- Per-config artifacts:
  `/home/alexk/documents/tree_options/artifacts/campaign-2026-09/pead-deep-2/trials/c09-pd2-<config>-g1.json`
  (24 files: stamp + edges + population disclosures + cell stats/legs/verdict
  + full inner-span trade rows).
- Summary:
  `/home/alexk/documents/tree_options/artifacts/campaign-2026-09/pead-deep-2/round1-summary.json`.
- Run logs: `/tmp/pd2-{plan,timing,register,execute,summarize}.log`.
