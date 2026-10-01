# PARKED: the derived-basis cost contract (quant-research lane)

The 2026-09-30 quant-research lane (`cc91b78`, 54 commits) carried a
parallel cost model: `desk-derived-cost/1`, a spread surface DERIVED from
the bundle's own reported marginals ("derived_from_reported_marginals"),
with its own governance vocabulary (`surface_kind`, split-local nulls,
entry_count_unit). It was an independent evolution of the same interface
the measured-cost PR (#48) landed from the CBOE chain corpus.

## Why parked, not merged

Two models claiming the same authority is the exact defect the provenance
dedup (PR #53) retired: one corpus, one description. The measured model
(`cost.py measured-spread/1`, n=18,783 tradeable rows of 612,371 quotes)
went through the adversarially-reviewed eight-PR wave, backs the sealed
restart threshold, and is the authority. The derived basis was never
reviewed against it.

## What is parked (blob shas at merge commit afd0bec)

- tests/unit/test_desk_derived_cost.py — 7e9569c0704f7588b16e69ebdd1162a4060157eb (15 tests)
- tests/unit/test_desk_derived_cost_governance.py — 748545e2171aa6879d03e205832a289450005cd5 (15 tests)

Their pinning targets (`surface_kind`, `desk-derived-cost/1`,
derived-spread basis) exist nowhere in the merged tree; the tests were
deleted rather than kept red. To revive the idea, re-derive it ON the
measured model's corpus and compare against `docs/desk/COST-CORPUS-STATS.md`.

## Kept from the lane instead

Everything additive: qsl.py, vixfloor.py, research tooling, cockpit
workspace/review UI, M0 transcript retention, and the honesty machinery
(assessment_class, duplicate-snapshot rejection, source custody) adopted
onto the landed contract.

## Also parked: the resume-custody / governance test suite (same category)

Two more test files pin the lane's parallel evolution of the EXECUTOR rather
than the cost model. Their invariants are GOOD and worth a named follow-up
campaign that re-derives them onto the landed engine (blob shas at merge):

- tests/unit/test_desk_longrun_custody.py — 811f3edf18a80f1469379dfebf7e3a342a50d681
- tests/unit/test_desk_measurement_governance.py — dc3975d5176b63c84313aa81f5c805a72c93f204

Invariants recorded for that follow-up (from the parked test names):
resume refuses changed payload / policy identity / scoring engine / source
metadata under unchanged board ids; outcome-table bytes bound before reusing
receipts; pre-custody plans cannot be silently adopted; duplicate snapshots
and repeat copies cannot inflate decision coverage or independent test-entry
counts; heldout participation/horizon cannot change tune ranking or tune
null; floor and null-version serialized and invalid values rejected; floor
change refuses resume without mutating registration; retrospective scoring
never claims confirmatory eligibility; whole+row vs actual-package pair
identity distinguished before outcome lookup; pair completion stays unknown
if either exit is missing; pair exit refuses contradictory instants.

ADOPTED tonight instead (the portable parts): assessment_class on every
digest (registered_protocol / retrospective_descriptive; redigest prefixes
its headline), source_hashes custody at the outcome-table loader,
OutcomeCache.bind_boards (conflicting/duplicate board identity refused),
and outcome-table collision detection (repeated key with differing payload
refused). Follow-up must not re-derive these four — they are live.
