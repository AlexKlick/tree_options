# EXIT-GRID-2 — round 1 results

Slot `exit-grid-2`, scopes `c09-eg2-a` (10) / `c09-eg2-b` (11) /
`c09-eg2-c` (6), menu order 4. Executor round 1, 2026-09-24. Runner:
`scripts/campaign/exit-grid-2_run.py` (this branch). Nothing is adopted,
nothing seals a card, the RESEARCH-LEDGER is not touched (consolidator owns
it). Verdict vocabulary: the slot's pre-declared set only.

## 1. Binding (verified before anything ran; REFUSED on drift)

- Menu v3 `docs/theory/campaign-2026-09-registration.json` sha256
  `4aca21014f6efa640d97bf2abd3fdbb912af5a7b9acb68f65b842f924379b10a`
  (sidecar-verified); slot doc `slots/exit-grid-2.md` sha256
  `13634c778316d6ea67933eec56bf3a4640829d15316b34de2eac44617d0a72bd`
  (menu `dataset_pinning` pin). Protocol raw sha equals the menu's
  `protocol_hash` (`2fde83db…`); canonical re-stamped per INV-14
  (`22c78231…`).
- T-NULL gate: `artifacts/campaign-2026-09/tnull/calibration-v3.json`
  stamps THIS menu sha and its slot verdict is CALIBRATED — family scoring
  unfrozen per `rules.sequencing` (amendment v3). The runner re-verifies
  this on every phase entry and refuses otherwise.
- Inputs (all sha-pinned to the menu, read from the MAIN checkout under
  the task's env pins): `ohlc-panel.json`, `earnings-calendar.json`,
  sealed NYSE calendar (2025-01-09 phantom excluded in the walker; file
  byte-identical), `VIX.csv`, `VIX3M.csv`.
- INV-13: all 27 config ids were registered in the trials sqlite
  (`artifacts/campaign-2026-09/exit-grid-2.db`, scopes `c09-eg2-a/-b/-c`,
  10/11/6 of the 32-cap) as REGISTERED with NO outcome BEFORE any outcome
  was computed or viewed; execution was one-shot per trial (REGISTERED ->
  RUNNING -> COMPLETED, per-config artifact as `metrics_uri`); one scored
  run per cell; no re-gridding. Registry state after the run: 21 COMPLETED,
  6 REGISTERED with no outcome (scope C, withdrawn — section 4).
- Provenance disclosure: a sibling executor's commit (`c0706ef`,
  pead-deep-2 round 1: its own runner + round doc only) moved the worktree
  HEAD between `--register` (at `81c8beb`) and `--execute`. The runner
  refused the naive run, then executed under the REGISTERED git provenance
  after proving the inter-commit diff touches nothing this slot binds to
  (no `docs/theory/`, `research_protocol.yaml`, `data/`, or this runner);
  every content hash (menu, sidecar, slot doc, protocol raw+canonical,
  five pinned inputs, per-config `config_hash`) was re-verified equal by
  the binder. Each executed artifact records BOTH shas
  (`git_sha` = registered, `executed_git_sha` = actual) plus the
  `provenance_note`; `runner_sha256` pins the executing code.

## 2. Inner folds (EMPTY BY DESIGN — what that means for this slot)

The sealed registration (menu `fold_mapping.inner`, slot doc section 4)
declares the inner loop EMPTY BY DESIGN for scopes A and B: every level is
pinned ex-ante with enumerated justification, nothing is selected on data,
so **no tuning fold is consumed and no inner-fold best exists**. The
reserved protocol-geometry windows (EG2-A train 2024-09-05..2025-09-08 /
validation 2025-09-09..2026-03-10 / sealed test 2026-05-21..2026-08-20;
EG2-B tuning rebalances 2022-11-01..2024-09-03 / validation block
2024-03-05..2024-09-03) were **not touched** — the registration states
they bind only a successor that introduces tunable parameters.

What was scored is exactly what the registration sealed: each registered
cell's ONE scored run on its sealed era (`fold_mapping.sealed_eg2a` /
`sealed_eg2b`) — the frozen-grid house precedent (EXIT-GRID.md "DECLARED
pre-registered runs", XSMOM-EXITGRID.md "Grid FROZEN", XSMOM-12-1-PREREG.md
"no re-gridding") that this slot extends. No cell was read for tuning, no
parameter moved after registration, multiplicity is charged at the full
declared count (m = 9 / 10 / 5) regardless of run subset, and the
descriptive "best cell" in section 5 is a ranking, never a selection.

## 3. What ran (exactly the registered grid)

- **EG2-A** (`c09-eg2-a`, m=9, crit t = 2.5392 one-sided Bonferroni at
  0.05/9, normal approx pinned at freeze): the beats-only `pead_beat`
  stream recomputed by the DESK code `pead_beats()` (`desk/signals.py`:
  move >= +1.5%, clean-back 5, hole/prior-gap guards) over the sealed
  calendar era 2024-09-05..2026-08-28, entries restricted to hold-complete
  (house hold filter: every session in (t, t+20]). Stream: 208 evaluated
  events, 66 beats fired, 0 same-(name, session) dedupes, 2 holds dropped
  incomplete -> **64 signals**; engine-computed boundary last entry
  **2026-08-19** (hold-complete through the panel end 2026-09-23). Power
  floor: 64 >= 30 sealed beats — met.
- **EG2-B** (`c09-eg2-b`, m=10, crit t = 2.5758): the monthly
  `xsmom_top3` card stream on the sealed basis era 2024-09-04..2026-08-28
  by the DESK ranking (`close(t)/close(t-273)-1`, the convention behind
  PROTOCOL-XSMOM.md): **23 rebalances 2024-10-01..2026-08-03, 69 legs, 0
  legs dropped** — the registration's own cross-check, enforced as a
  refusal in the runner. TQQQ picks at 2024-11-01, 2024-12-02, 2026-06-01
  (logged; included in spot stats; barred from options expression — moot,
  scope C never ran). Power floors: 69 >= 60 legs and 23 >= 20 rebalance
  clusters — met.
- Semantics: spot proxy, census close-trigger family — a condition
  observed at a session close exits AT that observed close; levels are
  `E*(1-x)` / `M*(1-x)` only; context conditions use PRIOR-session values
  (vixterm: prior-session VIX >= VIX3M closes, 16:15 print, INV-02;
  breadth: prior-session fraction of the 36 tradables with close > prior
  close, INV-10; mom20: prior-session close/close[-20]-1 on the name's own
  bars); time-stop = close[t+20]. Entry always close[t], long, Decimal
  closes. Paired diffs are net@5bp per signal/leg vs the scope's hold-20
  base on the identical signal set (one round trip per signal under every
  variant — the RT cancels in the diff; 10bp disclosed in artifacts);
  naive t, day-clustered t (cluster = signal session / rebalance session),
  conservative t = min(naive, clustered).
- Simulator selftest (synthetic bars, run BEFORE registration; no real
  data, no outcomes): 15 checks, 0 failures — arming, trailing ratchet,
  inclusive level ties, prior-session context reads, month-end alignment,
  zero-variance t conventions.

## 4. Scope C — DATA-GATED-NOT-RUN (withdrawn exactly as the menu marks it)

All six `c-*` ids (`c-ts20` base, `c-ts5`, `c-ts10`, `c-dstop70`,
`c-dstop15`, `c-expiry`) are withdrawn WITHOUT running, per the menu's
`data_gates` and the task ruling; no proxy was improvised. Gates recorded
with evidence in `artifacts/campaign-2026-09/exit-grid-2/scope-c-withdrawal.json`
and per-config artifacts:

- (i) long-dated option-bar capture: still in flight (menu ETA ~2026-09-27;
  `scripts/desk_longdated_capture.sh` present, not landed). On-disk
  option-bar history (`desk-store/iv-history/vwap_atm.json`) spans
  2024-08-26..2026-09-03 — short of the EG2-C evaluation span, which
  mirrors A (calendar era through 2026-08-28 with hold-20 completes
  needing bars through ~2026-09-25).
- (ii) post-M0 short-leg machinery: `research_protocol.yaml`
  `short_options` policy prohibits; debit-spread short legs require
  expiration/early-assignment/dividend/exercise-by-exception logic that
  does not exist (REGISTRATION-NOTES.md section 4, open question 3).

The trial rows stay REGISTERED with no outcome (the honest registry state
for a never-run config); m=5 stays charged at the full declared count; the
slot's value collapses to A+B, disclosed.

## 5. Results (sealed eras; verdicts per the menu criteria)

**EG2-A — HOLD-STANDS** (base reference: `a-hold20` n=64, net@5bp
+3.635%/trade, hit 57.8%, all 64 time-stopped at t+20):

| cell | paired mean | t(naive) | t(clust) | t(cons) | crit | verdict |
|---|---:|---:|---:|---:|---:|---|
| a-adv10-arm5 | +34.50bp | +0.98 | +1.03 | +0.98 | 2.539 | FAIL |
| a-adv05-arm10 | +29.52bp | +0.70 | +0.69 | +0.69 | 2.539 | FAIL |
| a-adv10-arm10 | +28.14bp | +0.78 | +0.86 | +0.78 | 2.539 | FAIL |
| a-trail10-arm5 | -21.98bp | -0.40 | -0.48 | -0.48 | 2.539 | FAIL |
| a-adv05-arm5 | -44.96bp | -0.54 | -0.47 | -0.54 | 2.539 | FAIL |
| a-mom20flip | -45.45bp | -0.43 | -0.38 | -0.43 | 2.539 | FAIL |
| a-trail15-arm5 | -61.22bp | -1.17 | -1.51 | -1.51 | 2.539 | FAIL |
| a-vixterm | -137.33bp | -1.54 | -1.30 | -1.54 | 2.539 | FAIL |
| a-breadth40 | -291.26bp | -2.19 | -2.10 | -2.19 | 2.539 | FAIL |

**EG2-B — HOLD-STANDS** (base reference: `b-hold20` n=69, net@5bp
+4.498%/leg, hit 50.7%):

| cell | paired mean | t(naive) | t(clust) | t(cons) | crit | verdict |
|---|---:|---:|---:|---:|---:|---|
| b-monthend | +49.56bp | +1.93 | +1.43 | +1.43 | 2.576 | FAIL |
| b-trail10-arm5 | -9.11bp | -0.15 | -0.13 | -0.15 | 2.576 | FAIL |
| b-adv10-arm10 | -27.29bp | -1.00 | -1.13 | -1.13 | 2.576 | FAIL |
| b-adv15 | -32.27bp | -1.21 | -1.57 | -1.57 | 2.576 | FAIL |
| b-adv10 | -86.89bp | -1.58 | -1.43 | -1.58 | 2.576 | FAIL |
| b-trail20-arm5 | -64.63bp | -1.76 | -1.85 | -1.85 | 2.576 | FAIL |
| b-trail15-arm5 | -101.11bp | -2.06 | -1.90 | -2.06 | 2.576 | FAIL |
| b-mom20flip | -112.89bp | -0.94 | -0.80 | -0.94 | 2.576 | FAIL |
| b-vixterm | -286.62bp | -1.92 | -1.21 | -1.92 | 2.576 | FAIL |
| b-breadth40 | -420.64bp | -2.34 | -1.59 | -2.34 | 2.576 | FAIL |

**EG2-C — DATA-GATED-NOT-RUN** (6 cells, section 4).

Verdict counts (per-cell vocabulary): 19 FAIL (9 A + 10 B, all with power
floors met), 6 DATA-GATED-NOT-RUN (C), 0 PASS, 0 UNDERPOWERED; the two
hold-20 BASE cells are paired references (census "—" precedent), excluded
from per-cell arithmetic. Scope verdicts: A HOLD-STANDS, B HOLD-STANDS,
C DATA-GATED-NOT-RUN.

**Descriptive best (NOT a selection — the grid is frozen and nothing
adopts):** `b-monthend`, paired mean **+49.56bp** vs `b-hold20`
(t_cons +1.43 < 2.576) — the only B cell above the economic floor, and it
never clears Bonferroni; best in A is `a-adv10-arm5` at +34.50bp
(t_cons +0.98). There is no inner-fold best (section 2).

## 6. Reading

The census null generalizes to the desk's actual card rules, exactly as
the registration priced (risks section 1): every premium here is
drift-carried and exits amputate it. The context timers are the worst
offenders — `a-breadth40` fires on 64/64 signals (mean exit i=3.9) and
gives up 291bp; `b-breadth40` fires on 69/69 (mean exit i=3.4) and gives
up 421bp; the vixterm timers amputate 137-287bp. Wide armed adverse stops
at -10% are the mildest family (within ~35bp of hold, three of four A
cells slightly positive) but nowhere near the 5bp economic floor plus
Bonferroni bar jointly. The hold-to-horizon discipline now stands on the
desk's hold-20 card rules at the sealed horizon; the exit question on the
survivors closes (scope C's options-expression exits remain untested
pending its gates — disclosed, not silent).

## 7. Posture

Nothing adopts: a PASS would only nominate an EXIT-SUCCESSOR for the
forward sealed card chain (>= 20 cards per rule, the XSMOM 18/20-card
review, pre-registered kill-switches — menu `rules.adoption`); no PASS
occurred. Any survivor reading still faces the ledger's base-rate/holdout
deflations and a DSR report at N = 24 declared alternatives
(DESK-CHECKS convention). Multiplicity charged at the full declared count
(9/10/5) regardless of the run subset.

## 8. Artifacts

- Registry: `/home/alexk/documents/tree_options/artifacts/campaign-2026-09/exit-grid-2.db`
- Per-config artifacts (21 executed + 6 withdrawals):
  `/home/alexk/documents/tree_options/artifacts/campaign-2026-09/exit-grid-2/trials/<trial_id>.json`
- Scope-C withdrawal stamp:
  `/home/alexk/documents/tree_options/artifacts/campaign-2026-09/exit-grid-2/scope-c-withdrawal.json`
- Round-1 verdicts (criteria arithmetic, fill mixes, trade rows via the
  per-config artifacts):
  `/home/alexk/documents/tree_options/artifacts/campaign-2026-09/exit-grid-2/verdicts-round1.json`
