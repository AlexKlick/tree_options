# Historical intraday action graph, 2026-09-27

Paper deployment status: **BLOCKED** pending executable quote/fill evidence,
supervised broker authority, and separate operator approval.

This is a research replay and read-only cockpit projection. It cannot place an
IBKR order, approve a mandate, or establish an executable spread price.

## Frozen source and schedule

The pre-window contract reference is the 2026-05-08 Massive capture for SPY,
QQQ, and IWM. The selection file under
`/home/alexk/documents/tree_options/artifacts/desk-store/evaluations/intraday-graph/20260927-v1/contract-selection-4mo-expanded.json` identifies its source
files and SHA-256 (`b7a4943deb71056bd9627a6e14cbb895edff80367b1a32a722ba6d59020853f4`).
The minute-bar capture under
`/home/alexk/documents/tree_options/artifacts/desk-store/evaluations/intraday-graph/20260927-v1/minute-bars-4mo-expanded.json` has 72/72 selected
option series and 339,457 traded-minute bars over 2026-05-25 through
2026-09-25; SHA-256 is
`37de77aa629f30edca74777befecc6d25287ba8c7d9a02a3cbdcf0efd277965f`.
The free-tier wire capture used 54 requests for the first selection and 18 for
the expanded selection. The other active Massive structural capture was
paused during each wire batch and resumed afterward.

The checked NYSE calendar produces two overlapping three-month windows:

| Window | Sessions | Scheduled decisions |
| --- | ---: | ---: |
| 2026-05-26 through 2026-08-25 | 64 | 512 |
| 2026-06-25 through 2026-09-24 | 64 | 512 |

There are eight decision instants per regular session, from 10:00 to 15:15 ET.
An early-close session uses five instants ending at 12:40 ET. The windows
share 43 sessions; their observations and outcomes must not be counted as
independent evidence.

## Decision and graph contract

At each scheduled instant, the engine constructs defined-risk verticals from
option minute bars observed at or before that instant and no older than 15
minutes. Missing or stale leg observations omit that candidate. A choice may
select one displayed candidate or skip. The action graph records the snapshot,
potential trades, choice, risk decision, later proxy entry, and later mark.
Entry uses the first subsequent traded-minute close for each leg within 15
minutes. A position remains open when no fresh exit marks exist. All prices
are trade-bar proxies and two legs may have different trade timestamps.

The modeled capital profile is $5,000 intended capital, $300 maximum loss per
entry, $1,500 combined reserved open loss, and a $300 realized daily loss stop.
All rejections and open positions remain in the graph. The runner can replay
external, outcome-free candidate IDs or an explicit no-trade, put-credit,
call-credit, put-debit, or call-debit fixed rule. Every replay creates a full
graph and a small read-only summary for `/api/desk/intraday-graphs` and the
Action Model page. The API strips candidate/action rows from its summary.

## Provider pilot and limits

At the 2026-08-26 10:00 ET snapshot in the initial 54-series capture, Z.ai
`glm-5.3`, Z.ai Flash `glm-5.3-flash`, and MiniMax `MiniMax-M3` each returned a
validated one-turn response to the same anonymized, outcome-free packet.
All selected the same put-debit spread. The provider-reported combined cost
was $0.666124, with a $0.30 per-call budget. Their receipts are at
`/home/alexk/documents/tree_options/artifacts/desk-store/evaluations/intraday-graph/20260927-v1/model-pilot-aug26-1000/manifest.json` (SHA-256
`f099506c65e7f0fd9b2125c11c5a3af5ccc75fab69f9f052a47450d509b3385a`).
The one-day later-bar replay modeled a $2 gain on that one choice, with $30
reserved maximum loss. It is one observation and supplies no high-win
probability or measured strategy edge. The chosen low-premium debit spread is
especially poor evidence for the stated preference for repeatable small wins.

No historical bid/ask quotes, synchronized two-leg fills, spread costs,
assignment, fees, slippage, live order handling, or broker-paper outcome is
established by this data. `long_recent_trade_move` and
`short_recent_trade_move` are backward-looking option trade-price movement,
not implied volatility or calibrated win probability. A favorable proxy P&L
must not be promoted to a deployable strategy.

## Validation state

`ruff check` on the changed Python files passed. The focused action-model and
desk replay suite passed 45/45 (`/tmp/trex-pr-focused-tests.log`). After the
month-end and source-custody correction, the intraday graph and desk web tests
passed 9/9 (`/tmp/trex-pr-iteration-tests.log`). The exact-head web check
passed TypeScript, 186/186 Vitest tests, production build, and bundle check
(`/tmp/trex-pr-web-check.log`); that run preceded the later Python-only
correction. The RAM-admitted rolling policy sweep is queued, so no full-window
strategy result is verified. No cockpit deployment or broker paper order
occurred.

The RAM launcher queued the full replay and web gates behind other active
work. A request to cancel the separate longdated capture to release capacity
returned exit 75 with `foreign job cancellation requires root operator
confirmation`; that job was left running. A queued workload is not a pass.
