# Synthetic era campaign — M2 runway on generated markets + M3 options spike

Status: IMPLEMENTED 2026-08-18 (owner sign-off "gogo" same day; workstreams
A–E delivered on `m2/synthetic-era-20260818` — see
`docs/m2-synthetic-evidence.md` for the delivered state and the review
record; this document is the signed plan of record, unedited). Base:
`main` @ `8bd34e4` (the PR #2 merge commit).

## 1. Context and owner decisions (recorded verbatim)

The M1 point-in-time authority (PR #2, merged as `8bd34e4`) is proven on a
synthetic fixture only. The owner decision of 2026-08-18:

1. **$0 data budget.** Real equity data is parked; the vendor decision is
   deferred until budget exists. Free tiers were rejected because no free
   source delivers delisted prices, which M1's no-survivorship machinery
   exists to handle.
2. **Next campaign = the synthetic era**: a parametric synthetic market
   generator (the M2 runway) plus the M3 options schema/coverage spike on
   free vendor samples. The generator is a vendor, not a shortcut: it emits
   M1 `RawPayload` rows through the unchanged ingest → manifest → authority
   pipeline, so M1's machinery is exercised for real on generated data.
3. **M2 prohibition amended (verbatim)**: "M2 pipeline machinery may be
   built and validated on synthetic data with known ground truth; every
   artifact must carry dataset provenance (dataset=synthetic/vN); and any
   real-market performance or discovery claim remains prohibited until
   real-data gates are green."
4. **Holdout discipline for the synthetic era**: a frozen validation-seed
   registry is declared BEFORE the first M2 trial — ~5 validation worlds
   per generator spec version, hashed and committed; dev seeds are separate;
   M5 trial recording carries seed/world lineage per trial; null-world
   false-positive control is an M2 acceptance criterion.

Supersession of `docs/m1-evidence.md` §6: item 4 (sector context) is
retired by this campaign (PIT sector membership is generated); item 5
(holdout undeclared) is retired for the synthetic era by decision 4 — the
real-data holdout rule remains deferred until real-data coverage
inspection, per the standing correction. Items 1–3 (vendor adapter, minute
bars, exchange transfers) stay parked with the vendor decision.

## 2. What exists and is reused (no duplication)

The M0 integrity stack is complete; this campaign plugs in at the data
layer only:

- `time/calendar.py` + `data/calendar/nyse_sessions_2018_01_02_2026_12_31.json`
  — 2263 real XNYS sessions (2018-01-02..2026-12-31), checksummed. The
  generator uses THIS calendar (it is a production module; the tests-only
  `time/synthetic.py` weekday double stays forbidden).
- `data/` module (M1): `RawPayload`, `ingest_snapshot`, manifests,
  `PointInTimeDataset`, quality gates — the generator targets this contract
  unchanged.
- `splitting/` — purged anchored-expanding walk-forward folds +
  `check_folds` invariants; free for any generated world.
- `guards/availability.py` (feature/filing PIT audit), `guards/fitting.py`
  (fit-once/apply-only), `candidates/filters.py` (long-only candidate
  filter), `schemas/market.py` (`QuoteEvent`/`as_tradable`),
  `ledger/book.py` + `fees.py` (FeeModel seam).
- `registry/` + `protocol/stamping.py` — register-before-outcome trials
  (INV-13) and `ArtifactStamp` (INV-14) whose `dataset_manifest_hash` field
  is exactly where the synthetic world fingerprint rides.

Design constraints honored: `research_protocol.yaml` stays frozen
(generator parameters live in `WorldSpec` → config hash + dataset
manifest, never the protocol); world identity rides in
`TrialScope.feature_set_id` (no scope-struct change in this campaign);
INV-14 stamping requires a clean worktree, as always.

## 3. Workstreams

### A — PIT sector schema (small, blocking)

`SecurityMasterRecord.sector_as_of` is a bare date with no availability
semantics; residual labels need sector membership knowable at decision
time. Extend the master record with a PIT sector mapping (sector value +
`available_at`, mirroring `TickerMappingRecord`). This is a schema change:
bump `MANIFEST_SCHEMA_VERSION` `m1/1` → `m2/1`, update the M1 fixture and
its tests red-first. No other master semantics change.

### B — Synthetic world generator v1 (equity)

New production package `src/tree_options/synth/`, stdlib-only (no new
runtime dependencies; adding numpy later would be a declared,
protocol-relevant change).

- `WorldSpec` (frozen pydantic): spec_version (`v1`), seed, world_id,
  n_securities, sector layout, action rates (splits / reverse splits /
  cash + stock dividends / renames / mergers / bankruptcies / voluntary
  delistings / IPO staggering), null-vs-alpha switch + effect parameters.
- `generate_world(spec, calendar) -> RawPayload` — pure and deterministic:
  same spec ⇒ byte-identical payload ⇒ identical `content_sha256`. All
  records carry `available_at` = session close + fixed 23:00-UTC
  publication offset (the M1 fixture convention), so the availability
  gates apply unchanged.
- Return process: market factor + sector factors + fat-tailed
  idiosyncratic draws (seeded per-stream via hash-derived `random.Random`
  instances). Splits/reverse splits/stock dividends derive post-event OHLC
  numerically from the pre-event close (the M1 fixture pattern) so the
  ratio-match quality gate passes deterministically; dividends stay small
  enough never to trip the discontinuity gate.
- Lifecycle: staggered IPOs, delistings with reasons (merger / bankruptcy /
  voluntary; bankruptcy final bars then silence, merger successor
  continues under the successor id), renames with ticker recycling by a
  different issuer.
- Worlds come in two kinds: **null** (effect coefficient exactly 0 — the
  false-positive control) and **alpha** (one planted cross-sectional effect
  family with known sign/magnitude). The generator emits a **truth
  sidecar** (planted parameters, effect definition) keyed by world hash —
  consumed only by evaluation code, never by feature construction.
- Provider id `synthetic/v1`; world identity rides in `snapshot_id`
  (e.g. `synth-v1-null-003`), which `features_as_of` already propagates.

### C — World registry and seed protocol

- `data/worlds/registry.json` (committed): per world — spec JSON, seed,
  dev-or-validation pool, expected `content_sha256` + bar/action/master
  counts. Validation pool: 5 worlds per spec version, frozen at commit;
  dev pool: freely used for development and tuning.
- `scripts/verify_worlds.py`: regenerates worlds from specs and asserts
  hash equality. The gate runs a byte-exact check on one SMALL world
  (fast); full-scale regeneration (default N=500 securities × 2263
  sessions ≈ 1.1M bars) is an artifact run, teed once to a log with
  recorded hashes — honest about which lane proves what.
- Rule (decision 4): M2 trials train/tune on dev worlds only; final
  validation runs exclusively on frozen validation worlds; every trial
  record carries world identity.

### D — M3 options schema/coverage spike (documentation lane, parallel)

`docs/m3-options-schema-spike.md` from free samples (Cboe DataShop Option
EOD Summary samples; Databento free sample credits; Cboe free volume
statistics for volume-distribution questions). Six required sections:
(1) vendor field inventory mapped onto `OptionContract` / `QuoteEvent` /
  `CandidateSnapshot` inputs, with gaps named (IV, OI, volume, greeks —
  delta is required by `candidates/filters.py`);
(2) contract symbology (OCC root/expiry/strike encoding; adjusted
  nonstandard roots vs `DeliverableSpec` + `corporate_action_id`);
(3) PIT availability semantics of every consumed vendor field;
(4) coverage statistics from the samples (chains/day, strike × expiry
  grid shape, spread and OI distributions);
(5) storage + cost estimate for a real campaign, with a priced vendor
  shortlist (Cboe / Databento / ThetaData / Polygon / EODHD, 2026-08
  prices) for the deferred owner decision;
(6) implications for the v2 options-overlay generator (deferred).
No production code, no real-data ingest — samples inform the schema doc
only.

### E — Evidence and gates (closeout lane)

Standard discipline: `docs/m2-synthetic-evidence.md`, gate once at the
final head (full suite + appended owner-scoped mutants M71+, e.g. seed
derivation, publication offset, sector availability, registry hash),
one bounded Codex review, merge with a normal merge commit preserving
the reviewed head. Every expensive run teed once to a log.

## 4. Acceptance criteria

1. **Determinism**: same `WorldSpec` ⇒ byte-identical canonical payload
   bytes and `content_sha256` across regenerations (stdlib-only; no
   wall-clock, no set-iteration-order dependence).
2. **Contract compliance**: every generated world ingests through M1
   unchanged — `verify_manifest` + `validate_snapshot` green; rows carry
   `source == "synthetic/v1"`; quality gates catch planted defects (a
   tampered world fails the right gate, proven by test).
3. **Registry integrity**: dev/validation split committed; all validation
   worlds' expected hashes recorded; `verify_worlds` proves byte-exact
   regeneration (small world in-gate; full worlds as teed artifact runs).
4. **Scenario coverage**: each of the nine M1 fixture lifecycle scenarios
   (rename, ticker recycle, split, reverse split, cash dividend, merger
   successor, bankruptcy delisting, IPO, finite listing end) occurs in the
   dev world pool — proven by a parameterized test.
5. **Sector PIT**: sector membership is availability-gated; a
   `features_as_of`-derived panel passes `AvailabilityGuard.audit_panel`.
6. **Truth separation**: the truth sidecar is unreachable from feature
   construction by construction (separate module boundary, enforced by
   import-lint test).
7. **Provenance stamps**: world identity propagates through
   `snapshot_id` into `ArtifactStamp.dataset_manifest_hash`; the amended
   M2 rule (§1.3) and seed rule (§1.4) are quoted in the evidence doc.
8. **M3 spike delivered** with all six sections, samples cited by URL and
   download date.
9. **No model, no labels, no backtest, no fill-engine changes** in this
   campaign; short legs remain prohibited everywhere.
10. **Scope**: no edits to `research_protocol.yaml`; no new runtime
    dependencies; `TrialScope` struct unchanged.

## 5. Non-goals / deferred

Options-chain overlay generator (v2, after the M3 spike); post-publication
bar revisions (v2); minute bars, real vendor adapter, exchange transfers
(parked with the vendor decision); earnings-calendar events (arrive with
the options overlay); short legs / early assignment (still deferred,
long-only by construction).

## 6. M2 entry criteria (what this campaign unlocks)

When 1–10 are green and merged: the M2 campaign proper may begin —
`LabelEvent` construction over `visible_bars`, model fitting under
`FittingGuard`, trials registered before outcomes on dev worlds, and the
two-part validation gate on frozen worlds: false-positive control at
nominal level on null worlds + detection power on alpha worlds, every
artifact stamped with dataset lineage. Real-market claims remain gated
behind real data.

## 7. Risks

- **Scale**: ~1.1M pydantic rows at full scale — gate tests use small
  worlds; full-scale generation is an artifact run with measured timings
  recorded once. If ingest proves too slow, downsizing the default world
  is a spec change recorded in the registry, not a silent tweak.
- **Overfitting the generator**: mitigated by decision 4 (frozen
  validation worlds) + null-world FP control at M2.
- **Synthetic-to-real gap**: mitigated by the claims gate (§1.3) and by
  the generator recording its generating equations in the spec.
- **Sample drift** (M3): the spike cites URLs + download dates; it is a
  snapshot document, cheap to refresh.

## Post-implementation corrections (2026-08-19, review rounds 1–3)

The signed text above is preserved as the plan of record; these
corrections, surfaced by the independent review, supersede specific
claims and are recorded here rather than edited into the signed prose:

1. Criterion 7 (dataset lineage into `ArtifactStamp.dataset_manifest_hash`)
   is delivered PARTIAL: world identity reaches every manifest
   (`snapshot_id`, content hashes pinned per registry entry), but the
   automatic snapshot→stamp wiring is M2 machinery and is declared not
   delivered (see `docs/m2-synthetic-evidence.md` §4.7).
2. "Every generated world passes the quality gates" holds for every
   accepted spec via the round-1–4 fixes (hazard-bounded specs,
   $1.00 minimum close, application-time ratio decisions, and a ±1.5%
   cumulative alpha-drift wall that bounds every combined session factor
   strictly inside the gate) — proven by property arithmetic plus hostile
   exercise (12-seed sweep, alpha twin, enormous-coefficient drift-wall
   sweep), not by enumeration; see evidence §4.2.
3. The gate-time registry check verifies the dev-pool small worlds
   byte-exact; full-pool verification is the teed artifact run retained
   with the PR evidence.
