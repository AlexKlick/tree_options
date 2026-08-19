# M2 synthetic era — evidence packet

Scope: the synthetic-era campaign per `docs/m2-synthetic-era-plan.md`
(owner sign-off 2026-08-18). Generator + registry + M3 spike ONLY — no
model, no labels, no backtest, no real data, no performance claim.

## 1. Coordinates

- Base: `main` @ `8bd34e4` (the PR #2 merge commit)
- Branch: `m2/synthetic-era-20260818`
- Code head (all product work): `d96b15d`; this doc commits on top.
- Commits (oldest → newest): `a35cf5a` campaign packet · `23df665`
  workstream A (PIT sector schema, manifest m2/1) · `4f45e4d` workstream
  B (synthetic world generator v1) · `e75bb10` workstream C (frozen world
  registry + verify) · `056b6b1` workstream D (M3 options spike, doc) ·
  `d9249a9` workstream E (mutants M71–M78 + M43 re-pin) · two gate-step
  style commits → `d96b15d`.

## 2. What was delivered (packet workstreams A–E)

- **A — PIT sector schema**: `SectorMappingRecord` (sector,
  effective_from, available_at) mirroring the ticker-mapping knowability
  discipline; `SecurityMasterRecord.sector_on(d, as_of)` fails closed on
  invisible records and returns None (honestly unknown) when no visible
  mapping is effective. The bare `sector_as_of` date is removed.
  `MANIFEST_SCHEMA_VERSION` bumped `m1/1 → m2/1` red-first.
- **B — generator v1** (`src/tree_options/synth/`): `WorldSpec` →
  `generate_world(spec, calendar) → GeneratedWorld` emitting vendor-shaped
  rows through the UNCHANGED M1 ingest→manifest→authority pipeline
  (provider `synthetic/v1`, world identity in `snapshot_id`, 23:00 UTC
  publication). Stdlib-only, per-stream seeded `random.Random` — same
  spec = byte-identical payload. Null and alpha worlds with the same seed
  share seats/sectors/tickers/event timelines; only closes differ.
  Lifecycle: staggered IPOs (initial 60% + 8/yr), renames with ticker
  recycling, splits/reverse/stock-dividend with EXACT ratio-derived
  closes, cash dividends, merger with successor, bankruptcy delisting
  (loss bounded 40–49% to stay under the 2× discontinuity gate),
  voluntary delisting, coverage lapse (finite end, no event). Truth
  sidecar (`synth/truth.py`) is unreachable outside `tree_options.synth.*`
  (AST-enforced import-lint test).
- **C — frozen world registry** (`data/worlds/registry.json` +
  `scripts/verify_worlds.py`): 9 worlds pinned to seeds/rates (never
  rewritten) and to the generator code sha (re-pinned only by an explicit
  `--recompute`). 5 frozen VALIDATION worlds (3 null + 2 alpha, full
  scale) are the synthetic-era holdout per packet §1.4; the dev pool has
  2 gate-speed small worlds + 2 full worlds. Seeds are disjoint across
  pools. Artifact run (teed): `WORLDS_OK=9 MISMATCH=0 SELECTED=9
  CODE_PIN=match` in 3m44s; full worlds carry ~680–712k bars, ~1,100
  actions, ~365–376 listed seats of 500 (300 initial + ~72 IPOs at 8/yr
  × 9y — exactly to spec). The gate runs the dev-pool small-world subset
  in-test; the registry test fails closed on any synth/ code change
  until a deliberate `--recompute`.
- **D — M3 options schema/coverage spike** (`docs/m3-options-schema-spike.md`):
  six sections from the downloaded Cboe DataShop Option EOD Summary
  sample (observed 34-column Calcs layout, dual NBBO snapshots 15:45+EOD,
  delta-gap named, T+1 PIT semantics, SPY coverage stats, storage
  estimate with stated assumptions, priced vendor shortlist). No
  production code, no ingest.
- **E — this packet** + owner-scoped mutants M71–M78.

## 3. Gate (release authority — local)

`bash scripts/m0_gate.sh` at code head `d96b15d` (`/tmp/m2-gate-3.log`,
sha256 `c7e6f5c0bcd8ece4…`):

| Step | Result |
|---|---|
| `uv sync --frozen` / format / lint / `mypy` / compileall | clean (50 source files) |
| `pytest -W error` | **310 passed, 0 failed, 0 skipped** |
| `scripts/mutate.py` | **KILLED=78, SURVIVED=0, INVALID_MUTANT=0, MUTATION_DRIFT=0, HARNESS_ERROR=0**; restoration full-suite pass |
| `uv build` + fresh-venv wheel smoke | ok |

61 M0 mutants + 9 M1 mutants unchanged + **8 new M2 mutants M71–M78**
(sector leak window, seed-stream pinning, alpha injection, publication
hour, ticker-recycle truth, initial-cohort listing, bankruptcy bound,
split exactness) — every kill behavioral. One anchor re-pin: **M43**
(security.py) now spans the ticker-specific loop because `sector_on`
legitimately added the same single-line shape; recorded in its invariant.

The OFFICIAL gate re-runs at this document's head (final head) and is
attached to the PR alongside a `git clone --no-local` clean-clone proof.

## 4. Acceptance criteria (packet §4)

1. **Determinism** — `test_same_spec_is_byte_identical` now compares the
   CANONICAL BYTES of every bar, action, and master record (not just
   model equality), the same-id ingest `content_sha256`, and
   `test_different_seed_diverges`. Full-scale byte-exact reproduction is
   the retained verify-all artifact run (§3). ✔
2. **Contract compliance** — `test_generated_world_ingests_and_verifies`
   (ingest + `verify_manifest` green, provider `synthetic/v1`,
   schema `m2/1`), `test_tampered_world_fails_quality_gate`, and — round-1
   P1-2 — `test_hostile_rate_spec_stays_gate_clean`: ANY spec the
   validator accepts generates a gate-clean world (a 200/yr split rate
   drives prices to the floor; unfulfillable ratio events are suppressed). ✔
3. **Registry integrity** — `test_registry_shape_and_pools` (exactly 5
   validation worlds with the EXACT frozen composition: seeds 701–705,
   3 null + 2 alpha; dev seeds 101–104, disjoint),
   `test_registry_pins_generator_code`, `test_verify_worlds_cli_gate_subset`,
   and — round-1 P1-3 — `test_verify_worlds_gates_quality_not_just_hashes`:
   regeneration runs `verify_manifest`, so a quality-invalid world can
   never be pinned or reported OK. ✔
4. **Scenario coverage** — `test_lifecycle_scenarios_present` covers all
   eight event kinds AND their payload/master ownership (snapshot action
   kinds include split/reverse/cash-dividend/merger; master delisting
   reasons include all three terminal kinds), plus later-cohort IPOs and
   recycled tickers. ✔
5. **Sector PIT** — `test_sector_pit.py` (reclassification, leak window,
   unknown-sector, invisible record, validation rejections, and — round-1 —
   bound fixture sector CONTENT, not just mapping presence);
   `test_every_security_carries_sector`; and
   `test_features_panel_passes_availability_audit`: a `features_as_of`
   panel over a generated world passes `AvailabilityGuard.audit_panel`
   with zero rejections at the guard's own decision instants. ✔
6. **Truth separation** — round-1 P2-1 strengthened the boundary: the AST
   test now rejects ANY import of `tree_options.synth` (not just
   `synth.truth`) from every module outside `synth.*` — a bare synth
   import would expose `generate_world(...).truth`. ✔
7. **Provenance stamps — PARTIAL, honestly scoped (round-1)**: world
   identity rides `snapshot_id` into every manifest (M70 identity binding)
   and the registry pins content hashes per world. What this campaign does
   NOT deliver: automatic propagation from a dataset snapshot into
   `ArtifactStamp.dataset_manifest_hash` — the stamp takes a caller hash
   by design, and the wiring of `TrialRecord.dataset_manifest_hash` +
   world id in `TrialScope.feature_set_id` is M2 machinery (packet §6
   entry criteria). Claimed only as far as built.
8. **M3 spike delivered** — six sections, samples cited by URL + access
   date; the sample zip/CSVs/readme are retained with sha256 at
   `~/m2-evidence/cboe-sample/` (round-1 P2-3) and the T+1 claim is scoped
   to the documented DATE, not a zero-hour leak window. ✔
9. **No model/labels/backtest/fill changes; short legs prohibited** — no
   code outside `synth/` + sector schema + registry/script/docs/tests. ✔
10. **Scope** — no `research_protocol.yaml` edits, no new runtime
    dependencies (pydantic+pyyaml only), `TrialScope` unchanged. ✔

## 5. Owner decisions and declared limitations

1. **Amended M2 rule (verbatim, 2026-08-18)**: "M2 pipeline machinery may
   be built and validated on synthetic data with known ground truth;
   every artifact must carry dataset provenance (dataset=synthetic/vN);
   and any real-market performance or discovery claim remains prohibited
   until real-data gates are green."
2. **Seed/holdout rule (verbatim)**: frozen validation-seed registry
   declared BEFORE the first M2 trial; dev seeds separate; M5 records
   world lineage; null-world false-positive control is an M2 acceptance
   criterion. The validation pool (seeds 701–705) is not to be used for
   tuning from this point on.
3. **Generator engineering defaults (not claims about real markets)**:
   bankruptcy sessions bounded 40–49% and ALL undeclared overnight moves
   clamped inside the 2× discontinuity gate (`DAILY_RET_LIMIT = ln 1.9`,
   round-1 remediation — fat tails previously could emit an ungated
   doubling); downward ratio events that would floor-clamp the close are
   SUPPRESSED, not emitted (round-1 P1-2); `WorldSpec` rejects empty
   sector lists and per-session event hazards ≥ 1.0; ratio-event sessions
   surrender the day's market move (exact closes); events at most one per
   security-session after two listed sessions; publication 23:00 UTC.
   Sector reclassification events are NOT generated in v1 (schema supports
   them; fixture covers them).
4. **Registry re-pin record (round-1)**: the remediation changed the
   generator, so the registry was deliberately re-pinned with
   `--recompute` (seeds/rates untouched). Both gate-speed dev worlds are
   BYTE-UNCHANGED (no clamp or suppression fires there); every full world
   re-pinned with ~10–13 fewer actions — the floor suppressions the
   hostile-rate test proves necessary, plus second-order event-timing
   shifts from the eligibility interaction.
4. **Deferred, declared**: automatic dataset→stamp provenance propagation
   (M2 machinery, criterion 7 partial); options-chain overlay (v2,
   informed by the M3 spike), post-publication bar revisions (v2), minute
   bars, real vendor adapter, exchange transfers, earnings events, short
   legs/assignment.
5. **Nonclaims**: no real market data anywhere in this campaign; the M3
   spike is sample-based documentation; no statistical property of the
   generator is claimed beyond determinism and gate cleanliness.

## 6. Review record

- **Round 1** (Codex, head `2b5c815`): **NO-GO** — P1-1 `sectors=()`
  accepted then crashes generation; P1-2 unbounded rates (hazard ≥ 1
  monopolizes the walk; floor-clamped ratio events break the 2% gate, so
  "every accepted spec is gate-clean" was false); P1-3 `verify_worlds`
  never ran the quality gates (the pre-fix recompute had silently pinned
  a quality-INVALID full world — proven live by the fix's first re-pin
  refusing on an undeclared 2.17× overnight move); P2-1 truth reachable
  via `generate_world().truth` (boundary test too narrow); P2-2 M74's
  kill was a construction crash (`hour+1`=24), not detection; P2-3 M3
  sample unretained + T+1 overclaimed; plus doc-truth items (DRAFT plan
  status, stale §3 citation, unsupported stamp-propagation claim,
  shape-only registry/sector assertions, same-process-only determinism).
  → remediation: spec validators (non-empty sectors, total hazard < 1),
  floor suppression + `DAILY_RET_LIMIT` clamp (hostile-rate test red
  first), `verify_manifest` inside regeneration (monkeypatch-proven),
  strengthened tests (canonical bytes, exact registry composition, bound
  sector content, payload-owned lifecycle, alpha/null structural identity,
  audit-panel gate), M74 re-pinned to `hour-1`, new mutants M79/M80,
  registry deliberately re-pinned, plan status updated, M3 sample retained
  with hashes + T+1 scoped, criterion 7 honestly re-scoped to PARTIAL.
  Post-fix: suite 316/0, KILLED=80/80.

## 7. Evidence invalidation

Any change to code, tests, protocol, dependencies, or the synth package
after the final head invalidates this packet — including the registry
generator pin (the registry test fails closed and demands an explicit,
recorded `--recompute`).
