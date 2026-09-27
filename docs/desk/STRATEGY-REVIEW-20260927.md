# TREX strategy review, 2026-09-27

## Scope and custody

This is exploratory analysis of the frozen six-name replay
`replay-20260927T065530Z-87344cc363e3.json` (SHA-256
`87344cc363e31bf669afb6719272914b1346737ca6cbd07eaa9e2bdd32445228`).
The spec spans decision dates 2025-01-01 through 2025-06-30, entry DTE 30–90,
next-session VWAP entry, a 20-session exit, a 1% assumed per-leg haircut,
$0.65 commission per contract, and a $300 modeled maximum loss per trade.
It is not the sealed DESK-BT-001 study and has no broker fills.

MiniMax-M3 and glm-5.3-flash each reviewed a frozen source-bound prompt with
no tools. Prompt SHA-256:
`52ebfe78363264cfc2cd6d630e997a9eda4c8d83e3fae801bf0ba0a4a29f0d49`.
Both exited successfully and returned valid JSON. Their receipts and full
proposals are preserved under
`/home/alexk/documents/tree_options/artifacts/desk-store/evaluations/model-review/20260927/`;
the proposals are hypotheses, not validated strategy decisions.

## Six signal and structure cells

| Direction signal | Option structure | Evaluable modeled trades | Evidence status |
| --- | --- | ---: | --- |
| XSMOM TOP3 | Long call | 0 | No outcome sample. |
| XSMOM TOP3 | Call debit spread | 0 | No outcome sample. |
| XSMOM TOP3 | Put credit spread | 1 | One AVGO scenario, +$41.19 modeled P&L on $148.45 modeled max loss. |
| PEAD beats | Long call | 0 | No outcome sample. |
| PEAD beats | Call debit spread | 0 | No outcome sample. |
| PEAD beats | Put credit spread | 0 | No outcome sample. |

The replay attempted 21 rows. Sequential exclusion branches classify them
as 18 over the $300 cap, one without an entry leg, one without a required
entry or exit bar, and one evaluable. Six additional rows lacked decision-day
spot or option data before the attempt counter. Source code inspection
resolves a model-review error: these counters are mutually exclusive for this
run, not overlapping. Per-attempt records would still make the omissions and
loss distribution auditable by signal, name, date, and structure.

## Model review, checked against source

Both model reviews identified the useful next measurements: more option bars
and dates, loss-cap eligibility by structure, an overlap-aware $1,500 open
loss simulation, and fill sensitivity using measured historical quotes when
available. These are proposals to test. The first replay contains no
measured spread, executable quote, historical earnings announcement vintage,
daily portfolio path, or supervised IBKR paper receipt. Its one favorable
modeled trade gives no reliable win-rate estimate or signal ranking.

Flash's proposed systems graph is retained in its JSON proposal; the
source-checked integration graph is `HISTORICAL-SYSTEM-GRAPH.md`. The latter
marks the missing authority, mandate, permit, and broker-reconciliation
links. Neither model can authorize dispatch.

## Frozen next measurements

1. Report eligibility and exclusions by signal, structure, name, and date,
   retaining the existing $300 per-trade cap and the original raw counters.
2. Run the broader 35-name historical replay against the already captured
   caches; record data coverage and modeled outcomes without selecting a
   winning variant on the same dates.
3. Complete the long-dated cache capture for additional dates and strikes,
   then freeze a new replay spec and source digests before rerunning.
4. Measure quote spreads or obtain a suitable historical bid/ask source;
   vary fill and commission assumptions as sensitivity tests.
5. Simulate the $5,000 strategy budget, $1,500 concurrent open-loss ceiling,
   and overlapping positions. Set a holdout period before any parameter
   selection, and keep the sealed DESK-BT-001 verdict separate.

No profitability, high win rate, or paper-trading readiness is established.

## Full-window replay received later on 2026-09-27

The queued 35-name replay completed from decisions 2024-09-03 through
2026-08-31 with the same 30–90 DTE, 20-session exit, 1% assumed haircut,
and $300 trade cap. Its immutable artifact is
`replay-20260927T081700Z-6f2f03a2bd1f.json` (SHA-256
`6f2f03a2bd1fe9f15e88ed9f392e4130b7ab5db29924ca69e37e2f285bf15fe4`).
It consumed two response caches with 16,206 and 4,721 source files. It is
still exploratory and has no executable quotes or $5,000 portfolio path.

Of 243 attempted rows, 173 exceeded the loss cap, 32 lacked a selected leg,
12 lacked an entry or exit bar, and 26 were evaluable. Another 93 rows lacked
decision-day spot or options, and 69 had no eligible expiry/ATM before the
attempt counter. All six variants have fewer than 20 evaluable trades:

| Variant | Evaluable | Modeled wins | Unconstrained modeled P&L |
| --- | ---: | ---: | ---: |
| XSMOM / long call | 1 | 0 | -$119.13 |
| XSMOM / call debit | 9 | 5 | -$14.65 |
| XSMOM / put credit | 9 | 5 | +$398.66 |
| PEAD / long call | 3 | 0 | -$329.07 |
| PEAD / call debit | 4 | 0 | -$387.06 |
| PEAD / put credit | 0 | 0 | no sample |

Only six names produced evaluable rows; INTC accounts for 13 of the 26.
The favorable XSMOM put-credit cell has nine selected observations, with
assumed fills and no measured spread. It is a hypothesis for a future frozen
test, not evidence to activate or size that structure. The six cells were
viewed together, so picking the best one from this artifact is selection on
the same data.
