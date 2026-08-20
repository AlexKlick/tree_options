# M3 — options era on synthetic data: overlay generator, PIT surface, exercise/settlement, options backtest, sealed gate

Status: APPROVED 2026-08-19 (owner rulings recorded verbatim in §1). Base:
`main` @ `7899e8f` (the PR #4 merge commit). Companion spike:
`docs/m3-options-schema-spike.md` (§6 is the generator spec, §3 the T+1
availability semantics this campaign mirrors).

## 1. Context and owner decisions (recorded verbatim)

M2-proper is green and merged (sealed gate #2 PASS, 398/0, KILLED 106/106).
Its plan §6 unlocks the options milestone. This campaign builds the options
machinery on the SAME frozen equity worlds — still synthetic-only, still
machinery-validation-only, and advancing the repo thesis: equity-signal
research expressed through liquid, defined-risk option positions.

Owner decisions of 2026-08-19 (the planning round):

1. **Structure scope: calls + puts.** Top-quintile scores → long calls,
   bottom-quintile → long puts; both `buy/open_long`, premium-defined
   risk. Debit spreads stay out — short legs are protocol-prohibited
   (`short_options.policy: prohibited`) until exercise/assignment machinery
   exists, and this campaign does not build assignment.
2. **Settlement: American with early exercise.** Contracts keep
   `exercise_style="american"`; the campaign builds the full exercise
   right: an election policy plus cash settlement for BOTH early exercise
   and expiry. Declared simplification (recorded in §5 and the evidence
   nonclaims): the settlement MEDIUM is cash (intrinsic × multiplier);
   physical delivery of shares is deferred — one settlement mechanism,
   exact-Decimal, conservation-checked.
3. **Packaging: two staged merge units.** MU-1 = pure machinery with no
   trial outcomes (workstreams A, B, C). MU-2 = strategy + backtest +
   trials + world amendment + sealed gate + evidence (D–H). One bounded
   review per unit; normal merge commits, never squash; the sealed gate
   runs only after MU-2.
4. **Fill policy: declared 7200-second quote-age override.** Option files
   publish 09:00 America/New_York on T+1; executing in the 10:00 ET entry
   window makes every legal fill structurally ~1h old. The options lane
   constructs `FillEngine(..., max_quote_age_seconds=7200)`. The override
   rides in the trial config dict → config hash → every artifact stamp.
   `research_protocol.yaml` stays byte-frozen. INV-10/INV-11 are untouched:
   execution is still strictly after decision at both levels, entry at the
   executable ask / exit at the executable bid, and `received ≤ execution`
   plus monotone selection still bind every fill.

Standing rules carried forward unchanged: no GitHub Actions ever (local
gates are release authority); `research_protocol.yaml` byte-frozen; `synth/`
byte-frozen — the overlay lives in a new sibling package with its own
registry lane and code pin, and the 13 equity worlds stay byte-identical;
red-first tests; the mutation harness must show zero of everything except
KILLED; expensive commands run once teed to a retained log; sealed gates
are one-shot with pre-declared criteria and FAIL is evidence; merges that
preserve reviewed evidence use normal merge commits, never squash; every
artifact carries dataset provenance (synthetic equity v1 + options v2
overlay) and no real-market claim exists anywhere.

## 2. What exists and is reused (no duplication)

- **Schemas, consumed as-is.** `OptionContract`/`DeliverableSpec`
  (`schemas/options.py:33`; the standard-contract validator enforces
  multiplier 100), `QuoteEvent`/`TradableQuote`/`as_tradable`/`select_quote`
  (`schemas/market.py`; graded Stale/Nonpositive/NonTick/Crossed/Locked/
  ZeroSize/NonTradableCondition; monotone selection with the
  exchange-timestamp tie-break), `Order`/`Fill` with the long-only pair
  (`schemas/trading.py:33-47`), and the RESERVED never-yet-minted
  `exercise_settlement` `LedgerEntry` kind (`schemas/ledger.py:10-17`) —
  this campaign mints it.
- **Candidate filter (§9.2)** with frozen protocol defaults (DTE 30–60
  calendar days, |delta| 0.30–0.60, OI ≥ 500, same-day volume ≥ 100 with
  the applicability flag, spread ≤ 10% of mid, underlying 20d-median dollar
  volume ≥ $50M, earnings span) — `candidates/filters.py`. Consumed
  unchanged.
- **Fill engine** `guards/fills.py:149` — the only `Fill` minter; already
  supports the per-instance `max_quote_age_seconds` seam. The options
  backtest goes THROUGH this engine on `QuoteEvent` streams (unlike the
  equity seam's bar-based `EquityFillEngine`).
- **Ledger** `ledger/book.py` — FIFO exact-Decimal with the independent
  replay oracle; extended additively by workstream C, never rewritten.
- **Trial machinery (INV-13/14)**: `TrialRegistry`/`TrialScope`/
  `build_stamp`/`write_artifact` and the register→running→complete/fail
  discipline (`trials/run.py:216`, `protocol/stamping.py`).
- **Sealed-gate driver pattern**: `scripts/run_m2_sealed_gate2.py`.
- **Equity seam** (`backtest/equity.py`) as pattern donor: next-session
  schedule map, sells-before-buys, affordability sizing,
  conservation-every-session, corporate-action-as-zero-fee-conversion-fill.
- **Frozen worlds**: 13 pinned in `data/worlds/registry.json` (dev
  101–104; validation 701–709). Full world ≈ 25–30 s to regenerate.
- **Mutation harness**: `scripts/mutate.py`, M01–M107; new mutants
  continue at M108.

### Timing design (the load-bearing decision)

Equity bars publish 23:00 UTC same-date, so at decision `close(d)` the last
visible bar is d−1 (the M2-proper §3.B information boundary). Option files:
session t's two snapshots (15:45 ET + close) carry `exchange_timestamp`
intraday-t and `received_timestamp` = 09:00 America/New_York on t+1
(zoneinfo-deterministic; frozen-at-receipt; mirrors spike §3's T+1
reality). Consequences:

- At `close(d)` the candidate filter sees file(d−1) (every `AsOf` is that
  file's receipt instant) and the model consumes equity features through
  bar d−1 — the SAME information boundary as the M2-proper labels, whose
  window is (d … d+H−1).
- Execution at 10:00 ET on d+1 fills against file(d)'s EOD snapshot
  (received 09:00 ET d+1 ≤ execution; the received tie breaks to the later
  exchange timestamp, so close beats 15:45). Entry premium is struck at
  close(d), exit premium at close(d+4): the 4-session hold spans EXACTLY
  the H=5 label window minus theta/spread. Honest note recorded in the
  evidence: the execution premium embeds session-d's underlying close —
  post-decision PUBLIC information priced into the fill, not leakage.
- Corporate actions (v1 announces at 23:00 UTC t, effective t+1):
  (i) entry exclusion for underlyings with a pending action visible at
  decision; (ii) execution-time cancellation for actions announced
  overnight — orders cancelled and counted, using only information
  available by execution; (iii) force-close open positions at the next
  10:00 ET window against the last pre-action file when a ratio action
  announces mid-hold; (iv) terminal delisting/merger mid-hold settles at
  intrinsic vs the final bar's close at its publication instant. The
  overlay never mints adjusted contracts — `standard_contract_flag=False`
  is exercised by fixtures only.

### Earnings-flag correction (found in planning, before any code)

`accepted` requires zero NOT_EVALUABLE (`candidates/filters.py:5`), so
feeding `spans_earnings=None` rejects every candidate. Synthetic worlds
contain NO earnings events at all — the truthful feed is
`AsOf(value=False, available_at=file_receipt)`. NOT_EVALUABLE weight then
comes from zero-bid spread tails, exactly as the filter was designed.

## 3. Workstreams

### A — Options overlay generator (`src/tree_options/synth_options/`, stdlib-only, new registry lane)

Files: `synth_options/{__init__,spec,greeks,generate,truth}.py`; registry
lane `data/worlds/options_registry.json`; verifier
`scripts/verify_options_worlds.py`. No byte of `synth/` is touched and no
module imports `tree_options.synth` (AST boundary test, mirroring
`test_synth_generate.py:246`); the package consumes
`(bars, master, actions)` records plus a calendar.

- `OptionsOverlaySpec` (frozen; every knob lives here, never the
  protocol): `overlay_version: Literal["v2"]`, `world_id`, `seed`,
  `eligible_top_n: int = 100` (top-N by 20d-median dollar volume from the
  world's own bars — PIT-honest and deterministic), `n_moneyness_nodes:
  int = 21` spanning ±30% (log-spaced, snapped to the $1/$2.50/$5 strike
  grid by price bucket), `max_live_expiries: int = 10` (weekly expiries
  while DTE ≤ 45 plus quarterly 3rd-Fridays out to DTE ≤ 397 — the fat
  tail), weekly `listing_dte_days: int = 70`, `risk_free: float = 0.03`,
  `dividend_yield: float = 0.0`, IV planted per (underlying, expiry) and
  CONSTANT across sessions (declared simplification — keeps delta
  internally consistent), spread model (ATM 1–2% of mid, wing-widening),
  `untraded_fraction ≈ 0.5`, snapshot times (15:45/close), publication
  09:00 America/New_York, `underlying_spread_bps`.
- `greeks.py`: `bs_price(...)`, `bs_abs_delta(...)` via `math.erf`.
  Stdlib-only is import-lint-enforced (the package never gains numpy —
  the pin hashes every `synth_options/*.py` byte). Delta is derived
  ANALYTICALLY from the planted IV and the snapshot's underlying mid so
  filter inputs stay internally consistent (spike §6.5).
- `generate.py`: `generate_overlay(*, bars, master, actions, spec,
  calendar) -> GeneratedOptionOverlay` — a pure, deterministic, LAZY
  function of its inputs with name-keyed `random.Random` streams
  (`f"{world_id}/{seed}/chain/{security_id}/{expiry}"`). Contracts:
  standard, `exercise_style="american"`, multiplier 100,
  `listing_end == expiration`, canonical ids
  (`OPT-{underlying}-{yymmdd}-{C|P}-{strike_cents}`). Per contract-day:
  TWO `QuoteEvent`s with sizes; premium = BS(planted IV, underlying mid);
  half-spread by moneyness/tenor; ask rounded UP to the tick grid, bid
  rounded DOWN and floored at 0.00 — deep wings naturally quantize to
  zero-bid markets, and zero-bid tails MUST exist; a small planted
  non-tradable-condition tail; `underlying_bid/ask` carried inside every
  snapshot (spike §6.6). OI/volume concentrated ATM/short-tenor; ~half of
  contracts untraded on a given day. Per file: the receipt instant and
  the planted 20d-median dollar volume from bars (t−19 … t).
- `truth.py`: `OptionsOverlayTruth` (planted IVs, eligible sets, spread
  and size parameters) — import-blocked outside `synth_options.*`.
- **Pin mechanism (own lane).** `data/worlds/options_registry.json`:
  `options_registry_version: "options-worlds/1"`, `synth_options_code_sha`
  (over every `synth_options/*.py` byte, `verify_worlds.py` style), and
  per overlay entry `{world_id, overlay spec, expected: {contract_count,
  analytic row-count commitments, sample_slice_hashes[]}}`. Verification
  is SAMPLE-BASED plus ANALYTIC COUNTS (full materialization is
  infeasible by design — §7 scale): determinism is pinned by
  regenerating a deterministic anchor sample (64 fixed
  (underlying, session) anchors + boundaries: first/last session,
  pre-action sessions, one expiry day) and hashing canonical bytes, with
  an analytically derived row-count commitment cross-checked on every
  sampled slice. `scripts/verify_options_worlds.py` refuses on a pin
  mismatch exactly like `verify_worlds.py` (exit 2) and supports a
  deliberate `--recompute`.

Red-first tests (spec validation; two snapshots per contract-day with
correct exchange/received stamps; ladder width/grid snap; zero-bid tails
EXIST at a floor rate; ~half untraded; OI concentrated ATM; put-call
parity within the combined spread (property); |delta| monotone in
moneyness; two-run and cross-process byte-identical slices; eligibility
matches an independent dollar-volume oracle (registry nulls/alphas are
independent worlds — v1 seeds every stream with world_id, so no
twin-sharing is claimed or relied on anywhere in M3); receipt never
before 09:00 ET t+1; DST-correct publication (November/March);
stdlib-only import lint; truth sidecar import-block lint; registry
verify catches a seeded slice tamper and a code-pin drift).

### B — Point-in-time options surface (`data/options_manifest.py`, `data/options_pit.py`, `data/quality_options.py`)

PARALLEL dataset, not a snapshot extension: `DatasetSnapshot`/
`DatasetManifest`/`verify_manifest` are scoped to exactly
{master, bars, actions} under `MANIFEST_SCHEMA_VERSION m2/1`, and widening
that model would either bump the schema version (staling the equity
manifest lane for every regenerated world) or smuggle optional fields
through a frozen contract. The equity lane stays byte-identical; options
get their own manifest with the same discipline.

- `OptionsManifest` (`schema_version "m3/1"`, content domain
  `b"tree-options-m3-options-v1"`): binds the contract master, the sampled
  slice hashes, the analytic counts, the overlay spec, the
  `synth_options_code_sha`, and the parent world's `content_sha256`
  (lineage pairing).
- `OptionPitSurface(overlay, calendar)` — the read gate:
  `file_as_of(underlying_id, as_of)` (latest file with
  `available_at ≤ as_of`; fail-closed if none), `contracts_as_of`
  (listing window AND file visibility), `quote_history(contract_id)`
  (whole life, ordered — `select_quote`/`as_tradable` own the filtering),
  `candidate_inputs(contract, decision_at)` (AsOf-wrapped delta/OI/volume/
  bid/ask/underlying liquidity from the correct file;
  `spans_earnings = AsOf(False, receipt)` per §2).
- `verify_options_manifest` in `data/quality_options.py` (new file;
  `data/quality.py` untouched): identity binding, content-hash
  re-derivation over the sample slices, count commitments, provider/source
  coherence, per-quote schema invariants (tick grid on quoted prices,
  `exchange ≤ received`, receipt instants on session+1).
- Pairing: the trial runner takes `(PointInTimeDataset,
  OptionPitSurface)`, asserts `snapshot_id == overlay.world_id` and
  calendar identity; the stamped `dataset_manifest_hash` is sha256 over
  the JSON pair `[equity.content_sha256, options.content_sha256)]`.

Red-first tests: future file invisible at `as_of`; visible exactly from
09:00 ET t+1; contract discovery honors `listing_start/end`; whole-life
ordered quote history; candidate inputs carry the file's receipt instant
(a T+1 leak test feeding a decision at close(t) with file(t) must NOT be
visible); manifest tamper on any sampled slice detected; world/overlay
pairing mismatch refuses.

### C — American exercise + expiry settlement (`options/settlement.py`, `options/exercise.py`; `ledger/book.py` additive only)

- `ExerciseSettlement(StrictModel)`: `settlement_id`, `contract_id`,
  `quantity`, `settlement_price: Price` (the reference close),
  `multiplier`, `cash: Money` (= `max(intrinsic, 0) × quantity ×
  multiplier`), `session` (exercise or expiration session), `ts` (the
  reference bar's `available_at` — the first instant the intrinsic is
  knowable), `ref_id` (the bar's `source_record_id`), `kind:
  Literal["expiry", "early_exercise"]`. The minter refuses: early
  exercise of non-american contracts; `session > expiration`; settlement
  without a knowable reference bar; quantity beyond held.
- **Early-exercise election policy** (pre-declared, PIT-honest): the
  election happens at the 10:00 ET window of session t using ONLY
  file(t−1) plus visible actions; the settlement strikes at close(t) with
  cash knowable at pub(t). Elect iff (a) CALL: a cash dividend on the
  underlying is visible with effective session in (t, expiration] AND
  dividend/share ≥ file(t−1) (mid − intrinsic); or (b) ANY: file(t−1)
  bid < intrinsic × 0.98 (the market pays less than 98% of intrinsic —
  exercising beats selling).
- `LedgerBook.apply_settlement(s)`: guards `DUPLICATE_SETTLEMENT`,
  `POSITION_UNDERFLOW`, `OUT_OF_ORDER` over the single merged
  fill+settlement timeline; closes FIFO lots (the same lot walk as
  sells); cash += s.cash; mints exactly one
  `LedgerEntry(kind="exercise_settlement", amount=s.cash,
  ref_id=s.settlement_id)`. OTM expiry is a zero-cash entry that still
  closes the lots.
- **Conservation oracle extension**: the replay additionally recomputes
  settlements from primitives; the flat-book identity becomes
  `cash == initial + Σ realized_gross (fills) + Σ settlement_cash −
  total_fees`, with the entry-sum check spanning both entry kinds.

Red-first tests + properties: intrinsic for C/P; OTM zero-cash close;
european early-exercise refused; over-position / duplicate / out-of-order
refused; both election branches fire on constructed files; the election
uses only file(t−1) data (a file(t)-only signal must not trigger);
randomized fill+settlement streams conserve to the penny under the
EXTENDED oracle; a settlement cannot validate a broken lot walk (oracle
independence).

### D — Candidate→order strategy (`options/strategy.py`)

`OptionsStrategyConfig` (frozen; enters the config hash): target |delta|
0.45 (the ladder strike nearest target inside the 0.30–0.60 band), expiry
= the in-band expiry nearest 45 DTE, `eligible_top_n`, quintile cut over
the OPTION-ELIGIBLE scored cross-section, per-session premium budget =
fraction × ledger.cash, whole contracts via a decremental
affordability search against cash INCLUDING fees,
`max_contracts_per_candidate`, exit = 4 sessions after entry execution,
force-close/cancellation toggles. `build_candidates(surface, dataset,
decision_session, scores)` (top quintile → calls, bottom → puts);
`plan_orders(...)` (`buy/open_long`, `decision_at = close(d)`);
`plan_exits(...)` (`sell/close_long`, decision at the exit session's
close — a time-based exit needs no data); the cancellation and
force-close passes.

### E — Options backtest (`backtest/options.py`)

`run_options_backtest(*, calendar, surface, dataset, signals,
initial_cash, config, arm)` — validation-first; per-session loop in
order: (1) settlements whose publication instant has passed (expiries,
elected early exercises, terminal delistings), (2) force-closes for newly
announced actions, (3) exercise elections at the 10:00 ET window, (4)
scheduled exits then entries at the same window (sell side first — it
frees cash) with execution-time cancellation, (5) conservative mark at
close(t) from file(t−1)'s EOD BID (strictly knowable; marks feed the
equity curve only — the §4 statistics use real fill-to-fill premium
returns), (6) `assert_conservation()` EVERY session. Two arms: **A
momentum hold** (4-session exit — the transfer arm) and **B
hold-with-exercise-policy** (ride to expiry settlement or early
exercise; machinery oracles only, no signal criterion). The payload
records per-position rows (entry/exit fill ids and prices, label, score)
plus counters (NOT_EVALUABLE rate, zero-bid/no-liquidity, locked/
non-tradable, cancellations, force-closes, early exercises, expiries).

### F — Options trial runner + dev calibration (`trials/options_run.py`, `scripts/run_m3_dev_trials.py`)

`run_options_trial(...)` mirrors `trials/run.py`: validation → config dict
(hashed: strategy config, arm, `max_quote_age_seconds=7200` override,
decision-session hash, the paired `dataset_manifest_hash`) → `build_stamp`
→ register (32-cap, fresh dev registry) → execute → stamped artifact →
complete/fail, never limbo. Dev phase, registered before any validation
payload is touched:

- **OD1** arm A on world 104 (alpha 0.002): measure (i) the per-position
  rank corr(premium return, H5 label) — the vehicle-fidelity factor;
  (ii) per-cohort IC sd; (iii) IC autocorrelation across lagged cohorts
  (the overlap input to §7).
- **OD2** arm B on world 103 (null): machinery oracles — settlement
  counts, conservation, NOT_EVALUABLE/zero-bid floors,
  cancellation/force-close/early-exercise counts.
- **OD3** eligible-set and rule-weight audit on 103/104: where the $50M
  line lands by rank, in-world liquidity-rule FAIL rate, filter audit
  histogram (if the in-world FAIL rate is ~0 the rule's weight in-world
  is recorded as such — its FAIL path is proven by fixtures).
- **Tripwire (the gate-#1 lesson, institutionalized):** the measured
  fidelity factor and per-cohort IC sd must land within 2 SE of the §7
  priors; otherwise HALT for root-cause, never silent adjustment. The
  gate coefficient is re-priced from MEASURED attenuation before
  workstream G pins anything.

### G — Validation-world amendment (one pre-declared window, before the gate)

Add `synth-v1-val-alpha-710` / `synth-v1-val-alpha-711`: full scale, fresh
seeds never generated before, coefficient **0.50** — owner-ruled
2026-08-20 as the disposition of the OD1 tripwire HALT
(`docs/m3-od1-tripwire-decision.md`): the §7 arithmetic re-priced from
MEASURED OD1 values gives t = 19.3·c·att over the attenuation band
[0.43, 1.0], under which the printed 0.05 is underpowered at every draw
(t ∈ [0.41, 0.96] vs 1.96) while 0.50 passes at every draw with ~2×
pessimistic margin (t ∈ [4.1, 9.7]) — pinned via
`scripts/verify_worlds.py --recompute`; `synth/` is
untouched so `generator_code_sha` stays byte-identical, and the registry
note records the options-gate rationale and the re-priced arithmetic
(mirroring the 706–709 amendments). Their options overlays plus the nulls
701/702 are pinned in `data/worlds/options_registry.json`. The amendment
window CLOSES at the first sealed trial registration.

### H — Sealed gate + evidence + mutants + review (closeout)

`scripts/run_m3_sealed_gate.py` mirroring gate #2: refuses registry/
artifacts reuse (`artifacts/m3-options-sealed.db`,
`artifacts/m3-options-sealed/`); verifies BOTH pins (equity
`generator_code_sha`, options `synth_options_code_sha`) and per-world
expected hashes for both lanes; pre-declared `SEALED_CONFIGS` committed
before the run (4 worlds × 2 arms = 8 trials + a stamped summary); the
verdict is computed from stamped payload files only; exit 4 on FAIL.
Then `docs/m3-options-evidence.md` (the outcome recorded verbatim), the
mutation run at the final head teed to a retained log, the clean-clone
`ARTIFACTS_IDENTICAL=1` proof (clock injection keeps artifacts
deterministic), one bounded review per merge unit, normal merge commits.

## 4. Evidence and gate plan (pre-declared criteria)

Worlds: nulls 701/702 (fidelity + FP arms), 710/711 @ 0.50 (transfer;
owner-ruled — §3.G and `docs/m3-od1-tripwire-decision.md`).
Dev evidence comes exclusively from the 103/104 overlays (OD1–OD3).

Criteria — PASS requires every one; all are evaluated from stamped
artifacts:

1. `conservation_every_session` — the extended oracle holds at every
   session close on every trial (both arms, all worlds).
2. `no_same_session_fills` and `quotes_received_le_execution` — zero
   violations across all fills (asserted from the payload's per-fill
   log).
3. `rejection_paths_live` — **AMENDED, owner-ruled 2026-08-20**
   (`docs/m3-od1-tripwire-decision.md`): zero-bid/no-liquidity execution
   rejections ≥ 100 per world (the rejection paths are exercised, not
   decorative) AND the filter-audit histogram shows the volume/untraded
   tail live (measured 62% `same_day_volume` FAILs at OD1). The
   originally printed "≥ 2% of candidate evaluations carry a NOT_EVALUABLE
   rule" first clause was structurally zero — the strategy evaluates one
   pre-selected near-ATM strike per name (always quoted on the file), so
   zero-bid tails surface as volume FAILs and execution rejections
   instead — and is dropped.
4. `machinery_terminal_states` — arm B: every position ends in exactly
   one settlement (expiry or early exercise) or a forced close; no
   position open past expiration.
5. `vehicle_fidelity_nulls` — pooled per-cohort Spearman(premium return,
   H5 label) on 701 and 702: mean ≥ a floor pre-declared at gate
   registration as (OD1-measured rho − 0.15); prior ≈ 0.8–0.9. Proves
   the machinery transmits underlying exposure WITHOUT any alpha.
6. `fp_nulls` — pooled per-session transfer IC |t| ≤ 2.5 on each null
   world (arm A).
7. `power_transfer` — pooled transfer IC t ≥ 1.96 on 710+711 (arm A;
   each world individually reported). The single detection criterion.

## 5. Non-goals / deferred (declared, not accidental)

Short legs and debit spreads (protocol-prohibited until assignment and
adjusted-deliverable logic exist); physical delivery (cash settlement
medium — declared); assignment (long-only MVP makes it moot);
adjusted/nonstandard contracts (fixtures only); the earnings-span flag
(fed False — the worlds contain none; real-data sourcing deferred); real
vendor data and any real-market claim; vendor greeks semantics; minute
bars / intraday depth; IV dynamics (constant per (underlying, expiry) —
no vol-of-vol, no smile beyond the planted per-expiry level; a declared
simplification that keeps delta internally consistent); multi-leg
structures; limit orders beyond the engine's existing path;
hyperparameter search; tuning against validation worlds in ANY dev
iteration; special dividends (v1 plants regular cash dividends only,
which do not adjust listed options).

## 6. Entry criteria unlocked (next milestones)

Richer exits (stop/target, delta-band rolls) once single-leg machinery is
proven; real-data campaign planning (vendor decision against the spike
§5 shortlist, holdout re-declaration per the standing correction);
short-leg/debit-spread design once assignment and adjusted-deliverable
logic exist; physical delivery.

## 7. Pre-registered power arithmetic (frozen into this packet) + risks

**Overlap-corrected estimator (planning-round fix):** adjacent 4-session
holds share return windows, so a naive all-session t overstates
precision. PRIMARY statistic: per-cohort Spearman IC on DISJOINT entry
cohorts (every 4th session ⇒ ~550 cohorts × ~40 positions ≈ 22k
observations; cohort return spans are session-disjoint). SECONDARY
(reported only): all-session IC with a block-bootstrap SE (block ≥ hold
length).

Priors: per-session rank IC(score, H5 label) ≈ 0.38·c (H5 ≈ 0.45 × H1;
H1 ≈ 0.85·c), drift-wall attenuation band [0.43, 1.0] (measured in
M2-proper), vehicle fidelity rho ≈ 0.9 (theta/spread are near-common
cross-sectionally; IV constant per (underlying, expiry) removes vol
noise by construction). Transfer IC ≈ 0.34·c·att. Per-cohort
σ_IC ≈ 1/√(40−1) ≈ 0.16.

    t ≈ 0.34·c·att × √550 / 0.16 ≈ 50·c·att

At c = 0.05: t ∈ [10.8, 25.1] over the attenuation band — ≥ 8 t-units
of margin above 1.96 even at the pessimistic 0.43 draw (the same margin
rule gate #2 used). At c = 0.01 (reusing 708/709): t ≈ 2·att ⇒
UNDERPOWERED at the pessimistic draw — that is why new worlds are
required, not reused. OD1 must land the fidelity factor, σ_IC, and the
cohort-IC autocorrelation within 2 SE of these priors before G pins the
coefficient; a larger deviation halts the campaign for root-cause, not
silent adjustment.

Risks and mitigations:

- **Scale (the binding constraint).** Naive full-universe chains ≈ 1.3B
  rows/world — infeasible. The designed-down grid (top-100 eligible × 10
  live expiries × 21 nodes × 2 C/P × 2 snapshots ≈ 840 rows per
  underlying-day) is still ≈ 190M rows/world equivalent, so the overlay
  is LAZY: day-slices are pure deterministic functions, never
  materialized wholesale; the pin is sample-slice hashes plus analytic
  count commitments. The backtest touches only ladder pricings plus
  held-contract quote histories ⇒ est. 10–20 min per world per arm, gate
  ≤ ~2 h, run once teed to a retained log. The estimate is validated
  with a small-world dry run in workstream A BEFORE committing registry
  scale. The eligible-set restriction is justified by the §9.2 liquidity
  rule itself ($50M 20d median implies only large names are tradeable;
  the measured rank of the line ≈ 80–120 of 500 — OD3 records it) and is
  PIT-honest (computed from the world's own visible bars).
- **Lazy-generator determinism.** Name-keyed streams, no set/dict
  iteration order in any output path, cross-process byte-identity tests
  on sampled slices, and the code-pin + `--recompute` discipline.
- **Power FAIL risk.** Priced from the measured attenuation band with ≥ 8
  t-units of margin; the OD1 tripwire converts an arithmetic surprise
  into a halt, not a quiet re-run; if the gate FAILs on power with
  machinery green, that is recorded evidence — a new lane needs a new
  owner ruling (the gate-#1 precedent).
- **Early-exercise correctness.** The election uses only file(t−1) plus
  visible actions; settlement cash is knowable only at pub(t); mutants
  M131–M134 target exactly this seam; arm B gives it live volume and
  fixtures give it precision.
- **Quote-age override misuse.** Declared in config → hashed into every
  stamp; `received ≤ execution` and the engine's monotone selection
  still bound it.
- **Artifact size.** Per-position rows (~40 × 2,200 ≈ 88k positions per
  world per arm) ⇒ ~10–20 MB stamped artifacts — acceptable; the summary
  carries aggregates only.

## 8. Sizes and dependency ordering

| WS | New files | ~LOC | ~Tests | Depends on |
|---|---|---|---|---|
| 0 plan of record | 1 doc | — | — | — |
| A generator + pin | 5 + registry + verifier | 1,100 | 45 | — |
| B PIT surface | 3 | 700 | 40 | A |
| C exercise + settlement | 2 + `ledger/book.py` additive | 550 | 35 | — (∥ A/B) |
| D strategy | 1 | 380 | 30 | A, B |
| E options backtest | 1 | 550 | 40 | C, D |
| F trials + dev driver | 2 | 680 | 18 | E |
| G world amendment | registry edits | — | registry tests | F (OD tripwire) |
| H gate + evidence + mutants | 2 + docs + `scripts/mutate.py` | 480 | driver-level | G |

Totals ≈ 4,400–5,100 new LOC, ~210 tests, 27 mutants (M108–M134).
Order: A ∥ C → B → D → E → F (OD1–OD3) → G (amendment window) → H (seal
once). **MU-1 merges after B+C green (A, B, C); MU-2 after H.**

Proposed mutants (owner-scoped anchor→defect format, added in workstream
H): M108 t+1-receipt-shifted-same-day; M109 received-stamped-as-exchange;
M110 eligible-set-uses-future-bars; M111 put-delta-sign; M112
spread-halves-swapped; M113 zero-bid-floor-removed; M114
oi-plumbed-from-wrong-instant; M115 volume-applicability-forced-true;
M116 settlement-pays-strike-side-swapped; M117 settlement-skips-lot-removal;
M118 settlement-kind-swapped; M119 conservation-oracle-drops-settlements;
M120 force-close-missed; M121 execution-cancellation-dropped; M122
exit-same-session; M123 mark-at-ask; M124 inv11-fraction-inverted; M125
sizing-ignores-fees; M126 quintile-over-full-universe; M127
dte-in-sessions; M128 future-file-visible; M129 dead-contract-tradable;
M130 spans-earnings-fed-None (empty-backtest collapse); M131
exercise-election-uses-file(t) (T+1 leak); M132
exercise-ignores-style-guard (european early-exercise accepted); M133
exercise-settlement-priced-at-strike-not-close; M134
exercise-election-ignores-dividend-branch.
