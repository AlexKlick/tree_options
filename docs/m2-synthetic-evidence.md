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

1. **Determinism** — `test_same_spec_is_byte_identical` (payload, master,
   truth equal; same-id ingest ⇒ identical `content_sha256`;
   `test_different_seed_diverges`); registry byte-exact reproduction
   covers it at full scale in the artifact run. ✔
2. **Contract compliance** — `test_generated_world_ingests_and_verifies`
   (ingest + `verify_manifest` green, provider `synthetic/v1`,
   schema `m2/1`) and `test_tampered_world_fails_quality_gate`
   (duplicate bar caught by the right gate). ✔
3. **Registry integrity** — `test_registry_shape_and_pools` (exactly 5
   validation worlds, both kinds, disjoint seeds),
   `test_registry_pins_generator_code`, `test_verify_worlds_cli_gate_subset`;
   full-pool artifact run WORLDS_OK=9 MISMATCH=0. ✔
4. **Scenario coverage** — `test_lifecycle_scenarios_present`: all eight
   event kinds + later-cohort IPOs + recycled tickers occur in a
   gate-speed dev world. ✔
5. **Sector PIT** — `test_sector_pit.py` (8 tests: reclassification,
   leak window, unknown-sector, invisible record, validation rejections);
   `test_every_security_carries_sector`. ✔
6. **Truth separation** — `test_truth_sidecar_import_boundary` (AST walk:
   no module outside `tree_options.synth.*` imports `synth.truth`). ✔
7. **Provenance stamps** — world identity rides `snapshot_id` into every
   manifest and thus into `ArtifactStamp.dataset_manifest_hash`; the
   amended M2 rule and seed rule are quoted in packet §1 (verbatim). ✔
8. **M3 spike delivered** — six sections, samples cited by URL + access
   date 2026-08-18. ✔
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
   bankruptcy sessions bounded 40–49% (the 2× discontinuity gate);
   ratio-event sessions surrender the day's market move (exact closes);
   events at most one per security-session after two listed sessions;
   publication 23:00 UTC. Sector reclassification events are NOT
   generated in v1 (schema supports them; fixture covers them).
4. **Deferred, declared**: options-chain overlay (v2, informed by the M3
   spike), post-publication bar revisions (v2), minute bars, real vendor
   adapter, exchange transfers, earnings events, short legs/assignment.
5. **Nonclaims**: no real market data anywhere in this campaign; the M3
   spike is sample-based documentation; no statistical property of the
   generator is claimed beyond determinism and gate cleanliness.

## 6. Review record

- To be appended per round.

## 7. Evidence invalidation

Any change to code, tests, protocol, dependencies, or the synth package
after the final head invalidates this packet — including the registry
generator pin (the registry test fails closed and demands an explicit,
recorded `--recompute`).
