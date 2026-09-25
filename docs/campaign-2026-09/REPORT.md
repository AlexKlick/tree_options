# campaign-2026-09 — CONSOLIDATION REPORT (round 1: inner folds; round 2: sealed outer windows)

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
evaluation is a separate, operator-supervised step; the windows
remained untouched through the round-1 report and were then opened
once each, operator-authorized, in round 2 — section 7.

## 7. Sealed outer window (round 2) — operator-authorized 2026-09-24

Consolidator, 2026-09-24 (round 2). Operator rulings of 2026-09-24, all
four recorded verbatim in every sealed artifact: (1) sealed reads
AUTHORIZED (operative) — each family's outer window opened ONCE;
(2) jepa FLIP-h21 registration DECLINED — scored exactly as registered,
no flipped variant; (3) jepa V3 divergence stands
NOT_EVALUABLE-defective — no successor registration; (4) pead-deep-2
stays descriptive — no sealed family read. Windows consumed: vrp-cond
ordinals 443..505 = 2026-06-02..2026-08-31 (63 sessions, opened once);
term-gate sealed card era 2024-10-01..2026-08-28 (23 XSMOM cards
2024-10-01..2026-08-03, 141 PEAD complete-hold cards 2024-10-16..2026-08-06,
0 missing index sessions, 4 forward-chain PEAD events after 2026-08-06
unscored); jepa-filter outer origins 2026-01-02..2026-09-23 (177
complete h=5 / 161 h=21).

**Nothing adopts on sealed evidence — any PASS is at most nomination;
promotion remains exclusively the forward sealed-card chain** (>= 20
cards per rule, `promotion.py`, the XSMOM 18/20-card review, the
pre-registered kill-switches in RESEARCH-LEDGER.md DESK-CHECKS). No
sealed cell in this round reached SURVIVOR-CANDIDATE.

Round-2 executor commits (worktree branch `exec/campaign-2026-09`,
nothing pushed): vrp-cond `b629847` (frozen-root repoint) + `7626595`
(stamped, guard fix + crash-resume); term-gate `58e3c5e`
(registration/execute HEAD as stamped) with interim `0dde2f8`
soft-reset/superseded and corrected bytes committed at round-final
`c2e308e`; jepa-filter `16c60a9`. Full logs:
`/home/alexk/.local/state/campaign-2026-09/{vrp-cond,term-gate,jepa-filter}-sealed.log`.

| family | verdict | one-line reading |
|---|---|---|
| vrp-cond | **both nominees NOT_EVALUABLE-SEALED** (power floors; never FAIL) | gate fired 0/6 (xe) and 7 beats < floor 8 (xp); question stays open, forward cards decide |
| term-gate | **WITHDRAW** (pre-committed posture) | 0 of 12 T1 cells cleared all four criteria; no CANDIDATE on either rule; T2 x12 NO-REGIME-SIGNAL |
| jepa-filter | **FAIL / FAIL / FAIL** ((a) h5, (a) h21 carried, (b)) | wrong-sign IC, below its own null spread; vix_term incumbent dominant; family closes |

### 7.1 vrp-cond — both nominees NOT_EVALUABLE-SEALED (power floors)

Five cells registered BEFORE any sealed outcome (INV-13; scope c09-vrp-e
rows 20 -> 25 of cap 32): the two nominees + the three round-1 placebos
(`xe-lag21`, `xp-lag21`, `xp-shuffle`) re-run on the identical
sha-pinned round-1 path. One scored run per cell; DB final 25 COMPLETED
+ 4 REGISTERED (scope O withdrawn).

- **xe-mkt-hi — NOT_EVALUABLE-SEALED.** Sealed ordinals 443..505 =
  2026-06-02..2026-08-31 (63 sessions); xe base = 2 rebalances
  (2026-07-01, 2026-08-03) x 3 = 6 entries. n_on_fired = 0 < floor 6 —
  the p_IWM >= 2/3 gate never fired (6 gate-off, 0 abstained);
  on_minus_off_5bp = None, bootstrap p = None. Tuning delta +0.149551 =
  selection evidence only.
- **xp-size-mkt — NOT_EVALUABLE-SEALED.** 7 sealed PEAD beats, all
  fired (size config always fires); n_on_fired = 7 < floor 8; zero
  condition-high (half-sized) trades, so on_minus_off_5bp = None and
  cond_minus_base_5bp = 0.0 (degenerate: strategy == base). Disclosed
  cross-reading: the hypothesis-falsification trigger technically fired
  at the degenerate 0.0, but the registered floor mapping governs the
  verdict. Tuning delta +0.003169 = selection evidence only.

This is the exact power outcome the registration itself predicted
("NOT_EVALUABLE-SEALED likely", ~8-15 expected PEAD beats; 7
materialized). Per the slot doc the question stays open and the forward
cards decide; the promoted configs continue as report-only monitored
gates (SEMI-gate precedent).

Disclosed defects/repairs (all recorded in the artifact):

- **Registration-internal inconsistency (xe sealed geometry)**: slot-doc
  prose + menu risk note say "3 sealed XSMOM rebalances (2026-06-02,
  2026-07-01, 2026-08-03; 9 entries)", but the byte-pinned calendar puts
  June's first session at 2026-06-01 = ordinal 442 — the registration's
  own "pre-seal shoulder, unused" row (the fold table dates 442 =
  2026-06-01; 443 = 2026-06-02 is not a first-of-month session). Under
  the byte-pinned rule the sealed xe region is TWO rebalances (6
  entries). The xe floor was NOT bent (n_ON >= 6, now met only if the
  gate fires on both rebalances — strictly harder, NOT_EVALUABLE-SEALED
  direction). Guard corrected pre-outcome in `7626595`.
- **Frozen-root assembly gaps (repaired, disclosed)**: (a)
  `docs/desk/IVHIST-001-verdict.json` was missing — copied byte-exact
  (sha256 `509aec01…`) into the frozen root (load_and_bind reads it for
  IV fidelity labels; no other input added/changed); (b) the frozen
  root's 37-name desk-universe.toml cannot load under universe.py
  (options_close lists PLTR/SPCX) — DESK_UNIVERSE was bound to the
  worktree desk-universe.toml (the exact round-1 bytes, execution
  commit `0433522`, 39 panel names) after proving it equals the frozen
  37-name pin + exactly {PLTR, SPCX}; the runner's derived universe
  still comes from the pinned 37-name panel (load_and_bind enforces
  37/35/36).
- **Crash-resume (INV-13)**: the first execute attempt refused
  PRE-SCORING on the mis-transcribed 3-rebalance guard — no sealed
  outcome computed or viewed, no artifact written, so the seal was NOT
  consumed; xe-mkt-hi-r2 sat RUNNING with no outcome (registry
  single-outcome PK keeps the scored run one-shot). Guard corrected,
  outcome-less RUNNING resume added, all 5 cells then COMPLETED once
  each. Disclosed with both HEADs (execute `b629847`, stamp `7626595`).
- The frozen root's data/calendar is a symlink into the main checkout's
  data dir as assembled by the operator; the calendar pin `7f9cccba…`
  verifies byte-exact through it and load_and_bind re-checks at bind.
  Main checkout otherwise never read for campaign inputs, never written.

### 7.2 term-gate — family WITHDRAW (pre-committed posture)

24 trial rows (c09-term-*-g2) registered BEFORE any sealed outcome. The
registry's 32-cap binds per scope_key and round 1 had already committed
24 rows, so the sealed rows register under the sealed fold's own
scope_key (outer fold campaign-2026-09/c09-term/outer-round2-sealed, its
own 32-cap, 24 rows) — same 24 registered menu cells scored, no
configuration added or re-gridded.

- **Family verdict: WITHDRAW** — the registration's pre-committed
  GATE-001 base-rate posture. 0 of 12 T1 cells cleared all four
  criteria; no CANDIDATE on either rule; the family bootstrap was never
  reached (no new-quantity CANDIDATE). Nothing adopts; no
  RESEARCH-LEDGER entry was made by the executor. A PASS-GATE would
  only have earned a context annotation + queued successor
  pre-registration.
- **T1 (12 cells): 8 NOT_EVALUABLE + 4 NOT_CANDIDATE.** PRIMARY T1-01
  NOT_EVALUABLE (OFF-n 6 < 8 floor; spread -0.121640; gated $1,690 vs
  ungated $7,760). Best incumbent T1-05 NOT_EVALUABLE (OFF-n 7 < 8).
  T1-12 is the near-miss: criteria 1a/2/3a/3b ALL held (B-excess
  +0.006637, spread +0.018985, gated $5,793 >= ungated $5,764) but t_on
  1.5995 < 2.64 and OFF-n 13 < 20 floor -> NOT_EVALUABLE by
  registration. NOT_CANDIDATEs: T1-02, T1-06, T1-09, T1-10 (full power,
  fails on substance — every evaluable spread negative or
  insignificant).
- **T2 (12 cells): 12 x DESCRIPTIVE-ONLY:NO-REGIME-SIGNAL** — the
  registered prior ("pooled t -12..-26, NO-REGIME-SIGNAL expected")
  confirmed on the full-era read (t_cons -4.118..-13.358 across the
  grid; d_mean -0.031..-0.272).

Disclosures (all resolved + recorded in the artifact):

- **PEAD card-rule reconciliation**: desk signals.pead_beats (one-sided
  >= +1.5%) yields only 63 sealed cards; the registration's 141 was
  reproduced exactly only under the published PROTOCOL-PEAD book rule
  |move| >= 1.5% (144 era events 2024-09-05..2026-08-06 minus the three
  2024-09 cards = 141 complete-hold cards). Bound to the published-book
  reading.
- **T2 full-era anchor mismatch**: the published XSMOM-EXITGRID
  full-era table (5364/447, 1341/447) predates the menu-pinned panel
  (5460/455 full-era signals); the copy-faithfulness anchor was bound
  on the count-matched publication subset (recovered uniquely; last
  signal 2026-06-16; max per-variant d_mean drift 1.4e-06 vs tol 6e-4)
  while the scored read uses the pinned panel's full era. First execute
  attempt REFUSED pre-T2-outcome on the over-strict equality guard.
- **Crash-resume provenance**: all 12 T1 outcomes scored in attempt 1
  (HEAD `58e3c5e`, pre-fix runner `55c5c831`); T2-01 in attempt 2
  (`0dde2f8`, runner `de495181`); T2-02..12 + the round stamp under
  `58e3c5e` with the corrected runner bytes in the working tree
  (vrp-cond pattern) because mark_running demands register and execute
  share one commit identity. No T2 seal was consumed by the refused
  attempts (no outcome, no artifact); nothing re-run or overwritten.
  Corrected bytes committed at `c2e308e`.
- **Criterion-1 clustered-t ambiguity**: menu v3 re-centered only the
  mean leg on B; decisive t = clustered-t(ON) per the slot doc's
  pre-amendment clause; a cross-reading with t = (ON - B)/sqrt(se_ON^2 +
  se_B^2) is stamped per cell and changes NO verdict.
- **Round-1 runner sha footnote**: the frozen g1 artifacts' execute
  stamps carry uncommitted bytes `82b05dd4…`; the committed,
  rank-phase-verified bytes are `84e38dac…` (identical-path pin). No
  machinery difference observable.

### 7.3 jepa-filter — all three questions FAIL; the family closes

Two sealed trial rows (c09-jf-JF-V2-H5-r2 / c09-jf-JF-V1-H21-r2;
scope c09-jf rows 8 -> 10 of cap 32, round-1 scope_key) registered
BEFORE any outcome; both COMPLETED one-shot, no crash, seal fully
consumed by the stamped artifact. Runner committed (`16c60a9`) before
any phase; round-1 runner bytes pinned `5c6c2ead…` and imported with
MAIN_ROOT rebound to the frozen root (vrp-cond pattern); all 10
dataset_pinning shas re-verified; 2025-10 refit train_rows 32887
identical to the round-1 stamp (cross-round consistency). Future-poison
PASS on both cells (max abs diff <= 8.9e-16 <= 1e-12); evaluability
guards pass (complete origins >= 126, 0 skipped, no latent collapse);
independent recomputation reproduced mean IC, rho_S, rho_vix, d exactly.

- **(a) h=5 JF-V2-H5 (SEL-a): FAIL.** Mean IC **-0.026626** on sealed
  outer origins 2026-01-02..2026-09-16 — negative, wrong sign
  (declared long-low surprise). One-sided circular block bootstrap
  (block=5, B=2000, PCG64(22)) p = 0.8455; band p5/p50/p95 =
  [-0.065197, -0.025573, +0.015956]. Baselines on identical cells:
  XSMOM +0.051291 (p 0.137), lnRV21 -0.022912, |r21| -0.082085; the
  learned surprise also sits BELOW its own 3-seed hash-randomized null
  spread (means +0.008641/+0.003501/+0.000245, p 0.25..0.47). Partial
  IC vs XSMOM+lnRV21: -0.037891 (t -1.688) — moot, not PASS.
- **(a) h=21: FAIL, carried from round 1 verbatim** (pre-declared
  SEL-a rule: no evaluable variant positive at h=21 — wrong-sign
  falsifier; the outer window was NOT opened for this question).
  Operator rulings confirm: V3 stands NOT_EVALUABLE-defective
  (excluded), FLIP-h21 DECLINED — assessment stays recorded as
  not-taken.
- **(b) JF-V1-H21 S_u vs vix_term incumbent: FAIL.** On forward
  RV21(SPY), 161 identical origins 2026-01-02..2026-08-24:
  rho_S = +0.070629 vs rho_vix_term = **+0.360594**; paired d =
  **-0.289964** (negative), one-sided paired bootstrap (block=21,
  B=2000) p = 0.8115; d band [-0.459117, -0.288341, -0.113437];
  vix_term side p_vs_zero = 0.0005, S_u side 0.2465. MDD21 twin: the
  challenger does not beat the incumbent on the twin either
  (rho 0.058495 vs 0.067876, paired p 0.4315).

Per declared_use: **vix_term remains the sole vol-regime context input
and the JEPA-SURPRISE family closes**; a successor needs its own sealed
pre-registration. Nothing adopts (a PASS/PASS-REDUNDANT on (a) would
have authorized nothing beyond nomination).

### 7.4 Superseded block record (9a81a2b)

The `9a81a2b` BLOCK record ("ohlc-panel drift vs menu pin — pinned
state unrecoverable") stands **SUPERSEDED by the pin-verified snapshot
recovery**: the exact pinned bytes were recovered from the Wave-1 econ
lane snapshot
`~/.local/state/trex-desk-w1-econ/paper-snapshot-0861f525/ohlc-panel.json`
(full 64-hex sha256 == menu pin `0861f525…`) into the frozen input root
`/home/alexk/.local/state/campaign-sealed-inputs/root`; the block
dissolved and every sealed bind re-verifies it (PANEL BLOCK HISTORY
recorded in each artifact). The main checkout's live 39-name panel
(`a3d3a485`) was never read for campaign inputs and never written. The
frozen-root inputs are otherwise operator-assembled byte pins: menu
`4aca2101` (sidecar-verified), calendar `7f9cccba`, calibration-v3
`5d0aa0ee` CALIBRATED, protocol raw `2fde83db` / canonical `22c78231`,
xsmom_exitgrid.py copied byte-exact (`6ef943f7` = round-1 stamp).

## 8. Operator decisions queued

1. **vrp-cond sealed evaluation — RESOLVED 2026-09-24 (authorized +
   executed)**: window 443..505 opened once; both nominees
   NOT_EVALUABLE-SEALED on power floors (never FAIL, per the registered
   mapping). The registered hypothesis direction stays untested on
   sealed data; per the slot doc the question is open and the forward
   cards decide. The promoted configs continue as report-only monitored
   gates (SEMI-gate precedent) — see item 8.
2. **term-gate sealed evaluation — RESOLVED 2026-09-24 (consumed)**:
   family WITHDRAW, the pre-committed posture. 8 NOT_EVALUABLE + 4
   NOT_CANDIDATE (T1), 12 x NO-REGIME-SIGNAL (T2, prior confirmed).
   Nothing adopts; the pre-committed vix_term-parity posture stands.
3. **jepa-filter rulings + sealed outer scoring — RESOLVED
   2026-09-24**: (a) V3 divergence ruled NOT_EVALUABLE-defective, no
   successor registration; (b) FLIP-h21 registration DECLINED, scored
   exactly as registered; (c) sealed outer scored: (a) h5 FAIL
   (wrong-sign, below its own null spread), (b) FAIL (vix_term
   incumbent dominant, paired d -0.289964). The JEPA-SURPRISE family
   closes; a successor needs its own sealed pre-registration.
4. **pead-deep-2 disposition — RESOLVED 2026-09-24**: descriptive
   default accepted (operator ruling 4) — no sealed family read; the
   strata evidence stays descriptive; PEAD-BIGSURPRISE survivor and its
   card chain untouched.
5. **Data-gate revisit (~2026-09-27)** — UNCHANGED, still queued: the
   long-dated option-bar capture ETA. If it lands >= 126 sessions AND
   an IVHIST-002-successor fidelity verdict passes, vrp-cond scope O
   gets its earliest sealed window (~2027-01); if it lands >= 466
   usable sessions AND post-M0 short-leg machinery exists, exit-grid-2
   scope C can register anew. Otherwise both withdrawals are terminal —
   record the closure. (Carry-forward: exit-grid-2 A/B HOLD-STANDS
   verdicts remain terminal; no sealed action was or is needed there.)
6. **agent-exec placeholder** — stays inactive; activation needs an
   explicit operator GO, a new sealed registration, and separate
   approval for the ~192-request intraday capture (the memo's 10-episode
   measurement-floor pilot first). No action queued by this round.
7. **T-NULL** — no decision pending: CALIBRATED; B(W, shape) and the six
   priors stood for every sealed read above (and were read only where a
   cell reached evaluability).
8. **NEW — vrp-cond monitored-gate disposition**: the two nominees'
   configs continue as report-only monitored gates (SEMI-gate
   precedent). With both cells NOT_EVALUABLE-SEALED, decide whether to
   keep the monitors running until the forward 20-card chain resolves,
   or retire them. Nothing adopts either way.
9. **NEW — vrp-cond slot-doc annotation**: the xe sealed-geometry
   internal inconsistency (prose "3 rebalances / 9 entries" vs the
   byte-pinned calendar's 2 rebalances / 6 entries — section 7.1) is
   disclosed in the artifact and governed by the byte-pinned rule;
   queue an annotation to the slot doc so the next registration
   transcribes the calendar directly.
10. **NEW — sealed-round ledger filing**: the round-1 consolidator
   filed a `campaign-2026-09 (inner-fold)` section in
   `artifacts/paper-trades/RESEARCH-LEDGER.md` (main checkout,
   untracked research state by design). The round-2 consolidator did
   NOT append sealed verdict lines (precedent commit `2ba9f25` touched
   only this REPORT; the ledger is not tracked). If the operator wants
   a sealed-round ledger section, it is a one-edit filing in the main
   checkout — decide and it lands.
11. **NEW — frozen-input root retention**: the frozen root
   `/home/alexk/.local/state/campaign-sealed-inputs/root` is now the
   byte-pin record for three consumed seals (menu/panel/calendar/IV
   pins + repaired assembly gaps, section 7.1). Decide its retention
   (keep as the audit frozen-root vs archive after the operator
   review); /tmp is wiped on boot, so any move must stay outside /tmp.

## 9. Artifact index

- Registries + per-config artifacts:
  `/home/alexk/documents/tree_options/artifacts/campaign-2026-09/`
  (`tnull/`, `vrp-cond/`, `pead-deep-2/`, `term-gate/`, `exit-grid-2/`,
  `jepa-filter/` — each with `trials/`, stamps, withdrawals, selections).
- Round 2 (sealed) — copied back to the live repo by the consolidator:
  `artifacts/campaign-2026-09/{vrp-cond, term-gate,
  jepa-filter}/sealed-round.json` + the updated `<slot>.db` registries
  (vrp-cond 25 COMPLETED + 4 REGISTERED; term-gate 48 COMPLETED; jepa
  8 COMPLETED + 2 FAILED — round-2 rows verified present after copy).
  Byte-source: `/home/alexk/.local/state/campaign-sealed-inputs/root/
  artifacts/campaign-2026-09/` (plain file copy; nothing else writes
  those DBs). Per-trial round-2 artifacts: `trials/c09-vrp-e-{xe-mkt-hi,
  xp-size-mkt, xe-lag21, xp-lag21, xp-shuffle}-r2.json`,
  `trials/c09-term-{T1,T2}-*-{01..12}-g2.json`,
  `trials/c09-jf-{JF-V2-H5, JF-V1-H21}-r2.json` in the same slots.
- Round docs (this branch): `docs/campaign-2026-09/{tnull, vrp-cond,
  pead-deep-2, term-gate, exit-grid-2, jepa-filter}-round1.md`.
- Ledger: `artifacts/paper-trades/RESEARCH-LEDGER.md` section
  `campaign-2026-09 (inner-fold)` (main checkout; untracked research
  state by design — not committed). No sealed-round ledger section was
  filed by the round-2 consolidation (see decision 10).
