# RESTART-THRESHOLD — the pre-registered forward go/no-go (sealed 2026-10-01)

Written BEFORE any forward measurement exists. The desk strategy program is
STOPPED (see `docs/desk/CAMPAIGN-EXIT-20260930.md`); this document is the
ONLY pre-registered condition under which measurement restarts, fixed now so
that it cannot be tuned to whatever a future corpus happens to say. It
authorizes measurement only — nothing in it enables a timer, arms a trade,
or implies live trading; the desk remains paper-only.

Anchors this threshold is built on (sources in the exit packet):

- The last long run (`20260929T094303Z`, 1,912/1,913 boards, 240 sessions
  2025-09-29..2026-09-25, 27 arms) produced 0 eligible arms, nothing
  promoted, and both walk-forward finalists failed their single
  confirmatory look (re-score digest
  `~/.local/state/trex-longrun/v3-final-accounting-20261001/digest.json`).
- The audit's economics: a genuine predictable residual of $9–19 per fill
  against a cheapest measured round trip of $19.30 — a perfect predictor
  breaks even or loses (PR #47 description, `Consequences` §4).
- The flat $14.60 round-trip constant is ~1.0x right on average and wrong
  in shape (measured n=18,783 tradeable rows: median 2-leg RT $8.60, mean
  $15.76, p90 $34.60; PR #48), so a restart must be judged on the measured,
  moneyness-aware cost, never on a scalar.
- 68.5% of the last run's table-level no-fills were MISSING DATA, not the
  market refusing the trade (6,238/9,113 `missing_later_entry_bars`; PR
  #54). A missing bar is not a no-fill.

## C. The corpus that qualifies (checked before any scoring)

- **C1 Forward-only, at the decision clocks.** Sessions strictly after the
  seal date, carrying real price observations AT the decision clocks (the
  8-clock schedule 10:00, 10:45, 11:30, 12:15, 13:00, 13:45, 14:30, 15:15
  ET; `src/tree_options/desk/intraday_action_graph.py:27`). EOD chain
  snapshots do not qualify: the capture window 17:45–06:30 ET cannot
  describe a 10:00 ET fill (`src/tree_options/desk/cost.py:56-57`). The
  intended source is the A1 forward Massive minute-bar capture (PR #55,
  `desk-forward-minutes.timer`, deployed DISABLED); any equivalent source
  must record its own capture clock.
- **C2 Size floor.** At least 60 captured sessions AND at least 1,000
  filled entries pooled across arms. Both numbers are round judgement
  calls, deliberately exposed here so they can be argued with and changed
  by resealing — never tuned silently (the `MIN_TEST_ENTRIES` posture of
  PR #47).
- **C3 Coverage floor, gaps classified.** At least 90% of scheduled
  (session, clock) decision points have a real observation for every
  traded underlying, and every gap is recorded with its reason. A row
  whose outcome cannot be priced because bars are absent
  (`missing_later_entry_bars`-class) is EXCLUDED from the fill denominator
  and reported as coverage — it never counts as a trade, as an abstention,
  or as a $0 outcome. This resolves, for every future run, the open
  operator decision recorded at `digest.md:102` of the final accounting.
- **C4 Per-FILLED-entry denominators.** Every restart statistic is per
  FILLED entry. `entry_risk_cap` and `legs_out_of_sync` are real no-fills:
  out of the economics denominator, with the fill rate itself reported
  beside the threshold so abstention can never win it (PR #47's lesson).

## S. Selection discipline

- **S1 Split.** First half of the qualifying sessions = selection window;
  second half = holdout. The holdout is never read by any selection,
  tuning, ranking, or plotting step before the single confirmatory look.
- **S2 Immutable roster.** The arm roster (the sealed 27 or a successor
  registered in writing before the first selection read) is immutable once
  scoring starts (PR #46's lesson).
- **S3 One look.** Exactly one confirmatory evaluation on the holdout. A
  NO does not license re-running for significance; that is a new study
  under a newly sealed threshold, never an edit here.

## G. The go/no-go — ALL must hold on the holdout

- **G1 Gross beats the toll.** The best arm's mean GROSS P&L per filled
  entry exceeds its mean measured round-trip cost per filled entry, cost
  computed by the measured moneyness-aware model (PR #48,
  `src/tree_options/desk/cost.py`) — never the flat constant, never a
  scalar fitted to make the arm pass.
- **G2 Uncertainty.** The 95% CI low of the paired per-fill (gross −
  measured cost) difference is above 0, block bootstrap over sessions (the
  harness's own paired-null machinery, per-arm entry-rate matched per PR
  #45).
- **G3 The trivial control fails.** `first_row` ("pick the first row on
  the board"), run on the same holdout under the same rules, does NOT
  satisfy G1+G2. If it does, the bar is measuring the regime, not skill —
  the p=0.014 lesson of 2026-09-30 (a bar the first_row control clears is
  not a skill bar).
- **G4 Multiplicity.** If more than one arm is tested against G1–G3, Holm
  step-down at alpha=0.05 over the tested arms (harness precedent).

If any of C1–C4 fails, the corpus does not qualify and nothing is scored.
If any of G1–G4 fails, the program stays stopped.

## Seal

This document is sealed by `docs/desk/RESTART-THRESHOLD.md.sha256`
(sha256sum format), written 2026-10-01 as part of the campaign exit packet
(`docs/desk/CAMPAIGN-EXIT-20260930.md`), before any forward corpus exists.
Any later edit to this file is detectable against the sidecar and voids the
pre-registration; a changed threshold requires a NEW sealed document with
its own sidecar, never an edit to this one.
