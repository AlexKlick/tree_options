# TERM-GATE — VIX term-structure context gate on the survivor card rules

Slot registration, campaign 2026-09. Author: TERM-GATE slot agent, 2026-09-23.
Status: PRE-REGISTERED; no cell has been run. This file enumerates every
attempted configuration in advance (INV-13); nothing below may change after
the first outcome is viewed. Execution is a separate later step; this
document registers, it does not run.

Scope pin: the 35 full-history chain-universe names (panel of 37 on disk at
`artifacts/paper-trades/ohlc-panel.json`, 2021-09-13..2026-09-23, verified:
no late starters, minus PLTR/SPCX which are in `desk-universe.toml` but not
yet in the panel). XSMOM ranks the 36 panel tradables (panel minus SPY).
PLTR/SPCX are later additions: they enter only via backfill plus a successor
registration, never silently. No arm of this slot is data-gated: it consumes
only the equity panel and CBOE index CSVs (the long-dated option bars ETA
~2026-09-27, the one-session chain store, and the IVHIST-001 low-fidelity
name-IV history are all irrelevant here — no IV-threshold rule is declared).

## 1. Hypothesis (falsifiable, one sentence)

A VIX term-structure gate evaluated at t−1 — the VIX9D/VIX3M ratio (or the
VIX3M/VIX1Y back slope) against a trailing-252-session quantile — withholds
XSMOM-TOP3 and PEAD-BIGSURPRISE cards whose sealed-window conditional mean
is below the kept cards' (ON > OFF, ON > 0 at the family bar, gated book no
worse than ungated) and separates kept from withheld cards strictly better
than the incumbent `vix_term` definition (VIX/VIX3M − 1, per
docs/theory/trade-jepa-exploration-2026-09-23.md line 125) run through
identical machinery on identical cards; if no new-quantity gate both passes
the cell bar and beats the incumbent on either rule's sealed window, the
term-gate family is withdrawn.

Secondary (Arm T2, descriptive only): the pooled failure of early exits on
the XSMOM long legs (XSMOM-EXITGRID.md: all 26 variants fail by conservative
t −12..−26) is not concentrated in the stressed term-structure regime — i.e.
no regime-conditional exit would have helped.

## 2. Declared direction / use (CONTEXT_ONLY, plan D5)

- The gate is a context condition in the `vix_term` family
  (`src/tree_options/desk/signals.py` CONTEXT_ONLY = {trend, breadth,
  vix_term}). It may only WITHHOLD an entry of an ALLOWED_DIRECTION signal
  (`xsmom_top3`, `pead_beat`): OFF at the decision session = the card is
  skipped entirely for that occurrence (XSMOM: the whole month's top-3;
  PEAD: that report's card). No deferral, no re-entry, no re-ranking, no
  name substitution, no inversion, no sizing-up, no short leg (`short_any`,
  `xsmom_short_leg` stay BANNED; naked shorts prohibited by protocol).
- It never points a direction and never becomes an input to name selection.
- A PASS-GATE earns exactly two things: (a) a recorded context annotation on
  future cards (the same status the refuted breadth caveat had), and (b) a
  queued successor pre-registration. It does not touch the sealed card rules
  mid-stream — the XSMOM-12-1 ruling (RESEARCH-LEDGER.md 2026-09-23: a
  successor "can only become [one] after the sealed rule's own 18/20-card
  evaluation, never a mid-stream swap") governs. Kill-switch accounting
  (DESK-CHECKS.md) stays on the ungated rules.
- Arm T2 is report-only, same posture as SEMI-GATE.md.

## 3. Feature definitions and exact data sources

Gate variable at decision session t uses ONLY index closes of session t−1
(availability: CBOE daily index closes finalize at/after 16:15 ET, after the
16:00 decision instant — timestamp_semantics INV-02/INV-10 conservatism;
this also absorbs the 1-session tail lag: index files end 2026-09-22, panel
ends 2026-09-23). Trailing statistics use sessions [t−252, t−1] inclusive
(252 sessions = protocol min_train 252; bracket corrected from [t−253, t−1]
at campaign assembly — 253 sessions contradicted the stated count), the
GATE-001 `spyvol` trailing-median idiom
(`artifacts/paper-trades/oos_validation.py` gate001()). All joins are on ISO
index dates present in the CSVs; the protocol calendar's phantom 2025-01-09
session (RESEARCH-LEDGER.md data-integrity note) has no CBOE row and no
panel bar, so date-keyed joins sidestep it.

- r93(d) = VIX9D_close(d) / VIX3M_close(d). Verified coverage on the panel
  window: 1262 of 1262 SPY∩VIX sessions; r93 > 1 on 149 (11.8%); median
  0.831, q75 0.932, max 1.697.
- inc(d) = VIX_close(d) / VIX3M_close(d) − 1 (the incumbent `vix_term`).
  inc ≥ 0 (backwardation) on 64 sessions (5.1%); median −0.113.
- bs(d) = VIX3M_close(d) / VIX1Y_close(d) − 1. bs ≥ 0 on <25% of sessions
  (above the 75th pct; median −0.135).
- Gate OFF = stressed side in every spec: value at t−1 ≥ threshold.
  Threshold styles: SIGN (fit-free: r93 ≥ 1.0 / inc ≥ 0 / bs ≥ 0) and
  Q60 (value ≥ its own trailing-252-session 60th percentile → ~40% OFF by
  construction). VIX1D-based gates are EXCLUDED: VIX1D history starts
  2022-05-13, inside the panel window — it cannot cover the tuning era.
  VIX6M is on disk but unused.

Sources (paths, verified ranges/rows; sha256 prefixes from
`artifacts/desk-store/indices/provenance.jsonl`):
- `artifacts/desk-store/indices/VIX9D.csv` 2011-01-04..2026-09-22, 3952 rows
  (94e916c0…); `VIX3M.csv` 2009-09-18..2026-09-22, 4278 rows (fd84b11a…);
  `VIX.csv` 1990-01-02..2026-09-22, 9278 rows (c1fa255f…);
  `VIX1Y.csv` 2007-01-03..2026-09-22, 4955 rows (a8bf534a…).
- Card rules and baselines: `artifacts/paper-trades/ohlc-panel.json`
  (provenance beside it), `src/tree_options/desk/signals.py` (xsmom_top3:
  FOM rebalance, close(t)/close(t−273)−1, top 3 of ≥30 ranked, hold 20;
  pead_beats: first post-report session, move ≥ +1.5%, hold 20),
  `artifacts/paper-trades/PROTOCOL-XSMOM.md` (46 months, +13,255 USD, 60.9%
  win) and `PROTOCOL-PEAD.md` (144 cards, +44/card) as the ungated
  baselines, `artifacts/paper-trades/earnings-calendar.json` (+seals) for
  report dates, `artifacts/paper-trades/RULES.md` sealed-basis accounting
  (close-to-close, 5bp RT).
- Arm T2 cells: `artifacts/paper-trades/XSMOM-EXITGRID.md` +
  `xsmom_exitgrid.py` (frozen grid, per-signal paired diffs vs hold-60).

Accounting disclosure: cells are evaluated on the card-lane accounting the
published baselines use ($2,500/leg, entry close[t], exit close[t+20], 5bp
RT) so ON/OFF cells are directly comparable to PROTOCOL-*.md. The protocol's
option-fill door (bid/ask primary, $0.65/contract) applies only to a later
options expression of a promoted gate — it is not part of this verdict.

## 4. Inner / outer fold mapping

Protocol folds (`research_protocol.yaml`): anchored_expanding, min_train 252,
embargo 5; card geometry declared per precedent (label_horizon = 20 sessions
— the card hold — as XSMOM-EXITGRID declared h60 and wave0 declared its own
`geometry_grid_fridays`; no protocol edit).

- Inner (tuning) sessions: all sessions ≤ 2024-09-03 (the ledger's holdout
  era; ~752 sessions ≥ min_train). Realized tuning cards: XSMOM entries
  2022-11-01..2024-08-01 (22 cards). PEAD has NO tuning-era cards (calendar
  era starts 2024-09-05) — its thresholds are therefore fit-free (SIGN) or
  trailing-quantile (self-updating, outcome-free); no PEAD outcome informs
  any threshold. The only tuning-era selection permitted: ranking the six
  gate specs on the 22 XSMOM cards, recorded before any sealed-era run.
- Purge/embargo to the sealed window: last tuning signal 2024-08-01, label
  20 + embargo 5 → first eval signal must satisfy ordinal > 2024-08-01 + 25
  sessions = 2024-09-09 (purge_gap_rule). Declared sealed boundary: signal
  dates ≥ 2024-10-01 (first FOM past the purge; PEAD follows the same date
  for symmetry, dropping its three 2024-09 cards).
- Sealed window (the single evaluation, one run, one verdict): XSMOM entries
  2024-10-01..2026-08-03 (23 monthly cards; the 2026-09-01 card is
  incomplete at panel end and excluded); PEAD entries 2024-10-16..2026-08-06
  (141 cards with completed 20-session holds). Cards after the panel cutoff
  accrue to the forward chain and are forward monitoring (demote-or-hold),
  never part of this verdict.
- Declared deviation, disclosed: no rolling 63-session outer folds — the
  sealed window is ONE block, matching the GATE-001/XSMOM-EXITGRID era
  idiom; 63-session rolls would fragment a 23-card sample. Validation
  window 126 ≈ the 6 months straddling the era boundary is subsumed into
  the embargo gap above.

## 5. Config count and enumerated grid (24 cells ≤ 32/scope)

Arm T1 — card-timing gates (12 cells; the cell = gate spec × rule;
ON/OFF conditional means, clustered t, day = entry date):

| id | gate | rule | OFF condition |
|---|---|---|---|
| T1-01 | r93 × Q60 (PRIMARY) | xsmom_top3 | r93(t−1) ≥ trailing-252 q60 |
| T1-02 | r93 × Q60 (PRIMARY) | pead_beat | same |
| T1-03 | r93 × SIGN | xsmom_top3 | r93(t−1) ≥ 1.0 |
| T1-04 | r93 × SIGN | pead_beat | same |
| T1-05 | inc × Q60 (incumbent) | xsmom_top3 | inc(t−1) ≥ trailing-252 q60 |
| T1-06 | inc × Q60 (incumbent) | pead_beat | same |
| T1-07 | inc × SIGN (incumbent) | xsmom_top3 | VIX(t−1) ≥ VIX3M(t−1) |
| T1-08 | inc × SIGN (incumbent) | pead_beat | same |
| T1-09 | bs × Q60 | xsmom_top3 | bs(t−1) ≥ trailing-252 q60 |
| T1-10 | bs × Q60 | pead_beat | same |
| T1-11 | bs × SIGN | xsmom_top3 | VIX3M(t−1) ≥ VIX1Y(t−1) |
| T1-12 | bs × SIGN | pead_beat | same |

Arm T2 — exit-grid regime interaction (12 descriptive cells; statistic =
per-signal paired diff (variant − hold60) within cell, conservative
t = min(naive, day-clustered); regime split r93(t−1) ≥ trailing-252 median
→ STRESSED ≈ 50% of sessions; cells from the frozen XSMOM-EXITGRID grid,
re-derived by adding a regime column to the frozen script — no parameter
changes; if per-signal diffs are recoverable from stored cell outputs, no
re-run at all):

| id | construction | variant | regime |
|---|---|---|---|
| T2-01 | 60-skip5-tercile-h60 | hold1 | CALM |
| T2-02 | 60-skip5-tercile-h60 | hold1 | STRESSED |
| T2-03 | 60-skip5-tercile-h60 | tp100c | CALM |
| T2-04 | 60-skip5-tercile-h60 | tp100c | STRESSED |
| T2-05 | 60-skip5-tercile-h60 | oco100_150 | CALM |
| T2-06 | 60-skip5-tercile-h60 | oco100_150 | STRESSED |
| T2-07 | 252-skip21-top3-h60 | hold1 | CALM |
| T2-08 | 252-skip21-top3-h60 | hold1 | STRESSED |
| T2-09 | 252-skip21-top3-h60 | tp100c | CALM |
| T2-10 | 252-skip21-top3-h60 | tp100c | STRESSED |
| T2-11 | 252-skip21-top3-h60 | oco100_150 | CALM |
| T2-12 | 252-skip21-top3-h60 | oco100_150 | STRESSED |

(Variants chosen: hold1 = the "hold-1 exit" question; tp100c = the tail
amputation the ledger says costs ~13.6pp on the top-3 book; oco100_150 =
the shallowest-maxDD bracket. 3 variants × 2 constructions × 2 regimes
= 12.)

Total 24 registered cells; the 32/scope budget leaves 8 slots unspent and
they expire with this registration.

## 6. Acceptance criteria and verdict vocabulary (fixed in advance)

Evaluated ONLY on the sealed window. Statistics: day-clustered t (ddof=1,
cluster = entry date), conservative t = min(naive, clustered); family bar
one-sided Bonferroni t ≥ 2.64 (m = 12, the EXIT-GRID/XSMOM-EXITGRID
precedent). Per T1 cell, all four must hold to be a CANDIDATE:

1. ON mean > 0 and clustered-t(ON) ≥ 2.64.
2. ON mean > OFF mean.
3. Utility: gated book mean/card ≥ ungated sealed-window mean/card AND
   gated total P&L ≥ ungated total (skipping cards must not destroy value —
   this kills gates whose OFF bucket contains the 2026-04-type month).
4. Power floor: OFF-n ≥ 8 (XSMOM arm) / ≥ 20 (PEAD arm), else the cell is
   NOT_EVALUABLE and claims nothing.

Family beat-or-withdraw test (the slot's mandate): among new-quantity gates
(G1 = r93×Q60, plus r93×SIGN, bs×Q60, bs×SIGN if they cleared 1–4), the
best ON−OFF spread must exceed the best incumbent (inc×Q60, inc×SIGN)
spread by entry-date block bootstrap (10,000 resamples of sealed entry
dates, one-sided P(new > incumbent) < 0.05) on the same rule.

Verdicts, named now: **PASS-GATE** (a new-quantity gate is a CANDIDATE and
beats the incumbent on that rule); **INCUMBENT-CONFIRMED** (only inc cells
are CANDIDATES — `vix_term` is confirmed as the context signal, no
successor); **WITHDRAW** (no CANDIDATE on either rule, or new-quantity
CANDIDATEs exist but none beats the incumbent — recorded as vix_term-parity
and the family dies); **NOT_EVALUABLE** (power floor or an index gap > 5
sessions inside the sealed window). T2 verdicts: **REGIME-SIGN-FLIP** (a
variant has paired mean > 0 with conservative t ≥ 2 in STRESSED while ≤ 0
in CALM), **NO-REGIME-SIGNAL**, **NOT_EVALUABLE** (any cell < 100 signal
dates) — all prefixed DESCRIPTIVE-ONLY: outcome conditioning on
already-viewed cells; the XU post-hoc-amendment precedent forbids any live
change from it; a regime-conditional exit needs a fresh sealed registration.

## 7. Novelty against the census (files read)

- `artifacts/paper-trades/GATE-001.md` (48 cells, with EXEC-001): the only
  prior gate family. Its gates were spyup (SPY mom20 > 0), breadth (fraction
  of names above own 20-mean > 0.5), and spyvol (SPY vol20 below its
  252-session median) — a trend gate, a breadth gate, and a SPY realized-VOL
  LEVEL gate. No VIX index appears in any GATE-001 cell; no term-structure
  quantity (no ratio of two tenors) was ever gated on; and the gated rules
  were MR / R3f+up / CONT / VOLSPIKE at holds 1–2 — all now in the ledger's
  DEAD list. The survivors (XSMOM-273, PEAD-beats) were never gated by
  anything. Result to beat: gates pointed the right way (ON > OFF in most
  cells) but lifted nothing to t ≥ 2 on the holdout — that is our base-rate
  prior and the reason WITHDRAW is pre-committed.
- `src/tree_options/desk/signals.py`: `vix_term` sits in CONTEXT_ONLY by
  DECLARATION (plan D5); no census cell has ever evaluated it as a card
  gate. The incumbent definition VIX/VIX3M − 1 is fixed by
  docs/theory/trade-jepa-exploration-2026-09-23.md (its §2.5(b) names it
  "the incumbent regime signal" with no card-gate evidence behind it). Our
  inc cells are the first evaluation of that incumbent as a gate; the r93
  and bs quantities are first uses of VIX9D/VIX1Y in the program.
- `artifacts/paper-trades/SEMI-GATE.md`: SMH mom20 > 0 gate on the five
  semis' long rules — a trend gate on non-survivors, report-only; different
  family, different target.
- `artifacts/paper-trades/XSMOM-EXITGRID.md` + `EXIT-GRID.md`: hold-policy
  grids unconditioned on any regime; T2's regime split is new conditioning
  but on already-viewed outcomes, hence descriptive-only. The ledger's
  standing questions (RESEARCH-LEDGER.md) list no term-structure item;
  SWEEP-001/002, SQUEEZE, GAPS, HORIZON-001, MOM60-REPL, SECT-ROT, XSMOM-12-1
  are rule/construction families, not gates on the survivors; PEAD-SIGN /
  PEAD-DEEP condition on the event's own surprise, not market regime.
- Closest prior in spirit: GATE-001's spyvol (vol level, dead rules,
  failed). The pivot from that evidence: different quantity (curve SHAPE at
  two tenors, forward-looking by construction, published 15+ min after the
  close hence cleanly lagged), different target (the two live card rules at
  their sealed 20-session hold), and a mandatory beat-the-incumbent bar so
  the family cannot survive as a re-labeled vol level. If the census
  question is "can ANY regime gate earn a live role," GATE-001 answered no
  for its three gates on dead rules; TERM-GATE answers it for the curve on
  the survivors or withdraws.

## 8. Risks and the most likely failure

1. Power (most likely failure of Arm T1 on XSMOM): 23 sealed monthly cards,
   ~40% OFF under Q60 → ~9 OFF cards against a floor of 8; a 5.1%-OFF SIGN
   cell yields ~1 OFF card → NOT_EVALUABLE by design. The family verdict
   effectively rests on the 141 PEAD cards.
2. Era conditioning: PEAD's sealed window (2024-10..2026-08) is the same
   single calendar era that produced PEAD-DEEP's positives; a gate PASS
   there carries the same regime-artifact caveat the ledger attaches to the
   RULES.md stats (OOS-2021 killed those). No pre-2024-09 PEAD data exists
   to defuse this; disclosed, not mitigable.
3. The gate skips the best months: momentum's largest cards plausibly START
   in stress (the 2026-04 +4,865 USD month began off an April stress window
   — a pattern, not a claim); criterion 3 (utility) then converts a
   statistically fine ON>OFF into a family WITHDRAW. This is the intended
   behavior, and the most likely literal failure mode.
4. Autocorrelation: term-structure regime persists for months, so 23
   monthly cards are few effective regime draws; day-clustering cannot
   manufacture independence.
5. Data: CBOE restates index history (sha256 pins in provenance.jsonl bound
   at run time); the 1-session index tail lag (files end 2026-09-22) is
   absorbed by the t−1 rule; a wider index outage (>5 sealed sessions)
   triggers NOT_EVALUABLE rather than imputation.
6. Arm T2 prior: pooled variant−hold60 margins are t −12..−26; a median
   regime split flipping any variant positive would be extraordinary.
   Expected verdict NO-REGIME-SIGNAL; registered to close the question the
   ledger leaves open, not to rescue exits.
7. Multiplicity laundering: the beat-incumbent bootstrap is computed only
   among cells that cleared the Bonferroni bar; a new gate that ties the
   incumbent is WITHDRAW (parity is not novelty). The banned-list trap
   (dressed-up vol level) is what the incumbent comparison exists to catch.
