# REGISTRATION-NOTES — campaign-2026-09 assembly and validation

Written 2026-09-23 by the registrar, at repo head `f075997`
(`fix(capture): keep master pages lazy and stream bar series to disk`),
protocol `research_protocol.yaml` v0.2.2 (raw-bytes sha256
`2fde83db80f7d8d601dd7d3b9d2f153beb47eed23caf0a010ef464a5248cc58e`; the
canonical identity is over the validated model and is re-stamped by the
runner, INV-14). Menu: `docs/theory/campaign-2026-09-registration.json`.
This file and that menu are the only files authored by the registrar;
two one-line protocol-number fixes were applied to slot docs (section 3).
No gate, no trial, no backtest, no commit was run. Every verification below
was read-only calendar/JSON/CSV arithmetic against the sealed calendar
(`data/calendar/nyse_sessions_2018_01_02_2026_12_31.json`, sha256
`7f9cccba…` — matches the ledger's pin), with **2025-01-09 excluded as the
phantom non-session** per the 2026-09-23 ledger ruling.

Menu order (fixed): `tnull` (T-NULL calibration FIRST), `vrp-cond`,
`pead-deep-2`, `term-gate`, `exit-grid-2`, `jepa-filter`, then the
`agent-exec` build-gated placeholder.

## 1. Validation results

### (a) Config counts fit `inner_loop.max_registered_configs: 32` per scope — PASS

Each slot owns its own scope id(s); no scope is shared, so no slot's grid
counts against another's budget:

| scope id | slot | configs | cap | slack |
|---|---|---:|---:|---:|
| c09-tnull | tnull | 3 | 32 | 29 |
| c09-vrp-e | vrp-cond scope E (equity timing) | 20 | 32 | 12 |
| c09-vrp-o | vrp-cond scope O (options expression) | 4 | 32 | 28 |
| c09-pd2 | pead-deep-2 | 24 | 32 | 8 |
| c09-term | term-gate (T1 12 + T2 12) | 24 | 32 | 8 |
| c09-eg2-a | exit-grid-2 A | 10 | 32 | 22 |
| c09-eg2-b | exit-grid-2 B | 11 | 32 | 21 |
| c09-eg2-c | exit-grid-2 C | 6 | 32 | 26 |
| c09-jf | jepa-filter | 8 | 32 | 24 |
| c09-agentexec | agent-exec placeholder | 1 | 32 | 31 |

Arithmetic: 3+20+4+24+24+10+11+6+8+1 = **111 registered configs**; max scope
load 24/32. Enumerated-id check (script over the menu JSON): 111 ids listed,
111 unique, per-slot counts equal the declared counts (exit-grid-2 = 27 cells
= 24 alternatives + 3 hold-20/ts20 bases, split across its three scopes).
Unspent slots expire with this registration (term-gate and pead-deep-2
declare this explicitly; the same rule is applied campaign-wide).

### (b) Banned-registry screen — PASS (no config expresses a BANNED family or points a new direction)

Screened against `src/tree_options/desk/signals.py`:
`ALLOWED_DIRECTION = {xsmom_top3, pead_beat}`,
`CONTEXT_ONLY = {trend, breadth, vix_term}`, `BANNED` = 22 refuted families
(volspike, mom_top_tercile, mr_h1, r3f, r3f_up, r1, cont, ts_momentum,
mom60_h60, squeeze, gap_fade, gap_cont, exec_open_gap, breadth_gate,
sector_rotation, xsmom_60skip5, sweep_divergence_swing, short_any,
xsmom_short_leg, pead_miss, short_vol_gated, semi_reversion).

- **vrp-cond**: gates entry timing/size/vehicle of the two ALLOWED
  directions; never sells vol, never shorts — distinct from BANNED
  `short_vol_gated`; distinct from CONTEXT_ONLY `vix_term` (per-name
  IV30-vs-HAR spread, not the index term structure). Direction signals are
  byte-identical to `signals.py` (no-skip 273 XSMOM, +1.5% PEAD beat).
- **pead-deep-2**: direction stays `pead_beat` in every cell; conditionings
  suppress/delay/size-zero cards only; within-beat (never `pead_miss`).
- **term-gate**: CONTEXT_ONLY `vix_term`-family gate that may only WITHHOLD
  entries; no deferral/re-ranking/inversion/shorts. T2 is
  DESCRIPTIVE-ONLY on already-viewed census cells with the XU post-hoc
  precedent forbidding any live change.
- **exit-grid-2**: times exits of ALLOWED directions only; CONTEXT_ONLY
  variables appear strictly as exit conditions; scope C is long-leg-led
  debit spreads (no naked shorts; short legs themselves data-gated on
  post-M0 machinery).
- **jepa-filter**: CONTEXT_ONLY door only, competing with `vix_term`.
  Companion (a)'s long-low/short-high is pinned as a **scoring convention
  for a one-sided rank test, not a trade** — no orders, no fills,
  `option_candidate_defaults` inert; even a PASS authorizes nothing
  (shorts are DEAD in the census and prohibited by protocol); promotion of
  (a) to a direction input requires a separate later sealed study. The
  FLIP rule forbids post-hoc sign reversal. This is the one entry where
  the screen is satisfied by declaration rather than by mechanism —
  recorded here as a CONDITIONAL PASS with that exact boundary.
- **agent-exec**: harness makes banned directions mechanically
  inexpressible (long-only declared direction on live-signal episodes;
  BANNED registry verbatim in every reflection prompt; post-hoc
  action-trace audit); plain-day actions are PAPER-SANDBOX-ONLY.
  Placeholder — not activatable without a new sealed registration.

No config id, gate, or exit condition in the menu references any BANNED
family name or constructs one (the term-gate incumbent comparison exists
precisely to catch a dressed-up vol level; the jepa PASS-REDUNDANT
tripwire exists to catch momentum/vol in latent disguise).

### (c) Fold mapping consistent with `research_protocol.yaml` — PASS after 2 fixes (deviations declared, not silent)

Verified by calendar arithmetic (read-only; all counts below recomputed
from the sealed calendar with 2025-01-09 excluded):

- Global: anchored_expanding; min_train 252; validation 126; test 63;
  roll 63; embargo 5; purge_gap_rule strict inequality — all honored as
  declared per slot below.
- **vrp-cond**: warm-up 1..252 = 2024-08-26..2025-08-27 ✓; V1 253..378 =
  2025-08-28..2026-02-27 (126) ✓; sealed 443..505 = 2026-06-02..2026-08-31
  (63) ✓; coda 506..508 = ..2026-09-03 ✓; 508 total IV sessions ✓; tuning
  restriction ordinal ≤ 417 (2026-04-24): 417+20+5 = 442 < 443 ✓ (purge at
  the desk hold length, stricter than the protocol's 5). **V2 was published
  as 317..442 — a 64-session roll; fixed to 316..441 (2025-11-26..2026-05-29)
  so the roll is the protocol's 63; ordinal 442 (2026-06-01) declared the
  pre-seal shoulder** (fix record in section 3).
- **pead-deep-2**: era 2024-09-05..2026-09-23 = 514 sessions ✓; outer walk
  train 252 = 2024-09-05..2025-09-08 ✓, val 126 = ..2026-03-10 ✓, tests
  63 + 63 (2026-03-11..06-09, 2026-06-10..09-09) ✓, tail 10 sessions
  2026-09-10..09-23 ✓; sealed = final 63 = 2026-06-25..2026-09-23 ✓; inner
  purge boundary 2026-04-20: ordinal 406, 406+40+5 = 451 < 452 =
  ordinal(2026-06-25) ✓. Declared geometry: per-cell label horizon = hold
  (10/20/40) with purge = hold+5 (stricter than the yaml's 5) — a
  registration-carried geometry in the wave-0 `geometry_grid_fridays`
  idiom, not a protocol edit.
- **term-gate**: trailing window **[t−252, t−1] = 252 sessions = min_train
  (fixed from [t−253, t−1], which spans 253)**; purge verified: last tuning
  signal 2024-08-01, ordinal+20+5 = 2024-09-06, so the first eligible eval
  session is 2024-09-09 under the strict rule — the file's stated 2024-09-09
  is correct; sealed boundary 2024-10-01 clears it. Declared deviation: one
  sealed block instead of rolling 63-session test folds (GATE-001 /
  XSMOM-EXITGRID era idiom; disclosed in the slot doc). Index coverage
  re-verified read-only: 1262/1262 SPY∩VIX sessions on the panel window,
  r93 > 1 on 149 (11.8%), median 0.830, q75 0.932, max 1.697; inc ≥ 0 on 64
  (5.1%), median −0.114; bs ≥ 0 on 7.0%; all index files end 2026-09-22.
- **exit-grid-2**: reserved EG2-A geometry verified — train 252
  (2024-09-05..2025-09-08), val 126 (2025-09-09..2026-03-10, 53 report
  dates ✓ recomputed from earnings-calendar.json), purge val-end+25 =
  2026-04-15 < sealed test 2026-05-21..2026-08-20 (63) ✓, hold-complete
  2026-08-20+20 = 2026-09-18 ✓. EG2-B val block 2024-03-05..2024-09-03 =
  126 ✓; purge reserve 2024-09-03+25 = 2024-10-08 ✓ (matches the file).
  **Flagged, no edit needed**: EG2-B's sealed era begins 2024-10-01, inside
  that purge reserve — legitimate ONLY because the inner loop is EMPTY BY
  DESIGN (frozen grid; nothing trained, so INV-06 has no train/eval pair to
  bind) and the slot's honesty statement (no virgin window; the seal binds
  the variant deltas) governs. The reserve binds a successor that
  introduces tunable parameters; the menu JSON records this reading.
- **jepa-filter**: initial train 2021-09-13..2024-06-30 = 703 ✓ (≥ 252);
  inner 2024-07-01..2025-12-31 = 378 ✓; outer span 2026-01-02..2026-09-23 =
  182 ✓ (complete origins 177 at h=5, 161 at h=21 — arithmetic consistent);
  roll = quarterly = 63 ✓; embargo 5 inside the declared purge h+5 ✓.
  Declared geometry: label horizon 21 at h=21; the 182-session sealed span
  replaces repeated 63-session test folds (wave-0 precedent).
- **tnull** (registrar-authored): fit-free by construction (INV-07
  vacuous); evaluated on the union of the families' sealed sub-eras so the
  band is read on the same sessions.
- **agent-exec**: inner 2024-09-05..2025-12-31; sealed outer from the first
  session ≥ 2026-01-09 (inner end + the protocol's 5-session embargo,
  applied literally). Placeholder.

### (d) INV-13 shape — PASS

Every one of the 111 configs carries an explicit id in the menu JSON
(enumerated per slot) and a registration-before-outcome path: the menu +
slot docs are sealed by sha256 sidecars at commit (FORECAST-001.md.sha256 /
XSMOM-12-1-PREREG.md precedent; exit-grid-2 and jepa-filter declare this
explicitly), every id is written to the trials sqlite
(`trials.register_before_outcome: true`, `duplicate_trial_id: reject`,
storage sqlite) before its outcome is viewed, one scored run per cell, and
no re-gridding after results (successor pre-registration is the only
reopen). The tnull configs carry wave-0-style `params_key` + `score_seed`
fields. The agent-exec placeholder has exactly one optimizer-config id and
its own gate: a trial row BEFORE any episode is evaluated.

### (e) Data-gated arms marked with their gates — PASS

| arm | gate |
|---|---|
| vrp-cond scope O (ox-*/op-*) | long-dated option-bar capture IN FLIGHT, ETA ~2026-09-27; requires ≥ 126 sessions of history AND an IVHIST-002-successor fidelity verdict; else WITHDRAWN without running (earliest sealed window ~2027-01) |
| exit-grid-2 scope C (c-*) | same capture + coverage/integrity check on the evaluation span (feasibility floor 466 usable sessions) AND the post-M0 short-leg machinery; else DATA-GATED-NOT-RUN forever |
| agent-exec (all) | intraday episode capture ~192 wire requests PENDING SEPARATE OPERATOR APPROVAL + four named builds (registration seam, capture, harness, SIM-FILL-INTRADAY-v1); macro-episode variant additionally gated on historical macro dates; activation needs an explicit operator GO and a NEW sealed registration |
| IV fidelity (campaign-wide) | only IWM 'ok'; 28 low-fidelity; 6 not-evaluable — absolute IV thresholds IWM-only, within-name constructs only, until an IVHIST-002 successor passes (vrp-cond binding; pead-deep-2 and jepa-filter exclude name-IV entirely) |
| macro window | 2026-01-01..2027-12-31 ONLY — no historical macro dates; no macro feature (jepa) and no macro episode (agent-exec MVE) is registered anywhere in this menu |

No other arm is data-gated: term-gate and exit-grid-2 A/B consume only the
equity panel and CBOE index CSVs; pead-deep-2's timing arm is forward-only
(zero historical coverage — a declared stratum, not a gate).

## 2. Census-novelty and dedup screen (registrar check)

Each slot's novelty section was checked against
`artifacts/paper-trades/RESEARCH-LEDGER.md` (the census, ~1,220 cells) and
the family reports it cites; no slot re-runs an existing census cell:
vrp-cond is the first IV/vol-spread conditioning of the survivors (both
enabling studies sealed 2026-09-23); pead-deep-2 explicitly DROPS the
pooled-sign big/small, SPY-mom20-regime, pooled-sign quintile, and
XSMOM-half cells PEAD-DEEP/PEAD-Q already ran; term-gate's T2 re-derives
the frozen XSMOM-EXITGRID grid with a regime column (descriptive-only, no
parameter changes) rather than re-running it; exit-grid-2's 24 alternatives
are absent from EXIT-GRID/XSMOM-EXITGRID and it does not re-litigate
hold 20 vs 60; jepa-filter is the first learned-state family. The one
cross-slot overlap risk (an IV-percentile gate registered twice) is empty:
vrp-cond is the only IV-conditioning slot — pead-deep-2 and jepa-filter
both EXCLUDE name-IV by the IVHIST-001 gate, and term-gate declares no IV
rule.

## 3. Fixes applied to slot docs at assembly (protocol-number contradictions only)

1. `docs/theory/campaign-2026-09/slots/vrp-cond.md` — inner V2 row:
   `317..442 (2025-11-28..2026-06-01)` → `316..441
   (2025-11-26..2026-05-29)`, plus a declared pre-seal-shoulder row for
   ordinal 442 (2026-06-01, unused). Reason: the published V2 start was a
   64-session roll off V1 (253 → 317), contradicting
   `folds.roll_forward_sessions: 63`; 253+63 = 316 = 2025-11-26
   (2025-11-27 is Thanksgiving, not a session). Everything downstream
   (sealed 443..505, restriction ordinal ≤ 417 = 2026-04-24, coda) was
   already consistent and is unchanged. File sha256 after fix:
   `8caa7aa0dbc74840156881ed497c82ec990b05e40c84aeefffad08f54c5c6795`
   (pre-fix `5e187247…`; the menu pins the post-fix bytes).
2. `docs/theory/campaign-2026-09/slots/term-gate.md` — trailing window:
   `[t−253, t−1] inclusive (252 ≥ protocol min_train 252)` →
   `[t−252, t−1] inclusive (252 sessions = protocol min_train 252)`.
   Reason: the published bracket spans 253 sessions while claiming 252;
   the fix also matches the file's own trailing-252 quantile/Q60/median
   definitions everywhere else. File sha256 after fix:
   `2b7e0b94c9031f6fcab27f33234a2ba3aec5b8740586982d30d4f49d85372d9d`
   (pre-fix `9d6aec54…`).

No other edit was made to any file the slot agents authored. Two
rounding-level data statistics in term-gate.md (r93 median printed 0.831 vs
recomputed 0.830; inc median printed −0.113 vs −0.114) are display
rounding, not protocol numbers — left as authored.

## 4. Open questions surfaced by the slot authors (deduped)

1. **Wave-2 playbook thresholds** (vrp-cond): FORECAST-001 deferred the
   cheap/fair/rich `r_IWM` thresholds to a Wave 2 decision; the
   `xe-playbook` / `xp-playbook` raw-threshold cells are alignment-only
   and bias-flagged pending that ruling.
2. **IV fidelity succession** (vrp-cond, pead-deep-2, jepa-filter, and
   scope O of everything): an IVHIST-002 successor must pass before any
   absolute name-IV threshold or options-expression arm un-gates; the
   long-dated capture (ETA ~2026-09-27) must land ≥ 126 sessions
   (~2027-01 earliest) for vrp-cond scope O, and 466 usable sessions for
   exit-grid-2 scope C.
3. **Post-M0 short-leg machinery** (exit-grid-2 scope C, agent-exec
   defined-risk spreads): debit-spread short legs require
   expiration/early-assignment/dividend/exercise-by-exception logic to
   exist and be tested before any spread cell runs.
4. **Operator GO on agent-exec** (agent-exec memo): the MVE needs an
   explicit accept/amend/reject, a NEW sealed registration with its own
   sha256, a trial row before any episode is evaluated, and separate
   approval for the ~192-request intraday wire capture. The memo's own
   recommendation: run the 10-episode measurement-floor pilot FIRST (does
   the un-evolved seed separate from no-trade beyond the k=3 actor-noise
   band?); if not, the MVE is NOT_EVALUABLE by construction.
5. **Earnings-timing coverage** (pead-deep-2): timing labels are
   `estimated`, coverage is 8 in-universe next reports (6 bmo, 2 amc), the
   amc stratum is effectively a COST/NFLX two-name contrast; the ≥ 12-card
   bar is years out; historical timing coverage is zero and cannot be
   created retroactively.
6. **HAR market aggregate** (pead-deep-2): considered and dropped — a
   per-name forecast with no defined market aggregate; recorded as a
   successor-study idea, not part of this menu.
7. **T2 re-derivation cost** (term-gate): whether per-signal paired diffs
   are recoverable from stored XSMOM-EXITGRID cell outputs (no re-run) or
   require adding a regime column to the frozen script.
8. **No virgin backtest window** (exit-grid-2 honesty statement; applies
   to term-gate's sealed era equally): the census consumed the panel
   through 2026-09-23; the seal binds the variant deltas and the forward
   card chain is the only clean OOS for any PASS.
9. **Display exposure** (vrp-cond): the desk has displayed `vrp_har` for
   2026-09-22 (features store); no outcome-conditioned selection was made,
   but the feature is not literally unseen — disclosed.
10. **PLTR/SPCX backfill** (all slots): joined `desk-universe.toml`
    2026-09-23, not yet in the ohlc panel; every slot pins to the 35
    full-history chain-universe names; widening is a successor
    registration.
11. **2026-09-01 XSMOM card** (term-gate): incomplete at panel end —
    excluded from the sealed window, accrues to the forward chain
    (demote-or-hold).
12. **Defective-run handling** (jepa-filter, agent-exec): V3 collapse,
    future-poison, skipped-origin defects are NOT_EVALUABLE by operator
    ruling only (IVHIST-001 run-1 precedent) — the operator should expect
    to be asked for rulings if those trip.

## 5. Operator-commit checklist (exact)

1. Inspect the working tree. Everything campaign-related is currently
   UNTRACKED (the authoring agents committed nothing); `git -C
   /home/alexk/documents/tree_options status --porcelain` shows exactly:
   - `?? docs/theory/campaign-2026-09/` — must contain ONLY
     `slots/{vrp-cond,pead-deep-2,term-gate,exit-grid-2,jepa-filter}.md`
     (vrp-cond.md and term-gate.md carrying the section-3 fixes) plus
     `REGISTRATION-NOTES.md`
   - `?? docs/theory/campaign-2026-09-registration.json`
   - `?? docs/theory/trade-agent-gepa-game-2026-09-23.md` (the agent-exec
     memo — a sealed source the menu pins by hash; include it in the seal
     commit or the pinned hash dangles)
   - `?? docs/theory/trade-jepa-exploration-2026-09-23.md` (jepa-filter's
     source memo — same treatment)
   Anything else in the status output is unrelated work and stays out of
   this commit.
2. Verify the menu parses and the arithmetic holds:
   `python3 -c "import json;d=json.load(open('docs/theory/campaign-2026-09-registration.json'));assert d['unique_configs']==111==sum(c['config_count'] for s in d['slots'] for c in s['scope_ids']);print('ok',d['unique_configs'])"`
   (run from the repo root), and that the two fixed slot files hash to the
   post-fix values pinned in the menu's `dataset_pinning`
   (`8caa7aa0…` vrp-cond.md, `2b7e0b94…` term-gate.md).
3. Seal with sha256 sidecars (FORECAST-001.md.sha256 precedent):
   `sha256sum docs/theory/campaign-2026-09-registration.json docs/theory/campaign-2026-09/REGISTRATION-NOTES.md docs/theory/campaign-2026-09/slots/*.md docs/theory/trade-agent-gepa-game-2026-09-23.md`
   writing `<file>.sha256` beside each, then re-run step 1's status (the
   sidecars now also appear as new files — expected).
4. Commit everything from steps 1–3 in ONE commit at head `f075997`
   (or the then-current head; update `registered_at_head` first if the
   head moved — the slot docs were authored against `f075997`). Suggested
   message:
   `docs(theory): campaign-2026-09 registration menu (T-NULL + 5 slots + agent-exec placeholder), 111 configs / 10 scopes`
   ending with:
   `Co-Authored-By: Claude Code <noreply@anthropic.com>`
5. After the commit: record the commit sha and the sidecar hashes; the
   first trial row for any config in this menu cites them (INV-13/INV-14).
6. Sequencing at execution (binding): T-NULL x3 runs FIRST and its
   calibration artifact is stamped before ANY family sealed-window
   scoring; a DEFECT-FLAGGED seed freezes family scoring pending an
   operator ruling.
7. Do NOT activate the agent-exec placeholder: no wire requests, no
   builds, no episodes — it requires its own operator GO, a new sealed
   registration, and a trial row before any episode is evaluated.
8. Nothing in this menu adopts anything: promotion for any nominee runs
   only through the forward sealed-card chain (>= 20 cards per rule,
   `artifacts/paper-trades/promotion.py`; the XSMOM 18/20-card review for
   exit/rule successors; the pre-registered kill-switches in
   RESEARCH-LEDGER.md DESK-CHECKS).

## 6. Verdict

Menu assembled and validated: **ready for the operator commit** — 111
configs across 10 scopes (max load 24/32), banned-registry clean,
fold mappings protocol-consistent after the two recorded fixes, INV-13
shape complete, all data-gated arms marked with their gates.
