# M4 — real-data options campaign plan (staged lane)

Status: plan of record for the shakedown stage (M4-A). Owner ruling
2026-08-21: proceed from the M3 base with the **staged lane** — a $0
adapter/coverage shakedown first, the deep-history vendor purchase
decided only after real coverage numbers are in hand.

## 1. Coordinates

- Branch `m4/real-data-20260821`, base `main` @ `6e56017` (post-M3:
  PR #6 merged `0e010f7` + evidence `025b07c` + gate3 record `6e56017`).
- Prior packets this builds on: `docs/m1-evidence.md` (vendor-shaped
  ingest seam: `build_payload`/`ingest_snapshot`, no-silent-drop,
  `source_row_hash`), `docs/m3-options-schema-spike.md` (field
  inventory, vendor shortlist, §6 generator implications),
  `docs/m3-options-evidence.md` §9 (this milestone's entry criteria).
- Retained real sample: `~/m2-evidence/cboe-sample/` (Cboe "Option EOD
  Summary" demonstration subset, hashes pinned in its SHA256SUMS.txt —
  zip `981be1aa…`, no-cgi CSV `e45af427…`; 32,672 data rows, one
  session 2023-08-25, SPY root, 34 columns, two versions showing the
  CGI-license difference for index underlyings).
- `research_protocol.yaml` (0.1.0) stays FROZEN through M4-A. No world
  registry edits. The final holdout remains undeclared per the standing
  correction (declared only AFTER real-data coverage inspection — M1
  §residual).

## 2. Decision gates

- **G0 — DONE**: staged lane ruled (this document).
- **G1 — DONE (2026-08-21)**: adapter shakedown green. The Cboe EOD
  adapter ingested the retained sample with zero silent drops, correct
  two-snapshot quote semantics, and a manifest pinning the source bytes;
  full local gate green (615 passed under `-W error`, mutation 141/141
  KILLED). Evidence: `docs/evidence-logs/m4/m4-a-shakedown.md`.
- **G2 — RESOLVED (2026-08-21): $0 workaround, purchase lane CLOSED.**
  First a partial ruling: the free Massive (Polygon) lane as the $0
  structural coverage era (M4-B; evidence
  `docs/evidence-logs/m4/m4-b-massive-structural.md`). Then the owner
  ruled **"no money spent — find a workaround"**: the sealed-window
  purchase (Massive Advanced / Cboe DataShop / ThetaData) will NOT
  happen. The quote-bearing capability comes instead from the free
  tier's per-contract daily aggregates (VWAP/volume/trade-count) plus
  model-implied greeks from the repo's own pricer — live-proven the
  same day; see `docs/m4-zero-dollar-workaround.md` for the
  blocked→workaround matrix, provenance classes, probe, honest limits,
  and the M4-C build order.
- **G3 — real ingest + amendment window (owner-gated, post-purchase;
  NOT started)**: real-data adapter run on purchased data, coverage
  re-inspected, then the protocol/world amendment window (new real-world
  registry entries, provider/schema tokens, holdout declaration) as its
  own packet. Its manifest prerequisite — a manifest/verify pair with
  input-hash lineage — is now satisfied by the `massive_manifest`
  module.
- **G4 — sealed real-data gate (NOT started)**: two-lane sealed
  validation on real worlds per the M3 discipline (one-shot, verdict
  verbatim, mutation campaign, clean-clone determinism). Separate plan
  at G3.

## 3. M4 workstreams ($0, this stage)

WS-A/B/C are the M4-A shakedown (delivered, on `main`). WS-D is the
Massive free-tier lane added by the G2 partial ruling of 2026-08-21.

**WS-A — Cboe EOD adapter** (`src/tree_options/data/cboe_eod.py` +
`src/tree_options/data/real_overlay.py`, ~400 LOC, ~35 tests):

- Parse the 34-column CSV into contract master rows + per-session day
  files. Column mapping (spike §1): `bid_size_1545/bid_1545/
  ask_size_1545/ask_1545` and the `_eod` quartet → the two
  `OptionQuoteSnapshot`s per contract-day (15:45 + close; a session
  with no 1545 snapshot is an early-close, matching the M3 shape);
  `trade_volume` → `same_day_volume`; `open_interest`;
  `delta_1545` → `abs_delta` (vendor-observed; the no-vendor-greeks
  rule was a generator constraint, not an ingest constraint — the
  inspector quantifies zero-greeks rows instead of importing them
  blindly); `strike`/`expiration`/`option_type`/`delivery_code` →
  contract identity (delivery_code is EMPTY in the sample for
  standard deliverables — trailing empty field is expected, not
  corruption); `underlying_bid_*/underlying_ask_*` → file-level
  underlying quotes (feeds the candidate filter without the parked
  equity vendor, spike §6.6).
- `RealOptionOverlay` implements the read surface `OptionPitSurface`
  consumes (`publication_of`, `entry_for`/`day_file`,
  `eligible_sessions`, `quote_history`, `contract`) so the ENTIRE M3
  PIT/backtest machinery runs unchanged on real data. T+1 publication
  wall (09:00 America/New_York next session) declared in the adapter
  spec and validated against `QuoteEvent.exchange_timestamp <=
  received_timestamp` for every row.
- `build_real_options_manifest` mirrors the synthetic manifest
  discipline with new tokens — provider `cboe-option-eod/1`,
  schema_version `m4/1` — pinning: source CSV sha256s + row count,
  contract master hash, sample-slice re-hashes, content self-binding.
  A parallel `verify_real_options_manifest` (fail-closed) checks them.
- Ingest accounting inherits the M1 rule: every input row is parsed,
  hashed, and either mapped or reported — `IngestionError`-style
  aggregation, zero silent drops. Known-odd rows (zero greeks,
  zero-bid, duplicate keys) are COUNTED into the coverage artifact,
  never dropped.
- Red-first tests: parse fidelity against hand-computed rows from the
  retained sample; duplicate-contract refusal; hash-pinning; the
  exchange<=received invariant; early-close handling; cgi vs no-cgi
  index-zeroing difference (no-cgi zeroes index underlying quotes —
  the adapter must REFUSE a no-cgi file for index underlyings rather
  than ingest zeros as tradable quotes).

**WS-B — coverage inspector** (`scripts/inspect_options_coverage.py`,
~250 LOC + ~15 tests): one-pass statistics over an ingested real
overlay: rows/underlying-day, contracts/underlying-day, strike-grid
width in spot units, live-expiry ladder depth, zero-bid fraction,
zero-greeks fraction, spread-as-fraction-of-mid bands vs the spike §6
priors (1–2% ATM, wider wings), OI/volume concentration by
moneyness/tenor, delivery_code distribution, early-close days.
Machine JSON + markdown report into `docs/evidence-logs/m4/`.

**WS-C — decision brief** (docs-only): rows/year and storage
extrapolation against spike §5 (re-verified prices at decision time),
universe/window options framed for the ruling, and the ThetaData
free-tier connector status (the connector is a small adapter variant
gated on an owner-provided API key — account creation is owner-only;
the key never enters the repo).

**WS-D — Massive (Polygon) free-tier structural lane** (client +
adapter + capture bridge + structural inspector):
`src/tree_options/data/massive_client.py` (wire: key custody, 5
req/min governor, key-redacted content-addressed cache, entitlement
gate), `src/tree_options/data/massive_options.py` (option semantics;
`build_option_candidate_inputs()` raises unconditionally — structural
only), `src/tree_options/data/massive_manifest.py` (manifest/verify
pair with input-hash lineage), `scripts/capture_massive_structural.py`
(budget-bounded live capture bridge — the only networked tool), and
`scripts/inspect_structural_coverage.py` (offline analysis).
Tests: `tests/unit/test_massive_client.py`,
`tests/unit/test_massive_options.py`,
`tests/unit/test_massive_manifest.py`,
`tests/unit/test_capture_massive_structural.py`,
`tests/unit/test_inspect_structural_coverage.py` — 142 lane tests.
First live capture 2026-08-21 (2 underlyings × 4 as_ofs, 44/45
requests, evidence `docs/evidence-logs/m4/m4-b-massive-structural.md`);
operator procedures in `docs/m4-massive-runbook.md`.

## 4. Nonclaims (M4-A)

- No research claims of any kind: the sample is a ONE-SESSION
  demonstration subset; every number it produces is schema/coverage
  evidence, never performance evidence.
- No protocol, registry, holdout, or sealed-gate edits.
- Vendor prices are checkout-variable; nothing here authorizes a
  purchase (G2 is the owner's gate, spike nonclaims carry forward).
- The `spans_earnings` candidate-filter input stays a declared
  non-goal until an events source exists (spike §6.7); the adapter
  records it as NOT_EVALUABLE-equivalent for real data, flagged in
  the coverage report rather than silently False.

## 5. Sizes and order

| WS | New files | ~LOC | ~Tests | Depends on |
|---|---|---|---|---|
| A adapter + manifest + overlay | 2 src + tests | 400 | 35 | — |
| B coverage inspector | 1 script | 250 | 15 | A |
| C decision brief | 1 doc | — | — | B |

Order A → B → C, one PR (MU-3) after the local gate is green. G2
delivery is the PR's exit artifact. Estimated wall-clock: 1–2
sessions.

## 6. Risks

- **Sample ≠ product drift** (readme warns versions differ): the
  adapter validates column presence + semantic invariants per file and
  fail-closes on drift; re-shake after any purchase against day 1 of
  the delivered data before bulk ingest.
- **Index underlyings**: no-cgi files zero the index underlying quotes
  (SPX) and VIX has none at all — refuse, don't ingest zeros; the
  cgi/historical variant is the only index-capable lane.
- **Adjusted chains across years**: single-session sample exercises
  none of it (splits, renamed roots) — the multi-year lane at G3 adds
  an adjustments pass; flagged in the brief.
- **Vendor delta semantics**: deep-ITM zero-greeks rows are counted
  and excluded from filter inputs via the existing NOT_EVALUABLE
  discipline, never treated as delta=0 candidates.
