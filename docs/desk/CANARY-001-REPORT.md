# CANARY-001: first supervised paper round trip (2026-10-14 to 2026-10-15)

**Status: DRAFT for owner review.** Paper account DUT143714 only. No live money.
Raw evidence (every command plus its output): `~/.local/state/trex-e2e-canary-20261012/LOG.md`.
Runbook: `~/.local/state/trex-e2e-canary-20261012/RUNBOOK.md`.

## Outcome

The full supervised path ran end to end for the first time: mandate, entry request, screening,
permit, IBKR paper order, fill, a desk restart while holding the position, the time-stop exit,
and a flat book. Before this canary, **no supervised exit had ever executed** (swarm verdict,
2026-09-29).

| | |
|---|---|
| Structure | SPY 2026-11-20 760/758 put debit vertical, qty 1 (BUY 760P / SELL 758P) |
| Entry | 2026-10-14 09:50:38 ET, order #11, **filled 0.47** (limit 0.48, cap 1.20) |
| Exit | 2026-10-15 09:48:03 ET, order #21, **filled 0.60** (TIME_STOP; limit 0.60, no reprice) |
| Gross P&L | +0.13 x 100 = **+$13.00** |
| Commissions | exit $1.26 (measured: 0.62 + 0.64); entry not visible next day (IBKR executions are per-day), est. ~$1.26 |
| Net (est.) | ~ **+$10.48** |
| Max loss at risk | $47 (0.47 x 100) |

The gain is direction, not skill: SPY fell between entry and exit (760P 7.14 -> 10.47). The
canary is an operational test of the machinery. It is not evidence about any strategy.

## Verification matrix

| # | Stage | Result | Evidence |
|---|---|---|---|
| 0 | Gateway healthy at entry and exit | PASS | `gateway.json`: ok / api_ok / logged_in, no needs_2fa (10-14 09:47, 10-15 09:31 ET) |
| 1 | Desk armed | PASS | `trex-desk.service` active; epoch `desk83-6acec29b` at entry; heartbeat <= 2 s |
| 2 | Profile digest | PASS | `439223d64e53aef4c2f3ef00fb5401a27e581159388d200dd33474a86965300b` rev 1 |
| 3 | Pre-entry readiness (drill-check) | PASS | DRILL GO at 09:48 and 09:50 ET (G2/G3/G4 GO; G5 spreads 0.4-1.0% of mid; OI from the CBOE chain: 22,721 / 737) |
| 4 | Mandate | PASS | `mandate-6acf87f6`: epoch-bound, max_orders 1, TTL 4 h |
| 5 | NEG: no-mandate request refused | NOT ISOLATED | the 10-13 probe hit `request_expired` first (the deadline check precedes the mandate check); covered by unit suites only |
| 6 | NEG: expired request blocked | PASS | 10-13 14:42 ET: `blocked [request_expired]`, no permit |
| 7 | NEG: over-cap request refused | PASS | `--debit 1.50 --cap 1.20` -> CLI exit 2 `invalid_request`, nothing written |
| 8 | Entry sent | PASS | `requests/canary-001.result.json`: sent, `permit-992c6e47e1315cc3`, receipt acknowledged, broker order 11 |
| 9 | Entry filled + marks | PASS (with defect D1) | `events.jsonl`: registered -> sent -> entry_adopted -> entry_reprice_not_sent (by design) -> entry_filled 0.47; `book.json` open; cockpit HTTP 200 |
| 10 | Restart while holding (re-adoption) | PASS | 10-14 17:45 MDT logoff -> restart 17:55, new epoch `desk83-6ad01673`; position retained; that process executed the exit |
| 11 | NEG: HALT gates the exit | PASS | HALT 09:40:04; time stop 09:45:03 -> `halt_no_new_orders` 09:46:03; **0 exit orders** while halted; exit 4 s after resume |
| 12 | Exit | PASS | exit_begin(time_stop) -> exit_authority -> exit_order #21 -> closed 0.60; broker fills tagged `orderRef trex:desk:canary-001` |
| 13 | Expired mandate never blocks an exit | PASS | exit ran under epoch `desk83-6ad01673`; the mandate (epoch `desk83-6acec29b`) expired 10-14 13:47 ET |
| 14 | Book and account flat | PASS | `book.json` closed / open_qty 0; broker positions for DUT143714 = **0** (read-only probe, clientId 84) |
| 15 | NEG: quota / loss gates | UNIT ONLY | `test_supervised_desk.py`, `test_supervised_canary.py`; not re-proven live |

## Defects found (none blocked the canary)

- **D1. The exposure reader cannot read real supervised specs.** `desk/book.py:_desk` parses
  `specs/*.json` as a bare `LegStructure`. `desk_runtime` writes the `trex-desk-spec/1`
  wrapper with the structure nested inside it. As a result the cockpit's account-exposure panel
  and drill-check's account line showed the open canary as 0 structures / $0 ("desk spec
  unreadable (ValidationError)"). The runtime's own `DeskSpec` loader parses the file
  correctly, and `desk_runtime` never imports `desk.book`, so neither entry screening nor
  restart re-adoption was affected. Fix: unwrap `structure` when `schema == trex-desk-spec/1`,
  and add a test that uses a spec the runtime itself wrote.
- **D2. Losing the gateway is an uncaught exception, not the designed exit 6.** Both the
  10-13 reboot and the 10-14 daily logoff ended in `ConnectionError: Socket disconnect` with
  a traceback and exit 1. systemd recovers it anyway (`Restart=on-failure`, 15 s; the start
  limit cannot trip), but the exit-code contract is not honored.
- **D3 (benign).** The first time-stop tick logged `exit_pending_no_quote`. The quote arrived
  one tick later.

## Promotion context (unchanged by this canary)

No arm is promoted, and nothing here promotes one. The `rule_check` was corrected in #71
(2026-10-13) to pair per **market session**: the earlier i.i.d. bootstrap over 2,484
game-session pairs was pseudoreplication, the same 207 frozen sessions replayed every night.
At 14 games, the paired CI95 vs no_trade per average game is: gepa:5db18ac9200c
[-280.5, 471.84]; gepa:e47e99125d37 [-181.24, 493.58]; Holm p 0.47. Clause 3 fails for both,
so neither is promotable. Promotion-grade evidence needs new out-of-sample sessions (the
forward corpus); more replay nights cannot provide it.

## Recommended next steps (owner decides)

1. Fix D1 and D2 (small PRs), then cut a cockpit release so the exposure panel counts
   supervised positions.
2. Hold further canaries until D1 is fixed. The account-level loss view must see supervised
   positions before more than one can be open.
3. Do not arm the desk for multi-day holds across a Sunday (12:00 ET cold restart = human 2FA).
4. The desk is left running with no active mandate (harmless: it only ever exits). No
   `supervised revoke` was run.
