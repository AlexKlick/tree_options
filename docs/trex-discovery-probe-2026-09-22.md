# Discovery lane gateway probe — 2026-09-22 (in-session, 15:52-15:56 ET)

Live paper gateway (127.0.0.1:4002), discovery clientId 74. Script:
`uv run --group trex python` driving
`tree_options.trex.discovery.probe.probe_json` over `["NVDA", "QQQ", "SPY"]`
(first run) / `["NVDA", "QQQ"]` (second run, after adding the DTE window to
expiry selection). Raw transcripts: `/tmp/c4-probe.json`, `/tmp/c4-probe3.json`
(session-local; distilled here).

## Answers

| Question | Outcome | Evidence |
| --- | --- | --- |
| Do option chains flow (delayed)? | **YES** | NVDA: 23 expirations / 274 strikes; QQQ: 31 / 523; SPY: 32 / 483 (`reqSecDefOptParams` on the qualified Stock conId, SMART row) |
| Do delayed tickers carry model greeks (tick 83)? | **NO** | 0 of 6 subscribed rows on the 20261016 monthly (24 DTE) carried `modelGreeks.delta`; 10091 "delayed market data is available" notices on each subscription |
| Do account tags arrive? | **YES** | Full cached `accountValues()` rows: NLV $1,000,252.09, cash $999,516.91, buying power $3,998,067.63, account DUT143714 — zero extra subscription needed |
| Mkt-data line budget | n/a yet | Probe held ≤6 simultaneous lines; runner must still cap + cancel between underlyings |

## Branch taken (per the plan's branch table)

**Chains OK + greeks ABSENT** → `target_mode="auto"` degrades to
**premium-floor** targeting:

- short strike selected by the credit (premium) window over the chain,
  not by delta;
- the `delta` rule is recorded `NOT_APPLICABLE` with reason
  "no greeks on delayed paper (0/6 rows, tick 83 absent)" in every scan
  artifact;
- spot context stays optional (paper equity quotes are dead — live
  `marks.json` shows `spots: {}`), so `%OTM` targeting is likewise
  unavailable unless a spot source is supplied later.

## Config defaults set from this probe

`deploy/trex/discovery.toml` (landed with C5):
`target_mode = "auto"` (degrades honestly per-scan and stamps
`effective_target_mode`), dte window 20-60, widths 5/10/15/20,
`min_debit`/liquidity thresholds per the engine rules.

## Probe fixes this run forced

- expiry selection must respect the DTE window: `expirations[0]` can be
  TODAY (SPY 0DTE at 15:52 → "Unknown contract" errors on every strike);
- tz-aware vs naive datetime subtraction (`expiry.date() - today.date()`);
- qualify gaps on individual strikes are expected — skip, don't abort.
