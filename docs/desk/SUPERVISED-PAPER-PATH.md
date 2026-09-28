# Supervised paper path

Status: authority layer LANDED as library + CLI; no broker wiring yet.
`execution_authorized` remains a property of an operator-granted mandate,
never of this code. Nothing here can arm itself.

## What this is

`src/tree_options/trex/supervised.py` implements the runtime counterparts
of the guards the proposal surface declares for `paper_effect` nodes
(`action_graph/proposal.py`):

| Declared guard | Runtime enforcement |
| --- | --- |
| `mandate_active` | `grant_mandate` / `active_mandate`: operator-granted, expiring (60 s .. 7 days; ruling 2026-09-28 relaxed the 8 h ceiling — grants beyond 8 h are flagged `long_running` and status shows `days_left`), account- and owner-epoch- and strategy-bound; one active; revoke is a permanent tombstone |
| `permit_active` | `issue_permit`: single-use `EffectPermit`, spent from the mandate budget at issue, consumed (atomically renamed) BEFORE the broker call |
| `account_bound` / `current_owner` | re-checked by `active_mandate` at issue AND at the send boundary |
| `fresh_risk_snapshot` / `fresh_quotes` | upstream of the permit: `issue_permit` refuses on ANY canary blocker (`action_graph.canary.review_canary_package` output is the gate); the send boundary refuses a stale permit/intent |
| `effect_hash_bound` | the permit carries `effect_sha256`; `send` refuses bytes that hash differently |
| `retry=reconcile_before_retry` | an uncertain effect is preserved (`.terminal` outcome `uncertain`); a NEW intent for the same package is refused until a reconciliation verdict `confirmed_not_submitted` exists; verdicts never downgrade (conflict = refusal) |
| `on_failure=preserve_uncertain_effect` | broker exceptions become uncertain receipts; `recover()` reclassifies orphan `.sending` claims the same way |

The outbox and journal reuse the broker-neutral execution contracts:
every submit writes an append-only `journal/<intent_id>.jsonl` of
`tree_options.execution` records, and `project_intent` folds them through
the pure `ExecutionLifecycle` (UNKNOWN / RECONCILIATION_REQUIRED stay
fail-closed there). The deterministic `PaperBroker` exercises the whole
chain in tests end to end.

## State layout (`TREX_SUPERVISED_DIR`, default `~/.local/state/trex/supervised`)

```
mandate.json                     active mandate (archived on expiry)
mandate.revoked.json             permanent tombstone
permits/<pid>.issued.json        unconsumed permit
permits/<pid>.consumed.json      spent permit (rename = atomic consumption)
outbox/<iid>.pending.json        durable intent (written first)
outbox/<iid>.sending.json        claim persisted BEFORE the broker call
outbox/<iid>.terminal.json       receipt | rejected | uncertain (preserved)
outbox/<iid>.reconciled.json     reconciliation verdict
journal/<iid>.jsonl              append-only execution records
```

## Operator surface

```
HOME=/home/alexk PYTHONPATH=<repo>/src python -m tree_options.trex.supervised \
    grant --account <paper account> --owner-epoch <gateway epoch> \
    --strategy operational-canary/1 --profile-digest <sha256> \
    --max-orders 1 --ttl-seconds 900 --granted-by <operator label>
python -m tree_options.trex.supervised status
python -m tree_options.trex.supervised recover
python -m tree_options.trex.supervised revoke --reason "<why>"
```

Refusals print `refused: <machine-readable reason> <detail>` on stderr,
exit 2. A revoked mandate can never be replaced inside the same state
dir; clearing it is a deliberate by-hand act.

## IBKR adapter (`trex/supervised_ibkr.py`)

`IbkrSupervisedBroker` implements the port over one `IbkrTrex` session.
The port's `submit(attempt, effect_payload)` receives the exact bytes the
permit hashed, and the adapter builds its order FROM those bytes:

- `SupervisedEffect` = the one order a permit may send: intent id, bound
  paper account, `LegStructure`, side (the structure's OPEN side only),
  quantity (<= the structure's), limit, and order tag. `effect_bytes` is
  its canonical encoding; `decode_effect` refuses any other encoding, so
  no unhashed field (a `tif`, a second account) can ride along.
- Wire: the real `IbkrTrex._order` (BAG/OPT, `validate_package_order`
  bounds, DAY limit), then `orderRef = trex:sup:<intent_id>` and an
  EXPLICIT `order.account` (carry-forward E5 item).
- Guards before `placeOrder`: connected; paper gateway port 4002; the
  dedicated clientId **83** (legacy monitor 71, entry runner 72,
  discovery 74, `IbkrTrex` default 77; the E5 runtime shares 83 - IBKR
  lets only the placing client cancel); the account is managed by the
  session and carries the paper prefix `D`; the effect is for this
  intent; no order with this tag already exists in ANY view (an
  unreadable view refuses). Every local refusal is `Uncertain("not_sent:
  ...")`: the core holds the effect until reconciliation (after its
  settle window) confirms nothing reached the broker. No broker fact is
  ever fabricated.
- Acknowledgement: bounded poll (`ACK_TIMEOUT_S` 10 s) for
  PreSubmitted/Submitted/Filled -> `Acknowledged`; Cancelled/Inactive
  with no fill -> `Refused` (reason `IB<errorCode>`); dead with fills or
  no status -> `Uncertain`. One order on the wire, never a retry.
- `lookup`: all-client open orders + the session's completed orders +
  executions, matched by tag and deduped by `permId`. `NotSubmitted`
  only when every view was read; two distinct orders for one tag are
  `LookupUnknown`. Views cover the current gateway session: reconcile
  the same session, or check positions by hand after a restart.
- `preflight(effect_payload)`: session guards, encoding, contract
  qualification, order bounds, a two-sided package quote. Never places.

Legacy non-adoption: the monitor's tag parser reads `trex:sup:<id>` as
structure `sup:<id>` (never prepared by a book; effects refuse `sup:`
structure ids). The legacy put-spread adoption path (`open_combo_trades`
+ `structure_for_bag`) matches BAGs by leg conIds WITHOUT reading the
tag; it is kept off supervised orders only by clientId isolation
(`openTrades()` on the monitor's clientId 71 lists that client's orders). FOLLOW-UP:
make that legacy adoption skip `trex:sup:` refs (a characterization-
pinned legacy change, its own lane).

Tests: `tests/unit/test_supervised_ibkr.py` (27) drive the REAL
`IbkrTrex` over `FakeGateway` plus all-client views and a scripted
status walk. Mutation pass: 10/10 KILLED (tag override, explicit
account, duplicate-tag refusal, unreadable views never prove absence,
canonical bytes, permId dedupe, clientId, paper port, intent match,
open-side rule).

The desk-safety gate now requires `ib_async`, `uvicorn` and
`jsonschema` to be importable: a venv synced without the `trex` group
used to SKIP every broker suite silently and still pass.

## Canary screening (`trex/supervised_canary.py`)

`collect_canary_screening(broker, effect, profile=, mandate_account_id=,
inputs=, clock=)` fills `action_graph.canary.CanaryFacts` from ONE live
paper session and returns every blocker plus a `screening_sha256` digest
of the evidence, which `issue_permit(canary_blockers=...,
screening_sha256=...)` binds into the permit. It never places, cancels
or modifies an order.

Observed (never assumed): the account snapshot and its time; the
adapter's session guards (`paper_gateway_verified`); contract
qualification + order bounds; the package quote, timed by the OLDEST leg
tick (a leg without a tick time makes the quote stale); the bound
account's non-zero positions; ALL clients' working BAG/OPT orders (the
monitor's included, supervised-tagged excluded); OUR modeled margin
(debit kinds: debit at the cap; credit verticals: width x 100 x qty)
because paper what-if margins are all zero. `checked_at` is read AFTER
the observations. Any unreadable broker view -> `facts=None` +
`broker_view_unreadable:<view>`.

Required inputs, never inferred (`OperatorCanaryInputs`): owner epoch
and health, `assignment_plan_verified`, `protective_exit_ready`,
reconciled open loss and day loss.

### Operator rulings (2026-09-28), recorded in every screening's evidence

| Id | Question | Ruling | Effect |
| --- | --- | --- | --- |
| 1a | `temporary_assignment_exposure` | **width x 100 x quantity** (the gap loss if the short leg is assigned before the long is exercised) | computed by `assignment_exposure(effect)`, no longer an input; a 5-wide SPY vertical = $500 < $5,000 capital (short-leg notional would have blocked every real underlying) |
| 2b | who owns a supervised position's protective exit | **the E5 desk runtime** | `protective_exit_ready` stays an input and is True only once E5 owns the position's exits; until then every live screening is blocked by design |
| 3b | flat-book scope | **the canary's own underlying only** | positions and working orders on other underlyings do not block (the NVDA book no longer blocks a SPY canary); the whole account stays in the evidence (`account_positions`, `account_working_orders`) and other books' risk still enters through the open-loss cap |

Mutation for the rulings: 6/6 KILLED (whole-account scope, no scope,
notional exposure, zero exposure).

Tests: `tests/unit/test_supervised_canary.py` (27), including
`test_supervised_chain_rehearsal_on_fakes`: mandate -> intent -> live
screening (a non-flat book refuses the permit) -> permit bound to the
screening digest -> send through the real `IbkrTrex` adapter ->
acknowledged receipt projected in the journal. Mutation: 11/11 KILLED
(after one survivor exposed a missing test: one fresh leg must not
vouch for a leg that never ticked).

## E5 desk runtime v1 (`trex/desk_runtime.py`) - the exit owner (ruling 2b)

`DeskRuntime` is the SAME process and clientId (83) that sends supervised
entries (IBKR lets only the placing client see and cancel an order), with
state in `~/.local/state/trex/desk-paper/` (`TREX_DESK_RUN_DIR`):
`specs/<sid>.json` (one `DeskSpec` per structure), a single-writer
`book.json` (`BookState`), `events.jsonl`, `runtime.lock`, and the `HALT`
/ `FLATTEN` kill files. Decisions are the pure desk engine's
(`engine.step`: drain the working order, then decide).

- `register(effect)` BEFORE the send: spec + PLANNED book entry
  (idempotent; another intent on the same structure id refuses). A
  PLANNED structure resolves from the broker's live `trex:sup:` order, or
  the supervised outbox: acknowledged -> ENTER_WORKING; rejected /
  confirmed-not-submitted / an intent whose send deadline passed unsent ->
  CLOSED; uncertain -> held (event). An order at the broker is never
  unowned, even across a crash between send and bookkeeping.
- Entry lane: cancel-only. `AbortEntry` (entry window closed, stale deal)
  cancels and waits for the confirmation; `EntryOrder` repricing is
  NEVER sent (the permit binds one limit; event `entry_reprice_not_sent`).
  An entry that left the live view resolves only from broker evidence
  (`IbkrTrex.entry_fill_evidence`): fills -> OPEN, provably none ->
  CLOSED, inconclusive -> held + event.
- Exit lane: every engine exit (expiry safety, touch/breach, assignment
  risk, take-profit, stop-loss, time stop; FLATTEN at marketable prices)
  is a DAY limit with `orderRef trex:desk:<sid>` and the bound account.
  Reprice = cancel -> confirmed -> merge the final fills -> replace the
  REMAINDER; an unconfirmed cancel keeps the old order and sends nothing.
- Legs-held guard before EVERY close: the bound account's leg positions
  must equal the book's open quantity (BUY legs long, SELL legs short),
  else refuse + `legs_mismatch` event. A double close (a reversed
  position) cannot be sent, across restarts too. (v1 assumes one
  supervised structure per leg contract; ruling 3b's same-underlying
  flat-book rule makes that true for canaries.)
- Unknown exposure (a tagged order whose side contradicts the book) is
  noted and never acted on. `HALT` sends nothing new (fills still drain).
- Arm gate: `exit_owner_ready(paths, now)` = heartbeat <= 30 s AND the
  runtime lock held. It is the canary's `protective_exit_ready`.
- Assignment: `assignment_plan(structure, dividend_snapshot, as_of)` is the
  canary's `assignment_plan_verified`: no short call -> covered by expiry
  safety; a short call needs a fresh snapshot and no PROJECTED (undeclared)
  ex-date in the hold window. The next DECLARED ex-date and the short
  call's leg mid feed the engine's pre-ex-date exit
  (`Snapshot.dividends`, `short_call_mids`).

Tests: `tests/unit/test_desk_runtime.py` (30) over the real `IbkrTrex` and
the real engine. Mutation: 15/15 KILLED (legs guard, abort-cancel,
cancel-confirm, exit tag, exit account, HALT, unknown exposure, uncertain
receipt, send deadline, inconclusive evidence, heartbeat, lock, short-call
mids, projected dividend, entry reprice).

### The exit left the live view while E5 was down (gap review, fixed)

| While E5 was down, the exit order... | Broker evidence | E5 now |
| --- | --- | --- |
| filled fully, same day | today's executions of that order | records the fill (price in debit orientation) -> CLOSED |
| filled partly, then expired (DAY) | partial executions; legs = remainder | records the partial -> re-places ONLY the remainder |
| expired unfilled | no executions; legs = book | re-places |
| filled on an EARLIER day | no executions today; legs flat | ambiguous (vs. positions not loaded): alerts once (`exit_flat_unexplained`), sends nothing, waits for the operator's `RESOLVE-FLAT-<sid>` file (honoured only while the legs are flat; closes the unexplained packages UNPRICED, so the day loss reads unknown and blocks new entries that day) |

Leg-mismatch alerts are deduped per condition. Mutation 7/7 KILLED,
including the pre-fix runtime (the tests reproduce the gap).

## The supervised desk process (`trex/supervised_desk.py`)

One process, one IBKR session (clientId 83): IBKR refuses a second
connection with the same clientId, so entries cannot come from a separate
CLI. `python -m tree_options.trex.supervised_desk run [--legacy-plan P]`:

- **Loop:** inside the session (NYSE session day, 09:30-16:15 ET) one
  `DeskRuntime.tick()` THEN the entry inbox (the tick's beat arms the gate
  the inbox's screening reads); outside it, a heartbeat only. A lost
  gateway exits 6 (systemd restarts; the book + broker re-adopt).
- **Entry inbox:** `desk-paper/requests/<intent_id>.json`
  (`trex-desk-entry-request/1`: `strategy_version`, `send_deadline`,
  `requested_by`, the `SupervisedEffect`) is claimed by an atomic rename
  and processed exactly once: mandate (this process's owner epoch AND
  the `profile.json` digest) -> adapter preflight -> canary screening
  (owner health, `exit_owner_ready`, `assignment_plan`, open/day loss of
  the E5 book + every `--legacy-plan` book) -> intent -> permit ->
  `register` -> `send`. The outcome is `<intent_id>.result.json`
  (`sent` / `blocked` + blockers / `refused` + reason / `invalid`).
- **Loss facts:** open-loss reservation = open packages at the debit paid
  (cap/floor when unpriced) + live entries at full size; realized day
  loss = today's losing exits; any unreadable book or unpriced exit makes
  the fact unknown, which the canary refuses.
- v1 is debit kinds only (`OrderIntent` represents BUY-to-open).

### The daily grant (ruling 2026-09-28: base + extra when quota is spare)

`python -m tree_options.trex.grant_policy` computes the day's order budget:
BASE 1 order, +1 per subscription window currently UNDER-USING (more left
than planned at this point in its reset window), capped at the rails' 3.
The input is `desk-paper/quota-windows.json` (schema `desk-quota-windows/1`;
refresh from the quota dashboard — the :8019 broker exposes raw tokens,
the planned-vs-actual comparison lives in the flow controller). Unknown
plan earns NO extra (fail closed); dry windows neither add nor block.
`--apply --owner-epoch <epoch> --profile-digest <digest>` runs the grant
(authorized agent sessions; default ttl 12 h).

### Arming runbook (operator)

1. Author `~/.local/state/trex/desk-paper/profile.json` (the handoff's
   envelope: `intended_capital` 5000, `max_loss_per_trade` 300,
   `max_open_loss` 1500; `max_daily_loss` and `horizon_days` are the
   operator's; `allowed_strategy_versions` ["operational-canary/1"]).
2. Install + start `deploy/trex/trex-desk.service` (not installed by any
   landing). Read `desk-paper/owner.json` for the owner epoch.
3. Grant — either by policy (preferred):
   refresh `desk-paper/quota-windows.json` from the dashboard, then
   `python -m tree_options.trex.grant_policy --apply --owner-epoch <epoch>
   --profile-digest <digest>`; or by hand:
   `python -m tree_options.trex.supervised grant --account <DU...>
   --owner-epoch <epoch> --strategy operational-canary/1 --profile-digest
   <digest> --max-orders 1 --ttl-seconds 3600 --granted-by <you>`, the
   digest from `python -m tree_options.trex.supervised_desk profile-digest`.
   A restarted process has a new epoch: re-grant.
4. Drop an entry request (`python -m tree_options.trex.desk_cli request
   --buy-strike 744 --sell-strike 742 --expiry 2026-11-20 --debit 0.90
   --exit-deadline 2026-10-23`); read its `.result.json`; watch
   `events.jsonl` (`desk_cli status` / `desk_cli events`).
5. Stop: `desk_cli halt` / `desk_cli flatten` (or the files in
   `desk-paper/`), `supervised revoke` (permanent), or stop the unit.

## What is deliberately NOT here yet

- Credit kinds (the execution records cannot represent SELL-to-open).
- The E6 desk-enter queue writing entry requests automatically (today an
  operator writes the request).
- Paper environment only: the mandate and the effect both refuse
  anything else.

## Review trail

Failing-first suite `tests/unit/test_supervised.py` (43 tests): mandate
matrix, TTL bounds, intent idempotency/collision/in-flight rules, permit
gates (blockers, deadline, budget, double-issue), send-boundary refusals
(hash mismatch, consumed/expired permit, expired mandate leaves the
pending intent untouched), uncertainty preservation, the reconciliation
matrix (clear/hold/conflict), orphan recovery (the recovered terminal
carries the package sha and keeps the package in flight), and status
inventory. The FILLED projection is asserted through the execution
package's own lifecycle over PaperBroker facts, not through supervised
code.

Adversarial review pass 1 (glm-5.3 main session, 2026-09-28; the Codex
round was skipped because Codex is meter-only per the 09-27 reservation)
found and fixed two double-send race windows: the outbox claim and the
permit consumption are now atomic RENAMES, not write-then-unlink pairs -
a crash or a concurrent runtime can no longer leave an intent both
pending and claimed, or a permit both issued and consumed.

Review pass 2 (Opus 5.5, fresh read, 2026-09-28) found and fixed:

| Sev | Defect | Fix | Killing test |
| --- | --- | --- | --- |
| P1 | an inconclusive lookup was persisted as a verdict and later confirmations refused as "conflict": one reconcile while the gateway was down stranded the package forever | only confirmed verdicts persist; `still_uncertain` goes to the per-intent reconcile audit and may be retried | `test_reconcile_upgrades_after_inconclusive_lookup` |
| P1 | a permit issued near the send deadline stayed valid up to 15 min past it | permit expiry = min(TTL, send deadline); `send` re-checks the deadline | `test_permit_expiry_clamped_to_send_deadline` |
| P1 | no lock: a CLI and a runtime could both spend `orders_used` (budget bypass) | one advisory flock on the state dir for every mutation; `state_busy` after `LOCK_WAIT_S` | `test_state_lock_refuses_when_busy` |
| P2 | broker facts journaled unvalidated; a contradictory fact poisoned the journal and crashed `status` | facts fold through the lifecycle BEFORE journaling (refused = uncertain `broker_facts_rejected`); `status` reports `projection_error` | `test_contradictory_broker_facts_become_uncertain`, `test_status_survives_a_poisoned_journal` |
| P2 | `NotSubmitted` accepted instantly: a lookup racing a just-sent order could clear it | `RECONCILE_SETTLE_S` (120 s) after the uncertain receipt | `test_reconcile_refused_inside_settle_window` |
| P2 | outbox writes not fsynced | file + directory fsync on every atomic write and rename | (durability; not unit-testable) |
| P3 | sha fields unvalidated; intent id reuse after a terminal not refused early | `Sha256Hex` pattern; `intent_id_reused` | `test_sha_fields_must_be_lowercase_hex`, `test_intent_id_reuse_after_terminal_refused` |

Mutation pass (`~/.local/state/trex-supervised-mutation/`): 9 KILLED /
1 SURVIVED. The survivor (send-side deadline re-check) is a deliberate
second lock behind the permit clamp; the combined mutant removing both is
KILLED.
