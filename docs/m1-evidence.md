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
  `b5133db` (gate format step on a mutant literal), `761945a` (first
  evidence packet), `1abb96c` (round-1 remediation), `5c2b209` (round-2
  remediation), and the round-3 remediation (this commit)
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

`bash scripts/m0_gate.sh` at this packet's head
(log `/tmp/m1-gate-final4.log`, `GATE_EXIT=0`):

| Step | Result |
|---|---|
| `uv sync --frozen` | ok |
| `ruff format --check src tests scripts` | 78 files already formatted |
| `ruff check src tests scripts` | All checks passed |
| `mypy` | Success: no issues found in 46 source files |
| `compileall` | ok |
| `pytest -W error` | **288 passed, 0 failed, 0 skipped** |
| `scripts/mutate.py` | **KILLED=70, SURVIVED=0, INVALID_MUTANT=0, MUTATION_DRIFT=0, HARNESS_ERROR=0**; restoration full-suite pass=True |
| `uv build` + wheel smoke | ok |

Mutation artifacts:
`artifacts/m0-mutations.json` sha256 `33e46f36f8dda9a8e66abb578ad2b207fef10aa1cd7ad779731285ebc5d051ce`;
`artifacts/m0-mutations.md` sha256 `952d34dca61bc8ff50155b8d7a1336b7a38a3f002d91193ffb52aaa2b2b1b70a`.

## 4. Mutation totals (verbatim from the JSON artifact)

```text
totals: {'KILLED': 70}  total=70
restoration full-suite pass: True
```

61 M0 mutants unchanged + 9 M1 mutants (M62–M70), each with its OWNING
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
2. **Active + delisted present** — delisted SEC-001 carries bars AND
   membership; merged SEC-006 and bankrupt SEC-007 are carried in the
   master with point-in-time membership (no bars — absent vendor
   coverage, honestly absent rather than zero-filled).
3. **No current-survivor filtering** — `universe_as_of` at three dates
   (Feb/Sep/Nov 2024) moves names in and out exactly at their listing
   windows and delistings (`test_universe_is_point_in_time_not_survivors`,
   mutant M67).
4. **Fixtures pass, behaviors pinned** — rename (SEC-001 NEWM→OLDA),
   reuse (SEC-002 takes NEWM), split (2:1, 80→40), reverse split (1:2,
   10→20), cash dividend (kind + $0.25 asserted), merger (SEC-006→SEC-001
   successor), delistings with reasons asserted: voluntary_delisting
   (SEC-001), merger (SEC-006), bankruptcy_11 with final_price_available
   False (SEC-007).
5. **Provenance + available_at on every feature** — V1 features (`ret_1`,
   `dol_vol`) carry source, source_record_id, revision_id and
   available_at <= decision_at
   (`test_features_as_of_enforces_availability_and_provenance`).
6. **Injected future record refused** — at the READ gate (`visible_bars`,
   mutant M66) and in resolution (mapping visibility, mutant M65;
   whole-record visibility, mutant M69). No schema-level decision-time
   ordering is claimed (a raw row carries no decision context to order
   against; the naive-timestamp ban is a timezone-awareness rule, not a
   future-data control).
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

## 7. Independent review record

- **Round 1** (Codex, head `761945a`): **NO-GO** — delta sweep, mutation
  manifest/history, and the retained logs themselves verified clean, but:
  **P1-1** the manifest content hash bound only bars/actions — the MASTER
  (listing windows, delistings = the universe definition) and the manifest's
  own metadata (provider, counts, coverage, row-hash list) were not bound, so
  post-ingest master swaps could silently move `universe_as_of` and a swapped
  provider would be emitted as feature provenance; **P1-2** `TickerResolver`
  indexed bare mappings and checked only mapping availability — a mapping
  inside a not-yet-knowable master record resolved before the record itself
  was visible, bypassing the M0 record-visibility contract; **P2-1** evidence
  overclaims (criteria 2/4/6 broader than the tests: dividend/reasons were
  fixture literals, "schema-level rejection" misnamed the read gate);
  **P2-2** the packet cited the pre-packet gate run instead of the final one.
  Remediated in this commit, red-first: `content_sha256` now binds
  master+bars+actions and `verify_manifest` cross-checks provider, counts,
  coverage, and the row-hash list (`test_master_tampering_is_detected`,
  `test_manifest_metadata_is_bound`; mutant M68); the resolver enforces
  whole-record visibility (`test_resolver_respects_master_record_availability`;
  mutant M69); the dividend/reason literals are pinned assertions; §3 cites
  this packet's final gate run. Final line of the round-1 verdict, verbatim:
  "NO-GO"
- **Round 2** (Codex, head `1abb96c`): **NO-GO** — P1-1 and P1-2 verified
  RESOLVED, P2-2 RESOLVED, delta/mutation/evidence-log items RESOLVED, but
  three findings: **P1-3** (new) the outer `snapshot_id` was rebindable
  post-ingest — nothing compared the outer id, the manifest id, and the
  per-row ids, so a renamed outer snapshot passed verification and the
  authority then trusted the altered id; **P2-3** (new) the metadata test
  pinned only provider and bar_count — action_count, session coverage,
  the row-hash list, source_row_count, and security_count were unchecked,
  so the round-1 remediation wording overstated the proof; **P2-1
  residual** criterion 6 still credited the naive-timestamp ban as a
  future-record control (it is a timezone-awareness rule). Remediated in
  this commit, red-first: verify_manifest binds outer+manifest+per-row
  snapshot identity FIRST (`test_snapshot_identity_is_bound`; mutant M70),
  checks source_row_count and security_count, and the metadata test
  tampers every recomputable field except schema_version (closed at
  round 3); the intended criterion-6 rewording did NOT land in that
  commit (a silent string-replace miss — caught at round 3). Final line
  of the round-2 verdict, verbatim: "NO-GO"
- **Round 3** (Codex, head `5c2b209`): **NO-GO** — P1-3 and P2-3 verified
  RESOLVED with file:line citations (identity binding + every round-2-named
  metadata field), the delta sweep found no new executable defect, and the
  retained gate/clean-clone evidence verified internally consistent — but
  three doc/evidence P2s: **P2-1 residual** criterion 6 still credited the
  naive-timestamp ban as a future-record control because the round-2
  rewording never actually landed (and §7 falsely said it had); **P2-4**
  `schema_version` is recomputable but was neither verified nor tampered;
  **P2-5** §1's commit list was stale ("this commit" still meant the
  round-1 remediation). Remediated in this commit: criterion 6 actually
  reworded (verified by assertion this time), `verify_manifest` pins
  `schema_version` (red-first: the metadata test tampers it), §1 names
  every commit including both remediation hashes, and this §7 record
  states the round-2 miss honestly. Final line of the round-3 verdict,
  verbatim: "NO-GO"
- **Round 4** (Codex, this head): verdict recorded in the PR body.
