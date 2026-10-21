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
  choice id is REJECTED, not adopted; a transient provider error is
  retried (see "Transient provider errors" below), a final provider
  failure is recorded and the run continues; flash never scores or judges.
- The window is scored by `intraday_action_graph.replay` — the same
  capital/open-cap accounting as the desk's fixed-policy baselines.
- Evidence is append-only: `artifacts/desk-store/evaluations/lab/
  <UTCts>-<policy>/summary.json` + `receipts.jsonl` (one line per model
  call: snapshot, prompt sha, raw reply parsed, latency, failures,
  attempts, retry reasons).

## Transient provider errors (implementation correction, owner ruling 2026-10-21)

Game `20261020T010000Z` lost 81 of 594 board calls (13.6%) to one provider
incident: 51 `TimeoutError`, 26 HTTP 429, 4 HTTP 500. `lab.ask_board`
called `chat_json` once with no retry, so every blip became a
`model_failures` entry, and the sealed promotion rule's clause 6 needs a
model-failure rate under 5%. The owner ruled on 2026-10-21 (default 4a):
retry transient errors twice, count only FINAL failures as
`model_failures`, and record the attempts. This corrects the
implementation only. PROMOTION-RULE.md is unchanged (no reseal, no window
reset). The challenge game runs its slices and policies one after another,
so there is no concurrency to tune.

`lab.BOARD_RETRY` (`RetryPolicy`) is the policy every `run_lab` board call
uses, and so every challenge game:

- **Transient** errors are retried: HTTP 429, any 5xx, `TimeoutError`, and
  URL/connection errors (`URLError`, `ConnectionResetError`,
  `ConnectionRefusedError`, `RemoteDisconnected`, ...). Nothing else is
  retried: another 4xx, a parse or envelope error, a truncated reply, a
  served-model mismatch or a missing key fails the board at once.
- **At most 2 retries**, after a 5 s then a 20 s delay.
- **Timeout growth.** After a timeout, every later attempt of that board
  runs at 2x the provider's configured timeout (zai 20 s -> 40 s).
- **90 s wall per board.** A retry starts only if at least 1 s is left
  after its delay, and it runs with at most the time left. On zai three
  timeouts take 20 + 5 + 40 + 20 + 5 = 90 s. The first attempt keeps the
  provider's configured timeout, so the cap never shortens a healthy call.
- **Circuit breaker.** After 6 consecutive FINAL failures in one run (a
  success resets the count), the run asks no more boards. Those boards are
  not calls and not failures: `summary.json` lists them under
  `breaker.skipped_snapshots` and counts them in `skipped_breaker`, and the
  receipt of the sixth failure carries `breaker_tripped: true`. They are
  not in `receipts`, because no model saw them (the hindsight gap samples
  read receipts as "boards the run showed"). `boards_shown` counts only
  the boards a model was asked, so the challenge budget refunds the rest.

What a run records:

| field | where | meaning |
| --- | --- | --- |
| `attempts` | each receipt | attempts made for the board (1-3) |
| `retry_reasons` | each receipt | one reason per retry made, e.g. `["HTTP 429"]` |
| `retry_stopped` | receipt, when set | `"wall_cap"`: the 90 s wall cut a retry |
| `breaker_tripped` | receipt, when set | this failure tripped the breaker |
| `model_calls` | summary | boards a model was asked (unchanged) |
| `model_failures` | summary | boards whose FINAL attempt failed |
| `model_attempts` | summary | total attempts over all boards |
| `first_attempt_failures` | summary | boards whose first attempt failed (retried or final) |
| `retry_reasons` | summary | tally of retry reasons over the run |
| `skipped_breaker`, `breaker` | summary | boards the breaker skipped, and where it tripped |

`first_attempt_failures` keeps the provider's raw noise visible after the
correction. On the boards a model was asked, it is the count the old
one-attempt accounting would have reported as `model_failures`.

The fields are sparse. A receipt carries `attempts`/`retry_reasons` only
when its board was retried (or the wall cut the retry), and a summary
carries the run-level fields only when some first attempt failed. A run
whose every board answered on its first attempt therefore keeps the exact
pre-correction bytes, which the env-v1 pins
(`tests/unit/test_desk_env_v1_pins.py`) hold every v1 consumer to. Read an
absent field as one attempt, no retries, `first_attempt_failures` 0 and no
breaker.
`ask_board(..., retry=None)` (its default) is still exactly one attempt.
The longrun and lab_overnight lanes call it that way and keep their own
failure semantics.

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

### rule-check pairs per MARKET SESSION (implementation correction, owner ruling 2026-10-13)

The sealed text of clause 3 reads "per-session paired difference". Until
2026-10-13 `challenge.rule_check` keyed the pairs by `<digest>:<session>` and
bootstrapped them i.i.d. Every nightly game replays the SAME frozen sessions,
so 12 games of 207 sessions became 2,484 "independent" pairs. The CI narrowed
with every replay while no new market evidence arrived. On 2026-10-12/13 that
reported both gepa arms ALL_PASS (Holm p 0.002-0.004) on what is a
pseudoreplication artifact. The PROMOTION-RULE text is unchanged (no reseal,
no window reset). Only the implementation now matches it:

- `_clustered_paired`: each market session contributes its mean over the
  games that replayed it; the bootstrap CI and sign-flip p run over
  sessions, on the scale of one average game.
- `accumulate_standings`: a game replays a session once per bundle that
  holds it (the three bundles overlap: 293 rows, 207 sessions per night).
  Those rows are now SUMMED. Before, they were overwritten, so the paired
  series measured a different quantity than `closed_pnl_sum`. Now
  `sum(session_pnl) == closed_pnl_sum` for every policy.

Live read after the fix (12 games, 207 sessions): gepa:5db18ac9200c CI95
[-282.72, 473.81], p 0.37; gepa:e47e99125d37 [-174.51, 497.80], p 0.23;
Holm 0.46. Clause 3 fails for both arms, so neither is promotable.
Consequence: replaying frozen bundles cannot accumulate promotion-grade
evidence at any game count. Each further game only averages out model
stochasticity on the same sessions. New out-of-sample sessions are the only
route (forward corpus / RESTART-THRESHOLD study).
