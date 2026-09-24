# EXIT-GRID-2 — exit/hold-policy grid on the survivors (PRE-REGISTRATION)

Written 2026-09-23, before any run. Campaign-2026-09 slot file. Sealed by the
sha256 of this file recorded in the commit that adds it (the
`XSMOM-12-1-PREREG.md.sha256` / `IVHIST-001.md.sha256` precedent); INV-13
(research_protocol.yaml: "Every attempted configuration is recorded before its
outcome is viewed") is satisfied by this enumeration plus the trial registry.
One scored run per cell. No re-gridding, no level edits, no added variants
after results; a new idea is a new pre-registration.

## 0. Slot contract

EXTENDS the exit-grid evidence — `artifacts/paper-trades/EXIT-GRID.md`
(52 cells: 4 rules x 13 variants, horizon 1-2; all four rules are now in
`BANNED` in src/tree_options/desk/signals.py) and
`artifacts/paper-trades/XSMOM-EXITGRID.md` (26 cells: the same 13 variants at
horizon 60 on the two research constructions; 0/26 pass) — with the delta on
the two ALLOWED directions at the desk's sealed card horizon (hold 20). This
slot declares 27 cells / 24 alternatives across three scopes (section 5). It
re-runs no existing census cell (section 7).

## 1. Hypothesis (falsifiable)

**One sentence:** on the two allowed directions' sealed hold-20 card streams,
at least one pre-registered post-entry conditioning exit — wide armed
adverse-drift, ratcheting trailing, context-flip timer (CONTEXT_ONLY
variables), calendar-aligned time-stop, or (data-gated) options-expression
time/delta stop — improves paired per-signal net return vs the hold-20
baseline by >= 5bp with conservative day-clustered one-sided Bonferroni
significance on its sealed evaluation era; otherwise the hold-to-horizon
discipline stands everywhere and the exit question on the survivors closes.

Per scope: EG2-A asserts it for `pead_beat` (m=9), EG2-B for `xsmom_top3`
(m=10), EG2-C for defined-risk spread expressions of `pead_beat` (m=5,
data-gated). The census prior is AGAINST: every exit variant tested anywhere
in this program lost to hold (EXIT-GRID.md: all 52 cells "no"/report-only;
XSMOM-EXITGRID.md: 0/26, "hold-60 stands"; RESEARCH-LEDGER.md SURVIVORS:
"hold-1 exit policy (EXIT-GRID.md): no variant ever passed; holds everywhere
it has been tested"). This registration is priced to confirm that null on the
desk's actual rules as much as to break it.

## 2. Direction/use declaration

- Every cell TIMES THE EXECUTION of an ALLOWED direction
  (signals.py `ALLOWED_DIRECTION = {xsmom_top3, pead_beat}`). No cell fires an
  entry, selects or substitutes a name, flips or points a direction, shorts,
  or applies to any BANNED rule.
- Conditioning variables appear ONLY as exit conditions: VIX term structure,
  panel breadth, and name trend are consumed strictly as `CONTEXT_ONLY`
  (signals.py) — they may shorten a hold, never widen the trade set.
- A PASS never edits a sealed rule mid-stream. It nominates an EXIT-SUCCESSOR
  evaluated exactly as XSMOM-12-1-PREREG.md prescribes for rule successors:
  after the sealed rule's own 18/20-card review, through the forward card
  chain with the pre-registered kill-switches (RESEARCH-LEDGER.md
  DESK-CHECKS), never a swap of a card in flight.

## 3. Features, signals, and exact data sources

| item | definition | source (read 2026-09-23) |
|---|---|---|
| signals | `xsmom_top3` (first session of month, rank 36 tradables by close(t)/close(t-273)-1, top 3, hold 20) and `pead_beat` (first post-report session, move >= +1.5%, hold 20) | `src/tree_options/desk/signals.py` (ALLOWED_DIRECTION, XSMOM_LOOKBACK=273, PEAD_THRESHOLD=0.015, HOLD_SESSIONS=20) — the desk code, not the legacy scripts |
| equity panel | split-adjusted OHLC, 37 names 2021-09-13..2026-09-23 (verified by counting SPY's series today) | `artifacts/paper-trades/ohlc-panel.json` |
| earnings calendar | 29 keys (26 single stocks + SPY/QQQ/IWM), 217 dates, 2024-09-05..2026-11-17; only single-stock events feed `pead_beat` (desk EARNINGS_NAMES excludes ETFs) | `artifacts/paper-trades/earnings-calendar.json`; `src/tree_options/desk/events.py` |
| VIX term | VIX close >= VIX3M close, PRIOR session (indices print 16:15 ET; INV-02 availability) | `artifacts/desk-store/indices/VIX.csv`, `VIX3M.csv` (multi-decade through 2026-09-22) |
| breadth | fraction of the 36 tradables with close > prior close, PRIOR session (INV-10: no same-close feature on a same-close fill) | computed from `ohlc-panel.json` (`backtest.breadth_map` precedent) |
| trend (name) | close/close[-20] - 1, PRIOR session | computed from `ohlc-panel.json` |
| option bars (arm C) | daily per-contract VWAP bars; on disk for the original 29 names, sessions 2024-08-26..2026-09-03; long-dated capture IN FLIGHT (ETA ~2026-09-27) | `artifacts/massive-cache/*.json` (per `docs/desk/IVHIST-001.md`); `scripts/desk_longdated_capture.sh` |
| exit vocabulary | touch / time-stop / expiry — mapped to close-trigger semantics for the spot arms (below) | `src/tree_options/trex/exit_watch.py` |

Data traps carried in: META hole 2022-01-28..2022-06-09 guard
(signals.py `HOLE_DAYS` convention); NFLX 10:1 split 2025-11-17 (panel is
split-adjusted); **2025-01-09 is a NON-session** (RESEARCH-LEDGER.md DATA
INTEGRITY) — engines walk each name's own panel series and treat it as a
non-session; the sealed protocol calendar is not walked raw. PLTR and SPCX
are in `desk-universe.toml` but NOT yet in the panel: all scopes pin to the
35 full-history chain-universe names (panel 37 minus TQQQ/SQQQ) and the 36
full-history XSMOM tradables (37 minus SPY); the two are later additions, a
universe edit is a new registration.

Exit semantics (spot arms, identical to the census machinery in
`artifacts/paper-trades/backtest_exit_grid.py` close-trigger family): a
condition observed at a session close exits AT that observed close; a level
is `E*(1±x)` or `M*(1-x)` only — no lookahead by construction, no gap
clamping needed (no intraday fills are simulated). Context conditions use
prior-session values. Time-stop = close[t+20]. Arm C (protocol lane): every
exit decision at close(t+i) fills at t+i+1 (`execution_session >
decision_session`, timestamp_semantics), vwap quote kind per the 0.2.0 G3
amendment (session_vwap_conservative_tick, participation-capped,
zero-volume unfillable), money $0.65/contract with $1/order minimum.

## 4. Fold mapping (inner/outer)

Protocol geometry (research_protocol.yaml `folds`): anchored_expanding,
validation 126, test 63, roll 63, min_train 252, embargo 5, label_horizon 5.

**Inner loop: EMPTY BY DESIGN for scopes A and B.** Every level is pinned
ex-ante (section 5 justification); nothing is selected on data, so no tuning
fold is consumed. The house precedent is the frozen grid with full
multiplicity (EXIT-GRID.md "DECLARED pre-registered runs"; XSMOM-EXITGRID.md
"Grid FROZEN ... before the first run"; XSMOM-12-1-PREREG.md "no re-gridding").
The reserved windows — should a successor registration introduce tunable
parameters — are, computed today from the panel:

- EG2-A (calendar era, 514 sessions 2024-09-05..2026-09-23): anchored train
  2024-09-05..2025-09-08 (252), validation fold 2025-09-09..2026-03-10
  (126, 53 report dates), purge >= 25 sessions (hold 20 + embargo 5 — the
  exit label spans the hold, so this slot's purge is H+embargo, stricter than
  the protocol's 5), sealed test 2026-05-21..2026-08-20 (63, hold-complete
  through 2026-09-18).
- EG2-B (first rankable rebalance 2022-11-01 — 274-session lookback consumes
  the panel's first year): tuning rebalances 2022-11-01..2024-09-03 (23
  month-starts), validation block the last 126 sessions (2024-03-05..2024-09-03,
  6 rebalances), purge 25 sessions after 2024-09-03 (ends 2024-10-08).

**Outer (single sealed evaluation window per scope), run once:**

- EG2-A: calendar era 2024-09-05..2026-08-28, beats-only, entries restricted
  to hold-complete (the family convention by which PEAD-DEEP's n falls
  209 -> 187 from h5 to h40); boundary session computed by the engine.
- EG2-B: sealed basis era 2024-09-04..2026-08-28 on the monthly card stream —
  23 rebalances 2024-10-01..2026-08-03, 69 legs (matches PROTOCOL-XSMOM.md's
  months 2024-10..2026-08). The daily-overlap research construction is NOT
  re-run: that is XSMOM-EXITGRID's ground, already answered at h60.
- EG2-C: fold mapping mirrors A on whatever bar span passes the data gate;
  feasibility floor 252+126+25+63 = 466 usable sessions (the on-disk cache
  spans 508; the long-dated capture may extend). Below the floor the scope
  records DATA-GATED-NOT-RUN.

**Honesty statement (load-bearing):** no virgin backtest window exists for
either survivor. The census consumed the panel through 2026-09-23
(XSMOM-12-1 scored 2026-09-23, 32/32 cells, RESEARCH-LEDGER.md;
PROTOCOL-PEAD.md publishes per-card hold-20 P&L through 2026-08-06), and
PEAD-DEEP/HORIZON-001 published the hold ladder over the calendar era. The
seal therefore binds the VARIANT DELTAS (none of these 24 alternatives exists
anywhere in the census), the base hold-20 outcomes are public, and the only
clean out-of-sample for a PASS is the forward card chain. Multiplicity is
charged at the full declared scope count regardless of run subset.

## 5. Config grid (27 cells / 24 alternatives; each scope <= 32)

**EG2-A — `pead_beat` hold-20 x post-entry conditioning** (spot proxy, 26
calendar-covered single stocks, beats-only stream recomputed from
signals.py — NOT PROTOCOL-PEAD's |move|>=1.5% card set; 5bp/10bp RT,
paired diffs vs hold-20 in which the round trip cancels: one round trip per
signal under every variant).

| cell | semantics (exit at the observed close; else time-stop close[t+20]) |
|---|---|
| a-hold20 (BASE) | the sealed rule |
| a-adv05-arm5 | first close at i>=5 with close/entry-1 <= -5% |
| a-adv10-arm5 | same at -10% |
| a-adv05-arm10 | first close at i>=10 with close/entry-1 <= -5% |
| a-adv10-arm10 | same at -10% |
| a-trail10-arm5 | first close at i>=5 with close <= running-max(prior closes)*(1-0.10) |
| a-trail15-arm5 | same at -15% |
| a-vixterm | first close whose PRIOR session has VIX >= VIX3M |
| a-breadth40 | first close whose PRIOR session's breadth < 0.40 |
| a-mom20flip | first close whose PRIOR session's name mom20 <= 0 |

Ex-ante level justification (no tuning): PEAD-DEEP.md's era-median |surprise|
is 1.46% and the census's only tested levels (+/-0.5/1/1.5%, EXIT-GRID.md /
XSMOM-EXITGRID.md) are intra-horizon noise at a 20-session hold — stop100c's
own fill-mix shows the -1% close trigger firing on 4,492 of 5,364 signals
(83.7%) at h60. 5%/10% and trailing 10%/15% are ~0.5-1x the scale of a
20-session single-name move; arming at 5/10 lets the drift season.

**EG2-B — `xsmom_top3` monthly card exits** (spot proxy, 36 tradables ranked,
top 3 held 20; TQQQ/SQQQ picks are logged, included in spot stats, barred
from options expression; 5bp RT, paired per leg vs hold-20, clustered by
rebalance session).

| cell | semantics |
|---|---|
| b-hold20 (BASE) | the sealed rule (exit t+20) |
| b-trail10-arm5 | trailing -10% off the running max close, armed i>=5 |
| b-trail15-arm5 | same at -15% |
| b-trail20-arm5 | same at -20% |
| b-adv10 | static adverse: first close <= entry*0.90 |
| b-adv15 | same at -15% |
| b-adv10-arm10 | adverse -10% armed i>=10 |
| b-mom20flip | exit leg at first close whose PRIOR session's leg mom20 <= 0 |
| b-vixterm | exit ALL open legs at first close whose PRIOR session has VIX >= VIX3M |
| b-breadth40 | exit at first close whose PRIOR session's breadth < 0.40 |
| b-monthend | calendar-aligned time-stop: exit at the month's last NYSE session |

This slot does NOT re-litigate hold 20 vs 60 (XSMOM.md hold ladder;
XSMOM-12-1.md per the ledger) and does NOT re-run the 13 fixed census
variants at any horizon.

**EG2-C — options-expression exits on defined-risk spreads (DATA-GATED)** —
debit spread mirroring `pead_beat` entries on the chain universe: long leg =
option_candidate_defaults candidate (dte 30-60, |delta| 0.30-0.60, min OI
500, min same-day volume 100, max spread 10% of mid, $50M 20d median dollar
volume, no earnings spanning hold), short leg = next-strike same-expiry
(naked shorts prohibited; short legs in debit spreads are post-M0 per
research_protocol.yaml `short_options` — that machinery gate must also be
live). Fills vwap/G3, $0.65/contract, $1/order min, next-session execution.

| cell | semantics |
|---|---|
| c-ts20 (BASE) | time-stop: close the spread at session t+20 |
| c-ts5 | time-stop at t+5 |
| c-ts10 | time-stop at t+10 |
| c-dstop70 | exit when short-leg abs(delta) >= 0.70 (delta provenance: model-derived-from-vwap accepted, 0.2.2) |
| c-dstop15 | exit when long-leg abs(delta) <= 0.15 |
| c-expiry | ride to expiry settlement (wave0 arm-B precedent, docs/theory/wave0-registration.json) |

Gates before any C cell runs: (i) the long-dated bar capture lands and passes
a coverage/integrity check on the evaluation span; (ii) the post-M0
short-leg machinery exists. Otherwise the scope's verdict stays
DATA-GATED-NOT-RUN forever — the slot's value collapses to A+B, disclosed.

## 6. Acceptance criteria and verdict vocabulary (fixed now)

Per cell, on the sealed evaluation era, paired vs the hold-20 baseline
(identical signal set), statistics exactly as the family machinery: mean
paired diff (net@5bp), naive t, day-clustered t (cluster = signal session /
rebalance session), conservative t = min(naive, clustered).

- **PASS** iff ALL of: paired mean > 0; conservative t >= one-sided Bonferroni
  quantile at alpha=0.05/m (m=9 for A, 10 for B, 5 for C; normal-approx
  critical values ~2.54 / 2.576 / 2.33, pinned exactly at freeze on the
  realized df); paired mean >= 5bp (economic floor, the intraday-pilot
  convention); power floor met (A: >= 30 sealed beats; B: >= 60 legs and
  >= 20 rebalance clusters; C: >= 40 spread episodes).
- **FAIL**: cell run with adequate power, any criterion missed.
- **UNDERPOWERED**: below the power floor — hold stands, no promotion;
  re-opened only by a successor registration (roll accrual is not an
  automatic second look).
- **DATA-GATED-NOT-RUN**: scope C while a gate is unmet.
- Scope verdicts: **HOLD-STANDS** (no cell PASSes; the hold-60/hold-1
  discipline extends to the desk's hold-20 card rules) or
  **EXIT-SUCCESSOR-NOMINATED** (>= 1 PASS; adoption only via the forward card
  chain after the sealed rule's 18/20-card review — section 2).
- Program-wide posture unchanged: any survivor also faces the ledger's
  base-rate/holdout deflations and a DSR report at N = 24 declared
  alternatives (DESK-CHECKS convention, iter005 dsr_block).

## 7. Novelty against the census (files read; if the census tried it, it is
excluded here)

- `EXIT-GRID.md` (52 cells): 4 rules x 13 variants at horizon 1-2 — every
  rule is BANNED today (mr_h1, r3f, r3f_up, cont in signals.py); variants are
  fixed +/-0.5/1/1.5% levels, hold1/hold2, openexit, OCO; the N-grid is an
  ENTRY-side earnings exclusion. No conditioning, no trailing, no context
  timers, no survivor rules, horizon 1-2 only. Best cell (MR hold2,
  t_cons 1.93 < 2.64) still failed.
- `XSMOM-EXITGRID.md` (26 cells): the same 13 variants at h60 on the two
  research constructions, long leg only; 0/26; the +16% premium lives in the
  tail exits amputate (tp100c fires 95.3% and gives up 13.6pp on top3-h60
  full era). Trailing, wide, armed, context-timed, calendar-aligned variants
  were NEVER run, and the monthly sealed hold-20 card rule was never
  exit-gridded at all.
- `PEAD-DEEP.md` / `HORIZON-001.md`: hold ladder {5,10,20,40} UNCONDITIONAL;
  the big/small/up/down cuts are entry-information (surprise at entry), not
  post-entry conditioning. No exit variant exists for PEAD anywhere.
- `GATE-001.md` (per RESEARCH-LEDGER.md: 48 cells, entry gates, none lifts a
  rule to t>=2 holdout) and `SEMI-GATE.md`: context entered as ENTRY gates
  only — the exit side of the CONTEXT_ONLY vocabulary is untouched.
- `intraday_exit_pilot.py`: pre-registered 2026-09-15 on R3f+up (banned),
  10 minute-bar variants at a 1-day horizon; its declared output
  (`~/documents/tree_options-logs/intraday-exit-pilot.json`) is absent on
  disk today — no result to cite; superseded here regardless (banned rule,
  wrong horizon).
- `PROTOCOL-PEAD.md` / `PROTOCOL-XSMOM.md`: the sealed hold-20 rules — the
  baselines, not exit variants.
- Disjointness: no (rule, variant, horizon) tuple above coincides with any of
  the 27 cells — different rules (survivors only), the desk horizon (20), and
  variant families absent from the census (trailing >= 10%, armed exits,
  prior-session context timers, month-end alignment, options time/delta
  stops).

## 8. Risks and the most likely failure

1. **The census null generalizes (most likely).** Both survivors' premium is
   tail-carried; exits amputate tails by construction. Expected outcome:
   every A/B cell posts negative paired diffs (the h60 grid ran t_cons -12
   to -19) and both scopes return HOLD-STANDS. The slot's value is then
   closing the exit question on the desk's actual card rules with the census
   machinery — a documented negative, not a null result by omission.
2. **Power.** B has 23 clusters / 69 legs; A's beats-only era stream is of
   order 60-90 signals; the conservative clustered t is capped by cluster
   count. UNDERPOWERED verdicts are likely even for genuinely mild effects.
3. **Contamination channel (disclosed).** The hold-20 base outcomes in both
   evaluation eras are published (PROTOCOL-PEAD.md per-card P&L;
   PROTOCOL-XSMOM.md monthly table); the variant deltas are virgin, but
   post-hoc knowledge of which beats drifted cannot be excluded — the
   forward card chain is the only clean test of a PASS.
4. **Calendar and panel traps.** 2025-01-09 phantom session in the sealed
   protocol calendar; META hole; label censoring at the panel end — all
   handled per section 3, each capable of silently shifting paired diffs if
   an engine walks the wrong series.
5. **Scope C never runs.** Capture late or coverage < 466 usable sessions, or
   the post-M0 short-leg machinery not live — verdict stays
   DATA-GATED-NOT-RUN and 6 of 27 cells return nothing.
6. **Close-trigger semantics on the spot arms** (exit at the observed
   triggering close, the census convention) are more conservative than a live
   intraday touch exit would be; a live implementation would exit earlier
   within the session. The desk's minute-bar refinement template exists
   (intraday_exit_pilot.py) and is deliberately NOT re-registered here.
