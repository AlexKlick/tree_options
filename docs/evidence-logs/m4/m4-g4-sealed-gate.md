# m4-g4-sealed/1 — sealed real-data gate (evidence log)

- head: `646a0dfe50966bf5e035c832971344d043a3f139`
- verdict: **FAIL** (recorded verbatim; one-shot — no re-run inside the campaign regardless of outcome)
- trial statuses: `{'lane2|A': 'COMPLETED', 'lane2|B': 'COMPLETED'}`

| # | criterion | verdict | lane-1 applicability |
|---|-----------|---------|----------------------|
| 1 | `manifest_integrity` | FAIL | applied |
| 2 | `candidate_discipline` | FAIL | declared_inapplicable |
| 3 | `fill_discipline` | PASS | declared_inapplicable |
| 4 | `rejection_paths_live` | FAIL | applied |
| 5 | `determinism` | PASS | applied |
| 6 | `mutation_campaign` | PASS | applied |

## manifest_integrity — FAIL

- failures (verbatim):
  - lane 2: verified series 1046462 != the era's stamped distinct_contracts 1046940
- reported (counts + samples):

```json
{
  "lane1": {
    "contract_count": 7630,
    "verified_series": 7630
  },
  "lane2": {
    "bars_files": 0,
    "era_target": {
      "distinct_contracts": 1046940,
      "expected_masters": 3045
    },
    "masters_files": 3045,
    "verified_series": 1046462
  }
}
```

## candidate_discipline — FAIL

- lane 1: lane-1-inapplicable: the T+1 publication wall means a close(t) decision on the ONE retained session sees no published file, so no accepted-candidate cross-section exists to judge — the volume-flow threshold pin and the NOT_APPLICABLE/OI-withheld disclosure family are the lane-2 regime's clauses
- failures (verbatim):
  - lane2|A: no counted open_interest NOT_APPLICABLE disclosure row — the dropped-with-disclosure term must never be a silent pass
  - lane2|A: no counted earnings_span NOT_APPLICABLE disclosed-absence row (owner ruling m4-022-ruling-20260828) — the 0.2.2 disclosure family is present + counted, never a silent pass
  - lane2|B: no counted open_interest NOT_APPLICABLE disclosure row — the dropped-with-disclosure term must never be a silent pass
  - lane2|B: no counted earnings_span NOT_APPLICABLE disclosed-absence row (owner ruling m4-022-ruling-20260828) — the 0.2.2 disclosure family is present + counted, never a silent pass
- reported (counts + samples):

```json
{
  "accepted_delta_provenance": [
    "vendor",
    "model-derived-from-vwap"
  ],
  "derivation_provenance": "model-derived-from-vwap",
  "per_trial": {
    "lane2|A": {
      "delta_pass_rows": 0,
      "earnings_span_not_applicable_rows": 0,
      "flow_min_session_volume": 100,
      "n_positions": 0,
      "open_interest_not_applicable_rows": 0
    },
    "lane2|B": {
      "delta_pass_rows": 0,
      "earnings_span_not_applicable_rows": 0,
      "flow_min_session_volume": 100,
      "n_positions": 0,
      "open_interest_not_applicable_rows": 0
    }
  }
}
```

## fill_discipline — PASS

- lane 1: lane-1-inapplicable: one retained session forbids an execution session (the purchase lane is closed; the $0 ruling stands) — no stamped fill can exist on lane 1
- no failures
- reported (counts + samples):

```json
{
  "n_fills": 0,
  "over_participation_pairs": 0,
  "participation_pairs": 0,
  "sample_participation": []
}
```

## rejection_paths_live — FAIL

- failures (verbatim):
  - lane 1: pooled FIRING parse refusals 0 < 50 (zero-bid rows 723 are the disclosed audit statistic, NOT counted)
- reported (counts + samples):

```json
{
  "floor": 50,
  "lane1": {
    "class_map": "FIRING parse refusals only",
    "counted": 0,
    "zero_bid_rows_disclosed": 723
  },
  "lane2": {
    "class_map": "zero-volume-bar refusals + MassiveDerivationError + master-row refusals + session_volume_flow below-min FAIL",
    "counted": 478,
    "massive_derivation_error_refusals": 0,
    "master_row_refusals": 478,
    "no_bar_not_evaluable_disclosed": 10033184,
    "session_volume_flow_fail": 0,
    "zero_volume_bar_refusals": 0
  }
}
```

## determinism — PASS

- no failures
- reported (counts + samples):

```json
{
  "diverged": [],
  "extra_in_replay": [],
  "missing_in_replay": [],
  "payload_count": 4,
  "replayed": true
}
```

## mutation_campaign — PASS

- no failures
- reported (counts + samples):

```json
{
  "killed": 357,
  "killed_entries": 357,
  "registry_digest_match": true,
  "registry_supplied": true,
  "registry_total": 357,
  "report_head": "646a0dfe50966bf5e035c832971344d043a3f139",
  "restoration_suite_passed": true,
  "sealed_head": "646a0dfe50966bf5e035c832971344d043a3f139",
  "supplied": true,
  "total": 357,
  "verdict_logic_mutants": [
    "M244-g4-cboe-foreign-schema-accepted",
    "M245-g4-cboe-real-verifier-bypassed",
    "M246-g4-massive-foreign-schema-accepted",
    "M247-g4-massive-foreign-capture-accepted",
    "M248-g4-massive-real-verifier-bypassed"
  ]
}
```

