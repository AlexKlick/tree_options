# campaign-2026-09 — round-1 CONSOLIDATION REPORT (inner folds)

Consolidator, 2026-09-24. Branch `exec/campaign-2026-09` (worktree
`/home/alexk/documents/tree_options-worktrees/campaign-exec`, off main
`dce50f8`). Sealed menu version 3:
`docs/theory/campaign-2026-09-registration.json` @ `3210e2e`, sha256
`4aca21014f6efa640d97bf2abd3fdbb912af5a7b9acb68f65b842f924379b10a`
(supersedes v2 `9fde5e66…`, v1 `3bb10a2f…`), 111 unique configs / 10
scopes; slot docs and `REGISTRATION-NOTES.md` sha-pinned in the menu.
**Every sealed window named in the menu was never scored, never read for
tuning, never plotted** (per-slot boundary proofs in the round docs:
deepest vrp-cond entry 2026-04-24, pead-deep-2 inner walk stops
2026-04-20, term-gate T2 holdout-only, jepa outer origins untouched,
exit-grid-2 reserved geometry untouched). INV-13 held in all six
registries. Nothing is adopted; nothing seals a card; the ledger entry
`campaign-2026-09 (inner-fold)` was filed by the consolidator in
`artifacts/paper-trades/RESEARCH-LEDGER.md` (main checkout, untracked
research state).

## 1. State at a glance

| slot | scope | reg rows (cap 32) | executed | unrun / failed | verdict summary | inner-fold best |
|---|---|---:|---:|---|---|---|
| tnull | c09-tnull | 6 (3 configs; 3 g1 voided) | 3 (g2) | 0 | CALIBRATED all seeds (v3) | B(W, shape) band + 6 priors |
| vrp-cond | c09-vrp-e | 20 | 20 | 0 | EVALUABLE 9 · NOT_EVALUABLE 11 (3 placebos) | **xe-mkt-hi** +0.149551 delta |
| vrp-cond | c09-vrp-o | 4 | 0 | 4 WITHDRAWN | DATA_GATED unmet | — |
| pead-deep-2 | c09-pd2 | 24 | 24 | 0 | CANDIDATE 0 · NULL 3 · INSUFFICIENT_N 21 | PD2-V-hi-h40 +6.8419% cond (descriptive) |
| term-gate | c09-term | 24 | 24 | 0 | NOT_EVALUABLE 6 · DESCRIPTIVE-ONLY:NO-REGIME-SIGNAL 12 · no round-1 verdict 6 | T1-03 ON−OFF −0.027427 (best of six, still negative) |
| exit-grid-2 | c09-eg2-a | 10 | 10 | 0 | 9 FAIL · 0 PASS → HOLD-STANDS | a-adv10-arm5 +34.50bp (t_cons +0.98) |
| exit-grid-2 | c09-eg2-b | 11 | 11 | 0 | 10 FAIL · 0 PASS → HOLD-STANDS | b-monthend +49.56bp (t_cons +1.43) |
| exit-grid-2 | c09-eg2-c | 6 | 0 | 6 DATA-GATED-NOT-RUN | gates unmet, terminal on failure | — |
| jepa-filter | c09-jf | 8 | 6 | 2 FAILED-defective | INNER-RECORDED 6 · NOT_EVALUABLE 2 | **JF-V2-H5** IC +0.012937 |
| agent-exec | c09-agentexec | 1 (placeholder) | 0 | never activated | — | — |

Totals: 113 trial rows / 111 unique configs; 101 COMPLETED rows (3 of
them the voided tnull g1 set, disclosed and retained); 2 FAILED
(jepa V3, no artifact); 10 REGISTERED-unrun (withdrawn data-gated arms).

## 2. The T-NULL band (why scoring was frozen twice and unfroze once)

- **v1 (cost-floor band [-15bp, +5bp])**: DEFECT-FLAGGED on all three
  seeds — not a code defect: hash-random longs sample the era's drift
  (SPY +35.3%, equal-weight 36-name +43.9%, all-names unconditional
  20-session net mean +1.55%, n = 17,136 over the card era). Family
  scoring froze.
- **Operator ruling 1 → menu v2 (`eb43b50`, drift-relative null)**: every
  cost-floor miss resolved; tnull-s1/s2 CALIBRATED, s3 still flagged only
  in the two 2-entry-day / 6-trade cells the registration itself had
  disclosed as "wide, weak band".
- **Operator ruling 2 → menu v3 (`3210e2e`, NOT_EVALUABLE floor)**: cells
  with < 5 entry-days or < 20 complete trades lose flag authority. All 12
  EVALUABLE cells pass both criteria on all three seeds (worst |t| =
  1.803). **Verdict: CALIBRATED; family sealed-window scoring UNFROZEN.**

Standing baseline B(W, shape) — unconditional all-names net (5bp RT)
per-trade mean, stamped in
`artifacts/campaign-2026-09/tnull/calibration-v3.json` (never a signal;
families in floored windows gate on B alone):

| window | shape | B net 5bp | day-clustered se |
|---|---|---:|---:|
| card-era 2024-10-01..2026-08-28 | xsmom | +1.5234% | 0.9255% |
| card-era | event | +1.1465% | 0.7475% |
| vrp-cond 2026-06-02..2026-08-31 | xsmom | +1.2636% | 2.0319% |
| vrp-cond | event | +2.0022% | 1.6970% |
| pead-deep-2 2026-06-25..2026-09-23 | xsmom | +1.2636% | 2.0319% |
| pead-deep-2 | event | +2.3181% | 1.5935% |
| jepa-outer 2026-01-02..2026-08-24 | xsmom | +1.4492% | 2.1887% |
| jepa-outer | event | +1.3189% | 1.3953% |

(pead-deep-2's cell bar reads the day-clustered B +1.0079% as its
primary leg, per-trade +2.3181% as the disclosed sensitivity leg.)
Tripwire priors, evaluable cells only (day-clustered sd of the per-trade
mean): card-era xsmom 1.4361% / event 0.8148%; jepa-outer xsmom 2.1394% /
event 1.3929% (union = card-era). vrp-cond and pead-deep-2 windows stamp
no prior — their null cells are floored, so those slots gate on B alone.

## 3. Per-slot narratives

### 3.1 T-NULL (`docs/campaign-2026-09/tnull.md`, rounds 1-3)

Ran first, as the menu binds. Three sha256-seeded random-long streams
(xsmom-shaped 72 entries, event-shaped 135) over the families' windows,
hold-20 close-to-close, 5bp/15bp RT, day-clustered t. Two voided
executions disclosed with full audit trail (empty g1 XSMOM stream caught
by the entry floor; a mis-tagged first calibration stamp) — artifacts
retained, nothing re-tuned. Outcome: CALIBRATED via the amendment chain
above; B(W, shape) + six priors now stand for the sealed round.

### 3.2 vrp-cond (scope E 20 cells, scope O withdrawn)

What ran: the registered IV30/HAR percentile conditioning grid over the
two ALLOWED directions (XSMOM top-3 no-skip 273; PEAD +1.5% beats),
inner V1+V2 pooled, tuning entries capped at ordinal 417, 5bp RT,
matched-sessions ON−OFF column stamped descriptively. Base cells: xe
+6.9908% (24 entries / 8 rebalances), xp +1.1156% (19 beats / 18 days).

Best configs (registered promotion rule, applied mechanically): **xe →
xe-mkt-hi** (fire top-3 only if p_IWM(t) >= 2/3): pooled
conditioned-minus-base net delta **+14.9551%** per trade (n_ON 9/6,
on-fraction 0.375; ON cell +21.9459% vs base +6.9908%). **xp →
xp-size-mkt** (always fire, half size if p_IWM(t-1) >= 2/3): **+0.3169%**
(n_ON 19/8). 9 EVALUABLE / 11 NOT_EVALUABLE (3 placebos among them).

What withdrew: scope O's 4 options-expression configs — DATA_GATED
(option-bar capture 27 as-of dates < 126 required; no IVHIST-002
successor), WITHDRAWN unrun, no proxy.

Reading: the registered hypothesis direction (cheap-vol conditioning
lifts allowed longs) found NO support — every cheap-side gate negative or
NOT_EVALUABLE; the xe nominee is sign-flipped (RICH side) and the sealed
round tests it once as registered. Sharpest deflation warning in the
grid: the xp-lag21 placebo posts +21.4371% at n_ON = 1.

### 3.3 pead-deep-2 (24 cells)

What ran: conditioning deepening of pead_beat over the inner walk
(50 beat events, 406 entry sessions 2024-09-05..2026-04-20): B references
at h10/20/40, within-beat median split, fixed magnitude bands, VIX
terciles, SPY RV20 terciles; day-clustered basis, B-read cell bar.

Verdict: **0 CANDIDATEs** — the pre-declared dominant risk (thin n) is
exactly what happened: all 21 strata sit below n >= 20 AND days >= 30
(n 13-25, days 12-23) → INSUFFICIENT_N, never pass/fail. The three
reference cells are NULL (t 1.12-1.54; a reference cannot beat itself).
Best conditioned cell descriptively: PD2-V-hi-h40, conditional +6.8419%
vs PD2-B-h40 (mean +10.5512%, t 2.50, n 17, days 13) with a V-arm
lo<mid<hi ordering at h20/h40 — the one-shot observation the sealed round
could have tested; nothing was nominated. Timing arm: INSUFFICIENT_COVERAGE
(forward-only, 22 timed reports: 6 bmo / 2 amc / 14 unknown). No
withdrawals (the IV and options arms were dropped at registration, never
registered configs).

### 3.4 term-gate (24 cells: T1 x12, T2 x12)

What ran: T1 VIX term-structure gates on the 22 tuning XSMOM cards
(entries 2022-11-01..2024-08-01; the 2024-09-03 FOM sits in the purge
gap, dropped and counted) — card-lane accounting, $2,500/leg, 5bp RT;
the PEAD legs are structurally NOT_EVALUABLE (zero tuning-era cards — the
calendar era starts 2024-09-05). T2: the frozen XSMOM-EXITGRID grid
re-derived with the r93 regime column, holdout era only, copy-anchored
to the published table (drift <= 3.6e-6).

Best: **T1-03 (r93 x SIGN), ON−OFF spread −0.027427** — best of the six
specs under the registered rule but still negative. Every evaluable
spread is negative (PRIMARY T1-01 −0.072802; best incumbent inc x Q60
−0.065805) and every gated book destroys value vs the ungated book
($3,991.8 vs $5,239.1 for T1-03) — the gates withheld the best months,
GATE-001's base rate repeating. T2: 12 x DESCRIPTIVE-ONLY:
NO-REGIME-SIGNAL (214-386 signal dates, no variant positive in either
regime). The mandated tuning-era ranking was stamped in
`inner-ranking.json` before any sealed-era run; PRIMARY cell T1-01
unchanged. Inner evidence is uniformly unfavorable — consistent with the
pre-committed WITHDRAW posture; PASS-GATE / INCUMBENT-CONFIRMED /
WITHDRAW belong to the sealed round.

### 3.5 exit-grid-2 (A 10, B 11, C 6 withdrawn)

Inner loop EMPTY BY DESIGN for A/B — every level pinned ex-ante, nothing
selected on data, so no inner-fold best exists; each cell's ONE scored
run is its sealed-era run (frozen-grid house precedent). What ran: 19
exit variants against the hold-20 paired references — EG2-A on 64 sealed
PEAD beats (m=9, crit t 2.539), EG2-B on the 23-rebalance / 69-leg
XSMOM card stream (m=10, crit 2.576); conservative t = min(naive,
clustered).

Verdict: **19 FAIL, 0 PASS, 0 UNDERPOWERED** (all power floors met);
scope verdicts HOLD-STANDS x2. Descriptive ranking (never a selection):
b-monthend +49.56bp (t_cons +1.43) — the only B cell above the economic
floor, never near Bonferroni; best in A is a-adv10-arm5 +34.50bp
(t_cons +0.98). Context timers are the worst offenders (breadth40 fires
on ~every signal and gives up 291-421bp; vixterm 137-287bp). The census
exit null now extends to the desk's actual hold-20 card rules — the exit
question on the survivors closes. Scope C's 6 options-expression cells
are DATA-GATED-NOT-RUN (long-dated capture in flight, ETA ~2026-09-27,
needs 466 usable sessions; post-M0 short-leg machinery absent),
withdrawn without proxy, terminal on gate failure.

### 3.6 jepa-filter (8 cells)

What ran: the registered inner geometry (origins 2024-07-01..2025-12-31,
378 sessions; quarterly anchored-expanding refits; one-session index lag;
future-poison precondition PASS at 3.6e-15) for four encoder variants at
h=5/h=21; declared direction long-low surprise.

Best: **JF-V2-H5 (PCA anchor, k_eff 11 capped by d=11)** — mean inner
daily cross-sectional Spearman IC **+0.012937** (373/378 origins, NW t
+0.95). SEL-a h=5 chose it for outer scoring (pending). Disclosures on
identical cells: XSMOM-score IC +0.019266 (the momentum-relabeling
question the sealed PASS-REDUNDANT tripwire decides); cheap vol proxies
beaten at h=5 (lnRV21 −0.008204, |r_21| −0.025137); per-vintage-segment
IC decays (worst segment 2025-07). (a) at h=21: recorded FAIL on inner
evidence (all three evaluable variants negative — wrong-sign falsifier;
no outer scoring). SEL-b fixed JF-V1-H21 regardless. Withdrawals: none —
but JF-V3-H5/H21 are FAILED-defective: the PINNED VICReg constants
diverge at GD iteration 7 on the registered inputs (rank-heavy identical
index block; hinge gradient ~−475 under lr 1e-2); registry FAILED, no
artifact, **operator ruling requested**; no constant was tuned. FLIP-h21
is indicated (all-negative) — a registration decision queued for the
operator, not taken by the executor.

## 4. What the refine rounds changed

Rounds 2 and 3 ran **DRY** — zero new configs, zero re-runs, zero
outcome-affecting changes. That is the correct terminal state, not a
stall: (i) one scored run per cell with no re-gridding is a hard rule,
and every runnable registered cell had run in round 1; (ii) the only
intra-round amendments were the two T-NULL menu amendments (v2/v3),
which re-derived null criteria from already-stamped sealed data and
introduced no new config ids (INV-13 count stays 111); (iii) the ten
unrun configs are the data-gated withdrawals (vrp-o 4, eg2-c 6), which
stay withdrawn until their gates land or expire; (iv) the agent-exec
placeholder requires a new sealed registration to activate at all.
Round-1 outcomes are therefore final for this registration.

## 5. Budget spent per scope (INV-13 registries, cap 32 each)

| scope | slot | rows used | unique configs | COMPLETED | FAILED | REGISTERED-unrun |
|---|---|---:|---:|---:|---:|---:|
| c09-tnull | tnull | 6 | 3 | 6 (3 voided g1 + 3 g2) | 0 | 0 |
| c09-vrp-e | vrp-cond | 20 | 20 | 20 | 0 | 0 |
| c09-vrp-o | vrp-cond | 4 | 4 | 0 | 0 | 4 (WITHDRAWN) |
| c09-pd2 | pead-deep-2 | 24 | 24 | 24 | 0 | 0 |
| c09-term | term-gate | 24 | 24 | 24 | 0 | 0 |
| c09-eg2-a | exit-grid-2 | 10 | 10 | 10 | 0 | 0 |
| c09-eg2-b | exit-grid-2 | 11 | 11 | 11 | 0 | 0 |
| c09-eg2-c | exit-grid-2 | 6 | 6 | 0 | 0 | 6 (DATA-GATED-NOT-RUN) |
| c09-jf | jepa-filter | 8 | 8 | 6 | 2 (V3 defect) | 0 |
| c09-agentexec | agent-exec | 0 | 1 declared | 0 | 0 | placeholder, never activated |
| **total** | | **113** | **111** | **101** | **2** | **10** |

Registries: `/home/alexk/documents/tree_options/artifacts/campaign-2026-09/<slot>.db`
(one per slot; scope_commitments 9 of 10 — agent-exec unspent).
Multiplicity was charged at each scope's full declared count (m = 9/10/5
for EG2 A/B/C; 24-cell scopes at 24) regardless of run subset. All heavy
phases ran through host-work with logs at `/tmp/<slot>-*.log` (exit 0).

## 6. Adoption posture (unchanged, binding)

Nothing in this campaign adopts anything. Inner-fold
SURVIVOR-CANDIDATEs (vrp-cond's two nominees; jepa-filter's JF-V2-H5)
nominate only. Promotion runs exclusively through the forward
sealed-card chain (>= 20 cards per rule, `promotion.py`, the XSMOM
18/20-card review for exit/rule successors, the pre-registered
kill-switches in RESEARCH-LEDGER.md DESK-CHECKS). Sealed-window
evaluation is a separate, operator-supervised step; the windows remain
untouched as of this report.

## 7. Operator decisions queued

1. **vrp-cond sealed evaluation** — run sealed criteria 1-4 for
   `xe-mkt-hi` and `xp-size-mkt` (operator-supervised). Criterion 3
   compares against the placebo path; note the xp nominee must beat the
   xp-lag21 placebo's +21.4371% (n_ON = 1) on the identical path, and
   the xe nominee is sign-flipped vs the registered hypothesis (tested
   once, as registered).
2. **term-gate sealed evaluation** — score the six T1 gate specs on the
   sealed card era under the registered criteria (B-excess leg, ON > OFF,
   utility leg, power floors, block bootstrap vs best incumbent);
   PRIMARY T1-01 stands; the stamped `inner-ranking.json` is the recorded
   pre-seal selection evidence. Family verdict PASS-GATE /
   INCUMBENT-CONFIRMED / WITHDRAW lands then; T2's full-era descriptive
   verdict accrues with it. Inner evidence says WITHDRAW is the likely
   landing.
3. **jepa-filter rulings + sealed outer scoring** — (a) rule on the V3
   divergence: confirm NOT_EVALUABLE-defective, or authorize a successor
   re-registration (new sealed sha; never mid-stream constant edits);
   (b) rule on FLIP-h21: a flipped variant may be registered consuming
   one of the four variant slots (displacing the lowest inner-ranked
   variant) or be declined; (c) then run the sealed outer window
   (2026-01-02..cutoff−h): (a)-track JF-V2-H5 with the PASS-REDUNDANT
   partial-IC tripwire, (b)-track JF-V1-H21's S_u vs the vix_term
   incumbent (paired circular block bootstrap).
4. **pead-deep-2 disposition** — with 0 inner CANDIDATEs nothing
   proceeds automatically: accept that the strata evidence stays
   descriptive (default), or direct a sealed family read anyway; either
   way the PEAD-BIGSURPRISE survivor and its card chain are untouched.
5. **Data-gate revisit (~2026-09-27)** — the long-dated option-bar
   capture ETA: if it lands >= 126 sessions AND an IVHIST-002-successor
   fidelity verdict passes, vrp-cond scope O gets its earliest sealed
   window (~2027-01); if it lands >= 466 usable sessions AND post-M0
   short-leg machinery exists, exit-grid-2 scope C can register anew.
   Otherwise both withdrawals are terminal — record the closure.
6. **agent-exec placeholder** — stays inactive; activation needs an
   explicit operator GO, a new sealed registration, and separate
   approval for the ~192-request intraday capture (the memo's 10-episode
   measurement-floor pilot first). No action queued by this round.
7. **T-NULL** — no decision pending: CALIBRATED; B(W, shape) and the six
   priors stand for every sealed read above.

## 8. Artifact index

- Registries + per-config artifacts:
  `/home/alexk/documents/tree_options/artifacts/campaign-2026-09/`
  (`tnull/`, `vrp-cond/`, `pead-deep-2/`, `term-gate/`, `exit-grid-2/`,
  `jepa-filter/` — each with `trials/`, stamps, withdrawals, selections).
- Round docs (this branch): `docs/campaign-2026-09/{tnull, vrp-cond,
  pead-deep-2, term-gate, exit-grid-2, jepa-filter}-round1.md`.
- Ledger: `artifacts/paper-trades/RESEARCH-LEDGER.md` section
  `campaign-2026-09 (inner-fold)` (main checkout; untracked research
  state by design — not committed).
