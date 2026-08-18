# M1 Evidence Packet — point-in-time equity data authority

Written only from captured artifacts (logs cited inline). **Invalidation
rule:** any change to code, tests, protocol, or dependencies after the
exact head this packet was gated at invalidates this packet in full.

## 1. Repository coordinates

- Repository: `/home/alexk/documents/tree_options`, branch
  `m1/pit-equity-data-20260818`, base `main` @ `6d0d9aa` (the PR #1 merge
  commit — M0's reviewed head `fb85771` is its parent, evidence preserved)
- Commits: `34bcfe1` (slice 1: workstreams B/D/E), `840c980` (slice 2:
  workstream C), `952f5d4` (slice 3: merger/bankruptcy fixtures),
  `b5133db` (gate format step on a mutant literal)
- Working tree: clean at gate time (the gate refuses a dirty tree and
  asserts, at exit, that the head did not move)
- M0's evidence packet is INVALID past `fb85771` by its own rule; the M0
  gate artifacts in `artifacts/` below are the M1-head regeneration.

## 2. Scope

M1 packet workstreams A–E against a SYNTHETIC vendor-shaped fixture
(`tests/fixtures/raw_vendor.py`): no paid vendor data has been ingested
(owner decision pending — see §6). The data AUTHORITY is complete and
mutation-proven; the vendor adapter that feeds it real payloads is the
next, owner-gated step.

## 3. Gate (release authority — local, per standing operator rule)

`bash scripts/m0_gate.sh` at head `b5133db`
(log `/tmp/m1-gate-run1b.log`, `GATE_EXIT=0`):

| Step | Result |
|---|---|
| `uv sync --frozen` | ok |
| `ruff format --check src tests scripts` | 78 files already formatted |
| `ruff check src tests scripts` | All checks passed |
| `mypy` | Success: no issues found in 46 source files |
| `compileall` | ok |
| `pytest -W error` | **284 passed, 0 failed, 0 skipped** (45.10s) |
| `scripts/mutate.py` | **KILLED=67, SURVIVED=0, INVALID_MUTANT=0, MUTATION_DRIFT=0, HARNESS_ERROR=0**; restoration full-suite pass=True |
| `uv build` + wheel smoke | ok |

Mutation artifacts:
`artifacts/m0-mutations.json` sha256 `144fad4acd8c22c492ece5a442dd4efd53e9452a91f7ad80846a5e1cb719b753`;
`artifacts/m0-mutations.md` sha256 `d43696bd3b58a6a99dd8da5fde48a1873da91a678989e5374b9f862afe584b9f`.

## 4. Mutation totals (verbatim from the JSON artifact)

```text
totals: {'KILLED': 67}  total=67
restoration full-suite pass: True
```

61 M0 mutants unchanged + 6 M1 mutants (M62–M67), each with its OWNING
test; every new kill was verified behavioral in the artifact detail (the
owner's own FAILED line — the round-9/10 M60 lesson is standing
practice). Anchor history, recorded honestly: M66/M67 first ran as
MUTATION_DRIFT (ruff format had moved both anchors into comprehension
clauses); they were re-pinned to the post-format text and re-run to
KILLED — never skipped. M64's replacement literal was reformatted
(quote style only, `b5133db`) to satisfy the format gate; the anchor and
the mutation semantics are unchanged.

## 5. Acceptance criteria (packet §"M1 acceptance criteria")

1. **Rows trace to raw records** — every normalized bar's
   `source_row_hash` is the sha256 of its raw row's canonical bytes
   (`test_every_bar_traces_to_its_raw_row`).
2. **Active + delisted present** — delisted SEC-001, merged SEC-006 and
   bankrupt SEC-007 all carry bars/membership in the snapshot and master.
3. **No current-survivor filtering** — `universe_as_of` at three dates
   (Feb/Sep/Nov 2024) moves names in and out exactly at their listing
   windows and delistings (`test_universe_is_point_in_time_not_survivors`,
   mutant M67).
4. **Fixtures pass** — rename (SEC-001 NEWM→OLDA), reuse (SEC-002 takes
   NEWM), split (2:1, 80→40), reverse split (1:2, 10→20), cash dividend,
   merger (SEC-006→SEC-001 successor), delisting (voluntary + merger +
   chapter-11 with no final price).
5. **Provenance + available_at on every feature** — V1 features (`ret_1`,
   `dol_vol`) carry source, source_record_id, revision_id and
   available_at <= decision_at
   (`test_features_as_of_enforces_availability_and_provenance`).
6. **Injected future record rejected** — schema-level availability
   ordering, the read gate (`visible_bars`, mutant M66), and point-in-time
   resolution (mutant M65) all refuse it.
7. **Current-ticker-join mutation killed** — M65 guts the mapping
   visibility check; the owner test fails.
8. **Byte-identical manifests on re-ingestion** — the manifest is a pure
   function of content; `retrieved_at` comes from the payload, never the
   wall clock (`test_manifest_is_byte_identical_on_re_ingest`, mutant M64
   binds it to content).
9. **No model, no performance chart** — nothing outside `src/tree_options/
   data/` + fixtures/tests/mutate.py changed; no training code exists.

## 6. Owner decisions and declared limitations

1. **Real vendor adapter pending** — the fixture payload proves the
   authority; choosing and wiring a real equity data source (security
   master with delistings, prices, corporate actions) is the owner-gated
   next step. No paid data was ingested.
2. **Daily bars only** — the 10:00 ET entry / 15:30 ET exit windows need
   minute bars (handoff §7 Phase 1); minute-bar ingestion is deferred to
   the vendor-adapter step or M3/M4, owner's call.
3. **Exchange transfer unmodeled** — `SecurityMasterRecord` carries a
   single exchange field; modeling a mid-listing exchange transfer needs
   a listing-history structure (protocol-relevant schema change). Not
   silently skipped: declared here for an owner decision.
4. **Sector/market context data not ingested** — handoff §12 M1 lists it;
   deferred with the vendor adapter (residual-return labels need it
   before M2).
5. **Holdout rule not yet declared** — per the standing correction, the
   final-holdout rule is declared only AFTER real-data coverage
   inspection; no real data has landed, so nothing is pre-declared and
   nothing is contaminated.
6. **No model, no labels, no backtest** — M1 acceptance 9; also no
   performance claim of any kind in this packet.
