# EB-RECHECK-001: era-boundary conditional replication of mom60 downside protection (PRE-REGISTRATION)

Written 2026-11-14, before any future MOM60-REPL boundary has been read and
before window 11 (2026-09-11..2027-03-11) completes. Sealed by
`EB-RECHECK-001.md.sha256` (the sha256 of this file, recorded in the commit
that adds it). PAPER-ONLY: this is a forward-only MEASUREMENT monitor, not a
strategy; its CONFIRMED verdict licenses nothing except its paired overlay's
own pre-registered forward test. Monitors/timers are owner-gated deployments,
out of scope here.

**Coupled pair.** This prereg and `TSMOM-SHIELD-001.md` (sealed the same day,
sha256 `6d903e18b0b8aaa0b0b0719e562b853e7fd2a8a34e9362ecf0349c72594fcaa3`)
are ONE registration unit: shared premise (the ledger's surviving TS-momentum
reading — window-level downside protection, RESEARCH-LEDGER L56-61), and THIS
document is the single premise/verdict channel for both. **The wave-1 phrase
"independent second evidence channel" is struck and may not be restored**:
both estimands descend from the same two 2022 windows of the same frozen grid
on the same panel family; a joint pass is correlated evidence about ONE
premise, never two confirmations (RT-3 C1). Every digest that mentions either
document mentions both, with that sentence.

Pinned at seal (sha256, first 16 hex):
- frozen grid `artifacts/paper-trades/mom60_repl.py` (re-used verbatim, never
  re-written): `1c7403b9e1f0e7a4…`
- panel `artifacts/paper-trades/ohlc-panel.json` @ 2026-11-13: `9fbde932dfade93e…`
- wave-1 spec `~/.local/state/trex-strategy-fanout-20261114/wave1/r2-conditioning.md` §C1: `2d184102c7b08694…`
- red-team verdict `…/wave2/r1-1-r2-1-tsmom-pair-redteam.md` (RT-3, MAJOR-REVISION,
  revisions R1-R6 — all applied here): `b2206fb6ff8f9b28…`

## The standing question this resolves

Ledger standing question 3 (RESEARCH-LEDGER L387): "re-check mom60_h60
conditional at the next era boundary." The surviving reading is not alpha but
window-level downside protection: in the only two half-year windows where the
hold-everything base lost more than ~5%, the mom60>0 filter beat the base by
+5.56% / +1.82% (MOM60-REPL.md rows 1-2); in bull windows it is ~zero or
negative (7/8 HORIZON-001 cells underperform the base; full-era conditional
−1.17%). The day-level split is the OPPOSITE (SPY-down conditional −2.13%,
SPY-up +0.89%; MOM60-REPL.md L16) — the protection is a drawdown-phase
phenomenon, not a down-day phenomenon. That structure either replicates at
the next bear boundary or was two lucky windows.

## Exact information set (as-of dates)

- Panel `artifacts/paper-trades/ohlc-panel.json`, read under the shared flock,
  **universe FROZEN at the original 37 names** (the current panel carries 39
  names / starts 2021-09-27; PLTR/SPCX would need their own successor
  registration — excluded here).
- Signal: trailing 60-session return > 0 at session i, hold 60, entry at
  close[i], outcome close[i+60]/close[i]−1, 5bp RT per trade, 10-calendar-day
  hole guard on [i−60, i+60] — the FROZEN `mom60_repl.py` grid verbatim
  (LOOKBACK/HOLD 60, Mar-11/Sep-11 window ends frozen 2026-09-11 before the
  first run; "completed" = boundary + 60 sessions, mechanical).
- Base: the identical walk with the signal filter removed.
- **As-of rule:** a window's row is computed only after its boundary PLUS 60
  sessions (window 11's last hold completes ~2027-06), evaluated ONCE, on
  pinned panel bytes (sha256 recorded at evaluation time).
- The trigger below uses the window's own BASE mean — observable at the same
  moment as the conditional delta, i.e. conditioned on a covariate declared
  NOW, not on any future selection.

## The rule (pre-registered)

1. **Mechanical row extension (no claim attached):** at every boundary,
   re-derive the FULL table (all rows, including the original 10) on the
   then-pinned panel bytes, and append the new window row. Full-table
   re-derivation each time — never a mixed-bytes table.
2. **Consistency re-anchor (first evaluation only), with VOID semantics.**
   The re-derived rows 1-10 are compared to MOM60-REPL.md. Disclosed diffs
   are expected (panel rebuild: 2021-09-27 start vs 2021-09-13). **The
   pre-declared known largest diff is W10's censoring/un-censoring: the
   published run carries W10 base +8.136% CENSORED; on current bytes the
   un-censored re-derivation gives +6.21% (RT-3 basis B) — benign, expected,
   and pre-declared here so no future operator improvises a tolerance**
   (RT-3 R5a). The other expected diffs, pre-declared: W1 moves −6.768% ->
   −6.428% (+0.34pp, start-date truncation, 66 -> 56 signal days; W1's cond
   actually strengthens +5.56% -> +6.88%); W2 is bit-identical (−5.099%,
   its signals/outcomes lie wholly inside both panels' coverage).
   **Premise check:** under the declared threshold, rows 1 and 2 must remain
   the ONLY windows with base mean <= −4.0%. If the re-derived table does not
   reproduce the two protected windows as the base<=−4.0% set, the protection
   premise is **VOIDED as an artifact of the old panel bytes** and the
   standing question closes that way (a legitimate outcome, recorded) — and,
   per the coupled pair, TSMOM-SHIELD-001 auto-retires (its binding premise
   table, adopted here by reference).
3. **Protection cell (fires conditionally):** a completed window W is a
   PROTECTION CELL iff `base_mean(W) <= −4.0` — the exact boundary
   arithmetic: base_mean is the MOM60-REPL day-clustered mean of base-book
   daily returns over W's signal dates, in percent, full float, compared
   inclusively against the declared constant −4.0, no rounding at any step.
   Statistic: cond(W) = day-clustered mean of (signal-book daily mean −
   base-book daily mean) over W's signal dates, 5bp RT both legs.
4. **Decision rule (fixed now):**
   - first future protection cell with cond > 0 → protection reading
     **REPLICATES-1 — sign-only and PROVISIONAL** (a single window is
     sign-only by construction; one cell never confirms anything);
     TSMOM-SHIELD-001 keeps its accrual priority toward its own 30-entry bar.
   - first future protection cell with cond <= 0 → protection hypothesis
     **DOWNGRADED** to closed-unless-reopened; TSMOM-SHIELD-001's accrual
     continues but its resolution verdict, if any, is permanently labelled
     PROVISIONAL-PANEL-FAILED and may never be cited without that label
     (binding consequence; the wave-1 "loses priority" wording is superseded
     by the pair's response table).
   - >=2 accumulated protection cells, all positive, pooled daily deltas
     one-sided sign-flip permutation (10,000 draws) p < 0.05 → protection
     **CONFIRMED** at measurement level (still not a trade; it licenses only
     TSMOM-SHIELD-001's own pre-registered forward test).

### The −4.0% trigger: declared once, robustness CITED, never re-shopped

−4.0% is declared ONCE here, before any recomputation on current bytes, with
its margin logic: in-sample fired margins −6.77% / −5.10% vs next-closest
−1.45% (published basis). RT-3's independent recompute MAY BE CITED as
verification, not as license: on the current panel (basis B, frozen 37 names)
the fired set reproduces as {−6.428%, −5.099%} with W2 1.10pp below the line
and W7 2.55pp above; and no in-sample threshold-shopping payoff exists — any
threshold in (−5.099%, −1.454%] fires the identical set {W1, W2} on every
basis tested (published / pre-rebuild / current-37 / current-39). The one
researcher degree of freedom is this constant. No threshold re-grid: changing
−4.0 voids the registration (new id, restart — the QSL/AMENDMENT precedent);
the re-anchor re-derives with the SAME constant; window-edge shopping is
foreclosed (grid FROZEN in `mom60_repl.py` constants before the first run).

## Null + placebo

- **Null (exact):** H0: in windows with base_mean <= −4.0%, cond <= 0
  (no drawdown-phase protection). One-sided.
- Placebo/falsification context (descriptive, reported every boundary): the
  (base_mean, cond) scatter over ALL windows — under H0 the two fired windows
  are exchangeable draws from the cond distribution of the others (in-sample:
  −3.92%..+1.44%). The pooled sign-flip permutation above is the only
  claim-bearing test; the scatter is context.

## Costs

No trades. The estimand is computed under the card-lane accounting
(close-to-close, 5bp RT, day-clustered), identical to MOM60-REPL — no mid
fills, no cost-convention change. Execution cost of any resulting overlay
lives in TSMOM-SHIELD-001's lane.

## Prior-cell count and multiplicity exposure (relabeled per RT-3 R5b)

Lineage already read: **26 mom60-family cells** (MOM60-REPL 10 windows +
HORIZON-001(a) 16 cells); the wave-1 "28 read cells" counted the HORIZON-001
doc's 2 PEAD cells — 26 mom60-conditional + 2 PEAD = 28 doc total, labeled
correctly here. Plus GATE-001's 48 gate cells as the regime-conditioning base
rate. EB-RECHECK scores **zero new cells on read data**: rows 1-10 are
re-derivations (consistency anchor, not selection) and future rows accrue one
per ~6 months. New researcher degrees of freedom consumed: **one** (the
−4.0% trigger, above). Multiple-testing exposure vs the ~1,220 ledger cells +
111 campaign configs: negligible and named.

## Power analysis (honest, with the pair's clock ordering)

Protection cells occur when the market delivers a base<=−4% half-year:
in-sample rate 2/10 windows → expected wait ~2.5 years, arrivals clustered
(bear eras). Within a fired window: ~115-128 signal days, day-clustered; the
in-sample fired deltas (+5.56%/+1.82%) would clear the pooled one-sided
sign-flip test over two windows; a SINGLE window is sign-only by construction
(first fired cell = REPLICATES-1, provisional-open, never confirm). Forward
only by construction: the entire 2021-09..2026-09 panel is read. First
possible protection-cell evaluation: the boundary after the first
base<=−4% window completes — earliest conceivable ~2027-06 (window 11
readable then, only if 2026-09..2027-03 is a heavy-loss window); **base case
2028+**. Clock ordering, stated for the pair: TSMOM-SHIELD-001's optimistic
bar (~2027-10) will likely arrive BEFORE this premise test's first fired
cell; any overlay verdict preceding it is PROVISIONAL on this channel
(TSMOM-SHIELD-001 §"Card unit, clocks, and bars").

## Banned-list cross-check

`ts_momentum` and `mom60_h60` are BANNED as direction signals (`signals.py`
BANNED, L57-82). EB-RECHECK never points a trade, never nominates a rule,
and its CONFIRMED verdict licenses only the paired size-throttle overlay —
which itself may never select a name, flip a sign, or add exposure
(TSMOM-SHIELD-001 constraints, adopted by reference). "Since protection
confirmed, trade mom60 directly" is the banned family and is auto-reject.

## Machinery (owner-gated; out of scope here)

Extend `mom60_repl.py` (frozen grid, report-only) with: (a) pinned-bytes
full-table re-derivation per boundary, (b) the base<=−4.0% protection-cell
annotation, (c) the decision-rule state machine. A biannual boundary reminder
is the only new operational piece; no new unit, no equity campaign runner,
no promotion by code. Deployment is an owner action.

## What was seen before sealing

- MOM60-REPL.md's full published 10-window table (2026-09-11 run), the
  HORIZON-001(a) 16 cells, the ledger deflation L54-61 — all read, all
  published; nothing here re-scores them.
- RT-3's read-only recomputes on three panel bases (published, pre-rebuild
  `.bak-20260923-predesk`, current 37/39) — cited above as verification of
  the trigger's margins; no outcome for any window after 2026-09-11 existed
  at seal (window 11 is accruing; panel current through 2026-11-13).
- TSMOM-SHIELD-001 sealed the same day (digest in the header); the pair's
  shared premise and response table are cross-pinned in both headers.
