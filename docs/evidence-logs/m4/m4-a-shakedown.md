# M4-A integration shakedown — real Cboe EOD sample end-to-end

- date: 2026-08-21, branch `m4/real-data-20260821` (uncommitted integration tree)
- source: `/home/alexk/m2-evidence/cboe-sample/UnderlyingOptionsEODCalcs_2023-08-25_cgi_or_historical.csv`
  (sha256 `1732795c0b38ad0dec0f66dff30e2de4dbbd41423a375f6a166a137319e74dd7`)
- driver: one-off `/tmp/m4_shakedown.py`; full capture `/tmp/m4-shakedown.log`
  (every number below is verbatim from that log; machine copy `m4-a-shakedown.json`)
- stack under test: `tree_options.data.cboe_eod` (WS-A adapter) →
  `tree_options.data.real_overlay.RealOptionOverlay` → **UNMODIFIED**
  `tree_options.data.options_pit.OptionPitSurface` (one `cast` at the call site),
  then `scripts/inspect_options_coverage.py` (WS-B inspector).

## 0. Integration reconciliation (the only cross-agent drift found)

The inspector CLI called `parse_cboe_eod_csv(csv, variant=...)` with no
underlying selection, but the retained demo bundles 4 underlyings in one file
and the adapter refuses those (`CboeEodMultiUnderlyingError`) — the CLI could not
run over the demo at all. Fixed consumer-side (fewest symbols; adapter
untouched): `scripts/inspect_options_coverage.py` gained `--underlying
SYMBOL` (default `None`) forwarded to the parser, plus
`test_cli_forwards_underlying_selection` in
`tests/unit/test_inspect_options_coverage.py` (22 → 23 tests). Everything
else the two agents contracted already matched (WS-B had cross-validated
against WS-A's landed modules before handoff).

## 1. Adapter parse (cgi file, per-underlying selection)

`rows_total = 32672` for the file under every selection (rows of unselected
symbols counted, never dropped); `sessions = 1` (`2023-08-25`).

| underlying | rows_mapped | duplicates | zero_greeks | zero_bid | early_close | nonstandard_delivery | contracts | distinct expiries |
|---|---|---|---|---|---|---|---|---|
| SPY | 7599 | 0 | 31 | 723 | 0 | 0 | 7630 | 29 |
| TSLA | 4075 | 0 | 163 | 259 | 0 | 0 | 4238 | 19 |
| ^SPX | 16277 | 2972 | 117 | 864 | 0 | 0 | 16394 | 50 |
| ^VIX | REFUSED | | | | | | | |

Totals over mapped underlyings: rows_mapped 27,951; duplicates 2,972;
zero_greeks 311; zero_bid 1,846; contracts 28,262. ^SPX issues_count=2977
(2,972 duplicate-row refusals + 5 summary lines).

Refusals exercised (all expected, all fail-closed):

- `CboeEodUnderlyingQuoteError` — ^VIX, degenerate underlying quote
  `0.0000/0.0000` (zero quotes even in the cgi file; matches the plan's
  "VIX has none at all").
- `CboeEodMultiUnderlyingError` — the file parsed without a selection names
  the underlyings and refuses.
- `IndexUnderlyingNotLicensedError` — ^SPX in the `no_cgi` file with
  `--variant no_cgi` (named license flattening); the same file parsed
  WITHOUT declaring the variant still refuses via the degenerate 0/0
  backstop (`CboeEodUnderlyingQuoteError`) — defense in depth.

## 2. OptionPitSurface smoke queries (SPY; surface code untouched)

- `publication_of(2023-08-25) = 2023-08-28T13:00:00+00:00` (09:00 ET on the
  next weekend-skipping session; Friday → Monday).
- `visible_file_session`: before-wall `None`, at-wall `2023-08-25`,
  after-wall `2023-08-25` — the T+1 read gate on real data.
- `live_expiries_as_of(after)`: 29 expiries (2023-08-25 … 2025-12-19).
- Strike ladders: front expiry (same-day 2023-08-25) depth **158**
  (275.00–540.00); deepest ladder 2023-12-15 depth **240**.
- `spot_mid_as_of(after) = 440.7900` (file pair 440.7800/440.8000).
- `contracts_as_of(after) = 7630`; `contracts_existing_on(2023-08-25) = 7630`
  (full C/P grid of observed ladders).
- Probe contract `OPT-SPY-230825-C-00044100` (front expiry, strike 441.00):
  `entry_as_of` abs_delta 0.4077, OI 17,981, volume 261,509;
  `quote_history` 2 events — 15:45 `0.2700/0.2800` (99/291) and EOD
  `0.0000/0.0100` (0/572), both `received=2023-08-28T13:00:00+00:00`,
  exchange stamps 19:45/20:00 UTC on the session date;
  `visible_quotes_as_of(after)` = same 2 events.
- `candidate_snapshot(decision_session=2023-08-25)` **refuses**
  (`NoOptionFileError`) — correct T+1 behavior: a close(t) decision can
  never see file(t), which publishes 09:00 ET t+1. (Multi-session snapshot
  paths are covered in the unit suites; the single-session demo cannot
  exercise them.)
- Real-data manifest round-trip VERIFIED:
  `content_sha256=8a8ff05659541f9f…`,
  `contract_master_sha256=e27a9ba74f75c43a…`, `contract_count=7630`.

## 3. Coverage inspector (CLI, one underlying per run)

Machine JSON: `/tmp/m4-coverage-{SPY,TSLA,SPX}.json` (regenerable:
`.venv/bin/python scripts/inspect_options_coverage.py --csv <csv>
--variant cgi_or_historical --underlying <sym> --out-json <path>`).

| underlying | contract rows | ATM n / median / p90 | wings n / median / p90 |
|---|---|---|---|
| SPY | 7599 | 711 / 1.17% / 13.33% | 6888 / 3.13% / 200.00% |
| TSLA | 4075 | 485 / 1.67% / 3.54% | 3590 / 3.40% / 100.00% |
| ^SPX | 16277 | 2133 / 1.29% / 2.31% | 13971 / 2.38% / 66.67% |

Other inspector lines (verbatim from the log): SPY concentration — delta
band 0.30-0.60 OI 16.10% / volume 20.55%; nearest-4 expiries OI 9.30% /
volume 70.59%; delivery codes standard=7599 nonstandard=0. TSLA — OI 16.59%/
13.27%, nearest-4 29.83%/91.20%. ^SPX — OI 14.12%/14.88%, nearest-4
5.29%/68.48%, zero-bid fraction 1.06% (173 fully-zero EOD quotes).
`spans_earnings` is reported `NOT_EVALUABLE` with an explanatory note for
every underlying (never a silent False).

Metric-definition note (both are correct, they measure different things):
the parse stat `zero_bid_rows` counts rows where every PRESENT snapshot bid
is 0 (includes one-sided EOD `0.00/ask>0` markets — SPY's 723), while the
inspector's day-table `zero-bid` counts fully-zero EOD quartets
(`0/0`, zero-mid — SPY 0, ^SPX 173); the inspector's `zero-greeks` day
count is 0 by construction because zero-greeks rows are not materialized as
chain entries — the authoritative zero-greeks count is the parse stat echoed
in the inspector's odd-rows line (SPY 31 / TSLA 163 / ^SPX 117).

## 4. Findings carried to G2 (purchase-decision brief)

- ATM medians 1.17–1.67% of mid sit at/above the top of the spike §6.3
  prior (~1-2%); wing medians 2.4–3.4% with heavy p90 tails (SPX 66.7%,
  TSLA 100%, SPY 200%) — relative spreads blow up in the zero-mid wings.
- ^SPX/SPXW: 2,972 rows (≈15% of ^SPX rows this session) are duplicate
  (underlying, expiry, strike, C/P) cells where SPX and SPXW roots collide
  on the canonical id; first row wins, second refused — a real coverage
  finding for the vendor conversation (root is not part of the canonical
  identity).
- ^VIX quotes are zero even with CGI — unusable from this product.
- Volume concentrates in the nearest-4 expiries (SPY 70.6%, TSLA 91.2%,
  SPX 68.5%) while OI is far more spread — tenor depth beyond the front 4
  is thin for per-session execution assumptions.
- `underlying_20d_median_dollar_volume` is a declared `Decimal("0")`
  sentinel on real overlays (this product has no underlying volume):
  candidate filters must set their liquidity threshold to 0 for real data.
