# Market-desk source probes — 2026-09-22 (M0)

Scripts: `scripts/probe_ibkr_equity_quotes.py` (clientId 75, writes
nothing) and `scripts/probe_free_sources.py`. Raw JSON: operator shell
reruns; findings summarized here.

## Answers

| Question | Answer |
|---|---|
| IBKR $30/mo sub: equity quotes on paper? | **SPY ONLY.** SPY bid/ask/last/close arrive and MOVE between reads at 18:12 ET (after hours -> likely real-time); NVDA + QQQ: nothing at all (the pre-subscription state). The sub does not blanket-cover US equities. |
| Real-time vs delayed verdict | Values changed after hours — strong real-time hint for SPY; definitive re-probe during 09:35-15:55 ET pending. |
| CBOE delayed equity quote | 200; keys `data.{bid,ask,bid_size,ask_size,close,iv30,last_trade_time,open,high,low,volume,price_change_percent,...}` + top-level `timestamp` ("2026-09-22 22:08:55") for source_as_of. SPY iv30 = 11.431. |
| CBOE delayed full chain | 200; **5.5 MB, 12,380 rows** for SPY; row keys include `option` (OCC symbol "SPY260922C00550000"), `bid, ask, iv, delta, gamma, theta, vega, rho, open_interest, volume, last_trade_time` — **the CBOE chain carries greeks** (IBKR delayed does not). Parse cap must be >= 16 MB. |
| Google News RSS | 200; 100 items; item children `title, link, guid, pubDate, source, description`. Needs Mozilla UA. |
| Polygon equity daily bars | 200 via MassiveClient `/v2/aggs/ticker/SPY/range/1/day/...`; 36 bars Aug 1 -> Sep 22; keys `o,h,l,c,v,t,n,vw`; `status: "DELAYED"` (free tier — today's bar may revise; disclose); last close 773.38 matches CBOE. |
| Local LLM (:18000) | `/models` responds; **chat/completions HANGS > 90 s** (curl-verified; GPU 0 idle, 13 stale connections on the proxy — text-main generation wedged; lane-owner fix needed). |
| z.ai | 200 on `/api/paas/v4/models`; `glm-5.3`, `glm-5.3-flash`, `glm-5.3-flashx` present. |

## Branch decisions

1. **Watchlist/symbol quotes + charts**: CBOE delayed quote (all symbols,
   uniform, keyless, carries `timestamp` for honest age) + Polygon daily
   bars (rolling window fetched with `use_cache=False` so the permanent
   cache can never relabel stale bars fresh; free-tier DELAYED disclosed).
2. **IBKR gateway equity**: does NOT join the chart source chain (SPY-only
   coverage is not worth a special case). Re-probe during RTH if the
   subscription is upgraded to full US-equity coverage.
3. **Shadow marking (M3)**: CBOE full chain is the marking universe — and
   since its rows carry delta/iv, shadow rows gain greeks for free.
4. **LLM proposals (M6)**: provider stays configurable (`local`, `zai`,
   `minimax`, `none`); **deployed default = `zai` / `glm-5.3-flash`** (2-5 s
   responses expected; local 27B measured hung — flip back via config when
   text-main generation is healthy). Bounded 20 s timeout, clean skip on
   failure, key via env var name only.
5. stooq: dead from this host (JS challenge) — confirmed dropped.
