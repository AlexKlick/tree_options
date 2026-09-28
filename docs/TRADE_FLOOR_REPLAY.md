# Historical trade floor

The `#/trade-floor` cockpit page replays decisions from the three model
traders as a spectator game. It uses a compact artifact built from completed
historical research. Loading the page does not call a model or a broker.

Each round shows the same as-of candidate board to Z.ai, Z.ai Flash, and
MiniMax. The viewer can step or play through the decisions, then reveal the
later modeled trade-bar mark. Each three-month window starts with $5,000 of
virtual closed capital. The screen keeps skipped and blocked entries visible
and excludes the documented calibration failure from every score.

## Data path

1. `scripts/export_desk_trade_floor.py --run <qualified-run-dir> --out <file>`
   verifies source, sample, provider, analysis-scope, and graph hashes. It
   reconstructs model choices, risk, later marks, and window scores before
   writing a compact `desk-trade-floor-replay/1` artifact. Output publication
   is exclusive and atomic; an existing output is never overwritten.
2. Publish that JSON under `<DESK_STORE>/evaluations/trade-floor/`. The
   app uses the serving checkout's ignored desk store by default. The current
   service uses `/home/alexk/documents/tree_options/artifacts/desk-store`.
   Keep raw provider responses and market bars in the qualified run, outside
   the web store.
3. `GET /api/desk/trade-floor` validates and whitelists up to four compact
   artifacts. Invalid evidence returns 503. The route has no write or order
   method; `POST` returns 405. The response and artifact both state that
   execution is disabled.

The initial published replay contains 23 comparable rounds across three
disjoint three-month windows. It is a bounded model sample from a larger
historical capture, so its scoreboard is an exploration aid, not a measured
trading edge. Option trade bars are valuation proxies; they cannot establish
executable quotes, fills, slippage, fees, assignment, or live profitability.

To add a later run, preserve its raw receipts and analysis-scope exclusions,
export a new compact artifact, validate it against the API, and publish it
with a new filename. Model sampling and replay generation remain separate
from any broker-paper authority path.
