# SIGNAL-CARDS-001: the signal-gated QSL-sibling forward card queue (PRE-REGISTRATION)

Written 2026-11-14, before any signal-gated option card is enqueued. Sealed
by `SIGNAL-CARDS-001.md.sha256` (the sha256 of this file, recorded in the
commit that adds it). Queue rows written by the future builder carry
prereg id **`QSL-SIGNAL-20261114`** and this file's sha256, exactly as QSL
rows carry `QSL-20260930` (the lane's `registration_timing` anti-backfill
rule is inherited verbatim: cards exist only for recorded sessions on or
after this seal, and re-running the builder later cannot create them).
PAPER-ONLY. Monitors/timers are owner-gated deployments, out of scope here.

Pinned at seal (sha256, first 16 hex):
- wave-1 spec `~/.local/state/trex-strategy-fanout-20261114/wave1/o2-options-sleeves.md` §O2-4: `7d456451efaf280e…`
- red-team verdict `…/wave2/o2-4-n1-1-forward-preregs-redteam.md` (RT-2,
  MAJOR-REVISION — all O2-4 required revisions applied here): `17cad66f46d4a802…`
- QSL prereg `docs/desk/QSL-PREREGISTRATION.md` (idioms adopted): `390606d99a3169b0…`
- exploratory replay artifact `artifacts/desk-store/evaluations/
  historical-replay/replay-20260927T081700Z-6f2f03a2bd1f.json`
  (see "What was seen before sealing"): `6f2f03a2bd1fe9f1…`

## The premise, stated truthfully (RT-2 revision 1)

The program's only promotion route for options expressions is forward sealed
cards (>= 20 resolved per family), and **the SIGNAL-GATED route is empty**:
no signal-gated option card or enqueue exists anywhere (RT-2 verified — QSL's
rows are mechanical gate-scans over all names; AI-BEAR-002's cards are
bear-regime signals). The **quote-bearing lane itself is NOT empty and never
was claimed empty by this prereg**: the QSL mechanical lane has been LIVE
since 2026-09-30 (first QSL call-debit mark 2026-10-01) — 25 daily queue
files with **153 admissible deals** (78 `debit_vertical` + 75
`credit_vertical`), and `evidence/qsl.sqlite3` holding **615 objects**
(145 candidates, 75 cohorts, 75 episodes, 260 marks each carrying
`modeled_net_pnl_dollars` at recorded-chain crossings, 25 daily evaluations)
— all measured read-only by RT-2 on 2026-11-14. The wave-1 sentence quoting
"0 rows / 0 evidence rows" was a six-week-stale 2026-09-30 fact copied as
present tense; it is struck. What is missing — and what this queue fills —
is the SIGNAL-GATED route: option cards enqueued only because `xsmom_top3`
or `pead_beat` fired.

## The two arms (deterministic; QSL idioms verbatim where possible)

Per recorded session D (~17:46 ET snapshot, schema `desk-chain/1`), if D is
a fired XSMOM rebalance or carries pead_beat fires: for each fired name,
enqueue ONE bullish call-debit candidate:

- **pead arm (R3 shape):** expiry nearest 90 DTE within [60, 120]; long =
  the strike of that expiry whose |delta| is nearest 0.62; short = the next
  listed strike above (fixed rank rule, not delta-tuned).
- **xsmom arm (R1 shape, single-leg):** expiry nearest 150 DTE within
  [121, 180]; long call |delta| nearest 0.62; NO short leg (the O2-1
  wing-vs-single-leg evidence question in live form; a winged variant is a
  separate pre-registration after O2-1 reports).
- **Rate/dividend convention, pinned:** strike selection reads the `delta`
  and `iv` values recorded on the chain legs by the lane's own surface code
  — the same recorded fields `desk/qsl.py` and `desk/shadows.py` consume.
  No independent Black-Scholes recompute is performed at selection, so no
  rate or dividend source is introduced (RT-2 §2).
- **Tie-breaks, pinned:** |delta| nearest 0.62 — ties to the LOWER strike;
  expiry "nearest N DTE" — ties to the LATER expiry; "next listed strike
  above" — the next strike present on D's own snapshot.
- **Gate (SPEC-2/QSL, both legs, D's own snapshot):** two-sided, sized >= 10
  both sides, oi >= 100, buyer's crossing penalty <= 0.10 x package mid.
  Failures are counted in `refused`, never enqueued.
- **Fill:** crossing prices (never mid), $0.65 per leg per side, qty 1.
- **Exit:** the row's own published time-stop (hold 20 sessions; min_dte 90
  for the xsmom arm), resolved at the deadline session's EOD snapshot,
  censored when absent (QSL's censure rule), marked at crossing.
- **CLOSED CARD, pinned (RT-2 §2):** a card is CLOSED iff it is
  expiry-resolved with its deadline-session mark present. Censored cards
  NEVER count toward the 20-card bar and EXTEND the clock; they are never
  dropped to accelerate a read.
- **Controls (forward null, AI-BEAR-002 idiom, deterministic):** per arm and
  session carrying at least one enqueue attempt, ONE control card,
  same structure, exits, costs and snapshot, on a deterministic non-signal
  name. **Non-signal predicate, pinned:** the draw pool is the gate-passing
  names on D EXCLUDING (i) any name that fired either signal on D, (ii) any
  name in the current month's sealed XSMOM top-3, (iii) any name held on an
  open card of this lane. Draw = `int(sha256("SIGNAL-CARDS-001|<arm>|<D>")
  [:16], 16) mod n` over the name-sorted pool; empty pool → no control that
  session, counted.
- **Control-n expectation, declared (RT-2 §2):** controls accrue one per
  arm-session-with-a-fire. Under the gated clocks below they will OUTNUMBER
  rule cards at the read (xsmom ≈ 35 controls vs 20 rule cards; pead ≈ 48
  vs 20). F3 compares means at deliberately unequal n; the mismatch widens
  the comparison's noise, it does not shift its expectation — declared here,
  before any card.

## Pre-declared bars (AI-BEAR-002 forward tests verbatim)

Per arm, at **>= 20 CLOSED rule cards** (definition above): F1 total net
R > 0; F2 month-clustered one-sided sign-flip p < 0.10 (10,000 draws, seed
20261114 — at n=20 this needs >= 14/20 positive cards, exact p(14)=0.058:
only real edges clear); F3 mean R above the control cards' mean R. PENDING
before that; interim reports at 10 cards are report-only. Any
re-parameterization restarts the clock under a new prereg id (amendment
discipline per AI-BEAR-002: separately sealed docs, never edits here).

## The clocks, owned (RT-2 revision 4 — gated arithmetic, multi-year horizons)

The wave-1 clocks ("xsmom ~7 months; verdicts 7-18 months out") assumed 100%
gate pass and are **2-3x optimistic — struck**. Recomputed with the lane's
own measured pass-rates (singles 42% at 60-120 DTE, 19% at 121-180; ETFs
11-12% and mostly FAIL the 10% penalty gate):

| arm | fires/month | gate pass | cards/month | months to 20 closed | first promotable read |
|---|---|---|---|---|---|
| xsmom (121-180) | 3 | ~0.19 | ~0.57 | **~35** | **~2029-10** |
| pead (60-120) | ~1.16 beats (2 in 7.5 weeks; Poisson 95% CI spans ~3x-to-huge) | ~0.42 | ~0.49 | **~41** | **~2030-03** |

**No verdict inside ~3 years per arm at current density.** Every recorder
miss pushes every clock further (the recorded-chain era already shows a
2026-10-26..10-31 signals hole and `desk-events` is FAILED). This lane's
value is the INSTRUMENT, not near-term verdicts: real crossing economics
and gate pass-rates accrue as forward findings from day one — which
supersedes the wave-1 Rank-3 "verdicts are 7-18 months out" rationale.

### Refusal stratum (pre-declared, RT-2 revision 6)

Gate-refused signal names accrue as a mid-marked refusal stratum
(report-only; no crossing verdict attaches to them): the attrition rate
itself gets a forward CI, so gate thinning is evidence rather than only a
slower clock.

## Family accounting (RT-2 revision 3 — 16 prior looks, counted honestly)

Family = options expressions of the two live direction signals. Prior
signal×option looks: **16 = 6 observed exploratory cells (the 2026-09-27
replay, below) + 10 registered-unrun cells** (DESK-BT-001's 6 sealed cells
+ campaign vrp-cond scope O's 4 registered cells). The wave-1 text counted
only the 10; corrected here. This lane adds **zero backtest cells** — its
evidence is its own 20-card forward bars. The O2 batch's Holm m=4 covers
ONLY that batch's 4 modeled cells (O2-1 x2, O2-2, O2-3); it does not cover
this lane and does not adjust the 16 prior looks.

## What was seen before sealing (RT-2 revision 2)

- **The 2026-09-27 exploratory replay** (`docs/desk/STRATEGY-REVIEW-20260927.md`,
  artifact `replay-20260927T081700Z-6f2f03a2bd1f.json`; window
  2024-09-03..2026-08-31, 26 evaluable modeled trades): six signal×structure
  cells on the SAME two signals — xsmom/long-call 1 obs −$119.13;
  xsmom/call-debit 9 obs, 5 wins, −$14.65; **xsmom/put-credit 9 obs, 5 wins,
  +$398.66 (the favorable cell)**; pead/long-call 3 obs −$329.07;
  pead/call-debit 4 obs −$387.06; pead/put-credit 0 obs. **Every call-side
  cell was negative** — the observed prior runs AGAINST this lane's arms,
  which is the honest direction for a forward test. The review doc's own
  conclusion stands: "picking the best one from this artifact is selection
  on the same data."
- **Standing fence on the favorable cell:** this lane is CALL DEBITS ONLY
  (`credit_open_not_supported_v1`); it contains no put structure and no
  credit open. **If credit opens are ever supported, the favorable
  XSMOM/put-credit cell (9 obs) may NOT serve as confirmatory prior for any
  credit-open sleeve without a fresh pre-registration disclosing it.** This
  fence is standing and binds future lanes, not just this one.
- **QSL episode observability on pick names:** the mechanical (non-signal)
  QSL episodes include put-credit and call-debit deals on XSMOM pick names
  (INTC in both recorded months) at 7-35 DTE — different tenor band,
  mechanical selection, six weeks of observable crossing-P&L on pick names
  existed before this seal. No contamination: cards sealed after this prereg
  differ in structure, tenor and gating.
- The measured gate pass-rates (42% / 19% / 11-12%; n = 390/924 and 174/914)
  and half-spread tables of the wave-1 O2 lane — lane-measured 2026-11-14,
  accepted by RT-2 as lane evidence.
- The sealed equity chain state: Card 8 (XSMOM card #1 of record) sealed
  2026-11-14; both chain-era XSMOM rebalances (2026-10-01, 2026-11-02) and
  both beats (2026-09-25, 2026-10-30) sit in the store unexploited — 8
  enqueue candidates would exist retroactively from this seal forward ONLY;
  **no backfill**.

## Banned-list cross-check

| banned entry | used? | how avoided |
|---|---|---|
| short_any / bear rows | no | call debits only; the pead arm's short call wing is inside a net-debit vertical |
| credit opens | no | `credit_open_not_supported_v1`; call debits only, standing fence above |
| exit re-search | no | published time-stops of R1/R3 verbatim |
| model-chosen entries | no | fully deterministic; the LLM advisory lane stays untouched |
| vol_state gates / vol conditioning | no | the UNCONDITIONAL signal is traded — what the equity evidence supports; R1's warm-up exemption and R3's `any` are sidestepped, not re-parameterized |
| xsmom_short_leg / 60skip5 / mom60_h60 / ts_momentum | no | sealed 273 no-skip rule fires the enqueue; no mom-family construct anywhere |

## Machinery (owner-gated; out of scope here)

QSL-sibling: a `desk/qsl.py`-pattern builder writing a second queue under
prereg id `QSL-SIGNAL-20261114`, consumed by the already-live `desk/shadows.py`
evidence lane into its own sqlite. No engine, no campaign, no repo state
mutation beyond this sealed pre-registration. This is the one lane whose
clock cannot be reclaimed later — every week of delay moves the first
promotable read — but the deployment itself (any timer unit) is an owner
action.

## Known limits

Snapshot (~17:46 ET) crossing economics, conservative vs session fills;
recorder misses push every clock; single-name SOXX-class ETF legs mostly
fail the gate (measured) — the xsmom arm logs them refused, never
substitutes; the pead density estimate rests on 2 observed beats in 7.5
weeks (CI spans a ~3x-to-huge range); the control pool at 121-180 DTE
averages ~174 gate-passing names per session, so exclusions never empty it.
