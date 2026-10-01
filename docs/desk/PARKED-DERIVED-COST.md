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
