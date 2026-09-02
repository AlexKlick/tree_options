# m4-g4-sealed/1 — sealed real-data gate (evidence log)

- head: `8ee37e930cdc5c7bc59bac383d6decb4efca519b`
- verdict: **FAIL** (recorded verbatim; one-shot — no re-run inside the campaign regardless of outcome)
- trial statuses: `{'lane2|A': 'COMPLETED', 'lane2|B': 'COMPLETED'}`

| # | criterion | verdict | lane-1 applicability |
|---|-----------|---------|----------------------|
| 1 | `manifest_integrity` | PASS | applied |
| 2 | `candidate_discipline` | PASS | declared_inapplicable |
| 3 | `fill_discipline` | FAIL | declared_inapplicable |
| 4 | `rejection_paths_live` | PASS | applied |
| 5 | `determinism` | PASS | applied |
| 6 | `mutation_campaign` | PASS | applied |

## manifest_integrity — PASS

- no failures
- reported (counts + samples):

```json
{
  "lane1": {
    "contract_count": 7630,
    "verified_series": 7630
  },
  "lane2": {
    "bars_files": 15631,
    "era_target": {
      "distinct_contracts": 1046940,
      "expected_masters": 3045
    },
    "master_row_refusals": 478,
    "masters_files": 3045,
    "verified_series": 1046462
  }
}
```

## candidate_discipline — PASS

- lane 1: lane-1-inapplicable: the T+1 publication wall means a close(t) decision on the ONE retained session sees no published file, so no accepted-candidate cross-section exists to judge — the volume-flow threshold pin and the NOT_APPLICABLE/OI-withheld disclosure family are the lane-2 regime's clauses
- no failures
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
      "delta_pass_rows": 127,
      "earnings_span_not_applicable_rows": 127,
      "flow_min_session_volume": 100,
      "n_positions": 90,
      "no_in_band_strike": 181,
      "open_interest_not_applicable_rows": 127
    },
    "lane2|B": {
      "delta_pass_rows": 119,
      "earnings_span_not_applicable_rows": 119,
      "flow_min_session_volume": 100,
      "n_positions": 91,
      "no_in_band_strike": 182,
      "open_interest_not_applicable_rows": 119
    }
  }
}
```

## fill_discipline — FAIL

- lane 1: lane-1-inapplicable: one retained session forbids an execution session (the purchase lane is closed; the $0 ruling stands) — no stamped fill can exist on lane 1
- failures (verbatim):
  - lane2: participation cap exceeded — OPT-COST-260515-P-00097500/2026-04-09: cumulative 6 > observed 4
- reported (counts + samples):

```json
{
  "n_fills": 271,
  "over_participation_pairs": 1,
  "participation_pairs": 254,
  "sample_participation": [
    {
      "bar_session": "2025-11-06",
      "contract": "OPT-AAPL-251219-C-00027500",
      "cumulative": 10,
      "observed": 3491
    },
    {
      "bar_session": "2025-11-13",
      "contract": "OPT-AAPL-251219-P-00027000",
      "cumulative": 10,
      "observed": 1635
    },
    {
      "bar_session": "2025-12-11",
      "contract": "OPT-AAPL-251219-P-00027000",
      "cumulative": 10,
      "observed": 5771
    },
    {
      "bar_session": "2025-12-04",
      "contract": "OPT-AAPL-260116-C-00028500",
      "cumulative": 10,
      "observed": 2261
    },
    {
      "bar_session": "2025-12-31",
      "contract": "OPT-AAPL-260116-C-00028500",
      "cumulative": 10,
      "observed": 2201
    }
  ]
}
```

## rejection_paths_live — PASS

- no failures
- reported (counts + samples):

```json
{
  "floor": 50,
  "lane1": {
    "class_map": "FIRING parse refusals only",
    "counted": 0,
    "floor": 0,
    "zero_bid_rows_disclosed": 723
  },
  "lane2": {
    "class_map": "zero-volume-bar refusals + MassiveDerivationError + master-row refusals + session_volume_flow below-min FAIL",
    "counted": 29658,
    "massive_derivation_error_refusals": 29121,
    "master_row_refusals": 478,
    "no_bar_not_evaluable_disclosed": 43828440,
    "session_volume_flow_fail": 59,
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
  "killed": 367,
  "killed_entries": 367,
  "registry_digest_match": true,
  "registry_supplied": true,
  "registry_total": 367,
  "report_head": "8ee37e930cdc5c7bc59bac383d6decb4efca519b",
  "restoration_suite_passed": true,
  "sealed_head": "8ee37e930cdc5c7bc59bac383d6decb4efca519b",
  "supplied": true,
  "total": 367,
  "verdict_logic_mutants": [
    "M244-g4-cboe-foreign-schema-accepted",
    "M245-g4-cboe-real-verifier-bypassed",
    "M246-g4-massive-foreign-schema-accepted",
    "M247-g4-massive-foreign-capture-accepted",
    "M248-g4-massive-real-verifier-bypassed"
  ]
}
```

