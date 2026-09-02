# M4-G4 sealed-event remediation 2 — the successor event's recorded FAIL verdict (2026-09-01)

## 1. The verdict (history, one-shot spent)

The successor sealed event (run `a93589eb18ac1ded40af52dd02cac08d5c78664c3d72a629a4a785367a5c92c8`,
head `646a0dfe…`, APPROVAL `f98193c3…` → CONSUMPTION `74463cce…`, after
the owner RECONCILIATION `7b67484f…` re-armed content `0a1ea282…`)
completed and recorded **verdict=FAIL, 3 of 6 criteria**:

- PASS `fill_discipline`; PASS `determinism` (the clean-clone replay
  reproduced every stamped payload hash byte-identically); PASS
  `mutation_campaign` (357/357 bound at the sealed head).
- FAIL `manifest_integrity`; FAIL `candidate_discipline`; FAIL
  `rejection_paths_live`.

Per the ratified terms (both approval records): FAIL = a remediation packet
plus a NEW pre-declared gate, never an in-place re-run. The content's
budget is exhausted (2 consumptions vs 1 reconciliation).

## 2. The root cause (read-only census, from the run's own stamped payloads)

**The sealed lane-2 input was the coverage-era manifest — masters + spot
proxy, ZERO bars.** The complete bars capture (3,045 masters + 15,631 bars
+ spot) lives at `artifacts/bars/capture/` and was never wired into the
sealed-input path (the campaign's owed "bars closeout" hygiene item).
`load_derived_surface("artifacts/bars/capture")` verifies clean
(2026-09-01, read-only probe). With zero bars every one of the 10,033,184
cells is honestly `not_evaluable_nobar` → zero candidates → the 0.2.2
disclosure rows never emit (their wiring is verified present at
`candidates/filters.py` — per-candidate rows, data-starved not unwired) →
zero positions. The three FAILs cascade from this one input-path fact plus
two criterion-domain mismatches:

- `manifest_integrity`: the era census stamps the MASTERS domain
  (1,046,940 distinct contracts); the manifest verifies the OVERLAY-ACCEPTED
  domain (1,046,462). The overlay's 478 refused master rows (identity
  restatements / canonical-id collisions / schema refusals — counted +
  disclosed as `master_row_refusals` in the run's own census) close the gap
  exactly: 1,046,462 + 478 = 1,046,940. The raw equality conflated domains.
  (Rebuilding the coverage-era overlay read-only refuses exactly 478 —
  receipt `g4-remediation` logs, 2026-09-01.)
- `candidate_discipline`: no counted `open_interest` NOT_APPLICABLE /
  `earnings_span` disclosed-absence rows because no candidate ever
  evaluated. Resolved by the input-path fix, not by amendment.
- `rejection_paths_live` lane 1: the pre-declared floor (≥50 FIRING parse
  refusals, "the PENDING owner calibration") — its premise measured FALSE:
  the real retained Cboe session parses perfectly clean (0 firing
  refusals; the 723 zero-bid rows are the disclosed audit statistic, not
  counted). Lane 2's counted classes pooled 478 + flow FAILs (≥ 50 ✓).

## 3. The owner rulings (2026-09-01, AskUserQuestion)

1. **Lane-2 input → the complete bars-era capture**
   (`artifacts/bars/capture/capture_manifest.json`, typed-verified). The
   new lane2 hash means a NEW content identity — the next event is FRESH
   one-shot authority (no reconciliation record needed).
2. **`manifest_integrity` → the CUSTODY IDENTITY**: verified series +
   counted master-row refusals == the era's stamped distinct_contracts.
   Both sides counted and disclosed; a row accounted for by neither side
   is the real failure (silent loss).
3. **`rejection_paths_live` lane-1 floor → 0 on real data** (the lane-2
   floor stays the pre-declared 50; fixture/pre-declared gates pass an
   explicit lane-1 floor to keep their teeth; path-liveness carried by
   lane 2's counted classes + the fixture gates).

## 4. This packet (branch `m4/g4-remediation-20260901`)

- `docs/m4-g4-sealed-gate-plan.md` §4: both amendments recorded in the
  plan (the criteria's source document), with `data/g4/sealed-criteria.json`
  re-transcribed in lockstep (the parity test + source hash pinned).
- `src/tree_options/seal/g4_gate.py`: criterion 1 evaluates the custody
  identity (refused rows read from the run's own stamped census, also
  reported); criterion 4 takes a per-lane floor —
  `REJECTION_LANE1_FLOOR = 0` (the ruling) with fixture gates passing
  explicit floors (`run_m4_sealed_gate.py` keeps the pre-declared 50; the
  machinery tests pass MINI_FLOOR).
- The fixture era census stamps the MASTERS-domain count (6 = 5 verified +
  1 refused), making the fixture honest under the identity.
- Tests red-first: `test_criterion1_is_the_custody_identity` (identity
  PASS / gap FAIL naming custody),
  `test_the_real_lane1_floor_is_zero` (default 0 PASS / explicit 5 FAIL).
  Mutants M370/M371 (registry 357→359), 2/2 KILLED with full-suite
  restoration; all 359 anchors audited exactly-once.

## 5. Forward (the NEW pre-declared gate)

1. This packet merges (owner merges NORMAL).
2. The next event's driver points `lane2_manifest` at the bars-era
   capture; fresh preflight at the merged head (the gate's mutation report
   must be stamped at the EXACT sealed head — the 2026-09-01 event's
   lesson: criterion 6 binds the report to rev-parse HEAD).
3. New gate → new packet (NEW content identity — fresh one-shot authority)
   → owner declaration + approval → the one-shot event. The two prior
   consumptions stay history.

## 6. The bounded review round (Codex, gpt-5.6-sol) — NO-GO, 2 P1, both probe-verified and fixed

- **P1-1 the custody identity accepted a foreign census.** Probe: the
  2026-09-01 zero-bars event's census stamp (manifest `419794d2…`)
  satisfied the identity against a packet verifying a DIFFERENT manifest
  (`119b0d2f…`), and a compensated count (verified −1, refusals +1) also
  passed. Fixed: the run's lane-2 census must name THE manifest the held
  packet verified — `_criterion_manifest_integrity` binds the census's
  `typed_manifest_content_hash` to the packet's (a mismatch FAILs naming
  the binding; `evaluate_and_record` threads it from the held packet).
  With the binding, a compensated count can only come from tampering the
  run's own mid-execution artifacts — the determinism comparison and the
  disclosed concurrent-tamper class own that.
- **P1-2 the documented real-data CLI forced the fixture floor.** The CLI
  (`run_m4_sealed_gate.py`, the documented real-data entry) hardcoded
  `rejection_lane1_floor=50` — a clean real lane 1 through it would take a
  false FAIL. Fixed: `--rejection-lane1-floor` defaulting to the ruling's
  0 (50 restorable for fixture rehearsal); the CLI test asserts the
  evidence json's reported lane-1 floor is 0.

Mutants M372/M373 added (registry 359→361); M370–M373 4/4 KILLED with
full-suite restoration; all 361 anchors audited exactly-once. Reviewer
receipts (focused 63/63, M370/M371 2/2 KILLED at the reviewed head,
read-only probes only): `g4-remediation-codex.log`.
