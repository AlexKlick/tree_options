# QSL pre-registration — the quote-bearing shadow ledger (QSL-20260930)

Frozen 2026-09-30, before the first forward-registered candidate exists. This
document is the pre-registration named by every queue row the QSL builder
writes (`tree_options.desk.qsl`, `prereg_id = QSL-20260930`): each row carries
this file's sha256, and the evidence store's hash chain binds every episode
and mark to the queue that carried it. Changing any frozen number below
restarts the clock under a new pre-registration id (the spec's own rule).
Nothing here authorizes an order; the lane is paper research only.

- Spec: `~/.local/state/trex-strategy-20260930/strategy-specs.md` SPEC 3
  (`qsl`), carrying SPEC 2's crossing-cost gate.
- Vehicle: `desk/shadows.py` (`desk-evidence.timer`, live) fed by the
  deterministic queue builder `desk/qsl.py` → `<TREX_DESK_STATE>/queue-qsl/`,
  consumed by `python -m tree_options.desk shadows --queue-dir <queue-qsl>
  --database <TREX_DESK_STATE>/evidence/qsl.sqlite3`.
- Commissions: rails' `COMMISSION_PER_CONTRACT_USD` $0.65 per contract per
  side (`desk/rails.py:140`), charged on both sides of both legs of a 1-lot
  vertical = **$2.60 per round trip** (exactly what the lane's
  `modeled_fees_dollars` records).

## 1. The frozen candidate rule (no model, no discretion)

Per recorded session D and name N with a recorded chain (`chains/<D>/<N>.json.gz`,
schema `desk-chain/1`, one post-close snapshot ~17:46 ET), for each family
`put_credit` and `call_debit`:

1. `E*` = the nearest chain expiry with 7 ≤ DTE ≤ 35, DTE in calendar days
   from D.
2. `short` = the strike of that expiry and family right whose OWN
   Black-Scholes |delta| at the mid-implied IV
   (`desk.surface.chain_quotes`; the vendor `delta`/`iv` columns are never
   read) is nearest 0.30; ties → the lower strike.
3. `long` = the listed strike strictly BELOW the short whose distance from it
   is nearest 5; ties → the smaller width. Both families are the bullish
   verticals of the board grammar (`intraday_action_graph.py:234-236`,
   `longrun.py:86`): the long leg is always the lower strike — the put credit
   sells the |0.30|-delta put and buys the further-OTM wing, the call debit
   buys the nearer-the-money call and sells the |0.30|-delta call.
4. Gate (SPEC 2, every clause, both legs, from D's own snapshot):
   `two_sided` (0 < bid ≤ ask), `sized` (bid_size ≥ 10 and ask_size ≥ 10),
   `oi ≥ 100` (rails `min_leg_open_interest`, `v2.toml:47`), and the buyer's
   crossing penalty ≤ θ × package mid with θ = 0.10 (rails'
   `max_leg_spread_frac_of_mid`, `v2.toml:48`), where for a credit
   `mid = mid(short) − mid(long)`, `cross = bid(short) − ask(long)`,
   `penalty = mid − cross`, and for a debit `mid = mid(long) − mid(short)`,
   `cross = ask(long) − bid(short)`, `penalty = cross − mid`. Prices are
   cent-quantized half-up (`trex.plan.cents`).
5. A gate pass is enqueued as ONE `trex.deal/1` admissible row with
   `fill` = the crossing price (never a modeled mid fill), `limit` = the
   first cent strictly beyond it (credit floor / debit cap), `ref_mid` = the
   package mid, quantity 1, `exit_deadline` = §2's deadline. Gate failures
   are never enqueued (not even as surfaced rows: the shadow lane creates
   episodes from parse-valid surfaced rows too, which would pollute the
   ledger with un-gated trades); they are counted in the queue's
   `qsl.refused` diagnostic block.

## 2. Exit convention (the one place the spec's prose could not be literal)

SPEC 3 says "deadline = expiry". That is not expressible in this lane:
`desk.contracts.parse_deal` refuses `deadline ≥ previous_session(first_expiry)`
(`deadline_safety_buffer`) and `trex.plan.LegStructure` refuses
`exit_deadline ≥ first_expiry` — the engine never holds to expiry, by design.
The frozen deadline is therefore **the session two before the first expiry**
(the engine's own expiry-safety bound, `miner.exit_deadline`'s
`sessions[before − 2]`), and the resolution mark is **that session's EOD
crossing snapshot** (DTE ≈ 2 at the typical 7–11 DTE entry), recorded by the
shadow lane, censored when absent. The primary metric is
**deadline-resolved at DTE≈2**, not expiry-settled; residual theta/assignment
risk between the deadline and the expiry is deliberately NOT measured and NOT
claimed. `no take-profit, stop, touch or breach exit` is modeled — the exit
is the time stop at the deadline, exactly what `desk/shadows.py` measures.

## 3. Frozen threshold (verbatim from SPEC 3 §3.2)

> - **Unit of observation:** one (name, family, entry-session). **Cohort** for power =
>   (name, expiry-week, family). Censored outcomes stay censored.
> - **Primary metric:** fully-crossed net per trade = (entry credit_x or debit_x) −
>   (exit crossing) − commissions, expiry-resolved. **Secondary:** mid-to-mid net (the
>   proxy convention) — the gap distribution between the two IS the fill-quality
>   measurement the program has never had.
> - **Interim (report-only, promotion impossible):** at 60 cohorts.
> - **Promotion threshold (fixed):** at **≥120 expiry-resolved cohorts per family**,
>   fully-crossed net mean > 0 with one-sided sign-flip p < 0.05 (10,000 draws),
>   name-clustered block-bootstrap 95% CI low > 0, no single dropped name flips the sign,
>   and the two expiry-halves agree in sign. Evaluated ONCE; any re-parameterization
>   restarts the clock under a new pre-registration id.
> - **Fill-quality side claim (independent):** at ≥500 entries, publish the p50/p90
>   crossing penalty by (DTE × moneyness × size) bucket — the honest replacement for the
>   flat $14.60 in every downstream sensitivity.

## 4. As-implemented definitions (so the evaluation cannot be gamed)

- **Entry (unit of observation).** One queue row = one (name, family,
  entry-session) candidate. The lane's cohort rule keeps ONE usable
  representative per (name, row, ISO-week-of-entry, tier, seals)
  (`shadows._cohort`); the FIRST row by (rank across sessions in session
  order) is the representative and every other row stays a `candidate`.
  "Entry" below means an EPISODE (the representative), never a lost duplicate.
- **Cohort (power unit).** (name, ISO-week of the legs' expiry date, family).
  A cohort is **expiry-resolved** when its episode's deadline session has a
  recorded mark (`mark` row at the deadline session); a censored deadline
  (quality event, no chain) leaves the cohort unresolved — it never counts,
  and is never imputed.
- **Primary metric (fully-crossed net), per entry:** the deadline mark's
  `modeled_net_pnl_dollars`, which IS the spec's formula because the builder
  sets `fill` to the crossing entry and the lane's `realistic`
  (`shadows.package_prices`) prices the CLOSE-side crossing — a held BUY leg
  is sold at its bid, a held SELL leg is bought back at its ask:
  `net_x = (entry credit_x − [ask(short) − bid(long)]) × 100 − 2.60` for
  `put_credit`, and `net_x = ([bid(long) − ask(short)] − entry debit_x) × 100
  − 2.60` for `call_debit`. The buyer's crossing therefore applies on BOTH
  sides: the entry at D's snapshot (the queue row's recorded leg bid/ask),
  the exit at the deadline session's own snapshot (the mark's
  `chain_sha256` pins the exact chain document). No mid-fill is ever scored.
  A censored exit (no chain at the deadline session) leaves the entry
  unresolved — never imputed from an earlier mark.
- **Secondary metric (mid-to-mid net, the proxy convention), per entry:**
  `(ref_mid − deadline mark mid) × 100 − 2.60` (credit orientation; the
  debit family negates), using the queue's `ref_mid` and the deadline mark's
  `mid`. The per-entry difference primary − secondary is the fill-quality
  measurement (the entry- plus exit-crossing penalties).
- **Promotion test.** Over the entries of ONE family: the mean of `net_x`
  > 0; one-sided sign-flip permutation over ENTRIES (10,000 draws, fixed
  seed 20260930); name-clustered block-bootstrap 95% CI (resampling names
  with replacement, 10,000 draws, the same seed) with low > 0; drop-one-name
  sign stability (no single dropped name flips the mean's sign); expiry-half
  agreement (entries split at the median expiry date, both halves' means the
  same sign). Holm within the 2-family family at α = 0.05, mirroring the
  harness rule (SPEC 1 §1.3).
- **Counting rule (anti-backfill).** Only episodes with
  `registration_timing == 'before_entry_window_end'` count toward the 60/120
  cohort thresholds and toward every test above: the queue must have been
  adopted before 11:30 ET on the entry session. Episodes the lane labels
  `retrospective_backfill` (the five 2026-09-22..09-29 sessions if backfilled,
  and any late-adopted session) are recorded and reported separately, never
  counted. This is the lane's own forward/retrospective distinction
  (`shadows.py` `registration_timing`) and cannot be gamed by re-running the
  builder later: a queue adopted after its entry window stays retrospective
  forever.
- **Evaluation ONCE.** The single promotion evaluation runs on the first
  session at which BOTH families have ≥120 expiry-resolved cohorts; the
  interim report at ≥60 is report-only. Any change to §1, §2 or §4 — θ, the
  delta target, the DTE window, the width target, the size/OI floors, the
  deadline rule, the metric definitions, or the counting rule — restarts the
  clock under a new pre-registration id.
- **Fill-quality side claim.** At ≥500 counted entries (pooled families):
  publish p50/p90 crossing penalty by (DTE × |moneyness| × touch-size) bucket
  at entry, from the rows' `qsl.crossing_penalty` and recorded sizes.

## 5. Measured baseline (n = 5 recorded sessions, fixed 37-name board)

Characterization run of the frozen §1 rule over the recorder corpus
(2026-09-22, 09-24, 09-25, 09-28, 09-29; 09-23 lost; XLV missing 09-22;
184 (name, session) pairs per family):

| | put_credit | call_debit |
|---|---|---|
| gate passes (rows) | 16 | 14 |
| episodes after the lane's cohort rule | 11 | 11 |
| distinct (name, expiry-ISO-week) cohorts | 9 | 10 |
| per-session cohort accrual | 1.8 | 2.0 |

Gate clause failures per family (of 184; clauses overlap, so they do not sum):
crossing penalty above θ 117 (put) / 132 (call), open interest below 100
90/92, touch size below 10 87/91, no two-sided long quote 16/0, no computable
package price 16/0, mid not positive 1/0. Passing names: PLTR 8, SPY 6,
IWM 4, NFLX 4, SPCX 2, AAPL/AMZN/GOOGL/INTC/PG/XOM 1 each. **XLF, XLE and
XLV passed zero candidates** — consistent with their 34–38% median relative
spreads (`mining-report.md` §2): the spec is silent on those names, so they
face the same gate as everyone else and the gate itself excludes them. No
special handling was added. Chosen-expiry DTE: 7–11 (median 7.5); chosen
widths: 4.5–5.0 against the listed grid.

Corpus-wide context (mining-report.md §2, n = 1,216 verticals): median
cross-debit 1.57× mid-debit, median crossing penalty $1.275 = 9.1% of the
strike width; over the same 5 sessions the §1 picker's candidates (n = 351
with a computable package price) have a median crossing penalty of 21.8% of
the package mid (p10 3.8%) — θ = 0.10 is the binding clause for most of the
board.

## 6. Clock projection (from the measured accrual, not the spec's estimate)

SPEC 3's "~74 candidates/session (37 names × 2 families)" over-estimates the
measured pass rate by ~13×: the frozen gate passes ~6.0 candidates/session
pooled (30 rows / 5 sessions), accruing ~1.8–2.0 cohorts per session per
family. The binding family needs 120 / 1.8 ≈ 67 recorded sessions (the
interim 60-cohort report: 34 recorded sessions). Recorder uptime observed to
date: 5 recorded of 6 elapsed NYSE sessions (09-23 lost).

Anchor: the first forward-registered session. With the lane armed for
2026-09-30 (entry 2026-10-01) and NYSE sessions from the committed calendar:

| scenario | interim (60) resolved | promotion (120/family) resolved |
|---|---|---|
| no further recorder loss | ~2026-11-24 | **~2027-01-13** |
| uptime stays at the observed 5/6 | ~2026-12-03 | **~2027-02-02** |
| degraded to 4/6 | ~2026-12-18 | ~2027-03-03 |

Each additional lost session moves the date ~1 session later (±2 weeks per
the spec's own uncertainty), and each week the lane is not armed moves it a
week later — the clock is the recorder's, not the code's. The spec's
"~2026-12-01" first promotable reading is NOT achievable at the measured
gate pass rate; the honest projection is **~2027-02-02 ± 3 weeks** at the
observed uptime. This is a measurement, not a design choice: loosening θ to
hit the spec's date would be exactly the re-parameterization §4 forbids.

## 7. What this pre-registration does NOT claim

- No expectancy claim, now or at the interim: the interim report at 60
  cohorts is descriptive only and promotion is impossible before §3's
  threshold is met in full.
- No intraday fill claim: the record time is post-close (~17:46 ET) where
  spreads are wider than the liquid session — the ledger is conservative on
  crossing costs and measures snapshot-crossing economics, not a 10:00-ET
  fill simulation.
- No vol_state, iv_rank, per-name percentile, or any 120-session
  conditional: the rule reads only the decision session's own snapshot.
- No order, no canary, no mandate: `execution_enabled` stays false on every
  artifact this lane produces.
