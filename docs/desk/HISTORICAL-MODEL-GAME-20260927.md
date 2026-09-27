# TREX blind historical model exercise, 2026-09-27

Paper deployment status: **BLOCKED**. This report is a completed historical
selection exercise; broker authority and supervised paper proof are separate.

This is a research exercise on the 35-name exploratory replay, not a broker
paper trial or a strategy promotion. The replay SHA-256 is
`6f2f03a2bd1fe9f15e88ed9f392e4130b7ab5db29924ca69e37e2f285bf15fe4`.
The source contains 26 evaluable modeled option trades after missing-data and
$300 risk-cap exclusions. The first 13 decisions before 2025-09-01 are training;
the later 13 are a blind selection period. The split was made once before the
new model calls.

`tree_options.desk.model_game.prepare` masks names and calendar dates, shows
training modeled P&L, and removes blind P&L. The prompt SHA-256 is
`0f87376ebd1c6b7687317b2fb39abd8a0043ab8eb4b7af144ee0b406666af1d3`.
Z.ai `glm-5.3`, Z.ai `glm-5.3-flash`, and MiniMax `MiniMax-M3` each received
the same prompt through a no-tools, one-turn provider call. Raw envelopes,
stderr, parsed proposals and hash receipts are held under
`/home/alexk/documents/tree_options/artifacts/desk-store/evaluations/model-game/20260927-blind-001/`.
The provider proposals are selections only; the scorer verifies source,
prompt, raw response and proposal identities before opening sealed outcomes.

`scripts/score_desk_model_game.py` compares each selected set to accepting all
13 blind candidates. It enforces the $300 modeled per-trade loss cap, $1,500
combined open-loss reservation and $5,000 closed-capital budget. An exit on an
entry date does not release risk for that entry. This data cannot test the
$300 realized daily loss stop or actual intraday exit behavior.

The exercise tests candidate selection and risk discipline on thin,
selection-biased daily VWAP outcomes. It lacks executable bid/ask quotes,
complete point-in-time volatility features, broker fills, assignment facts,
and a second untouched holdout. Any apparently better modeled score is a
single-split observation, not a measured win rate or deployable edge. No
proposal authorizes a paper order or changes the dormant selector.

## Blind-period result

The three raw provider envelopes report successful one-turn calls and model
usage keys matching the requested models. The scorer verified the raw
responses, parsed proposals, source hash, prompt hash and sealed outcomes.
The provider envelopes reported a combined `$0.777619` for these calls;
this is a provider-reported amount, not a billing reconciliation.

| Selection policy | Blind selections | Modeled wins | Closed modeled P&L | Peak reserved loss |
| --- | ---: | ---: | ---: | ---: |
| Accept all eligible | 13 | 6 | −$165.17 | $823.00 |
| No trade | 0 | 0 | $0 | $0 |
| Z.ai `glm-5.3` | 10 | 6 | +$130.96 | $326.46 |
| Z.ai `glm-5.3-flash` | 6 | 3 | +$98.55 | $227.44 |
| MiniMax `MiniMax-M3` | 11 | 6 | +$104.49 | $326.46 |
| Fixed put-credit-only rule | 6 | 3 | +$98.55 | $227.44 |
| Fixed XSMOM defined-spreads rule | 11 | 6 | +$104.49 | $326.46 |
| Fixed all-XSMOM rule | 12 | 6 | −$14.64 | $590.73 |

Flash exactly matched the put-credit-only rule and MiniMax exactly matched
the XSMOM defined-spreads rule. Z.ai used that latter rule plus an exclusion
of one higher-risk call-debit candidate; the modeled difference is $26.47 on
this one blind split. The open-risk ceiling did not bind any of these actual
selections, so this artifact does not demonstrate handling of a constrained
book under pressure. The scorer's synthetic overlap test covers the cap
logic, not live trading behavior.

The frozen report is `scoreboard-v3.json` in the game directory (SHA-256
`0e795ecc96c1ad71fdb34cf258cb4651a61208b0ebb388ec5bd6b6545cb8d3e0`).
It records clean scorer code head `ef07dd8` and source hashes. The earlier `scoreboard.json` and
`scoreboard-v2.json` are preserved as preliminary scoring receipts.
