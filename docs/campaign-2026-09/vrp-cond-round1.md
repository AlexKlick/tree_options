# VRP-COND — round 1 results (INNER FOLDS ONLY)

Slot `vrp-cond` (menu order 1), scopes `c09-vrp-e` (20 configs, cap 32) and
`c09-vrp-o` (4 configs, cap 32). Executor round 1, 2026-09-24. Runner:
`scripts/campaign/vrp-cond_run.py` (this branch). Nothing is adopted, nothing
seals a card, the RESEARCH-LEDGER is not touched (consolidator owns it).
Verdicts use the slot's pre-declared vocabulary only: per-config
PASS / FAIL / NOT_EVALUABLE (scope E) — PASS/FAIL are SEALED-round verdicts and
were never assigned in round 1 — plus DATA_GATED / WITHDRAWN (scope O) and the
sealed-round family verdicts SURVIVOR-CANDIDATE / DEFLATED / NOT_EVALUABLE-SEALED.

## 1. Binding (verified before anything ran; every phase REFUSED on drift)

- Menu v3 `docs/theory/campaign-2026-09-registration.json` @ `3210e2e`,
  sha256 `4aca21014f6efa640d97bf2abd3fdbb912af5a7b9acb68f65b842f924379b10a`
  (sidecar-verified by the runner); slot doc `slots/vrp-cond.md` sha256
  `8caa7aa0dbc74840156881ed497c82ec990b05e40c84aeefffad08f54c5c6795` (menu
  `dataset_pinning` pin, re-verified). Protocol raw sha equals the menu's
  `protocol_hash`; canonical re-stamped per INV-14.
- T-NULL gate: `artifacts/campaign-2026-09/tnull/calibration-v3.json` stamps
  THIS menu sha and its slot verdict is CALIBRATED — family scoring unfrozen
  per `rules.sequencing` (amendment v3).
- Inputs (all sha-pinned to the menu): `ohlc-panel.json` (read under the
  shared flock), `earnings-calendar.json`, sealed NYSE calendar (2025-01-09
  phantom excluded in the walker; file byte-identical),
  `desk-store/iv-history/vwap_atm.json` (schema `desk-ivhist/1`, 35 names x
  508 sessions, fidelity labels IWM `ok` / 28 `low-fidelity` / 6
  `not-evaluable` honored: absolute thresholds IWM-only).
- Geometry re-verified from the sealed calendar: IV window 508 sessions
  2024-08-26..2026-09-03; inner V1 = ordinals 253..378, inner V2 = 316..441;
  tuning entries additionally restricted to ordinal <= 417 (INV-06 at the desk
  hold length: 417+20+5 = 442 < 443). **The sealed window (ordinals
  443..505 = 2026-06-02..2026-08-31) was NEVER scored, read for tuning, or
  plotted**: across all 20 executed artifacts the deepest entry is
  2026-04-24 (ordinal 417, the purge boundary) and the deepest hold exit is
  2026-05-22 — both inside the purge gap — and the runner hard-refuses any
  deferred entry past ordinal 417.

## 2. INV-13 registry state

All 24 config ids were written to the slot registry sqlite
(`artifacts/campaign-2026-09/vrp-cond.db`) as REGISTERED with NO outcome at
06:17:49Z — BEFORE the first outcome was computed (first outcome_at
06:17:54Z); execution was one-shot per trial (REGISTERED -> RUNNING ->
COMPLETED, per-config artifact as `metrics_uri`); one scored run per cell,
no re-gridding. State after the run: **20/20 scope-E trials COMPLETED
(20 outcome rows, 64 events), 4 scope-O trials REGISTERED with no outcome
(withdrawn, not run — house convention identical to exit-grid-2 scope C).**

## 3. What ran (exactly the registered grid)

Directions are BYTE-IDENTICAL to `src/tree_options/desk/signals`:
XSMOM-TOP3 via `signals.xsmom_top3` (`close(t)/close(t-273)-1`, no-skip, top
3) and PEAD beats via `signals.pead_beats` (first post-report session,
move >= +1.5%). Evaluation basis: census spot proxy — signal-close entry,
close-to-close hold 20 (house hold filter, Decimal closes), 5bp RT primary /
15bp robustness, 2025-01-09 excluded. Feature: `r = iv30 / harvol20` with
`harvol20 = sqrt(F_HAR,h20 * 365 / c(t, t+20))` (monthly expanding refits
from 2024-09-03, strictly point in time); `p_i` = fraction strictly below
over the trailing 252 sessions ending AT t (window-inclusive, n >= 126);
`pbar` = mean pick percentile; PEAD conditions read at t-1; reporter
conditions withheld when the forward schedule does not pin the event count
through t+20. Tuning population: **8 XSMOM rebalances / 24 entries
(matches the registration's count exactly) and 19 PEAD beats over entry
ordinals 253..417.** Base cells (unconditioned, 5bp net per trade): xe base
+6.9908% (24/24 complete, 8 entry-days), xp base +1.1156% (19/19 complete,
18 entry-days). Two xp beats carried a NOT_EVALUABLE condition (no evaluable
r: IV30 or HAR missing) and abstain under every gated xp config (counted,
never silently ON); the two xp-size-* configs always fire (0 abstentions).

## 4. Results (per-config artifacts under
`artifacts/campaign-2026-09/vrp-cond/trials/`; selection stamp at
`artifacts/campaign-2026-09/vrp-cond/round1-selection.json`)

Metric = the registered selection metric: pooled V1+V2 conditioned-minus-base
per-trade net delta at 5bp RT (SQUEEZE conditional column). The
ON-minus-OFF matched-sessions column is stamped descriptively (it is the
decisive SEALED criterion 1, never a round-1 selector).

| config | base n | n_ON/floor | delta 5bp | delta 15bp | ON−OFF 5bp | abst NE | verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| xe-book-lo | 24 | 0/6 | — | — | — | 0 | NOT_EVALUABLE |
| xe-book-hi | 24 | 3/6 | −12.1726% | −12.1726% | −13.9116% | 0 | NOT_EVALUABLE |
| xe-book-mid | 24 | 21/6 | +1.7389% | +1.7389% | +13.9116% | 0 | EVALUABLE |
| xe-mkt-lo | 24 | 3/6 | −12.5366% | −12.5366% | −14.3276% | 0 | NOT_EVALUABLE |
| xe-mkt-hi | 24 | 9/6 | +14.9551% | +14.9551% | +23.9282% | 0 | EVALUABLE |
| xe-playbook | 24 | 12/6 | −12.0599% | −12.0599% | −24.1199% | 0 | EVALUABLE |
| xe-size-mkt | 24 | 24/6 | −3.4512% | −3.4512% | +23.9282% | 0 | EVALUABLE |
| xe-size-book | 24 | 24/6 | +0.8115% | +0.8115% | −13.9116% | 0 | EVALUABLE |
| xe-defer | 24 | 24/6 | +0.4382% | +0.4382% | +25.0968% | 0 | EVALUABLE |
| xe-lag21 (placebo) | 24 | 0/6 | — | — | — | 0 | NOT_EVALUABLE |
| xp-name-lo | 19 | 5/8 | −0.9325% | −0.9325% | −1.2655% | 2 | NOT_EVALUABLE |
| xp-name-hi | 19 | 7/8 | +4.1477% | +4.1477% | +6.5672% | 2 | NOT_EVALUABLE |
| xp-name-mid | 19 | 5/8 | −5.5922% | −5.5922% | −7.5894% | 2 | NOT_EVALUABLE |
| xp-mkt-lo | 19 | 4/8 | −0.2102% | −0.2102% | −0.2662% | 2 | NOT_EVALUABLE |
| xp-mkt-hi | 19 | 3/8 | −3.6972% | −3.6972% | −4.3905% | 2 | NOT_EVALUABLE |
| xp-playbook | 19 | 14/8 | −2.8708% | −2.8708% | −10.9090% | 2 | EVALUABLE |
| xp-size-name | 19 | 19/8 | −0.9366% | −0.9366% | +6.5672% | 0 | EVALUABLE |
| xp-size-mkt | 19 | 19/8 | +0.3169% | +0.3169% | −4.3905% | 0 | EVALUABLE |
| xp-lag21 (placebo) | 19 | 1/8 | +21.4371% | +21.4371% | +22.6281% | 2 | NOT_EVALUABLE |
| xp-shuffle (placebo) | 19 | 5/8 | −3.0513% | −3.0513% | −4.1410% | 2 | NOT_EVALUABLE |

**Round-1 verdict counts (scope E, 20 configs): EVALUABLE 9 ·
NOT_EVALUABLE 11 (of which 3 placebos).** PASS/FAIL are sealed-round
verdicts (criteria 1–4 bind on the sealed window) and were NOT assigned
here. Scope O (4 configs): WITHDRAWN 4.

Construction disclosure (arithmetic, disclosed not amended): the
conditioned-minus-base delta is cost-invariant — RT enters the strategy and
base means as the same constant (also through the weighted size mean
`sum(w·net)/sum(w)`), so delta at 15bp equals delta at 5bp identically for
every config. Sealed criterion 4 ("delta > 0 at 15bp RT") is therefore
satisfied identically whenever the delta itself is > 0 under this
construction; the criteria stand unchanged.

## 5. Selection (the registered rule, applied mechanically)

Within each family, the promoted config is the largest pooled conditioned-
minus-base delta among floor-met non-placebo configs; ties -> sparser gate,
then id lexicographic (no tie arose — both maxima are strict). Independently
recomputed from the 20 artifacts; reproduces the stamp exactly.

- **xe -> `xe-mkt-hi`** (fire the month's top-3 only if `p_IWM(t) >= 2/3`):
  **delta +14.9551%** per trade (n_ON 9/6, on-fraction 0.375, ON cell net
  +21.9459% vs base +6.9908%).
- **xp -> `xp-size-mkt`** (always fire; half size when `p_IWM(t-1) >= 2/3`):
  **delta +0.3169%** (n_ON 19/8, on-fraction 1.0).

Placebos ran through the identical path and are CONTROLS, not candidates:
sealed criterion 3 compares each promoted config's sealed delta against its
family's placebo deltas. Round-1 reading for the consolidator: **the
`xp-lag21` placebo posts +21.4371% at n_ON = 1** — the loudest deflation
warning in the grid; the xp nominee's sealed delta must beat that number on
the identical path. `xe-book-lo` and `xe-lag21` never fired (0/8 rebalances
in the cheap tercile of pbar) while `xe-book-mid` fired 21/24 and
`xe-book-hi` 3/24 — the tuning-era pbar distribution sat mid/hi; recorded
as the asymmetry disclosure, not evidence.

## 6. Scope O withdrawal (data-gated arm, recorded not proxied)

`ox-cheap`, `ox-rich-fallback`, `op-cheap`, `op-rich-fallback` are DATA_GATED
on (i) the in-flight long-dated option-bar capture landing >= 126 sessions
of history and (ii) an IVHIST-002-successor fidelity verdict. Gate facts at
execution (read-only, no outcome involved): capture manifest exists with
**27 as-of dates < 126 required**; **no IVHIST-002-successor exists** —
neither gate met. The four configs are registered and **WITHDRAWN without
running** (`artifacts/campaign-2026-09/vrp-cond/scope-O-withdrawal.json`,
verdict WITHDRAWN, not_run true). No proxy was improvised; if the capture
fails the withdrawal is terminal (menu scope-O gate).

## 7. Drift baseline for the sealed round (B read, never recomputed)

The vrp-cond window (2026-06-02..2026-08-31) is null-NOT_EVALUABLE in
calibration-v3 for BOTH shapes on all three seeds (event: 14 days < 20
complete trades; xsmom: 2 days < 5, 6 trades < 20), so per amendment v3
`B(W, shape)` is the standing drift gate for this slot's sealed round and no
tripwire prior exists for the window. Read from
`artifacts/campaign-2026-09/tnull/calibration-v3.json` (stamped, not
recomputed): `B(vrp-cond, event)` net per-trade mean **+0.020022**
(day-clustered mean +0.006760, cse 0.016970); `B(vrp-cond, xsmom)` net
per-trade mean **+0.012636** (day-clustered mean +0.012636, cse 0.020319).
Round-1 selection used only the registered relative delta, which does not
read B.

## 8. Multiplicity and adoption posture

24 registered cells (placebo pair included) bound the multiplicity; no
re-gridding after results — a new idea is a successor pre-registration. The
hypothesis's preferred direction (cheap-vol conditioning lifts the allowed
longs) found NO support in the tuning region: every cheap-side gate (xe-book-lo,
xe-mkt-lo, xe-playbook, xp-name-lo, xp-mkt-lo, xp-playbook) is negative or
NOT_EVALUABLE; the only sizeable positive sits on the RICH side
(xe-mkt-hi) — sign-flipped relative to section 1's hypothesis, which the
sealed round will test once as registered (no sign flip was or can be
adopted mid-stream). If the promoted configs survive sealed criteria 1–4 the
family verdict is SURVIVOR-CANDIDATE (nomination to CONTEXT_ONLY only; >= 20
forward sealed cards before anything changes in `signals.py`); any miss is
DEFLATED; floors unmet on the sealed window are NOT_EVALUABLE-SEALED
(report-only monitored gate). Era disclosure inherited: no 2020-style crash
in the window; 28/29 IV names are low-fidelity (within-name constructs
only). Round 1 nominates at most.

## 9. Artifacts

- Registry: `/home/alexk/documents/tree_options/artifacts/campaign-2026-09/vrp-cond.db`
  (24 trials, scopes `c09-vrp-e` 20/32 + `c09-vrp-o` 4/32; 20 COMPLETED, 4
  REGISTERED-withdrawn).
- Per-config artifacts:
  `/home/alexk/documents/tree_options/artifacts/campaign-2026-09/vrp-cond/trials/c09-vrp-e-<config>-r1.json`
  (20 files: stamp + base/ON cells + deltas + ON−OFF column + condition
  tallies + full trade rows).
- Selection: `/home/alexk/documents/tree_options/artifacts/campaign-2026-09/vrp-cond/round1-selection.json`.
- Scope-O withdrawal: `/home/alexk/documents/tree_options/artifacts/campaign-2026-09/vrp-cond/scope-O-withdrawal.json`.
- Run logs (host-work units, all exit 0): `/tmp/vrp-cond-{plan,register,execute,select}.log`.
