# campaign-2026-09 / T-NULL (scope `c09-tnull`) — results, round 1

Runner: `scripts/campaign/tnull_run.py` (wave-0 idiom). Registration:
`docs/theory/campaign-2026-09-registration.json` sha256
`3bb10a2feb340e4b3aac92980fb0537352a767fc2e53af4bb3fc70e25d3ce440`
(sidecar-verified; worktree bytes identical to main's). Protocol raw
sha256 `2fde83db…` matches the menu pin; canonical re-stamp
`22c78231…` (INV-14). Inputs pinned and verified against the menu's
`dataset_pinning`: ohlc-panel `0861f525…`, earnings-calendar `916569ad…`,
sealed calendar `7f9cccba…` (2025-01-09 removed as a non-session per the
2026-09-23 ledger ruling; the file stays byte-identical).

## VERDICT: DEFECT-FLAGGED (all three seeds) — family scoring FROZEN

The menu's acceptance criteria bind per seed x sub-era: day-clustered
|t| < 2 on the gross per-trade mean AND net per-trade mean (5bp RT)
within [-15bp, +5bp]. Every seed violates the net band in the card era
on BOTH streams, and multiple further sub-eras violate; `tnull-s3`
additionally trips the t-band in the 6-trade pead-deep-2 / vrp-cond
XSMOM cells (|t| = 4.69 on a 2-day cluster). 22 defect reasons in total
— enumerated verbatim in `DEFECT-FLAG.txt` and `calibration.json`.

Entry floors were MET for every seed (xsmom 72 >= 60, event 135 >= 100),
so no stream is NOT_EVALUABLE; the failure is the bands, not the counts.

### Calibration numbers (net of 5bp RT; day-clustered t on gross)

| seed | stream | window | n/days | gross mean | t | net 5bp |
|---|---|---|---|---|---|---|
| s1 | xsmom | card-era | 69/23 | +1.89% | +1.44 | **+1.84%** |
| s1 | xsmom | jepa-outer | 24/8 | +0.78% | +0.94 | **+0.73%** |
| s1 | event | card-era | 132/132 | +1.38% | +1.71 | **+1.33%** |
| s2 | xsmom | card-era | 69/23 | +1.44% | +0.95 | **+1.39%** |
| s2 | event | card-era | 132/132 | +1.45% | +1.80 | **+1.40%** |
| s2 | xsmom | pead/vrp | 6/2 | -6.64% | -0.75 | **-6.69%** |
| s3 | xsmom | card-era | 69/23 | +2.33% | +1.58 | **+2.28%** |
| s3 | xsmom | pead/vrp | 6/2 | +0.87% | **+4.69** | **+0.82%** |
| s3 | event | card-era | 132/132 | +0.70% | +0.84 | **+0.65%** |

(full matrix — union/card-era/vrp-cond/pead-deep-2/jepa-outer x both
streams x three seeds, including 15bp robustness and the per-cell
realized sds — in `artifacts/campaign-2026-09/tnull/calibration.json`)

### What the defect is (read-only characterization, no tuning)

The machinery computes what the menu pinned (trade-level arithmetic
re-verified by hand against the panel on six sampled trades; entry
streams verified: 24 first-of-month sessions 2024-10-01..2026-09-01,
135 unique first-post-report sessions over the 26 reporters). The
evaluation eras simply do not satisfy the null's assumption: over the
card era SPY returned +35.3%, the equal-weight 36-name universe +43.9%
(median name +31.7%), and the ALL-NAMES unconditional 20-session
per-trade mean over the union span is +1.55% (n = 17,136 name-session
holds). Hash-random longs sample exactly that drift — the null's net
per-trade means land on the market baseline, +65bp..+228bp above the
+5bp cost-floor ceiling. Per the sealed hypothesis this is a machinery/
data-regime defect, not an edge: the band every family's sealed delta
was to be read against does not hold at zero. NO tripwire prior was
stamped (`tripwire_priors: {}` — priors come only from a CALIBRATED
null), so family sealed-window scoring stays frozen pending an operator
ruling (menu `rules.sequencing`; REGISTRATION-NOTES checklist item 6).

## Execution record

- Registry: `/home/alexk/documents/tree_options/artifacts/campaign-2026-09/tnull.db`
  (scope `scope-v1:d77ffaa0…`, 6 rows of the 32 cap). g2 trials
  `c09-tnull-tnull-s{1,2,3}-g2` REGISTERED before outcome (INV-13) ->
  RUNNING -> COMPLETED, artifacts under
  `artifacts/campaign-2026-09/tnull/trials/`.
- Configs (from the sealed menu, drift-refused if they diverge):
  `tnull-s1/s2/s3`, model family `null-sha256/1`, seeds
  `campaign-2026-09-null-{1,2,3}`, params_key
  `[card-lane, null-sha256/1, <seed>, hold20, 5bp]`, run_index 1/2/3.
- Windows (entry-date containment; sub-eras overlap by design): union
  2024-10-01..2026-09-23; card-era 2024-10-01..2026-08-28; vrp-cond
  2026-06-02..2026-08-31; pead-deep-2 2026-06-25..2026-09-23; jepa-outer
  2026-01-02..2026-08-24 (earliest last session among the 35 = 2026-09-23,
  minus 21 sessions; the h=5 end 2026-09-16 is disclosed, not scored).
- Conventions: hold-20 close-to-close on Decimal closes with the house
  hold filter (all sessions in (t, t+20] present; beyond-panel holds
  dropped and counted: 3 xsmom [2026-09-01 month] + 3 event
  [2026-08-27 NVDA, 2026-09-03 AVGO, 2026-09-11 ADBE]); day-clustered t
  over per-entry-day means (iter003/xu_xsmom `stats_of`); XSMOM stream =
  top-3 sha256 scores over the 36 tradables per first-of-month session;
  event stream = top sha256 reporter per first-post-report session among
  the chain-35.

## Voided executions (full disclosure, audit trail preserved)

1. **g1** (trial ids without `-g2`): the runner's `previous_session`
   used `bisect_right`, so `is_first_session_of_month` never fired and
   the XSMOM stream executed EMPTY (0/0); caught by the menu's entry
   floor before any calibration existed. Rows stay COMPLETED in the
   registry; artifacts retained; `trials/VOIDED-g1.md` records it. g2
   re-ran the SAME pre-registered configs under fresh ids (no parameter
   changed; scope load 6/32).
2. **calibration-VOIDED-v1**: the first `--calibrate` stamped sub-era
   cells from a first-match sub-era TAG instead of entry-date
   containment, starving the overlapping windows (days=0). The trade
   rows were always correct; the corrected stamp re-derives every cell
   from the sealed trade rows. Voided file kept beside the valid one.

Nothing outside this slot was run; no family config was scored; the
RESEARCH-LEDGER was not touched; nothing was adopted or promoted.
