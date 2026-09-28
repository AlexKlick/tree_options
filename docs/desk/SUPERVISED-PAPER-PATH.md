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
| `mandate_active` | `grant_mandate` / `active_mandate`: operator-granted, expiring (60 s .. 8 h), account- and owner-epoch- and strategy-bound; one active; revoke is a permanent tombstone |
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

## What is deliberately NOT here yet

- No IBKR adapter: `send`/`reconcile_intent` take an injected
  `SupervisedBroker` port. The production port wraps `trex.ibkr`
  (account snapshot, fresh leg quotes, orderRef `trex:sup:<intent>`),
  and is the next integration step before any live-canary rehearsal.
- No CanaryFacts collector: the permit gate consumes the blocker tuple
  the runtime must derive from live observations (paper gateway account
  age <= 60 s, quote age <= 30 s, legacy book flat, qty-1 vertical).
- No timer/service: nothing schedules sends. E5-style arming is a later,
  operator-gated change that composes this module with desk rails.
- Paper environment only: the mandate model refuses any other value.

## Review trail

Failing-first suite `tests/unit/test_supervised.py` (33 tests): mandate
matrix, TTL bounds, intent idempotency/collision/in-flight rules, permit
gates (blockers, deadline, budget, double-issue), send-boundary refusals
(hash mismatch, consumed/expired permit, expired mandate leaves the
pending intent untouched), uncertainty preservation, the reconciliation
matrix (clear/hold/conflict), orphan recovery, and status inventory.
The FILLED projection is asserted through the execution package's own
lifecycle over PaperBroker facts, not through supervised code.
