# TERM-GATE round 1 — inner folds (campaign-2026-09, slot order 3)

Executor: term-gate slot agent, 2026-09-24. Branch `exec/campaign-2026-09`.
Runner: `scripts/campaign/term_gate_run.py` (this commit). Registry:
`artifacts/campaign-2026-09/term-gate.db` (scope `c09-term`, 24/24 COMPLETED,
one scope commitment). Per-config artifacts:
`artifacts/campaign-2026-09/term-gate/trials/c09-term-<id>-g1.json`; the
mandated tuning-era ranking:
`artifacts/campaign-2026-09/term-gate/inner-ranking.json`.

## Binding

- Menu: `docs/theory/campaign-2026-09-registration.json` v3, sha256
  `4aca21014f6efa640d97bf2abd3fdbb912af5a7b9acb68f65b842f924379b10a`
  (sidecar-verified). Slot doc
  `docs/theory/campaign-2026-09/slots/term-gate.md` sha256
  `2b7e0b94c9031f6fcab27f33234a2ba3aec5b8740586982d30d4f49d85372d9d`
  (menu-pinned). Protocol raw sha `2fde83db…` = menu pin.
- Inputs pinned and verified at bind time: ohlc-panel, earnings-calendar,
  sealed calendar, `slots/term-gate.md`, and the four CBOE index CSVs
  (VIX, VIX1Y, VIX3M, VIX9D — all four sha256 match the menu's
  `dataset_pinning`).
- Null gate: `artifacts/campaign-2026-09/tnull/calibration-v3.json`
  (sha256 `5d0aa0ee…`, verdict **CALIBRATED**, binds this menu sha, v3).
  B read, never recomputed: **B(card-era, xsmom) = +0.015234**
  (day-clustered se 0.0092550), **B(card-era, event) = +0.011465**
  (se 0.0074746). The card-era window (2024-10-01..2026-08-28) is
  EVALUABLE in the v3 stamp (the NOT_EVALUABLE windows are pead-deep-2 and
  vrp-cond), so the sealed round reads the normal ON − B excess leg, not
  the B-alone gate.

## Round-1 scope (inner folds ONLY)

The sealed windows named in the menu — XSMOM entries 2024-10-01..2026-08-03,
PEAD entries 2024-10-16..2026-08-06 (card-era 2024-10-01..2026-08-28) — were
NEVER scored, never read for tuning, never plotted. Executed:

- **T1 XSMOM legs**: the 22 tuning cards, entries 2022-11-01..2024-08-01
  (verified: exactly 22 fired FOMs; the 2024-09-03 FOM fires but sits in the
  purge gap — dropped and counted). Gate at t−1, trailing [t−252, t−1]
  inclusive on the index-session grid (joins on ISO index dates; zero index
  gaps on the tuning span; the 70 calendar sessions missing index rows are
  all after 2026-09-22, the files' last row).
- **T1 PEAD legs**: zero tuning-era cards exist (earnings-calendar era
  starts 2024-09-05 — verified, 0 events with entry ≤ 2024-09-03). The
  registration declares PEAD thresholds fit-free; inner verdict
  NOT_EVALUABLE (structural, by registration design, never a defect).
- **T2**: the frozen XSMOM-EXITGRID grid re-derived with the r93 regime
  column, **holdout era only** (signal dates ≤ 2024-09-03 — the same inner
  boundary the slot declares). The frozen grid's "full" era (signal dates
  > 2024-09-03) overlaps this slot's sealed window and was NOT computed; it
  accrues to the sealed round. Copy-faithfulness anchored on the PUBLISHED
  holdout table: pooled (regime-ignored) hold-60 signal sets reproduce
  8009/683 (60-skip5-tercile) and 1425/475 (252-skip21-top3) exactly, and
  every pooled variant d-mean drift vs the published table is ≤ 3.6e-6
  (bar 6e-4).

Accounting: card-lane, $2,500/leg, entry close[t], exit close[t+20] (Decimal
closes, house hold filter), 5bp RT primary / 15bp robustness, day-clustered t
(ddof=1, cluster = entry date). T2 uses the frozen grid's own machinery
verbatim (hold-60 baseline, worst-case fill simulator, conservative
t = min(naive, clustered)).

## INV-13 record

All 24 config ids were written to `term-gate.db` (REGISTERED, no outcome)
before any outcome was computed or viewed; execution is one-shot per trial
(REGISTERED → RUNNING → COMPLETED, artifact as metrics_uri). **Disclosed
re-registration**: the first registration (at branch HEAD 81c8beb) was
voided before any outcome existed when a sibling slot's commit moved the
branch HEAD between `--register` and `--execute` and the registry's
provenance check correctly refused; the db then held 24 REGISTERED rows and
0 outcomes (verified), was deleted, and re-registered at the then-HEAD
(0433522) immediately before the successful execution chain. No outcome was
viewed at any point before the final registration. A second runner defect
(a `sys.modules` registration needed to import the frozen grid script) also
crashed execution before any T2 outcome existed; the partial T1 artifacts
from that attempt were deleted and the whole run re-executed
deterministically. The executed artifacts bind the executing runner's sha
(`82b05dd4…`); the committed runner additionally contains ranking-print
fixes only (no cell computation touched after execution).

## T1 inner results (22 tuning XSMOM cards, 66 legs, ungated book +3.1752%/card, +5,239.1 USD)

| id | gate | ON n/cards | ON net | ON t | OFF n/cards | OFF net | ON−OFF spread | gated vs ungated P&L |
|---|---|---:|---:|---:|---:|---:|---:|---|
| T1-03 | r93×SIGN | 57/19 | +2.8012% | 1.243 | 9/3 | +5.5439% | **−2.7427%** | 3,991.8 vs 5,239.1 |
| T1-05 | inc×Q60 (incumbent) | 42/14 | +0.7823% | 0.315 | 24/8 | +7.3628% | −6.5805% | 821.4 vs 5,239.1 |
| T1-01 | r93×Q60 (PRIMARY) | 45/15 | +0.8588% | 0.372 | 21/7 | +8.1390% | −7.2802% | 966.2 vs 5,239.1 |
| T1-09 | bs×Q60 | 60/20 | +2.3465% | 1.106 | 6/2 | +11.4626% | −9.1160% | 3,519.8 vs 5,239.1 |
| T1-07 | inc×SIGN (incumbent) | 66/22 | +3.1752% | 1.573 | 0/0 | — | n/a (no OFF cards) | 5,239.1 vs 5,239.1 |
| T1-11 | bs×SIGN | 66/22 | +3.1752% | 1.573 | 0/0 | — | n/a (no OFF cards) | 5,239.1 vs 5,239.1 |

Reading (tuning era only; no verdict attaches): every evaluable spread is
NEGATIVE — in 2022-11..2024-08 the gates withheld the BEST months (OFF
buckets are small and strongly positive), and every gated book destroys
value against the ungated book (the criterion-3 utility analog fails on
the inner fold for all Q60 cells). This is the slot's own disclosed risk 3
("the gate skips the best months") and matches the GATE-001 base rate that
motivated the pre-committed WITHDRAW posture. The 15bp robustness spreads
are identical to the 5bp spreads (the per-trade cost cancels in the
ON−OFF difference). Power floors (OFF-n ≥ 8 XSMOM / ≥ 20 PEAD) bind the
sealed round only; inner OFF-card counts are 3–8, and both SIGN cells with
fixed thresholds have zero OFF tuning cards (the slot's disclosed
"5.1%-OFF SIGN cell" risk).

**T1-02/04/06/08/10/12 (PEAD legs): NOT_EVALUABLE (inner)** — zero
tuning-era cards by registration; no PEAD outcome informs any threshold.

## Mandated tuning-era ranking (recorded before any sealed-era run)

Statistic (recorded convention): ON−OFF per-trade net mean spread (5bp RT)
over the 22 tuning XSMOM cards; ties → sparser gate, then config id. Cells
with zero OFF cards rank below every cell with a defined spread (no
evidence of separation — recorded, never dropped).

1. T1-03 r93×SIGN −2.7427% (new-quantity)
2. T1-05 inc×Q60 −6.5805% (best incumbent)
3. T1-01 r93×Q60 −7.2802% (PRIMARY)
4. T1-09 bs×Q60 −9.1160%
5. T1-07 inc×SIGN — n/a (0 OFF cards)
6. T1-11 bs×SIGN — n/a (0 OFF cards)

**Inner-fold best: T1-03 (r93×SIGN), ON−OFF spread −0.027427.** The
registration's PRIMARY cell T1-01 stays the primary; this ranking is the
recorded tuning-era evidence, not a re-registration. Note honestly: the
"best" spread is still negative, and the new-quantity leader's inner
spread (−2.74%) exceeds the best incumbent's (−6.58%) — on tuning-era
cards only, with 3 OFF cards, i.e. no power behind it.

## T2 inner results (holdout era, DESCRIPTIVE-ONLY; interim read)

| id | cell | n | days | paired d_mean | t_cons |
|---|---|---:|---:|---:|---:|
| T2-01 | 60-skip5 / hold1 / CALM | 3528 | 297 | −6.9596% | −22.21 |
| T2-02 | 60-skip5 / hold1 / STRESSED | 4481 | 386 | −3.5959% | −14.72 |
| T2-03 | 60-skip5 / tp100c / CALM | 3528 | 297 | −5.8995% | −20.29 |
| T2-04 | 60-skip5 / tp100c / STRESSED | 4481 | 386 | −2.4974% | −10.96 |
| T2-05 | 60-skip5 / oco100_150 / CALM | 3528 | 297 | −7.0940% | −22.53 |
| T2-06 | 60-skip5 / oco100_150 / STRESSED | 4481 | 386 | −3.6620% | −14.93 |
| T2-07 | 252-skip21 / hold1 / CALM | 783 | 261 | −10.6071% | −12.96 |
| T2-08 | 252-skip21 / hold1 / STRESSED | 642 | 214 | −10.9641% | −14.42 |
| T2-09 | 252-skip21 / tp100c / CALM | 783 | 261 | −9.0091% | −11.38 |
| T2-10 | 252-skip21 / tp100c / STRESSED | 642 | 214 | −9.2732% | −12.78 |
| T2-11 | 252-skip21 / oco100_150 / CALM | 783 | 261 | −10.6923% | −13.02 |
| T2-12 | 252-skip21 / oco100_150 / STRESSED | 642 | 214 | −10.9398% | −14.35 |

Every cell clears the ≥100-signal-date floor (214–386 days). No variant is
positive in either regime — nowhere near the REGIME-SIGN-FLIP pattern
(which needs paired mean > 0 with conservative t ≥ 2 in STRESSED while
≤ 0 in CALM). Verdicts (interim, inner read):
**DESCRIPTIVE-ONLY:NO-REGIME-SIGNAL × 12** — exactly the slot's expected
outcome ("a median regime split flipping any variant positive would be
extraordinary"). The registered descriptive verdict also covers the frozen
grid's full era, which overlaps the sealed window and is deferred to the
sealed round; the XU post-hoc precedent forbids any live change from
either read.

## Verdict summary (pre-declared vocabulary only)

- 24 configs run, 0 withdrawn (the slot declares no data-gated arm; the
  menu's gated arms — vrp-cond scope O, exit-grid-2 scope C — belong to
  other slots).
- NOT_EVALUABLE: 6 (T1-02/04/06/08/10/12 — zero tuning-era PEAD cards,
  structural, by registration).
- DESCRIPTIVE-ONLY:NO-REGIME-SIGNAL: 12 (T2-01..T2-12, interim inner
  read).
- No round-1 verdict: 6 (T1-01/03/05/07/09/11 — the T1 cell bar is
  "Evaluated ONLY on the sealed window", slot section 6; the inner stats
  exist to rank the six gate specs, and the ranking is stamped). The
  family verdicts (PASS-GATE / INCUMBENT-CONFIRMED / WITHDRAW) are owned
  by the sealed round.

## Sealed round handoff (not run here)

The sealed evaluation reads, per T1 cell: (1) ON mean − B(card-era, matching
shape) > 0 AND clustered-t ≥ 2.64 (one-sided Bonferroni, m=12); (2) ON >
OFF; (3) gated book mean/card AND total P&L ≥ ungated; (4) OFF-n ≥ 8 (XSMOM)
/ ≥ 20 (PEAD) else NOT_EVALUABLE — then the beat-or-withdraw block bootstrap
vs the best incumbent (10,000 resamples of sealed entry dates, one-sided
P < 0.05). B values and ses are already carried in every trial artifact's
hyperparameters (`B_card_era`), read from calibration-v3.json. The tuning-era
ranking above is the recorded pre-seal selection evidence.

Nothing is adopted, nothing seals a card, no RESEARCH-LEDGER entry is made
by this executor. The inner-fold evidence is uniformly unfavorable to the
gate family (negative spreads, value-destroying gated books, no regime
rescue) — consistent with the pre-committed WITHDRAW posture — but that
question belongs to the sealed round.
