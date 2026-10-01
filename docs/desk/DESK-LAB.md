# The desk's historical theory lab (Mode H)

One half of the agent trading desk (operator 2026-09-28): while the
supervised desk (Mode A) trades paper, the lab BURNS the zai + MiniMax
subscriptions converting idle quota into evidence — LLM traders choosing
on as-of boards from the desk's recorded history, scored by the same
replay accounting the desk's own baselines use.

## What a run is

```
python -m tree_options.desk lab-run \
  --bundle artifacts/desk-store/evaluations/intraday-graph/20260927-v1/minute-bars-4mo-expanded.json \
  --policy model:zai \        # or model:minimax, model:minimax-flash, model:local,
                              # or the rules baselines: no_trade, put_credit, ...
  --sessions 2 --boards-cap 12 \
  --windows ~/.local/state/trex/desk-paper/quota-windows.json
```

- The run takes the bundle's most recent N sessions and walks each
  scheduled decision clock (8 per session). Each board is the packet's
  top candidates by reward/risk (`intraday_action_graph.decision_packet`:
  as-of features only — no lookahead by construction), aliased (no
  tickers, no dates) and capped at 12 rows.
- A model policy answers once per board (STRICT JSON `{choice, note}`
  through `discovery.llm.chat_json`: glm-5.3-flash on zai,
  MiniMax-M3.1-Flash-Preview on minimax (effort high) and on minimax-flash
  (effort max), loopback Qwen on local; keys by env name only, never in
  artifacts). MiniMax-M3 was retired from both minimax lanes on
  2026-09-28; no recorded lab run used `model:minimax` before that, so
  the arm id carries no M3 history. A minimax reply whose envelope
  `model` does not echo the requested id is a provider failure (MiniMax
  answers unknown ids with HTTP 200 from another model). An unknown
  choice id is REJECTED, not adopted; a provider
  failure is recorded and the run continues; flash never scores or judges.
- The window is scored by `intraday_action_graph.replay` — the same
  capital/open-cap accounting as the desk's fixed-policy baselines.
- Evidence is append-only: `artifacts/desk-store/evaluations/lab/
  <UTCts>-<policy>/summary.json` + `receipts.jsonl` (one line per model
  call: snapshot, prompt sha, raw reply parsed, latency, failures).

## Quota discipline (the burn is a consumer)

A model policy runs ONLY while a subscription window is under-using
(actual left > planned left, per `desk-paper/quota-windows.json`, schema
`desk-quota-windows/1` — the same snapshot the daily grant reads; refresh
it from the quota dashboard). No snapshot, or no spare window: the run
skips with a reason (exit 0). Rules baselines are never gated.

`deploy/desk/desk-lab.{service,timer}` (installed at the operator's
discretion): hourly 07:00–21:00 MDT, one `model:zai` batch of ≤12 boards;
each tick self-skips when quota is on plan.

## The law (unchanged from the desk's research rules)

Runs are evidence, not authority. Nothing here touches the broker, the
supervised chain, or the live desk. A policy (model or rules) may become
an ADVISORY input to live selection only by PRE-REGISTERED rule:
register the promotion rule, run it sealed, the operator rules. A good
scoreboard number alone promotes nothing.

## Limits (declared, not hidden)

- Last-traded-minute closes are valuation proxies, not executable quotes;
  no fills, slippage, fees or assignment are modeled.
- The bundle is one frozen 4-month selection (72 series, 2026-05-25 ..
  2026-09-25); sessions overlap across windows and results are not
  independent samples.
- glm-5.3-flash answers the boards; per the standing tiering rule it is
  never a judge, never a scorer, never quoted as must-be-right.

## Promotion rule REGISTERED (sealed 2026-10-01)

The rule below was a DRAFT until 2026-10-01, when the operator registered
it as **`docs/desk/PROMOTION-RULE.md`** (sha256 sidecar
`docs/desk/PROMOTION-RULE.sha256`, sealed before any digest carried paired
columns). The registered rule is the DRAFT plus the #47/measured-cost
lessons: a paired-CI significance clause, the **first_row control must FAIL
the same bar**, Holm across tested arms, and a euthanasia clause (40
sessions with no arm passing → the program closes). Read the registered
artifact for the exact clauses; the digest's `vs_no_trade`/`vs_first_row`
columns are its mechanical inputs.

Nothing in this repo implements promotion: `best_advisory` returns
`promoted: false` by construction, and the register -> seal -> rule steps
are operator acts on pre-registered artifacts.
