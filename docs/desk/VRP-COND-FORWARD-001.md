# VRP-COND-FORWARD-001: forward accrual resolving the vrp-cond nominees (PRE-REGISTRATION, forward-only)

Written 2026-11-14 and sealed the same day (owner-approved registration,
TREX strategy fan-out CP1). PAPER-ONLY. This is a sealed registration
document: it restores the sealed vrp-cond round's criteria, floors, decisive
column and placebo family VERBATIM, fixes the forward conditioner's
construction before any forward card is conditioned by it, and specifies
(report-only) machinery. It deploys nothing; the emitter in §8 is owner-gated
plumbing, not code that ships with this file.

Sealed at body sha256 `49c0865ec2cd62b49d9dce36aeb7a88d04d8d3ebc21a123ff02ccf415469638d`
— the sha256 of every line below the `<!-- body -->` marker (reproduce:
`tail -n +22 docs/desk/VRP-COND-FORWARD-001.md | sha256sum`). The sidecar
`VRP-COND-FORWARD-001.md.sha256` carries the sha256 of the whole committed
file (`sha256sum -c` format); the commit and the sidecar are authoritative.
Corrections are separate `VRP-COND-FORWARD-001-AMENDMENT-n.md` files with
their own sidecars — sealed text is never edited. Any re-parameterization
of the conditioner, pools, floors or criteria restarts under a new
pre-registration id.

<!-- body -->

## 1. The question, and the sealed round of record

Both vrp-cond nominees came back NOT_EVALUABLE-SEALED at their power floors
(gate fired 0/6 for xe; 7 beats < floor 8 for xp) — never FAIL. The
campaign's own disposition (docs/campaign-2026-09/REPORT.md §7.1): "question
stays open, forward cards decide". This registration is that forward
collection: what conditions a forward card, what accrues, what decides, and
when it can possibly resolve.

The sealed round of record is
`artifacts/campaign-2026-09/vrp-cond/sealed-round.json` (serving checkout;
sha256 `3b68f3b38aa338590a6d04453086428bc28c9c75e3bc9917b9d4ba847888f103`),
produced by the registered runner `scripts/campaign/vrp-cond_run.py`
(stamp `runner_sha256`
`4fa8d9e079935482f7be5920ac6cb2444c4a535cbe221026a92dfd56495fbb6f`),
sealed window ordinals 443-505 (2026-06-02..2026-08-31, 63 sessions).
Everything normative in §2, §3.4 and §5 below is quoted from that file or
that runner, byte-identical. Where this registration must go beyond the
sealed text (the forward percentile pool, §3), it DECLARES the departure
instead of mislabeling it "registered".

The two nominees, as registered (runner `GATE_RULES`, byte-identical, no
re-litigation):

- **xe-mkt-hi** — "XSMOM entry: fire only if p_IWM(t) >= 2/3", where
  `r(t) = iv30_IWM(t) / harvol20_IWM(t)` and p_IWM is the trailing
  percentile of §3. Tuning delta +14.9551%/trade (n_ON 9/6, on-fraction
  0.375) — selection evidence only. The nominee sits on the RICH side,
  sign-flipped vs the registration's hypothesis direction (CHEAP vol
  lifting the longs found no support anywhere in the grid); it is tested
  once, exactly as registered.
- **xp-size-mkt** — "PEAD size: always fire; half size when
  p_IWM(t-1) >= 2/3". Tuning delta +0.3169% (n_ON 19/8) — selection
  evidence only. The `xp-lag21` placebo's +21.4371% at n_ON=1 is the
  loudest small-n warning in the family; no forward delta means anything
  before floors.

## 2. Sealed acceptance and floors (VERBATIM)

From `sealed-round.json`, quoted exactly:

> **registered_sealed_acceptance**: "sealed acceptance, ALL four: (1)
> sealed ON-minus-OFF-session base delta > 0 (matched-sessions conditional
> column decisive, SQUEEZE precedent); (2) day-clustered block bootstrap
> (block 10, 2000 resamples, one-sided) p < 0.10; (3) delta exceeds BOTH
> same-family placebo deltas (xe-lag21 / xp-lag21 / xp-shuffle) on the
> identical path; (4) delta > 0 at 15bp RT"

> **registered_power_floors**: "power floors: xe-* n_ON >= 6 sealed
> entries; xp-* n_ON >= 8 sealed trades; below floor = NOT_EVALUABLE,
> never FAIL"

Reading of the floors (the sealed round's own convention,
`power_floor.binds_on` = "fired n_ON (round-1 convention)"):

- **xe floor: 6 fired ENTRIES** — entries the gate let fire, over sealed
  fired rebalances (the sealed round counted 6 entries over 2 fired
  rebalances; it observed 0 fired and fell NOT_EVALUABLE-SEALED).
- **xp floor: 8 fired TRADES** — fired sealed beats (the size rule fires
  on every beat; the sealed round fell short at 7 < 8).

ON/OFF semantics (sealed `on_off_semantics`): xe — fired (ON) vs
base-not-fired (OFF) complete trades, matched region; xp — half-sized
(condition-high, ON) vs full-sized (OFF) entries. The decisive column is
the matched-sessions ON-minus-OFF column: criterion 1 names it, criterion
2 bootstraps it, criterion 3 compares it to the placebos' identical-path
columns, criterion 4 recomputes it at 15bp RT. The cond-minus-base deltas
are DISCLOSURE columns only — the sealed artifact itself stamps them "NOT
the decisive sealed column", and it demonstrates the two readings disagree
(xp criterion 3 is FALSE on the decisive column, TRUE on cond-minus-base).
The wave-1 spec's swap of criterion 2 for a pooled cond-minus-base sign
test is RETIRED here; so are its re-unit-ed floors ("6 fired rebalances
AND 6 OFF", "8 half-sized beats AND 20 total") — those were 3x stronger
and differently-unit-ed than the sealed floors and were never "the sealed
floors".

Bootstrap seed handling, pinned: the sealed round's menu pinned the
bootstrap parameters but no seed, which was fixed deterministically at
`sha256("vrp-cond-sealed-bootstrap-1")` and disclosed. The forward run
does the same, with new bytes declared here:
`vrp-cond-forward-bootstrap-1`. Draw conventions: the sealed round's
(ceil(n/block) moving blocks of 10, starts uniform over 0..n-block,
concatenated, truncated to n; 2000 resamples; one-sided H1 delta > 0;
undefined resamples skipped; p = #{resample delta <= 0}/valid).

## 3. The forward conditioner p_IWM (a successor conditioner, honestly labeled)

### 3.1 r construction (both legs forward-computable)

- `iv30_IWM(D)`: ATM 30-calendar-day interpolated IV from the recorded EOD
  chain snapshot for session D (`standard_occ()` filter; refuse-and-mark on
  any session without a usable surface; never carry stale IV).
- `harvol20_IWM(D)`: the same pooled log-HAR machinery FORECAST-001
  validated (monthly refits, strictly PIT).
- `r_IWM(D) = iv30_IWM(D) / harvol20_IWM(D)`, strictly point in time.

### 3.2 The seam, declared (zero overlap is a fact, not a guard to wait for)

The sealed IV source `artifacts/desk-store/iv-history/vwap_atm.json`
(sha256, per the sealed stamp,
`b7305fd9dd229f26254cc10948608b53bd7e5f6b797b6a59dc9ab65d99b160d4`) is
FROZEN: its window is 2024-08-26..2026-09-03 (452 evaluable IWM
r-observations via the runner's own HAR machinery; 508 sessions in file).
Recorded chains begin 2026-09-22. The two estimators share ZERO sessions;
the 19-session void 2026-09-04..09-21 is covered by no capture anywhere.
There will never be overlap, so the wave-1 guard "no forward card counts
until >= 50 overlapping forward r-observations exist" was VACUOUS as a
vintage control and is RETIRED. What replaces it: a DECLARED VINTAGE BREAK
at 2026-09-03 / 2026-09-22, stated as fact.

### 3.3 What was measured at the seam, and why it is still unidentifiable

Measured read-only by the red team (RT-5, the campaign runner's own
modules; wave2/r1-2-vrp-cond-redteam.md §1), frozen last-32 r-sessions
(~2026-07..09-03) vs forward 32 sessions (09-22..11-13): iv30 mean +6.3%
(chain-mid vs VWAP-atm), harvol20 mean +5.9%, and **r mean +0.4%**
(0.9449 → 0.9490) — the ratio largely self-corrects because HAR vol moved
with IV over the gap. Full frozen r-distribution (n=452): mean 0.973,
p25 0.903, p50 0.966, p75 1.030, p90 1.103; forward r (n=32): min 0.781,
mean 0.949, max 1.091. Two hard caveats stand: (a) it is a 32-observation
sample spanning a real vol move, and with zero overlap the split between
"estimator offset" and "regime shift" is UNIDENTIFIABLE from recorded
data — the +6.3%/+5.9% decomposition could be any mix; (b) even a ±3%
IV-estimator offset with matched harvol moves p_IWM by ~0.10-0.15 against
a distribution whose interquartile range is 0.90-1.03.

### 3.4 The percentile pool, pinned at seal

The registered statistic (runner `percentile`, verbatim docstring): "the
fraction of strictly-lower evaluable r over the trailing 252 sessions
ending AT session (window-inclusive), n >= 126 required" — constants
`PCT_WINDOW = 252`, `PCT_MIN_N = 126`. The registered rule ranks within
the SAME-VINTAGE series. That cannot exist forward (the frozen source is
dead), so the forward percentile is a SUCCESSOR CONDITIONER, p_IWM-F —
never called "the registered p_IWM" — defined as:

- **PRIMARY POOL (the verdict binds to this)**: the registered statistic
  itself — fraction of strictly-lower evaluable r among the trailing 252
  sessions ending at session t (window-inclusive), minimum n = 126 —
  evaluated on the SPLICED series: the frozen vwap_atm-derived IWM
  r-observations through 2026-09-03, then the chain-mid-derived
  r-observations from 2026-09-22 (the declared break; no interpolation
  across the void). Early forward sessions' windows reach into the frozen
  vintage; that is the splice, disclosed. The pool is not thinned by
  schedule status; a session without evaluable r refuses (never stale).
- **SENSITIVITY POOLS (reported at every gate decision, never
  verdict-binding)**:
  (a) mixed full-base CDF — r(t) ranked against all evaluable r in the
  frozen base (452 obs) plus all forward obs to date; and
  (b) forward-only trailing-252 — the registered window computed over
  forward-vintage observations only, reported once n >= 126 forward
  r-obs exist (~2027-07+); before that it is reported `n/a`. (Rank-only-
  within-vintage from a cold start is NOT viable as a primary — its first
  year of percentiles is noise on n<50, observed swinging 0.0→1.0 on the
  current tail — which is why the primary splices and this pool is
  sensitivity.)

Pool-choice sensitivity is pre-committed as an irreducible band: measured
within the frozen vintage (last 60 frozen sessions, RT-5), mean
|p_trailing252 − p_fullbase| = **0.081**, max **0.168** percentile
points. Any gate decision sitting within ±0.17 of the 2/3 threshold is
pool-choice-dependent and must be reported as such; the verdict still
binds to the primary, and the band is disclosed wherever the verdict is
reported. Threshold semantics as registered: gate ON iff p >= 2/3
(`TER_HI`); xe reads p at t, xp reads p at t-1.

### 3.5 Warm-up (a warm-up, not a vintage control)

No card is conditioned until **>= 50 forward r-observations** exist. That
count is a WARM-UP giving the forward tail minimum mass inside the primary
window; it is not a seam test (§3.2 killed the only honest reading of
that). Before it is met, cards are recorded and marked
`DEFERRED-UNCONDITIONED`, excluded from both numerator and denominator of
any criterion needing p. At ~4.2 obs/week (32 in 7.5 weeks, with outages),
the 50th observation lands in late January: the 2026-12-01 and 2027-01
rebalances are DEFERRED-UNCONDITIONED and the **first conditioned
rebalance is 2027-02-01**.

### 3.6 Seam diagnostic (pre-committed; NOT_EVALUABLE, never pass/fail)

At each 6-month interim: take the W = min(63, n_forward) most recent
forward r-observations, rank them within the frozen 452-observation base
CDF, and compute their mean percentile M_fwd. Compare M_fwd against the
empirical distribution of mean percentiles of all consecutive W-length
windows of the frozen base. If M_fwd falls outside that empirical
[5th, 95th] percentile band, the seam is declared NON-STATIONARY and the
verdict at the next floor check is **NOT_EVALUABLE** (never pass/fail) —
the honest outcome the wave-1 spec itself anticipated. Deterministic, no
new seed, no fitted model. (On the current 32-obs tail the r-mean sits
+0.4% from the frozen last-32 mean — inside any plausible band; the
diagnostic exists for the regime-shift case, not this one.)

### 3.7 The desk's live `iv_rank` is NOT this conditioner (labeled)

Every `desk features` block since 2026-09-22 carries `iv_rank` with
`current_method: "chain mid ATM 30d"`, `history_method: "IVHIST-001 VWAP
ATM 30d"`, `history_label: "unlabeled"` — a mixed-vintage, IV-LEVEL
percentile with no shift adjustment, emitted by `desk/surface.py`
(RANK_WINDOW 252, RANK_MIN_N 120). It is not r-based, not pool-pinned
here, and not read by this registration. The measured disagreement
between it and the r-based mixed CDF on IWM (mean mixed iv-level
percentile 0.274 vs r-based 0.456 over the same 32 sessions — ~0.18
apart) is itself a measure of how much construction choice matters.
Fixing that labeling is a future desk change, out of scope here.

## 4. Cards, accrual, contamination boundary

- **Registration timestamp** = the commit that adds this file (2026-11-14).
  **The accrual starts at registration.**
- **Cards**: every XSMOM monthly rebalance and every PEAD beat sealed after
  registration. xe: record the gate state at t (the nominee as registered
  WITHHOLDS entries when the gate is OFF — abstention is the rule, not a
  defect). xp: record p_IWM(t-1) and the size it dictates. The bare
  comparator (identical cards, unconditioned) accrues on the same path for
  free. Costs: the house card convention (signal-close entry, hold-20, 5bp
  RT primary / 15bp robustness); xe's withholding inherently charges RT
  only on ON entries — exactly what the sealed on-minus-off measured.
- **Operational definitions (pinned)**: a "fired rebalance" is a monthly
  XSMOM-TOP3 rebalance sealed under the house seal discipline on the
  byte-pinned calendar's first-session-of-month rule (the sealed round's
  own calendar-derived convention). A "beat" is a PEAD beat event —
  report-session move >= +1.5% on the first post-report session of a
  sealed-calendar report (the CRON PEAD-BIGSURPRISE construction).
- **Data gate, declared**: the PEAD feed runs through
  `desk-events.service`, currently FAILED (owner flag, 2026-11-14). xp
  accrual is data-gated until it is repaired; xe accrual is unaffected
  (xsmom drafts come from desk-eod-equity).
- **Excluded pre-registration observations, disclosed**: two forward gate
  states are already scorable today (computed read-only by RT-5 under the
  mixed CDF): p_IWM(2026-10-01) = 0.546 (gate OFF) and p_IWM(2026-11-02)
  = **0.772 (gate ON)**. They are pre-registration observations —
  observed-but-unscored, EXCLUDED from the accrual, never counted toward
  floors or criteria. The 2026-11-02 rebalance is paper-trades Card 8
  (sealed 2026-11-14, the same day as this registration; the first
  de-facto ON cohort): its outcome resolves 2026-12-01 and stays excluded.
- **Residual freedom, disclosed**: this construction is being fixed with
  32 sessions of forward r and one ON rebalance already known. The
  commitment at seal: no re-parameterization of the CDF, pools, threshold
  semantics or refusal rules after this seal; one sha-pinned builder; any
  change restarts under a new pre-registration id.

## 5. Placebos (VERBATIM from the sealed round; per-name, not market-level)

From `sealed-round.json` `placebos.*.gate_rule`, quoted exactly, with the
base configs' registered semantics (runner `GATE_RULES`):

- **xe-lag21** — "placebo: xe-book-lo's rule with pbar lagged 21
  sessions", where xe-book-lo is "XSMOM entry: fire the month's top-3 only
  if pbar(t) <= 1/3" and `pbar(t)` is the mean of p_i(t) over the
  rebalance's top-3 picks — a book-level cross-name mean of PER-NAME
  percentiles, not p_IWM. Forward re-run: compute per-name p_i (the §3.4
  primary construction, per name — chains are recorded for every panel
  name each session) for the top-3 picks, form pbar, lag it 21 sessions,
  apply xe-book-lo's threshold.
- **xp-lag21** — "placebo: xp-name-lo's rule with p_i lagged 21 sessions",
  where xp-name-lo is "PEAD entry: fire the beat only if p_i(t-1) <= 1/3".
  Forward re-run: the PER-NAME p_i for the beating name, lagged 21
  sessions.
- **xp-shuffle** — "placebo: xp-name-lo's rule with p_i permuted across
  reporter-events by sha256(vrp-cond-xp-shuffle-1)". Same seed bytes, same
  object (per-name p_i across reporter-events) — a cross-event permutation
  of per-name percentiles, NOT a market-level date shuffle.

Criterion 3 keeps its "exceeds BOTH" wording; per-nominee families are
xe → {xe-lag21}, xp → {xp-lag21 AND xp-shuffle}. Identical-path guarantee:
same cards, same dates, same costs, same forward p construction — only the
conditioner input differs. The wave-1 formulation "re-run with the same
forward p_IWM construction" silently redefined this family to
market-level placebos and is RETIRED. ETF names (incl. IWM) are never
schedule-withheld; PEAD conditions read p at t-1 — both runner conventions
carry over.

## 6. Verdict procedure (one scored run)

- Floors unmet at any interim = **NOT_EVALUABLE, never pass/fail**; interim
  reads are report-only at 6-month marks (first ~2027-05, carrying the §3.6
  seam diagnostic).
- ONE scored run per nominee when its floor is met (or when declared
  unresolvable, §7): the sealed criteria 1-4 of §2, verbatim, on the
  accrued forward cards, decisive column per §2, bootstrap seed bytes
  `vrp-cond-forward-bootstrap-1`, placebos per §5 on the identical path.
- A seam-diagnostic NON-STATIONARY declaration (§3.6) forces NOT_EVALUABLE
  at the next floor check.
- Multiplicity: exactly the two nominees + three placebos. Any new
  conditioning idea is a successor pre-registration, not an amendment.
- Nothing promotes by code; a PASS is the first evidence that could
  support an owner ruling. Promotion, if ever sought, runs through
  PROMOTION-RULE.md's >= 20 forward sealed cards — a separate gate from
  these floors.

## 7. Power and realism (measured inputs; honest ETAs)

Fire-rate inputs, all measured: tuning-era gate ON at rebalances 3/8 =
0.375 (p_IWM 0.740 / 0.764 / 0.944 on the three ON months); the
registered rule on the frozen last-12m sessions: 28.3% of sessions
p >= 2/3; the mixed CDF on the 32 forward sessions: 25% of sessions, 1/2
rebalances. Use pi ≈ 0.30 [0.25, 0.375]. (The in-sample "0/6 for xe" is
an outcome, not a rate.)

- Warm-up ⇒ first conditioned rebalance **2027-02-01** (§3.5).
- **xe under the SEALED floor** (6 ON entries = 2 ON rebalances):
  2/pi ≈ 5-7 rebalances from the first conditioned one ⇒ **~mid-2027**.
- **xp under the SEALED floor** (8 fired trades): ~7 months at the
  campaign-era ~1.2 beats/month ⇒ **~late-2027** — but ONLY if
  desk-events is un-FAILED; with the feed down it does not resolve, and
  **2028** is the realistic band.
- Recording reality: the chain corpus is **33 EOD sessions**
  (2026-09-22..2026-11-13) plus 9 clock-probe snapshot dirs that are NOT
  sessions; 2026-09-23 and the whole 2026-10-26..10-31 week are missing;
  one features gap (2026-10-23). Outages extend every observation-count
  guard roughly pro-rata, and the never-carry-stale-IV rule turns gaps
  into DEFERRED cards.
- The honest default: the most likely verdict at every checkpoint through
  2027 is NOT_EVALUABLE. That is correct behavior, not a design failure —
  the alternative is scoring overlays on burned history, which the program
  has already had to deflate three times.

## 8. MACHINERY (new code, owner-gated to deploy)

No gate-state emitter exists today: nothing in `src/`, `scripts/` or
`deploy/desk/` computes or emits xe-mkt-hi / xp-size-mkt gate states, and
REPORT.md item 8 ("keep the monitors running...") is a PENDING operator
decision, not a running system. The builder, the card-ledger annotation
and the placebo path are ALL NEW CODE under this registration, sha-pinned
before first conditioned use. The emitter must, per recorded chain
session D:

1. compute `iv30_IWM(D)` per §3.1 from the EOD chain snapshot;
2. compute `harvol20_IWM(D)` per §3.1;
3. `r_IWM(D) = iv30/harvol20`, appended to the spliced series (frozen base
   through 2026-09-03, forward from 2026-09-22);
4. compute p under the PRIMARY pool and both sensitivity pools, with n,
   status and refusal marks per pool;
5. write ONE JSON per session into the vrp-cond state area (e.g.
   `<TREX_DESK_STATE>/vrp-cond-forward/gates/<D>.json`): date, iv30,
   harvol20, r, per-pool {p, n, status, threshold read (ON iff p >= 2/3)},
   input shas (chain file, panel state) and the builder's own sha256;
6. per card event (xe rebalance seal, xp beat seal): append the gate state
   at t (xe) / p_IWM(t-1) and the dictated size (xp) to a card-ledger
   annotation in the same state area.

Read-only with respect to the desk store and the paper book; REPORT-ONLY
gates with no trading semantics — the emitter never gates, sizes, pauses,
refuses or prices an actual card, and never edits sealed cards or
LEDGER.md. Deploy of any timer/unit is an owner action and is NOT
authorized by this document; REPORT item 8 remains the pending decision
this section feeds. The one-shot verdict scorer (§6) is separate code,
run once at floors, consolidator-supervised.

## 9. What was seen before sealing

- The full sealed round and every tuning artifact of scope c09-vrp-e
  (20 round-1 cells + 5 sealed rows; IV window 2024-08-26..2026-09-03 with
  tuning entries to 2026-04-24; sealed ordinals 443-505). Nothing after
  2026-08-31 was conditioned by that family.
- The pre-seal forward tail: 32 forward r-observations (2026-09-22..11-13,
  RT-5's read-only pass, cross-validated against the desk's own features
  blocks: r-mean 0.949 vs 0.9466) and the two gate reads of §4
  (OFF@10-01, ON@11-02).
- The desk's mixed-vintage `iv_rank` percentiles, readable in the desk
  store by any desk agent since 2026-09-22 (§3.7). No gate read has
  surfaced in any digest, RESEARCH-LEDGER row or state file through
  2026-11-14 (checked by RT-5).
- IWM is the one name IVHIST-001 rated `ok`; chains are recorded for all
  panel names every session, so per-name placebos (§5) are forward-
  computable.

## 10. Banned / burned cross-check

Neither nominee nor VRP-style conditioning is on the BANNED list
(`src/tree_options/desk/signals.py` L57-82, verified by RT-5); the shape
is CONTEXT_ONLY — conditions may shape a structure but never pick a
direction or a name. xe gates whether an already-fired allowed signal
enters; xp scales size. Names, direction and timing come 100% from
`xsmom_top3` / `pead_beat`. Auto-reject tripwires, inherited verbatim: any
per-NAME VRP conditioning, or any use of p_IWM to pick entries into a
different rule, = direction signal = dead. The prior is and stays
skeptical: a +14.96% tuning delta that never fired in the sealed window,
sign-flipped from the hypothesis, with a +21.4% placebo at n_ON=1 in the
family — which is exactly why the floors, the decisive column and the
placebos above are the sealed ones, unchanged.

## 11. Consequences (fixed now)

- Report-only until floors; no verdict changes any playbook row, drift
  weight, roster or rail; nothing promotes by code.
- The accrual's own report path is the state area of §8 plus 6-month
  report-only interims; at most two promotion claims reach the operator in
  any 30-day window (the 2026-09-30 pattern, inherited).
- This registration consumes no further history: no historical window is
  scored, re-scored or re-litigated by it or by its machinery.
