# Trade-agent GEPA game — exploration memo (NOT a registration)

Written 2026-09-23. Status: EXPLORATION. Nothing here is sealed, registered,
or run. No episode was fetched, evolved, or scored; no minute bar exists on
disk (verified: `artifacts/intraday-cache/` is absent). This memo proposes
research program #3 — a GEPA-evolved LLM agent playing an intraday
defined-risk trading game — as a candidate family with a minimal viable
experiment (MVE) for the operator to accept, amend, or reject; if accepted,
the pre-registration is a NEW sealed document with its own sha256, in the
FORECAST-001 manner, and a trial row in the sqlite registry BEFORE any
episode is evaluated.

Inspected for this memo and nothing else: `RESEARCH-LEDGER.md` (EXEC-001,
survivors, banned families), `src/tree_options/desk/signals.py` (the
ALLOWED_DIRECTION / CONTEXT_ONLY / BANNED sets), `research_protocol.yaml`,
`artifacts/paper-trades/fetch_ohlc.py` and `intraday_exit_pilot.py`
(vendor and cache conventions), `src/tree_options/guards/fills.py` +
`ledger/fees.py` (fill and fee semantics), `data/desk/events/macro-2026-2027.json`,
`earnings-calendar.json` / `earnings-timing.json`, the desk-store layout,
`docs/desk/FORECAST-001.md` + `-results.md`, and the sibling
`docs/theory/trade-jepa-exploration-2026-09-23.md`.

## 1. Concept mapping

GEPA (Agrawal et al.; the completed DSPy 3.3.1 campaign on this machine is
the operational reference) is reflective evolutionary prompt optimization:
a population of candidate policies (prompt programs with decoding params)
is evaluated on a fitness; a REFLECTOR model reads evaluation traces and
proposes mutations; a Pareto frontier over a multi-metric fitness is
maintained across generations. The search is the learning. Applied here:

- **candidate policy** = the agent's full decision program: a system prompt
  (policy text + few-shot trace exemplars) plus pinned decoding params
  (temperature, max tokens) plus the action-parser version. ONE predictor
  module (the per-step policy call) so GEPA's reflection unit is the step;
  sibling calls are folded into feedback (trap 2, section 3.7).
- **fitness** = the episode-score vector of section 2.5, computed entirely
  in CODE. No LLM judges anything — the established finding on this machine
  is that flash is the worst judge (balanced acc 0.65) while directing as
  well as Codex; the actor may be cheap and noisy, the scorer never is.
- **reflector** = a strong model (glm-5.3 non-flash, 2-concurrent, or
  MiniMax, weekly-unlimited; never flash).

**The game.** An episode is one name's session sliced into N-minute bars
(N = 15, section 2.2). At each step the agent observes the rolling bar
window, its position/PnL state, and declared point-in-time desk context;
it emits one typed action from a four-action set (all defined-risk). The
episode ends at the session close; the reward is net PnL in units of the
episode risk cap, plus a risk penalty, tracked as a three-metric vector.

**Why this is an EXEC-family candidate, not a signal.** The census already
ran EXEC-001 (GATE-001.md): entering at the NEXT OPEN instead of the signal
close moves the MR book by ±2–4bp — no gap-capture alpha. That is the
fixed-rule null of this family. The agent game asks the next question down
the same axis: can a LEARNED policy time and size entries within the
session better than open-entry, on the same expressions? The direction the
agent may express is never its choice: `desk/signals.py` pins
ALLOWED_DIRECTION = {xsmom_top3, pead_beat} (CONTEXT_ONLY = trend, breadth,
vix_term), and the BANNED registry (volspike, gap_fade/cont, squeeze,
sector_rotation, short_vol_gated, semi_reversion, ts_momentum, mom terciles,
… 20 families) may never point a trade. The harness enforces this
mechanically: on live-signal episodes the only expressible direction is
the declared one (long, defined risk); on plain no-signal days any action
is PAPER-SANDBOX-ONLY (informational, never promotion-grade). The agent
TIMES and SIZES; it never PICKS.

**The discipline statement, fixed now.** The WHOLE GEPA loop — seed
population, generations, minibatch schedule, reflection model, budget caps
— is registered in the trials sqlite as ONE optimizer configuration (git
sha + config hash) BEFORE any episode is evaluated (INV-13). Its output is
a single frozen policy artifact (the winning candidate's prompt text +
decoding params + parser version, sha256-hashed into the trial row). That
artifact alone receives ONE sealed evaluation on the declared outer window
(2026 episodes, section 3.4). Nothing from the outer window — episodes,
bars, outcomes, or reflections — ever feeds the loop. A second loop run,
a different population, or a post-hoc seed swap is a NEW registered config
consuming another inner_loop slot (budget 32 per scope; this program plans
≤ 4).

## 2. Game / episode specification

### 2.1 Episode unit and mix (declared)

Episode = (name, session). Candidate episodes are drawn, stratified and
seeded, from the inner window 2024-09-05 .. 2025-12-31 (the earnings
calendar's span; the panel has 37 names 2021-09-13..2026-09-23):

| stratum | definition | train | val |
|---|---|---|---|
| XSMOM card day | first session of a month, one of that month's top-3 picks (no-skip 273 construction, the sealed rule) | 24 | 8 |
| PEAD beat day | first session after a report whose move >= +1.5% (the sealed beats-only rule) | 24 | 8 |
| plain day | a session with NO live allowed signal for that name | 12 | 4 |

Earnings-timing note: `earnings-timing.json` carries bmo/amc for many
reports but "unknown/estimated" for others (spot-check: AAPL 2026-10-29
`timing: unknown`); episodes split bmo/amc/unknown as a declared stratum
of the observation, and the bmo report session itself is a candidate
episode only through the PEAD stratum's next-session rule (the drift
window, not the print). **Macro days (FOMC/CPI/NFP/OpEx) are EXCLUDED from
the MVE**: `macro-2026-2027.json` covers 2026-01-01..2027-12-31 only, so
no macro episode dates exist for the inner window. A macro-episode variant
is gated on a build item (4.5), not silently mixed in with a train/serve
skew (inner folds would show `macro: null` forever).

### 2.2 Bar cadence (N = 15 primary)

Vendor 1-minute aggregates, aggregated in the harness to N-minute bars.

- **N = 15 (primary):** 390 = 26 x 15 exactly; clean session partition,
  aligned to the quarter-hour quote convention. The operator's
  "~14-minute" ask is honored within one minute; a literal 14 partitions
  390 as 27 bars + a 12-minute stub, which breaks bar-count invariance
  across episodes and biases the terminal decision window — rejected with
  reason, recorded here.
- **N = 5 (sensitivity arm, one separate registered config):** 78 steps;
  triples actor cost and decision density without adding context a policy
  can act on; the exit-grid evidence says finer exit granularity amputates
  tails, so the prior is against it — run only if N = 15 passes.
- **N = 1:** rejected. 390 steps, ~15x cost, microscopic noise, and the
  largest look-ahead surface. Not registered.

### 2.3 Observation per step (all PIT: available_at <= decision)

- rolling window of the last 8 N-minute bars of the episode name
  (compact OHLCV table) + session VWAP-so-far + prev-session
  open/high/low/close from the daily panel (free, no wire);
- position/PnL state: current spread mark, unrealized/realized PnL in R,
  contracts held, entry debit, remaining episode loss budget,
  minutes-to-close;
- desk context, declared daily features only: XSMOM score and card-day
  flag (allowed direction + name), PEAD beat flag and report-move size,
  HAR h = 20 vol forecast (the FORECAST-001 PASS incumbent) with
  prev-session IV30 and the cheap/fair/rich read, vix_term (CONTEXT_ONLY),
  earnings timing (bmo/amc/none), day-of-week;
- lane-3 extension, LATER variant only: the JEPA surprise s_t as one more
  numeric field (section 6).

IV discipline: the session-t IV30 is solved from session-t option VWAPs,
so it is NOT available intraday at t; the harness serves session t-1's
IV. Every field's available_at is pinned in the harness config and
asserted by a future-poison test (below).

### 2.4 Action set (typed, validated in harness code, frozen)

| action | params | constraint |
|---|---|---|
| `no_action` | — | always valid |
| `enter_debit_spread` | `width` in {1, 2, 5} strikes; `dte_bucket` in {30–45, 45–60}; `size` in {1, 2} contracts | long ONLY, in the declared direction on live-signal episodes; total debit <= $500 (the episode risk cap); no entry in the last bar |
| `exit_all` | — | requires a position |
| `exit_half` | — | requires >= 2 contracts |

All expressions are purchased debit spreads: max loss = debit, no naked
shorts (`short_options.policy: prohibited`), and with dte >= 30 no expiry
ever falls inside an episode — expiry/assignment logic is deliberately out
of the game's scope. Action params draw from `option_candidate_defaults`
(dte 30–60, |delta| 0.30–0.60); any malformed, unparseable, or
constraint-violating emission is coerced to `no_action` with a counted
penalty — never dropped (GEPA trap 1).

### 2.5 Reward and the Pareto vector

Per episode, net PnL in R-units (R = the $500 episode risk cap), net of:
$0.65/contract fees ($1/order minimum, `PerContractFeeModel`), the
declared spread penalty (5% of mid per side; 10% as the sensitivity
class), and bar-volume participation caps (4.3). The recorded vector:

1. mean net R per episode (return);
2. max intraday drawdown in R (risk);
3. contracts traded per episode (turnover).

Selection scalarization, fixed before any run:
`fitness = meanR − 0.25 x maxDD_R − 0.01 x contracts_per_episode`.
GEPA maintains the Pareto frontier on the 3-vector; the frozen artifact is
chosen by the declared scalarization on the validation fold.

### 2.6 Hard invariants (the INV-02 analogs)

- Only bars with close time <= decision instant are rendered; the
  observation at step k is a pure function of bars 1..k−1 plus PIT daily
  context. A future-poison test mutates all data dated after t and asserts
  the observation, and hence the seeded-policy action, is unchanged (the
  FORECAST-001 idiom).
- Execution at the NEXT bar open at worst (the protocol's
  `execution_at > decision_at`, `execution_session > decision_session`,
  compressed to bar granularity); the final mark-out is the session close.
- Per-episode loss capped at the $500 debit by construction (mirroring the
  desk's defined-risk rail); the harness rejects any action that would
  exceed it.
- Splits: the corrected trex calendar (2025-01-09 is NOT a session; the
  Carter closure), never the stale protocol calendar.

## 3. Minimal viable experiment

**Question: does a GEPA-evolved policy beat fixed rules on inner folds?**

### 3.1 Shape

- Episodes: 60 train / 20 validation (table 2.1), inner window
  2024-09..2025-12. Selection uses train; the frontier finalists are
  ranked on validation; nothing in 2026+ is touched.
- Loop: population 4 seed policies x 3 generations, DSPy GEPA
  `auto="light"` (35-episode selection minibatches), ONE predictor.
- Noise: k = 3 rollouts per validation episode, score = MEDIAN of the 3
  (flash is noisy run-to-run; median, not mean, because the noise is
  heavy-tailed). Train minibatches run k = 1. Actor decoding temperature
  pinned in the artifact (T = 0.2), so the artifact hash covers sampling.
- Roles: actor = glm-5.3-flash via the quota broker (~20 concurrent);
  reflector = glm-5.3 non-flash (2-concurrent) with MiniMax as declared
  fallback; Codex (gpt-6-astra) for the post-loop transpilation diagnostic
  (5.1). The 27B text-main lane (:18000, one request in flight) is NOT in
  the loop — reserved for interactive use.

### 3.2 Baselines (declared up front, identical harness, identical fills)

1. **fixed EXEC-001-style rules:** enter at the first N-bar open, hold to
   session close (and a tighten-variant: enter at open, exit at the first
   −1R mark). The null EXEC-001 already prices at daily granularity.
2. **no-trade:** all `no_action`; the floor every other arm must beat.
3. **random-valid-actions:** uniform over the typed action set, fixed
   harness seed — the "any policy" control.
4. **un-evolved seed prompt** (the ablation): seed #1 run through the SAME
   k = 3 validation machinery, no evolution. This isolates GEPA's
   contribution from the LLM's: evolved-vs-seed is the evolution effect,
   seed-vs-rules is the prompting effect.

### 3.3 Budget arithmetic (declared against the compute reality)

- 1 rollout = 26 steps = 26 actor requests, ~2k input tokens each (policy
  prompt + observation; no prompt caching assumed on the broker lane),
  ~150 output tokens.
- Selection: 12 candidate-evals x 35-episode minibatches x k = 1
  = 420 rollouts (~900 metric calls is the auto="light" envelope; 420
  fits with margin).
- Frontier finals: 4 candidates x (60 train k = 1 + 20 val k = 3)
  = 480 rollouts.
- **Total ≈ 900 rollouts ≈ 23,400 actor requests ≈ 47M input / ~1.9M
  output tokens.** Wall clock on flash at 20 concurrent and ~4 s/request
  ≈ 78 minutes of actor time for the whole MVE; reflection is 8 strong-
  model calls (minutes at 2-concurrent). Hard caps in the harness, part
  of the registered config: 30,000 actor requests and 60M input tokens —
  beyond either, the loop aborts and scores 0 (never silently continues).

### 3.4 Splits and the sealed outer window

Inner (ALL tuning, including every GEPA decision): 2024-09-05..2025-12-31.
Sealed outer, declared now: episodes from the first session >= 2026-01-09
(inner end + the protocol's 5-session embargo, applied literally even
though an intraday episode's label realizes within its own session)
through the capture date, same strata plus (optionally, after build 4.5)
macro days. One run, one verdict, no re-gridding; the frozen artifact is
the only thing that crosses the seal; forward results never promote.

### 3.5 Verdict vocabulary and thresholds (fixed before any run)

On the inner validation fold, median-of-k episode scores, paired
block-bootstrap over episodes (one-sided):

- **PASS:** evolved > un-evolved seed > best fixed rule; evolved beats the
  seed by >= +0.05 R mean and p < 0.10 (n = 20 episodes detects only
  sizable effects — disclosed; the sealed outer run uses p < 0.05).
- **PASS-RULES-ONLY-SEED:** seed ≈ evolved (within the noise band) and
  seed > best fixed rule at the same bar — the LLM policy beats rules but
  GEPA's evolution added nothing.
- **FAIL:** anything else, including evolved <= fixed rules and anything
  not beating no-trade.
- **NOT_EVALUABLE:** harness or data-integrity defects — budget abort,
  minute/daily close integrity gate failing on > 10% of episodes, or the
  measurement floor absent (7.10).

### 3.6 Config accounting

The MVE = ONE registered optimizer config (the whole loop, seeds included).
Planned registrations for the program: MVE, the N = 5 arm, the JEPA-obs
variant, the macro variant — 4 of 32 inner_loop slots. Nothing else runs
on this game without a new registration.

### 3.7 GEPA traps, baked into the harness (from the completed campaign)

1. Any exception inside a program makes Evaluate DROP the item and GEPA
   crash with IndexError: every actor call is wrapped; parse failure,
   timeout, or constraint violation = `no_action` + counted penalty +
   score contribution 0. Items are never dropped.
2. GEPA's reflection sees ONE random trace per predictor per item: the
   feedback builder aggregates ALL step-calls of the episode (sibling
   misses summarized: which steps diverged from the best rollout, at what
   cost in R) so the reflector sees the episode, not a lucky step.
3. auto="light" budgets ~35-item minibatches (~900 metric calls):
   population x generations sized to 420 selection calls inside the
   envelope (3.3), with the declared hard caps behind it.
4. The stock proposer invents domain rules: the reflection prompt embeds
   the BANNED registry VERBATIM, the ALLOWED_DIRECTION/CONTEXT_ONLY
   semantics, the action grammar, and the protocol invariants — evolution
   cannot lean on refuted families because the proposer is constrained
   AND the harness rejects what it emits (5.2).
5. Evolved-policy evaluation is noisy: k = 3 median voting, declared here,
   with the un-evolved seed run through identical machinery.

## 4. Data + builds needed (named, not built)

1. **Intraday episode capture** (build, ~0.5 day): per (name, session)
   ranged minute call `/v2/aggs/ticker/{t}/range/1/minute/{day}/{day}`
   with `adjusted=true` — exactly the pre-registered
   `intraday_exit_pilot.py` pattern (its `artifacts/intraday-cache/`
   convention: SEPARATE from `artifacts/massive-cache`, which is
   load-bearing for cycle 2 and must never be written into). The
   "grouped" minute endpoint (`/v2/aggs/grouped/...`) was considered and
   rejected for episode capture: one call per day returns the whole
   market's minute bars (multi-MB, result-capped, unfiltered breadth),
   worse than two small ranged calls when an episode needs 1 name + SPY.
   **Wire cost, MVE-exact: 80 episodes x 2 name-days = 160 requests
   (+20% retry budget = 192); at the 5 req/min governor convention =
   32 min (38 min worst case); 62,400 minute bars ≈ 8 MB raw JSON.**
   Storage: one JSON file per name-day under
   `artifacts/intraday-cache/{session}/{NAME}.json`, atomic writes, a
   provenance file in the `ohlc-panel.provenance.md` idiom, sha256 of the
   whole capture recorded in the trial row. PIT safety: files keyed by
   session; the harness reads only bars with close time <= decision.
   Integrity gate (the pilot's): the minute-day implied close must match
   the panel close within 0.5%, else the episode is EXCLUDED and counted.
2. **Intraday fill model** (build, ~1 day): `SIM-FILL-INTRADAY-v1` —
   entries/exits at next-bar open ± a declared spread penalty (5% of mid
   per side primary, 10% sensitivity), spread legs priced with the
   hash-pinned Black-Scholes pricer (`massive_derived.implied_vol`'s
   `bs_price`) off PREV-session IV30 and the current bar's underlying
   price, strikes at declared deltas, participation capped by bar volume.
   **This is a NEW, WEAKER fidelity class than the daily FillEngine**
   (which fail-closes on real quotes, INV-09/10/11, 13 rejection codes):
   no intraday option quotes exist (chains = ONE session, 2026-09-22), so
   contract-existence is asserted from the standard monthly grid, not
   verified. Every result carries the fidelity label
   `fills: sim-intraday-v1` and, under the M2 synthetic-era rule, is
   SYNTHETIC-CLASS evidence: no real-market performance or discovery
   claim may rest on it, promotion runs only through the paper sandbox,
   never this simulator.
3. **Game harness + artifact hashing** (build, ~2 days): episode renderer
   (PIT-enforcing), action validator, reward/fill accounting, future-poison
   test, seeded strata sampler, broker-lane actor client with the budget
   counter, and the frozen-artifact hasher (prompt text + decoding params
   + parser version -> sha256). The GEPA seam is a single DSPy module
   (one predictor, one metric, custom feedback per trap 2).
4. **Trial registration seam** (build, ~0.2 day): one row in the trials
   sqlite (`registry/sqlite.py` conventions) with git sha, config hash,
   capture sha, and the sealed outer window, written before any evaluation
   (INV-13); the theory-lane JSON shape (`docs/theory/*-registration.json`)
   as the human-readable artifact.
5. **Historical macro-date capture** (optional build, ~0.3 day, zero
   vendor wire — public calendars): FOMC/CPI/NFP dates 2024-01..2025-12
   entered under the `MACRO-SEALS.md` hand-declared convention; unlocks
   the macro-episode variant. NOT needed for the MVE.

**Rank by cost/value:** (4) registration seam — trivial cost, mandatory;
(1) episode capture — half a day, 32 min of wire, unlocks everything;
(3) harness — the real engineering, and the reusable asset for lanes 2–3;
(2) fill model — the fidelity ceiling of the whole program, cheapest to
get wrong quietly, so the label travels with every number; (5) macro
dates — only if the macro variant is wanted.

## 5. Failure modes (ranked, most-likely-first)

1. **The evolved policy is just a fixed rule.** Diagnostic (mandatory,
   part of the MVE): transpile the frozen policy's behavior by tracing it
   on every inner episode and fitting the simplest decision rule that
   reproduces its (observation -> action) map; if a small rule (declared:
   a <= 8-node tree over the observation features) matches >= 95% of
   actions and its simulator PnL is within the noise band, GEPA added
   nothing — the result is recorded as rule-equivalent, the rule itself is
   NOT registerable as a signal (post-hoc discovery; if it falls in a
   BANNED family it is dead on arrival), and the honest headline is the
   PASS-RULES-ONLY-SEED / FAIL vocabulary, never "we learned a policy".
2. **Rediscovery of banned families.** Three layers: the proposer is
   constrained by the verbatim BANNED registry + protocol text in every
   reflection prompt (trap 4); the harness action validator makes banned
   directions inexpressible (long-only, declared direction on live days);
   and a post-hoc action-trace audit correlates the frozen policy's actions
   with banned-feature proxies (overnight gap, |return|, vol spike) — a
   disguised volspike/gap_fade shows up as action-gap correlation and is
   disclosed as NOT_PROMOTABLE.
3. **Overfit to few episodes.** 60/20 episodes is small by design; the
   countermeasures are the stratified declared mix, the seed-pinned
   sampler, the un-evolved-seed ablation (which carries the same
   overfitting surface — if the seed matches the evolved policy, selection
   pressure is not the story), the relaxed-but-declared thresholds, and
   the sealed outer window that selection never touches. Power is
   disclosed, not pretended.
4. **Actor noise masquerading as policy improvement.** k = 3 median
   voting; identical machinery for seed and evolved; paired
   block-bootstrap over episodes; fixed harness seed for the random
   baseline; and the pilot batch's measurement-floor question (7.10)
   answered BEFORE any evolution runs.
5. **Look-ahead leaks in the bar feed.** The future-poison test is part of
   the sealed run (mutate everything dated after t; observations and
   actions must not move); execution at next-bar-open at worst; IV lagged
   one session; the corrected calendar (2025-01-09); the integrity gate
   keeps adjusted-basis drift out of the feed.
6. **Cost blowup.** Declared hard caps (30k requests / 60M input tokens)
   abort the loop and score 0; the budget counter lives in the harness and
   the trial row; the wall-clock estimate (3.3) is 78 minutes of actor
   time — an evening, not a week, and the broker's 20-concurrent lane is
   the only shared resource touched (the 27B interactive lane is never
   used).

## 6. Relationship to the other two lanes

Lane 1 is the desk campaign's direction menu: the sealed survivors
(XSMOM-TOP3 no-skip 273, PEAD-BIGSURPRISE beats) and the FORECAST-001 HAR
vol forecast feeding the VRP/cheap-fair-rich conditioning — the directions
and context the agent may time. Lane 2 is the JEPA surprise (sibling memo):
a per-name world-model-error score whose only sanctioned channel is the
CONTEXT_ONLY door; in this game it enters, if at all, as one more numeric
observation field (2.3) — an input the policy may condition timing on,
never a direction. Lane 3 is this memo: the execution layer. Composed, the
registration menu becomes a three-axis product — direction (lane 1,
sealed rules pick WHAT), context (lanes 1–2, vix_term/JEPA surprise shape
or withhold), execution (lane 3, WHEN within the session and at WHAT size)
— with each axis evolving inside its own registered, sealed discipline and
no axis able to borrow authority from another: a JEPA-gated XSMOM card
timed by a GEPA policy is three registrations, one per axis, and the
composite's only promotion path is the paper sandbox's forward cards.

## 7. Verdict-ready summary

1. Proposal: GEPA reflective evolution over a one-predictor LLM agent
   policy playing a 26-step, N = 15-minute-bar, defined-risk intraday
   game on declared episode strata; whole loop = ONE registered optimizer
   config; output = one hashed frozen policy artifact; one sealed outer
   evaluation, nothing outer feeds reflection.
2. Positioning: EXEC-family (learned entry timing/sizing vs EXEC-001's
   next-open null), never a direction signal — the banned registry and
   the harness's long-only, declared-direction action validator make
   refuted families inexpressible.
3. MVE: 60/20 episodes 2024-09..2025-12, population 4 x 3 generations,
   auto="light" (420 selection calls), k = 3 median voting, flash actor +
   strong reflector, four declared baselines including the un-evolved-seed
   ablation; verdicts PASS / PASS-RULES-ONLY-SEED / FAIL / NOT_EVALUABLE
   with thresholds fixed in advance.
4. Compute: ~23,400 actor requests ≈ 47M input tokens ≈ 78 minutes on the
   20-concurrent flash lane; hard caps 30k requests / 60M tokens; the 27B
   interactive lane untouched.
5. Wire: 160 vendor requests (+20% retries) ≈ 32 minutes at the 5 req/min
   governor; ~62,400 minute bars ≈ 8 MB; capture per the pre-registered
   pilot's conventions, cache separate from massive-cache, integrity-gated.
6. Fidelity ceiling: chains exist for ONE session, so fills are
   SIM-FILL-INTRADAY-v1 (next-bar-open, declared spread penalty,
   participation cap) — a weaker class than the daily FillEngine; every
   result is synthetic-class under M2, promotion only via the paper
   sandbox.
7. Expected information value: high even on FAIL — the repo's timing
   evidence (EXEC-001 null, GAPS dead, exits 0/26 and 0-for hold-1) all
   says intraday timing does not pay; a learned upper bound that also
   fails CLOSES the family at the evolved frontier, stronger than any
   fixed-grid null (SWEEP-001/002's 1,008 empty cells being the
   precedent), and the harness is reusable for lanes 2–3.
8. Cost: ~4 analyst days (registration, capture, harness, fill model,
   run, results doc) + one evening of flash quota + 32 minutes of vendor
   wire; no GPU, no new hard dependencies beyond DSPy in a scratch venv
   (repo venv stays numpy-only).
9. Most likely failure: the evolved policy transpiles to a small fixed
   rule (5.1) — and the design's honest answer is the ablation + verdict
   vocabulary, not a retuned loop.
10. Recommendation: GO at exploration cost, with the pilot episode batch
    FIRST; its single question — **does the harness have a measurement
    floor at all: on 10 episodes, does the un-evolved seed separate from
    no-trade by more than the k = 3 actor-noise band?** If not, the MVE is
    NOT_EVALUABLE by construction and the fix is the simulator/observation,
    not the optimizer.
