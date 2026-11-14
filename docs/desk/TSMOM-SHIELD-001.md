# TSMOM-SHIELD-001: TS-momentum defensive half-size throttle on the live card book (PRE-REGISTRATION)

Written 2026-11-14, before any overlay-eligible card exists and before any
overlay statistic has ever been computed. Sealed by `TSMOM-SHIELD-001.md.sha256`
(the sha256 of this file, recorded in the commit that adds it). PAPER-ONLY.
Monitors/timers are owner-gated deployments and are out of scope of this
document; this seal fixes the RULE, its bars, and its coupling, nothing else.

**Coupled pair.** This prereg and `EB-RECHECK-001.md` (sealed the same day)
are ONE registration unit: they share one premise (the ledger's surviving
TS-momentum reading — window-level downside protection, RESEARCH-LEDGER
L56-61; MOM60-REPL rows 1-2: conditional edge +5.56% / +1.82% in the only two
half-year windows where the hold-base lost −6.77% / −5.10%), and EB-RECHECK is
the single premise/verdict channel for both. **No independent-evidence claim
is made anywhere in this pair**: both estimands descend from the same two 2022
windows of the same frozen grid on the same panel family, so a joint pass is
correlated evidence about ONE premise, never two confirmations. Every digest
that mentions either document mentions both, with this sentence.

Pinned at seal (sha256, first 16 hex):
- Card 8 (card #1 of record for XSMOM, sealed 2026-11-14, AMD/LLY/INTC,
  2026-11-02 close -> 2026-12-01 close):
  `c70acd6883b8b27f89436154f1f080a52143b4911d31b7dbe3408bc070eb1891`
- panel `artifacts/paper-trades/ohlc-panel.json` @ 2026-11-13: `9fbde932dfade93e…`
- frozen grid `artifacts/paper-trades/mom60_repl.py`: `1c7403b9e1f0e7a4…`
- wave-1 spec `~/.local/state/trex-strategy-fanout-20261114/wave1/r1-overlays.md` §C1: `f373e660ec29d250…`
- red-team verdict `…/wave2/r1-1-r2-1-tsmom-pair-redteam.md` (RT-3, MAJOR-REVISION,
  revisions R1-R6 — all applied here): `b2206fb6ff8f9b28…`

## Why forward-only

The entire 2021-09..2026-09 panel is read: HORIZON-001(a) scored 16 ts-mom
cells on it, MOM60-REPL re-split the 60d-long-h60 cell into 10 half-year
windows (the source of the two protection readings), and the ledger deflated
the family (L54-61). Any overlay score on that history would be tuned on seen
data. The one honest test is the live forward card path, which the TS-mom
family never touched (wave1: xsmom forward from 2026-10-01, pead from
2026-09-25; first sealed XSMOM card = Card 8, 2026-11-14). RT-3 verified no
tsmom/overlay state or scoring artifact exists anywhere on disk — the
estimand has never been run on history.

## The estimand (protection delta, not alpha)

The allowed signals (`xsmom_top3` monthly, 3 entries/rebalance, sealed
no-skip construction per the 2026-09-23 ruling; `pead_beat` beats >= +1.5%,
hold 20) still pick 100% of names, directions and entries. The overlay only
scales the SIZE of an already-fired entry, and the claim is exclusively the
protected-vs-bare delta in drawdown episodes. A positive verdict does not
license the overlay to point a trade, ever (CONTEXT_ONLY tier; see banned
cross-check).

## Binding premise clause (RT-3 R1 — the overlay may NOT outlive its premise)

EB-RECHECK-001's outcomes bind this monitor definitively:

| EB-RECHECK outcome | binding consequence for TSMOM-SHIELD-001 |
|---|---|
| premise VOIDED (consistency re-anchor fails: the re-derived table does not reproduce rows 1-2 as the only base<=−4.0% windows) | **auto-retire**: stop accrual, mark all accrued data motivation-void, NO resolution run; the standing question closes that way |
| first fired cell cond <= 0 (DOWNGRADED, closed-unless-reopened) | accrual CONTINUES but any resolution verdict is permanently labelled **PROVISIONAL-PANEL-FAILED** and may never be cited without that label |
| CONFIRMED (>=2 fired cells, all positive, pooled sign-flip p < 0.05) | accrual priority restored; the overlay's own 30-entry bar proceeds unchanged |

This table replaces the wave-1 one-directional coupling ("loses priority"):
a downgrade does not kill the monitor, but its verdict can never be quoted
clean. The pair registers as ONE reporting unit; the joint disclosure
sentence (header) appears in every digest. The book-inherited retire clause
also stands: if the bare book retires under its pre-registered
kill-switches (RESEARCH-LEDGER DESK-CHECKS), the overlay retires with it.

## Exact information set

At a card's entry session t (the session the sealed rule fires):
- `close_N(t)` and `close_N(t-60)` for the card's entry name N — 60 NYSE
  sessions, panel `ohlc-panel.json` under the shared flock;
- nothing else. No VIX, no per-name chains, no LLM output, no recorded states.

## Deterministic rule (one config, no grid)

```
DEF_N(t) = sign( close_N(t) / close_N(t-60) - 1 )
size(card) = 1.0  if DEF_N(t) > 0          # defensive OFF — identical to bare
size(card) = 0.5  if DEF_N(t) < 0          # defensive ON — half size, entry still taken
size(card) = 1.0  if DEF_N(t) = UNKNOWN    # hole guard: never throttle on missing data
```

- Hole guard (STRICTER than the burned cells, and conservative): the burned
  guard was "no >10-calendar-day gap in [i−60, i+60]"; this overlay's is
  "name missing ANY session of the window -> UNKNOWN -> full size". RT-3 C3:
  the guard difference biases toward FULL size for hole-prone names —
  harmless on 36 large-cap tradables, stated here.
- Formula-level continuity, not byte-level (RT-3 R6): DEF is the burned
  cells' own construction (`mom60_repl.py`: signal = c[i]/c[i−60] − 1 > 0;
  the "skip was never a skip" ruling, RESEARCH-LEDGER L233-243) — but the
  panel was rebuilt 2026-09-23 (39 names, 2021-09-27 start), so bytes differ
  from the published run. "Exact construction" is NOT claimed.
- HALF SIZE, not skip: a skip-gate would make DEF an entry-picker (the
  term-gate failure mode); a size cut keeps every entry the allowed signal
  made. Monotone-reduce-only: size in {0.5, 1.0}; never adds exposure, never
  selects a name, never flips a sign, never touches an exit (hold-20 stands;
  exit-grid-2 closed the exit question).
- Applies identically to XSMOM-TOP3 and PEAD-beat entries. **Cards already
  live at this seal are untouched (no retro-throttle).** Card 8 (sealed
  earlier on 2026-11-14) is the CLOCK ANCHOR only: card #1 of record for
  XSMOM, resolving 2026-12-01. The overlay binds from the first card sealed
  AFTER this registration (next XSMOM rebalance: entry 2026-12-01).
- Descriptive secondary, never a selector: the SPY aggregate
  `sign(close_SPY(t)/close_SPY(t-60)-1)` — see placebos.

### Why half, pinned (RT-3 R3)

0.5 is HOUSE CONVENTION, not a performance choice: the vrp-cond `xp-size-mkt`
half-size nominee, the wave-1 C3/C4 half actions, and plan-D5 all use it. No
in-sample overlay scoring has ever existed to choose it (RT-3 C3 verified none
on disk); the power arithmetic below ("0.5 x (−6%) ≈ +3%") is arithmetic on
the published 2022 windows, not a tuning pass. Unrevisable without a new
prereg id.

### Researcher degrees of freedom consumed (census, RT-3 R3)

Exactly five constants, all fixed at this seal: the −4.0% trigger
(EB-RECHECK's, declared there), the −3% down-episode cut, the 0.5 size, the
20-session placebo block, the 200 placebo seeds. Any change to any of them
restarts under a new prereg id.

### Construction vs scoring — the burned-era line, verbatim (RT-3 R6)

Panel sessions dated <= 2026-09-11 may feed DEF inputs only (construction
warm-up); scored outcomes begin at the first overlay-eligible card; no
overlay statistic is ever computed on pre-registration outcomes. Concretely:
the first eligible rebalance (entry 2026-12-01) has its 60-session lookback
land on ≈ 2026-09-08, inside the burned panel window (which ends 2026-09-11)
— each of that card's up-to-3 per-name DEF inputs reads one burned-era close
as INPUT, and nothing more (RT-3 C7).

## Costs

House census convention (signal-close entry, close-to-close hold 20, 5bp RT
primary / 15bp robustness). The overlay changes size, not price or turnover:
both legs carry identical per-unit costs; deltas reported at 5bp and 15bp. No
crossing-instrument claims.

## Null + placebo design (pre-registered, seed 20261114)

- Primary metric: per-card delta = size(card) x bare_card_return −
  bare_card_return = −0.5 x bare_card_return for throttled cards, 0
  otherwise; book-level delta = sum.
- Down-episode: a calendar month where the BARE book's card mean < −3% (the
  census cut above). Protection claim = mean per-card delta > 0 within
  down-episodes.
- Placebos (200 seeds, identical card path): (i) random throttles matching
  the observed ON-fraction, day-blocked in 20-session blocks; (ii) the
  INVERTED overlay (throttle when DEF_N > 0) as the sign check; (iii) the
  SPY-aggregate throttle — **promoted by RT-3 R4 from descriptive to a
  REPORTED comparator at the resolution run (not a gate)**, so a pass can be
  read as name-level information vs plain beta throttling.
- Pass requires the down-episode delta to exceed the 95th percentile of
  placebo (i) AND beat its own inverted twin.

## Card unit, clocks, and bars (RT-3 R2 — the ambiguity resolved)

- **The card unit is the ENTRY (per-name card).** 30 overlay-eligible
  ENTRIES ≈ 10 XSMOM rebalances ≈ 10 months from the 2026-12-01 rebalance →
  the optimistic resolution date is **~2027-10** — and it is labelled the
  OPTIMISTIC case: it assumes down-episodes cooperated. Counted as sealed
  DOCS instead, the same bar is 30 months → **~2029-11**; that reading is
  rejected here (unit = entry), but both are stated so no future operator can
  shop the unit (RT-3 C5).
- The binding clock is DOWN-EPISODES, not cards: protocol-era rate 13/46
  months ≤ −3%; on a calm tape the honest outcome at entry 30 is
  NOT_EVALUABLE-OPEN, extension to entry 45 maximum, and the expected
  resolution (any verdict) is **2028+**.
- **Provisionality (RT-3 C5, clock ordering):** EB-RECHECK's first fired cell
  reads ~2027-06 at the earliest (only if window 11's base <= −4%) and 2028+
  in the base case — so any overlay verdict will likely PRECEDE the premise
  test. Every overlay verdict issued before EB-RECHECK's first fired cell
  reads (or before the premise voids) is **PROVISIONAL on the premise
  channel**, and carries the binding-premise label above if the premise then
  fails.
- FAIL at resolution if: overall mean per-card delta < 0, OR down-episode
  mean delta <= 0, OR either placebo comparison misses.
- Interim: report-only digest at every 10 cards. One scored run at
  resolution; any re-parameterization (cut, window, action) restarts under a
  new prereg id (amendment discipline per AI-BEAR-002: amendments are
  separately sealed docs, never edits to this file).

## Power analysis (honest, LARGE-effect-only)

Card flow ~3 XSMOM entries/month + ~0.5-2 PEAD beats/month (desk-events
currently FAILED suppresses the PEAD term to zero). Effect size needed: the
2022 cells showed +5.6%/+1.8% conditional cushions; at half size a bad-month
per-card delta is ~0.5 x (−6%) ≈ +3% protected. With ~3-5 cards per
down-episode and day-clustered noise, 3 episodes give a coarse but real read;
5+ (entry ~45) is the comfortable bar. Blocked-random throttles straddling
episodes is the point of the null: the edge is in-episode alignment, the null
distribution over the identical tiny card set is the honest reference, and
the design detects LARGE effects only (RT-3 C4 concurrence).

## Banned-list cross-check

`ts_momentum` and `mom60_h60` sit in `signals.py` BANNED **as direction
signals**; the ban's text and the ledger's surviving reading both leave the
defensive-overlay use. This prereg proves it is not a disguised direction
signal by construction: (1) evaluated only on cards the ALLOWED signals
fired — it cannot create an entry; (2) monotone-reduce-only; (3) never
selects among names (same DEF rule for every name, applied after selection);
(4) never flips sign (no short leg; `short_any` banned); (5) never touches
exits. Registration home: CONTEXT_ONLY tier (the `trend` precedent);
promotion, if ever, is to CONTEXT_ONLY status, never ALLOWED_DIRECTION.
Auto-reject tripwire: any downstream use where DEF_N gates WHETHER to enter,
points a trade, or picks among names = direction signal = dead.

## Machinery (owner-gated; out of scope here)

One sealed prereg doc (this file) + a nightly annotation stage beside
`eod_equity.py` writing, per session, DEF per panel name and per card at
entry the would-be size — into a NEW state dir
(`~/.local/state/trex-desk/overlay/tsmom-shield/`), never touching live card
state, drafts or LEDGER. **The sealer stays blind to overlay state when
sealing** (RT-3 C6b): sizes are hypothetical, written to separate state, and
the bare book never reads them. Evaluation is a one-shot scorer at the bar,
consolidator-supervised. Deploy of any timer unit is an owner action.

## What was seen before sealing

- The published cells cited as motivation only: HORIZON-001(a) (16 cells),
  MOM60-REPL (10 windows, the two 2022 protection readings), RESEARCH-LEDGER
  L54-61 deflation. Zero new historical scoring; zero overlay artifacts ever
  run (RT-3 C3/C6 verified).
- The live book state on 2026-11-14: 33 signal sessions since 2026-09-23;
  sealed-card chain: Card 8 sealed this morning (untouched by the overlay,
  clock anchor); desk-events FAILED (PEAD feed throttled).
- Card 8's draft-and-seal history (late administrative seal, disclosed
  in-card): its entry prices were observable at this seal; no overlay size
  was computed for it and none ever will be (no retro-throttle).
