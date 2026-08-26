# M4-B coverage era — closeout census (exit 5, owner escalation)

- date: 2026-08-26, branch `m4/coverage-era-results-20260826`,
  code_sha `0c13e8f3f3f19e28871eaf2eeeb786f75f092968` (merged main,
  PR #13)
- instrument: `scripts/build_coverage_census.py --capture-dir
  artifacts/m4b-coverage-era` — run ONCE (17:06:02Z → 17:07:29Z,
  `/home/alexk/documents/tree_options-logs/m4-census.log`), **exit 5 =
  emitted-but-incomplete, NOT whole coverage**; per the exit contract
  the artifacts stand and the census is never re-run.
- content_sha256:
  `43b0b040ea3c7936fc08e6b1028ce446e46c99f44ca1d87da9fec02099e12e14` →
  `artifacts/census/43b0b040ea3c/` (gitignored). Machine copy:
  `m4-b-coverage-census.json` (elided, §Elision below). Era narrative:
  `m4-b-coverage-era.md` §4.

## Coverage against the predeclared universe

| class | pairs |
|---|---:|
| COMPLETE | 2,871 |
| TRUNCATED | 0 |
| ERROR | 0 |
| MISSING | 0 |
| SPOT_MISSING_SESSION | **29 — the exit-5 driver** |
| SPOT_MISSING_HOLIDAY | 145 (EXPECTED) |
| **total == expected_masters** | **3,045** |

- expected_masters 3,045 == masters_observed 3,045 (manifest_entries
  3,045; capture_complete_false 0; rows_disagree 0; unparseable 0).
- rows_declared_total 10,049,160 == rows_parsed_total 10,049,160;
  distinct_contracts 1,046,940; spot_sessions_with_close 2,871 (support
  session_pairs 2,900 = 3,045 − 145); spot_holiday_fridays 5;
  bar_volume_observations 0 (NOT_EVALUABLE — the era ran `--bars 0`).
- The 29 session gaps are one Friday — **2026-08-21, all 29
  underlyings** — detail "session friday with no spot close (vendor
  availability gap)". This is the owner-escalation item.
- The 145 holiday gaps are the five holiday Fridays 2025-04-18,
  2025-07-04, 2026-04-03, 2026-06-19, 2026-07-03 × 29 underlyings —
  "no close by definition (exchange closed)". EXPECTED: holiday Fridays
  do not block exit 0.
- Exit contract (census.md verbatim sense): exit 0 iff zero pairs sit
  in INCOMPLETE_CLASSES (MISSING/TRUNCATED/ERROR/SPOT_MISSING_SESSION)
  and masters observed == expected_masters; otherwise the census is
  emitted with exit 5. Here: 29 session pairs ⇒ 5.

## Values (four-class taxonomy)

- observed_census_fact: the 13 counts above (confidence PARTIAL;
  bar_volume_observations NOT_EVALUABLE).
- predeclared_derivation_input: expected_masters 3,045 /
  universe_underlyings 29 / universe_fridays 105 (confidence EXACT).
- owner_ratified_policy_value: **empty by construction** — nothing is
  ratified by this evidence.
- not_yet_decided:
  - `flow_min_session_volume` — AWAITING_OWNER_RULE. The G3
    derivation-source contradiction, verbatim: "G3 packet Ask D derives
    flow_min_session_volume from 'era bar-volume distributions', but the
    coverage era captured --bars 0: no option bar exists until the
    ATM-grid bars era, which the declared sequence orders AFTER
    protocol 0.2.1. No derivation rule is repo-declared; the rule is an
    owner-ratified input bound to this census's content hash (owner
    decision 2026-08-23, to be resolved at era-results)."
  - `final_holdout_window` — AWAITING_OWNER_DECLARATION at era-results.

## Provenance

- code_sha `0c13e8f3f3f19e28871eaf2eeeb786f75f092968`; command
  `scripts/build_coverage_census.py --capture-dir artifacts/m4b-coverage-era`
- protocol_hash `77903fc77dc012cf7353c1dd8c127dfafa6bfe3716efb03e99d48f7aaf0e2ca9`
  (raw sha256 `04743de114aa39f717a528b1aa650ffd4163fce97dadfda7ebc82e093476660f`)
- input_manifest_sha256
  `1732e1d053d9da3c2d76afe2ac61a78ce8c350026dfc81d3c2a9d586a5c41029`
- universe_manifest_sha256
  `4553fc7adf35209bd4d43dc7695887fb3cc0345e3d14548c5bc91578833d85a1`
- uv_lock_sha256
  `e97782ffed746acd068e9662d7a484268e10bfc0ec7cb0f32ef928131cb8270b`

## Elision (committed machine copy)

`m4-b-coverage-census.json` is the census with its top-level keys,
order and every scalar verbatim, minus `coverage.findings` (174 entries
uniform modulo (underlying, as_of)): the 145 SPOT_MISSING_HOLIDAY
entries collapse to per-date counts; the 29 SPOT_MISSING_SESSION
entries — the exit-5 evidence — are retained in full. Digest caveat
(adversarial verify): `43b0b040…` is the builder's canonical
domain-separated content hash (dir name + self-declared field);
`sha256sum` of the emitted census.json BYTES is
`60ff012332fb67adc94be0d5a8adb15b162b9acb65a97e139aa156609f6baf53`,
which is what the sidecar `census.json.sha256` carries (bare-hex
format, so `sha256sum -c` refuses it). Neither number indicates
tampering; the committed copy is elided, not byte-identical — the
authoritative bytes live only in the gitignored digest dir.

## Nonclaims

1. Exit 5: this is NOT whole coverage — 29 session-Friday pairs
   (2026-08-21 × 29 underlyings) lack a vendor spot close.
2. No 0.2.1 value is set; no holdout window is declared; the census is
   never re-run (the artifacts stand for the escalation).
3. The 30-vs-29 underlyings docs discrepancy is recorded for the owner,
   not reconciled here (`m4-b-coverage-era.md` §4.4).
4. Raw vendor payloads and the census digest dir stay gitignored under
   `artifacts/`.
