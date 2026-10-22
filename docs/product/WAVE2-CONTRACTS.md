# TREX Wave 2 frozen cross-lane contracts

Status: FROZEN for Wave 2 lane work (branch `w2/contracts`, based on
`wave1/base`). Code: `src/tree_options/trex/contracts/`. Tests:
`tests/contracts/test_wave2_contracts.py` with fixtures in
`tests/contracts/fixtures/`.

Wave 2 is execution completeness on IBKR paper: whole-share, long-only US
equities beside the existing options path, per-trade owner approval, an
entries-only pause, and books with broker reconciliation. This document
fixes the data shapes the seven lanes exchange so they can be built in
parallel. Nothing here sends an order, grants authority or touches a
running service.

## Rules for every lane

- Import from `tree_options.trex.contracts`; never redefine these shapes.
- A contract changes only additively (a new optional field, a new
  `Blocker` member, a new union member). Anything else is a new schema
  version (`.../2`), never an edit of `.../1`.
- The contracts package imports no broker library (`ib_async` stays
  unloaded; a test pins this), so the broker-free web backend can use it.
- Existing shapes are reused, not changed: the option effect
  `supervised-effect/1` (`trex.supervised_ibkr.SupervisedEffect`), the
  desk spec `trex-desk-spec/1` (`trex.desk_spec.DeskSpec`), the
  `supervised-mandate/1` / permit / outbox of `trex.supervised`.

## Conventions

| Topic | Rule |
| --- | --- |
| Canonical bytes | JSON-mode dump by alias, then sorted-key compact UTF-8 JSON (`action_graph.proposal.canonical_bytes`, the encoding the option effect already uses). Every hash below is sha256 over these bytes. Key order and instant spelling never change a hash. |
| Instants | Timezone-aware only (naive input refuses); normalized to UTC on validation and serialized as `...Z`. Durations are whole seconds (`time.sessions.shift_instant`; no `timedelta` outside `tree_options/time/`). |
| Ids | `PathSafeId` (`^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$`) for anything that becomes a file name (intent ids); `IdempotencyKey` = lowercase canonical UUID; `Sha256Hex` = 64 lowercase hex. |
| Money | Orders, limits, quotes and risk figures: integer **cents**. Broker-reported execution prices, average costs, commissions and ledger cash deltas: integer **micros** (1 USD = 1,000,000). |
| Models | `StrictModel`: frozen, `extra="forbid"`; quantities and money are strict ints (`1.0`, `"1"`, `True` refuse). Versioned documents carry `schema` (field `schema_version`, alias `schema`). |
| Order tags | Entry: `trex:sup:<intent_id>`. Desk exit: `trex:desk:<structure_id>`; an equity position's structure id IS its intent id. `parse_order_ref` reads both. |

## Lane map

| Lane | Owns | Produces | Consumes |
| --- | --- | --- | --- |
| A pause + admission | entry screening, `PAUSE_ENTRIES`, per-session entry cap | blocker lists | C2.1, C2.2, C2.4, C2.5, C2.7, layout |
| B IBKR stock wire | stock qualification, quotes, STK order placement and lookup | `BrokerExecution`, `BrokerPortfolioItem`, quotes | C2.1 (decodes effect bytes) |
| C runtime exits | equity exit loop, v2 spec registration, HALT/FLATTEN | `trex-desk-spec/2`, exit events | C2.1, C2.6, C2.7 |
| D books | ledger, holdings, risk snapshot, reconciliation | C2.4, C2.5 | B's broker facts, C2.6 specs |
| E authority | `CommandAuthority`, approval, mandate/2, permit binding | C2.3, `Actor` | C2.1, C2.2 |
| F owner orchestration | compose/approve/reject flow, proposals | C2.1 effects, C2.2 proposals, command results | C2.2 commands, A's blockers |
| G web command backend | HTTP ingress, command durability, idempotency | C2.2 commands (+ `accepted`/`refused`/`duplicate` results) | E's `CommandAuthority`, everything for display |

## C2.1 `supervised-equity-effect/1` and `EffectV1`

Module `contracts.effects`. The one whole-share, long-only stock order a
per-trade mandate authorizes, plus the exit policy the desk enforces.

| Field | Type | Invariant |
| --- | --- | --- |
| `schema` | `"supervised-equity-effect/1"` | discriminator of `EffectV1` |
| `intent_id` | `PathSafeId` | |
| `account_id` | str | IBKR paper prefix `D` (as the option effect) |
| `symbol` | `^[A-Z][A-Z0-9.]{0,9}$` | allowlist is policy (A), not schema |
| `con_id` | int >= 1 | the qualified IBKR contract |
| `side` | `"BUY"` | long-only: SELL refuses |
| `quantity` | int >= 1 | whole shares; fractional/zero refuse |
| `limit_cents` | int > 0 | DAY limit |
| `tif` | `"DAY"` | fixed, hashed |
| `outside_rth` | `false` | fixed, hashed |
| `currency` / `exchange` | `"USD"` / `"SMART"` | fixed, hashed |
| `order_ref` | str | equals `trex:sup:<intent_id>` |
| `exit_policy.stop_price_cents` | int > 0 | `< limit_cents` |
| `exit_policy.target_price_cents` | int > 0 or null | `> limit_cents` when set |
| `exit_policy.exit_deadline_session` | ISO date | >= the entry session (checked on the proposal) |
| `exit_policy.stop_confirm_ticks` | int 1..20, default 3 | consecutive quote ticks with bid <= stop |
| `exit_policy.forced_exit_et` | `"15:45"` | ET wall time on the deadline session |

- `effect_v1_bytes` / `effect_v1_sha256`: the permit hashes these bytes.
  For the option member it calls the EXISTING `effect_bytes`.
- `decode_effect_v1(payload)`: parses either member and refuses any
  non-canonical encoding (as `decode_effect` does today).
- `EffectV1 = SupervisedEffect | SupervisedEquityEffect`, discriminated on
  `schema`; a missing or unknown schema refuses. The option member is the
  existing class, imported unchanged.
- Golden: the canary-001 option effect (synthetic account `DU0000001`)
  encodes to 636 bytes with sha256
  `9beed2502c1d3c0950ed4adb94dac3881430ece3df8a464fb12007291cee13ea`
  through both the existing builder and the union. Read-only live check
  (2026-10-21, not committed): rebuilding the real canary-001 effect from
  its claimed request with today's `effect_bytes` reproduces the
  `effect_sha256` recorded in its consumed permit.
- Risk arithmetic (integer cents, shared so lanes cannot diverge):
  `equity_notional_cents = quantity x limit`;
  `equity_stop_loss_cents = quantity x (limit - stop)`;
  `equity_max_loss_cents = max(stop loss, ceil(notional x gap_bps / 10000))`
  with `gap_bps` default 2000.
- Producers: F (composes). Consumers: A (screens), E (hash binding),
  B (builds the `Stock` order FROM the decoded bytes: conId, SMART, USD,
  `LMT`, DAY, `outsideRth=False`, explicit `account`, `orderRef`),
  C (spec + exits), D (attribution by `order_ref`), G (display).

## C2.2 `trex-entry-proposal/1`, `trex-command/1`, `trex-command-result/1`

### Entry proposal (`contracts.proposals`)

Written by F to `proposals/<proposal_id>.json` (canonical bytes) only after
A's screening of the effect returned zero blockers.

| Field | Type | Invariant |
| --- | --- | --- |
| `proposal_id` | sha256 | sha256 of the canonical document WITHOUT `proposal_id` (all other fields bound) |
| `effect` | `EffectV1` | |
| `effect_sha256` | sha256 | equals `effect_v1_sha256(effect)` |
| `strategy_version`, `program_id` | str | |
| `sleeve_ref` | str or null | required key, explicit null allowed |
| `proposed_at`, `send_deadline` | UTC instants | `proposed_at < send_deadline <= proposed_at + 900 s` |
| `review.quote` | `{bid_cents, ask_cents, market_data_type, as_of}` | `bid <= ask`; `as_of <= proposed_at`; type in realtime/delayed/frozen/unknown |
| `review.max_loss_cents`, `review.notional_cents` | int > 0 | equity: notional = qty x limit; max loss >= stop loss |
| `review.screening_sha256` | sha256 | A's screening evidence digest |
| `review.owner_epoch`, `review.profile_digest` | str, sha256 | the desk's live bindings at compose time |

`EntryProposal.create(body)` mints the id; a supplied wrong id refuses.
`proposal_sha256` = sha256 of the full file bytes (what an approval repeats
beside `proposal_id`). Equity only: `exit_deadline_session` >= the ET date
of `send_deadline`.

### Command (`contracts.commands`)

Discriminated on `kind`; every kind has its own validated `args`.

| Common field | Invariant |
| --- | --- |
| `schema` = `"trex-command/1"` | |
| `idempotency_key` | lowercase UUID; the file name `commands/<key>.json` |
| `issued_at`, `expires_at` | `issued_at < expires_at <= issued_at + 300 s` |
| `issued_by` | `Actor {sub, ingress: loopback/family, request_id, request_sha256}` stamped by the backend from `CommandAuthority.verify` |

| `kind` | `args` |
| --- | --- |
| `compose_entry` | `program_id`, `candidate_id`, `asset_kind` (option/equity), `sleeve_ref?`; Wave 5 amendment: `manual_equity_ticket?` (see the amendment log) |
| `approve_entry` | `proposal_id`, `proposal_sha256`, `effect_sha256`, `owner_epoch`, `profile_digest` (all required) |
| `reject_entry` | `proposal_id`, `reason` |
| `pause_entries`, `resume_entries`, `halt`, `flatten`, `resume_all` | `reason` (<= 500 chars, may be empty) |
| `backup_state` | `label?` (path-safe) |

Idempotency: `command_body_sha256` hashes the command with `issued_by`
reduced to `{sub, ingress}`. Same key + same body hash: `duplicate`
(reason `duplicate_command`, `result` names the first outcome). Same key +
different body hash: `refused` with `command_body_mismatch`. A browser
retry (new `request_id`/`request_sha256`) is therefore a duplicate; a
different principal reusing a key is a mismatch.

`CommandAuthority` (Protocol, E implements, G calls):
`verify(raw_request: RawCommandRequest) -> Actor`, raising
`CommandAuthorityRefused` (default reason `command_unauthorized`).
`RawCommandRequest = {method, path, headers (lower-case), body, peer_host,
request_id}`; `peer_host` is the transport peer, never a forwarded header.
`actor_binds_request` checks the actor was minted for those exact bytes.

### Command result

`commands/<key>.result.json`: `{schema, idempotency_key, kind,
command_body_sha256, status, reason, detail, result, at}`.

| `status` | Meaning | `reason` |
| --- | --- | --- |
| `accepted` | validated, durably queued, nothing applied | null |
| `applied` | effect done; `result` carries facts | null |
| `refused` | refused before any effect | required (a `Blocker` or an existing `SupervisedRefused` reason) |
| `failed` | applying failed; any broker effect is preserved as uncertain by the supervised core | required |
| `duplicate` | same key and body as an earlier command | `duplicate_command` |

## C2.3 `supervised-mandate/2`

Module `contracts.mandates`. One approval authorizes exactly one order for
exactly one effect. Stored at `<supervised dir>/mandates/<intent_id>.json`.

| Field | Invariant |
| --- | --- |
| `intent_id` | `PathSafeId`; the file name |
| `environment` | `"ibkr-paper"` |
| `account_id`, `strategy_version` | copied from the proposal |
| `proposal_id`, `effect_sha256` | the approved proposal and effect |
| `max_orders` | `1` (literal) |
| `approval_id` | the approving command's `idempotency_key` |
| `owner_epoch`, `profile_digest` | the approval's bindings |
| `send_deadline` | copied from the proposal |
| `granted_at`, `expires_at` | `granted_at < expires_at <= min(send_deadline, granted_at + 300 s)` |
| `granted_by` | the approving `Actor` |

- `approval_blockers(proposal, command, now, owner_epoch, profile_digest)`
  is the pure approval check (E authoritative, G may pre-check):
  `approval_proposal_mismatch` (id or file hash), `approval_effect_mismatch`,
  `approval_epoch_mismatch` (args or proposal vs the LIVE desk epoch),
  `approval_profile_mismatch`, `command_expired`, `approval_expired`
  (send deadline reached).
- `mandate_for_approval` builds the mandate; `approval_expiry` =
  `min(send_deadline, approved_at + 300 s)`.
- `mandate_blockers(mandate, now, effect_sha256, owner_epoch,
  profile_digest)` is the permit-time check.
- Consumption: minting the permit renames the file to
  `<intent_id>.consumed.json` (rename = consumption, as permits do);
  a second attempt refuses `mandate_already_consumed`.
- A desk restart mints a new owner epoch, so every pending approval stops
  matching: restart invalidates approvals (event `approvals_invalidated`).
- The v1 single active `mandate.json` is untouched.
- Producer and consumer: E. Readers: A, G (status).

## C2.4 Books

Module `contracts.books`.

### Broker facts (produced by B, folded by D)

`BrokerExecution`: `exec_id`, `base_exec_id`, `perm_id` (>= 1),
`order_ref` (may be empty for outside orders), `account_id`, `con_id`,
`sec_type` (`OPT`/`STK`/`BAG`), `symbol`, `side` (`BOT`/`SLD`, IBKR's
vocabulary), `quantity` (int >= 1), `price_micros`, `multiplier` (STK = 1),
`time`, `commission_micros` (null until the commission report arrives).

Correction rule: `base_exec_id = exec_id` up to its LAST `.` (an id with
no dot is its own base). IBKR re-reports a corrected execution with the
last segment incremented (`...01.01` then `...01.02`); same base = same
fill, the later report corrects the earlier.

`BrokerPortfolioItem`: `account_id`, `con_id`, `sec_type` (`OPT`/`STK`),
`symbol`, `position` (signed int; option short legs negative; 0 rows
appear after a close), `avg_cost_micros`, `market_price_micros?`,
`market_value_cents?`, `as_of`.

A broker row of any other security type, or a fractional quantity, cannot
be represented: the producer surfaces the view as `unavailable` /
`reconciliation_required`, never drops the row. `BAG` executions are
evidence only; the ledger books the leg executions.

### `trex-ledger-event/1` (D, `ledger/events.jsonl`, append-only)

Common: `event_id`, `kind`, `account_id`, `at` (broker time),
`recorded_at`, `attribution`, `quantity_delta` (signed),
`cash_delta_micros` (signed), optional `con_id`, `sec_type`, `exec_id`,
`base_exec_id`, `perm_id`, `order_ref`, `supersedes_event_id`,
`ref_sha256`, `detail`.

| `kind` | Shape | `event_id` |
| --- | --- | --- |
| `fill` | exec ids + con_id + sec_type + perm_id; quantity moves | `fill:<exec_id>` |
| `correction` | as fill + `supersedes_event_id`; deltas are corrected minus superseded | `correction:<exec_id>` |
| `commission` | exec ids; no quantity | `commission:<exec_id>` |
| `cash_adjustment` | no exec ids, no quantity | `cash:<source id>` |
| `reconciliation` | marker; no quantity, no cash; `ref_sha256` = the reconciliation document hash | `reconciliation:<ref_sha256>` |

The ledger is a pure sum (position = sum of `quantity_delta`, cash = sum
of `cash_delta_micros`); deterministic ids make a replayed broker fact a
no-op append.

### `Attribution`, `Holding`

`Attribution {intent_id, strategy_version, program_id, sleeve_ref}`:
`intent_id`, `strategy_version`, `program_id` are all set (ours) or all
null (outside); `sleeve_ref` defaults to `"unassigned"`. D attributes by
`parse_order_ref(order_ref)`: `sup:<iid>` directly, `desk:<sid>` through
the spec (equity `sid` = intent id).

`Holding`: `account_id`, `con_id`, `sec_type`, `symbol`, `quantity`
(non-zero; STK must be positive: long-only), `cost_basis_micros` (net fill
cash for the open quantity; commissions are their own rows), `lots`
(one `AttributedQuantity` per attribution, summing to `quantity`).

## C2.5 `trex-risk-snapshot/1` and `trex-reconciliation/1`

Module `contracts.risk`. Produced by D from one broker session; read by A
(admission), G (display), F.

`RiskSnapshot`: `account_id`, `as_of`, `status` (`complete` = every figure
read; `unavailable` = all figures null), `open_loss_cents` (>= 0),
`day_realized_loss_cents` (>= 0), `cash_cents` (settled; negative =
borrowing; W2c: net of the booked stock cash the broker's settled figure
does not show yet, see the amendment log), `equity_gross_cents` (>= 0),
`outside_positions` (rows in
contracts the Wave 2 ledger never touched), `stale_after_s` = 120,
`cash_source` (optional, additive 2026-10-21: `SettledCash` or
`TotalCashValue`, the account-value tag `cash_cents` was read from; absent
when unknown, never on an unavailable snapshot).
`risk_snapshot_blockers`: absent, unavailable, older than 120 s or
future-dated gives `risk_snapshot_stale`.

`Reconciliation`: `account_id`, `status`
(`clean`/`reconciliation_required`/`unavailable`), `as_of`, `mismatches`
(`{con_id, local_qty, broker_qty, kind}`, one per con_id), `max_age_s` = 120.
Scope: contracts the ledger has touched. `unexplained` = local 0, broker
non-zero; `missing` = local non-zero, broker 0; `quantity_mismatch` = both
non-zero and different (kind is validated against the quantities).
`reconciliation_required` lists at least one mismatch; `clean` and
`unavailable` list none. `reconciliation_blockers`: anything but a fresh
`clean` gives `reconciliation_required`. Both block ENTRIES only, never
exits.

## C2.6 `trex-desk-spec/2` and the run-dir layout

Module `contracts.desk_spec_v2`. `specs/<sid>.json` holds v1 (unchanged,
options, still parsed by `trex.desk_spec.DeskSpec`) or v2. v2 common fields:
`intent_id`, `account_id`, `entry_order_ref` (= `trex:sup:<intent_id>`),
`registered_at`, discriminated by `asset_kind`:

- `option`: `structure` = the existing `LegStructure`;
- `equity`: `position = {position_id (= intent_id), symbol, con_id,
  quantity, entry_limit_cents, exit_policy}`.

`parse_desk_spec(doc)` reads both versions (unknown schema refuses: report
it as uncertainty). `desk_spec_v2_for(effect, registered_at)` is what C
registers before sending. Producer C; consumers C, D, exposure readers, G.

Layout (`contracts.layout`, pure paths):

| Path | Root | Content |
| --- | --- | --- |
| `PAUSE_ENTRIES` | run dir | entries-only pause (exits, fills, reconciliation continue) |
| `HALT`, `FLATTEN` | run dir | existing kill files (HALT also gates exits) |
| `specs/<sid>.json` | run dir | v1 or v2 spec |
| `commands/<key>.json`, `commands/<key>.result.json` | run dir | command, latest result |
| `proposals/<proposal_id>.json`, `proposals/<proposal_id>.rejected.json` | run dir | proposal, rejection tombstone |
| `ledger/executions.jsonl`, `ledger/events.jsonl` | run dir | raw broker facts, ledger |
| `ledger/holdings.json`, `ledger/risk-snapshot.json`, `ledger/reconciliation.json` | run dir | latest projections |
| `mandates/<iid>.json`, `mandates/<iid>.consumed.json` | supervised dir | mandate/2, consumed |

Run dir = `TREX_DESK_RUN_DIR` (default `~/.local/state/trex/desk-paper`);
supervised dir = `TREX_SUPERVISED_DIR` (default
`~/.local/state/trex/supervised`).

## C2.7 Vocabulary

Module `contracts.vocabulary`. `Blocker` (wire strings, frozen by a test):

- from the plan: `entries_paused`, `reconciliation_required`,
  `risk_snapshot_stale`, `equity_quote_not_realtime`,
  `equity_symbol_not_qualified`, `insufficient_cash`, `approval_expired`,
  `approval_epoch_mismatch`, `approval_profile_mismatch`,
  `approval_effect_mismatch`, `mandate_already_consumed`,
  `duplicate_command`, `command_body_mismatch`;
- added by this lane: `approval_proposal_mismatch`, `command_expired`,
  `command_unauthorized`, `proposal_unknown`,
  `session_entry_limit_reached`.

- added by the 2026-10-21 amendment (lane K; see the amendment log), each
  value exactly as its producer spells it:
  - kill files and owner facts (`trex.pause`, `trex.admission`,
    `desk.enter`): `halt`, `auto_off`, `exit_owner_not_ready`,
    `shadow_only_build`, `selection_rule_not_activated`;
  - entry screening (`trex.supervised_canary`, `action_graph.canary`):
    `credit_open_not_supported_v1`, `risk_snapshot_account_mismatch`,
    `profile_limits_missing`, `effect_account_not_mandate_account`,
    `equity_not_whole_share_long`, `quote_stale_or_future`,
    `broker_owner_unhealthy`, `protective_exit_unavailable`,
    `equity_notional_exceeds_intended_capital`,
    `trade_exceeds_intended_capital`, `trade_loss_cap_exceeded`,
    `open_exposure_unknown`, `open_loss_cap_exceeded`,
    `daily_loss_unknown`, `daily_loss_cap_exceeded`;
  - equity preflight (`trex.supervised_ibkr`): the session guards it shares
    with the option preflight (`gateway_disconnected`,
    `not_paper_gateway_port`, `wrong_client_id`,
    `account_not_managed_by_session`, `account_not_paper`) and its own five
    (`equity_con_id_mismatch`, `equity_quote_incomplete`,
    `broker_view_unreadable:account_values`,
    `broker_view_unreadable:open_orders`, `cash_commitment_unbounded`);
  - per-trade authority refusals (`trex.supervised`, `trex.grant_policy`):
    `effect_invalid`, `effects_halted`, `entry_mandate_already_granted`,
    `intent_id_not_path_safe`, `mandate_absent`, `mandate_account_mismatch`,
    `mandate_binding_mismatch`, `mandate_scope_mismatch`,
    `permit_already_issued`, `product_strategy_requires_approval`;
  - holdings checks before a desk equity sell (the `why` of the runtime's
    `reconciliation_required` event): `holdings_mismatch`,
    `holdings_excess`, `not_a_stock_row`, `fractional_position`,
    `order_fill_not_whole_shares`;
  - web command ingress and store (`trex_web.commands_view`,
    `trex_web.command_store`): `body_too_large`, `invalid_json`,
    `issued_by_is_stamped_by_the_backend`, `invalid_command`,
    `command_not_yet_valid`, `unknown_command`, `command_store_unavailable`,
    `commands_dir_unwritable`, `command_store_unreadable`,
    `command_store_foreign_file`, `command_result_invalid`,
    `staged_command_invalid`, `staged_command_mismatch`;
  - web views (proposal list problems, `GET /api/portfolio`):
    `proposal_unreadable`, `proposal_invalid`, `proposal_not_canonical`,
    `proposal_id_mismatch`, `mandate_unreadable`,
    `<label>_missing` / `<label>_unreadable` / `<label>_invalid` for the
    labels `holdings`, `risk_snapshot`, `reconciliation`,
    `risk_snapshot_unavailable`, `reconciliation_stale`,
    `reconciliation_unavailable`, `account_mismatch`, `non_paper_account`;
  - reserved for the Wave 2b lanes (coordinator, not yet emitted):
    `entry_window_closed`, `outside_undefined_risk`, `exit_quote_blind`.
- added by the W2c integration amendment (2026-10-21; see the amendment
  log): `flatten` (`trex.admission`: a standing FLATTEN blocks entries);
  the owner entry flows' (`trex.desk_entries`) `outside_regular_session`,
  `profile_absent`, `profile_invalid`, `account_unbound`,
  `candidate_unknown`, `candidate_blocked`, `candidate_invalid`,
  `candidate_program_mismatch`, `candidate_kind_mismatch`,
  `candidate_expired`, `intent_already_used`, `quote_unavailable`,
  `proposal_rejected`, `broker_rejected`, `send_uncertain`,
  `desk_book_unreadable`; the owner command consumer's
  (`trex.desk_commands`) `command_invalid`, `command_key_mismatch`,
  `apply_error`, `owner_stopped_before_result`.

`DeskEvent` (names for `events.jsonl`): `proposal_composed`,
`entry_approved`, `entry_rejected`, `entries_paused`, `entries_resumed`,
`approvals_invalidated`, `command_applied`, `command_refused`,
`equity_exit_stop`, `equity_exit_target`, `equity_exit_deadline`,
`equity_exit_forced`, `reconciliation_clean`, `reconciliation_required`;
added by the 2026-10-21 amendment (the desk runtime's equity path):
`equity_contract_mismatch`, `equity_unqualified`,
`exit_evidence_inconclusive`, `holdings_unverifiable`,
`structure_tick_failed`; added by the W2c integration amendment:
`commands_failed` (a contained command-stage failure), `gateway_connected`,
`books_problem`, `books_failed`.

Both enums are `@unique` (no two members share a value) and every member
name is its value spelled as an identifier (`:` becomes `_`, so
`Blocker.BROKER_VIEW_UNREADABLE_ACCOUNT_VALUES`). A value may sit in both
enums (`entries_paused`, `reconciliation_required`): a blocker and the
event that records it. Producers may keep passing the plain strings: a
`StrEnum` member compares and serializes equal to its value.

## C2.8 Test fakes (deferred until Wave 1 merges)

`tests/unit/trex_fakes.py` is being edited by Wave 1, so the shared fakes
land after it merges. They must provide:

1. `FakeCommandAuthority`: loopback and family ingress; deterministic
   `Actor` bound to the raw body; refusal modes (bad signature, wrong peer,
   expired signed actor).
2. Stock qualification on `FakeGateway`: `Stock("SPY", "SMART", "USD")`
   to conId 756733; an unqualifiable symbol; `reqMarketDataType` switching
   between realtime (1), frozen (2) and delayed (3) with timestamped NBBO
   ticks, including a bid sequence for `stop_confirm_ticks`.
3. STK DAY limit placement honoring `orderRef` and explicit `account`;
   partial fills; `execDetails` with IBKR-shaped exec ids, a correction
   re-report (`.01` then `.02`), commission reports arriving after the
   execution, `permId`; all-client open/completed order views for lookup.
4. Positions/portfolio/account values: settled cash, net liquidation,
   outside positions, and injectable faults (fractional position, unknown
   secType, unreadable view) to exercise fail-closed paths.
5. A session clock across NYSE sessions including an early-close day, and
   a desk restart that mints a new owner epoch.
6. Builders reusing `tests/contracts/fixtures/`: equity effect, proposal,
   approve command, mandate/2, ledger events.

## Defaults and open questions

The 2026-10-14 plan is silent on these; each default is in
`contracts.defaults` or the schemas and holds until the owner rules.

| # | Question | Default chosen |
| --- | --- | --- |
| 1 | Margin? | Cash only, no margin, no shorts (`CASH_ONLY`). Negative settled cash or notional > cash gives `insufficient_cash`. |
| 2 | Which equities? | SPY only (`EQUITY_SYMBOL_ALLOWLIST`) until more real-time data is licensed; others give `equity_symbol_not_qualified`. |
| 3 | Real-time data? | An equity entry needs `market_data_type == "realtime"`; delayed/frozen/unknown give `equity_quote_not_realtime`. Options keep their existing freshness checks. |
| 4 | Broker-side stops? | None. The desk manages equity exits (stop confirm, target, deadline, forced 15:45 ET); no resting broker stop or target. |
| 5 | Gap risk without a resting stop | `equity_max_loss_cents` reserves max(stop loss, 20% of notional) (`EQUITY_GAP_RESERVE_BPS` = 2000). |
| 6 | Approval lifetime | `min(send_deadline, approved_at + 300 s)` (`APPROVAL_TTL_S`). |
| 7 | Desk restart with pending approvals | Invalidated (new owner epoch; `approval_epoch_mismatch`); the owner re-approves. |
| 8 | Entries per session | 3, both asset kinds combined (`MAX_ENTRIES_PER_SESSION`; `session_entry_limit_reached`). Open: per asset kind instead? |
| 9 | Proposal lifetime | `send_deadline <= proposed_at + 900 s` (`PROPOSAL_MAX_TTL_S`). |
| 10 | Command lifetime and who applies what | `expires_at <= issued_at + 300 s`. `pause_entries` and `halt` (safety direction) may be applied by whichever process receives them by writing the kill file; `resume_entries`, `resume_all`, `flatten`, compose/approve/reject are applied only by the desk process. |
| 11 | `resume_all` | Removes `HALT`, `FLATTEN` and `PAUSE_ENTRIES` (superset of `desk_cli resume`); does not revive approvals. W2c amendment: never a stop issued after the resume (see the amendment log). |
| 12 | `flatten` scope | Every position the desk owns, both asset kinds; never outside positions. |
| 13 | Early-close sessions | `forced_exit_et` stays `"15:45"` in the schema; C applies min(15:45, close minus 15 min), so 12:45 on a 13:00 close. |
| 14 | Maximum equity hold | Not bounded beyond `exit_deadline_session >= entry session`. Open: cap it (e.g. 5 sessions)? |
| 15 | v1 vs v2 mandates for options | Both coexist; E uses a v2 mandate when one exists for the intent, else the v1 path. Open: retire v1 grants once v2 is live? |
| 16 | Live trading | Out of scope: the equity effect requires a paper (`D`) account and mandate/2 is `ibkr-paper` only. Live needs new schema versions. |
| 17 | `compose_entry` inputs | `program_id`, `candidate_id`, `asset_kind`, `sleeve_ref?`. Open: the candidate source (miner deal id vs discovery candidate id). |
| 18 | Risk snapshot `open_loss_cents` | Equities: max(quantity x (mark minus stop), gap reserve of mark notional); options: remaining defined max loss. Open: confirm with the owner. |

### Deliberate deviations from the brief

- **Mandates live under the supervised state dir**, not the desk run dir:
  minting the permit and consuming the mandate then share the one advisory
  lock `issue_permit` already holds; across two directories they could race.
- **Books use micros** for broker-reported prices, commissions and cash
  deltas (`price_micros`, `commission_micros`) instead of cents: IBKR
  reports sub-penny fills and fractional-cent commissions, and a cents
  ledger would not sum to the broker.
- **Added fields**: the equity effect hashes `currency` and `exchange`
  (no order attribute rides along unhashed); the proposal adds
  `effect_sha256` and `proposed_at`; mandate/2 adds `environment`,
  `account_id`, `strategy_version`, `proposal_id`, `send_deadline`; the
  command adds `issued_at`; the command result adds `kind`,
  `command_body_sha256`, `detail`; the risk snapshot adds
  `day_realized_loss_cents` (today's screening needs the day loss).
- **Added blockers** listed under C2.7 (and the 2026-10-21 vocabulary
  amendment, see the amendment log).

## Verification (2026-10-21)

- `tests/contracts/test_wave2_contracts.py`: 192 passed (host-work test
  profile). Round-trips for all 17 fixtures; canonical bytes against a
  stdlib oracle; hash sensitivity per economic field; every validator
  refusal named in the brief; option golden bytes; no `ib_async` import.
- Spot mutants: 9 of 10 killed. The survivor (option bytes via the generic
  encoder instead of `effect_bytes`) is equivalent by construction, which
  is the property the golden test pins.
- `ruff format --check` and `ruff check` clean on the new files; `mypy`
  (repo config, 349 files) clean.

## C2.8 delivered (2026-10-21, on `wave2/base`)

Added below the Wave 1 fakes in `tests/unit/trex_fakes.py`. The existing
fakes (`FakeGateway`, `SupervisedGateway`, `FakeDeskBroker`, helpers) are
unchanged; the new code imports no broker library (a test pins this).
Tests: `tests/unit/test_trex_fakes_wave2.py`.

| C2.8 item | Use |
| --- | --- |
| 1 | `FakeCommandAuthority(clock=, refuse_with=)`: `verify`, `make_request(body, family_sub=, peer_host=)`, `family_actor()`; records `calls` / `actors` / `refusals`. `sign_family_actor` mints family-apps v1 `x-family-actor` values (golden-checked against `src/actor.mjs`). Refusals: reason `command_unauthorized`; `detail` names the mode (`wrong_peer`, `bad_signature`, `actor_expired`, `wrong_audience`, `wrong_route`, `forwarded_without_actor`, forced). |
| 2-4 | `Wave2Gateway(con_ids=, account="DU0000001", stocks={"SPY": 756733}, clock=)`, a `SupervisedGateway` subclass. Market data: `reqMarketDataType`, `set_market_data_type`, `script_ticks` / `script_bids` / `push_tick`. Orders: STK `placeOrder` checked against the C2.1 wire shape (`wire_violations`), `script_fills(order_ref, [FillStep(qty, price_micros, commission_micros)])`, `auto_fill`, `cancelOrder`, DAY expiry at the session close. Executions: IBKR exec ids (the first is the fixture's), `execDetailsEvent`, late `commissionReportEvent` (`commission_lag`, `deliver_commissions`), `correct_execution` (`.01` to `.02`). Positions: `positions()`, `portfolio()`, `accountValues()` (with `SettledCash`), `marks`; faults `add_broker_position` (unexplained, outside, fractional, unknown secType), `hide_position` (missing), `override_position` (quantity mismatch), `account_view_error`. |
| 5 | `SessionClock()`: ET-aware and callable, over the trex NYSE calendar; `advance`, `to`, `to_et`, `to_open`, `to_close`, `to_next_open`, `close_of` (early closes), `next_session`, `trading_session`. `restart(down_s=, until=)` mints a new `owner_epoch`; the first epoch is the fixtures' `owner-20261022T133000Z-4242`. |
| 6 | `contract_fixture`, `equity_effect`, `entry_proposal`, `approve_command`, `mandate_v2`, `ledger_event`; with no changes each reproduces its fixture. |

Choices lanes should know:

- Stock support is a subclass, not an edit of `FakeGateway`. As in ib_async
  2.x, its `qualifyContracts` returns None for an unqualified contract.
- Fills, ticks, cancels and commission reports advance one step per
  `sleep`, and each `sleep(s)` advances the gateway's clock by `s`.
- A wire violation raises `AssertionError` and is also recorded, so an
  adapter that swallows the raise still fails its test.
- Stock positions are derived from fills; option positions stay manual
  (`position_rows`). The fake does not compute realized PnL (ib_async
  reports `0.0`) or charge commissions to cash.
- Verification: the required Wave 1 suites, `tests/soak/`,
  `tests/contracts/` and the new file: 453 passed (host-work test profile).
  `ruff` and `mypy` (repo config, plus the two test files) are clean.

## Amendment log

### 2026-10-21: C2.7 vocabulary, additive (lane K, coordinator-approved)

Coordinator decision 3 (2026-10-21): the codes the merged Wave 2 lanes
introduced become additive members of `contracts.vocabulary`. Nothing was
renamed or removed; every new value equals the string its producer already
emits, so no producer changed.

- Added 80 `Blocker` members (77 emitted today, 3 reserved; 98 in all)
  and 5 `DeskEvent` members (19 in all), listed under C2.7.
  Scope: every code a Wave 2 surface emits as an entry, approval, command or
  sell blocker, a refusal reason, a view reason or a desk event name. That
  includes Wave 1 strings a Wave 2 surface now returns beside `Blocker`
  members (`halt`, `auto_off`, the shared profile caps of
  `review_equity_entry`, the preflight session guards, the existing
  `SupervisedRefused` reasons the per-trade path raises).
- Reserved for the Wave 2b lanes, not yet emitted: `entry_window_closed`,
  `outside_undefined_risk`, `exit_quote_blind` (as `Blocker` members:
  they refuse entries; a lane that also records one as an event adds the
  `DeskEvent` member, as `reconciliation_required` has both).
  `approvals_invalidated` was already a `DeskEvent` member.
- `Blocker` and `DeskEvent` are now `@unique`; the `Blocker` docstring
  now also covers refused sells and unavailable view facts.
- Left out on purpose (still plain strings): free-text `detail` values
  (the command authority's refusal modes such as `wrong_peer` or
  `forwarded`, error class names); codes with a dynamic suffix
  (`effect_invalid:<ErrorClass>`, `order_unbuildable:<ErrorClass>`);
  document statuses (`accepted`, `applied`, `pending`, ...); the equity
  exit rule reasons (`trex.equity_exits.EquityExitReason`); the exit
  watcher's own statuses (`monitor_down`, `touch_blind`, `quote_blind`);
  audit-file reasons (mandate retirements: `expired`,
  `owner_epoch_changed`, `profile_changed`); and the Wave 1 desk
  lifecycle events the equity path reuses unchanged (`registered`,
  `exit_begin`, `exit_order`, `exit_fill`, `closed`, `cancel`,
  `unknown_exposure`, ...).
- Tests (`tests/contracts/test_wave2_contracts.py`): both enums pinned as
  literal sets (plan plus amendment, disjoint); values unique and names
  derived from values; the coordinator's named strings round-trip
  (value, `str`, JSON, lookup by name); every amended value is a string
  literal of its producer module (AST scan, independent of the enum); the
  producers' constants equal the members; and the admission, equity review
  and portfolio view outputs of fixed scenarios are all members.

### 2026-10-21: C2.7 vocabulary, additive (W2c integration)

The Wave 2c integration lane (branch `w2c/seams`) made the plain strings
the Wave 2b owner lane introduced additive members, values unchanged, and
added the two codes its own fixes emit. Nothing renamed or removed.

- `Blocker`: `flatten` (new: `admission.entry_send_blockers` now refuses
  every entry sender while `FLATTEN` stands) and the owner lane's 20
  refusal codes listed under C2.7 (`command_not_yet_valid` was already a
  member).
- `DeskEvent`: `commands_failed` (new: the owner contains a command-stage
  failure and records it), `gateway_connected`, `books_problem`,
  `books_failed`.
- Tests (`tests/contracts/test_wave2_contracts.py`): the W2c set is pinned
  disjoint from the earlier sets; every W2c code round-trips; every value
  but `commands_failed` (whose producer spells the member) is a literal of
  its producer module; the producers' constants equal the members.

### 2026-10-21: C2.5 `cash_cents` after same-day stock buys (W2c integration)

No schema change. IBKR's `SettledCash` moves only when a trade settles
(US stocks T+1), so the snapshot's cash overstated what a second same-day
purchase could spend. The books now publish `cash_cents` = `SettledCash`
minus every booked stock execution whose settlement session (the first
session after its ET trade date; an unplaceable date counts as unsettled)
is after the snapshot's ET date: a purchase's price net of corrections plus
its commission, a sale's commission alone (a sale's proceeds are never
added early). Conservative by construction: a double count could only
understate cash and refuse an entry (`insufficient_cash`), never admit one;
exits never read it (`trex.risk_snapshot` module docstring).

### 2026-10-21: C2.5 cash tag fallback, `cash_source` (paper account hotfix)

Additive: one new optional field, no vocabulary change. Observed live on
the IBKR paper account (21:52 ET): it is margin-type, its account values
carry `TotalCashValue` but no `SettledCash`, so the books logged
`books_problem: risk:settled cash unreadable` and every entry failed
closed. One rule (`risk_snapshot.account_cash`, read by the books and by
the equity preflight through `IbkrTrex.account_cash`): `SettledCash` when
the account reports it (USD, else BASE), else `TotalCashValue` (USD, else
BASE), else unreadable as before; a reported but junk or ambiguous figure
is unreadable, never replaced. `RiskSnapshot.cash_source` records the tag
(absent from the document when None, so every earlier snapshot and the
frozen fixture keep their bytes; an unavailable snapshot never carries
it). The W2c subtraction is unchanged; `TotalCashValue` already shows a
same-day purchase (the double count only understates) and also a same-day
sale's proceeds, so from it the books also take every booked execution's
unsettled proceeds (stock or option, same settlement rule) back out:
never more than the `SettledCash` figure of the same books.

### 2026-10-21: default 11, a resume never lifts a newer stop (W2c refix)

Additive; no schema or vocabulary change. A W2c review P1 found that the
first W2c command budget applied every queued stop before an OLDER queued
resume, which then lifted it while the stop's own answer read `applied`.
Now:

- The owner applies the kill-switch kinds (`halt`, `flatten`,
  `pause_entries`, `resume_all`, `resume_entries`, `backup_state`) first,
  in `issued_at` order among themselves (on a tie the resume before the
  stop), never deferred by the pass budget; then the entry kinds.
- A stop placed by a command records its `issued_at` in the file (`HALT` /
  `FLATTEN`: beside `placed_by`, `reason`, `command`, `at`;
  `trex-pause-entries/1`: an added optional `issued_at`). A stop applied
  while its file stands re-stamps it when newer.
- `resume_all` / `resume_entries` leave any stop recorded as issued after
  them, and answer `applied` with `superseded_by_newer_stop` (a list of
  the file names kept; empty when none) beside `removed`. A stop without a
  recorded time (the terminal's, an older record) lifts as before.
- Tests: `tests/unit/test_desk_commands.py` (the review's probes A-E plus
  late delivery, re-stamp and terminal-stop cases) and
  `tests/unit/test_trex_pause.py`.

### 2026-10-21: C2.7 `DeskEvent.exit_quote_blind` (Wave 3/4 integration)

Additive. The desk runtime now records `exit_quote_blind` (an equity close
pending without a usable bid for 300 s, once per structure per ET session,
`trex.equity_exits.ExitQuoteWatch`, wired into `DeskRuntime._equity_close`)
and pushes it urgent. Following this log's rule (a code a lane also records
as an event gets the `DeskEvent` member, as `reconciliation_required` has
both), `DeskEvent` gains `EXIT_QUOTE_BLIND`, equal to the reserved
`Blocker.EXIT_QUOTE_BLIND`. Tests (`tests/contracts/test_wave2_contracts.py`):
the W3/4 set is pinned disjoint from the earlier sets, the member equals the
runtime's event name and the reserved Blocker, and it is in the runtime's
notify set. The Wave 4 alert sweep already reads it from `events.jsonl`.

### 2026-10-21: C2.2 `compose_entry` manual equity ticket, C2.7 (Wave 5)

Additive. The owner initiates every entry ("manual plus qualified ideas",
the 2026-10-14 plan); this settles open question 17 (the candidate source):
a draft file stem under `proposal-drafts/` (listed by `GET /api/ideas`,
`trex-idea-list/1`, WAVE3-CONTRACTS.md amendment log) or a manual ticket.

- `ComposeEntryArgs` gains the optional `manual_equity_ticket`
  (`contracts.commands.ManualEquityTicket`): `symbol`, `quantity` (whole
  shares, >= 1), `stop_price_cents`, `target_price_cents?` (above the stop),
  `exit_deadline_session` (ISO date), `limit_policy` = `"ask"` | `"mid"` |
  `{"limit_cents": n}` (an explicit limit sits between the stop and the
  target). Strict integers, `extra="forbid"`. Set exactly when
  `candidate_id` is `manual:<lower-case uuid>` (`MANUAL_CANDIDATE_PREFIX`),
  and only for `asset_kind` equity; a `manual:` candidate without a ticket
  refuses, as does a ticket on any other candidate. An absent ticket is
  absent from the document, so every earlier `compose_entry` keeps its
  bytes and its `command_body_sha256` (pinned by a stdlib oracle).
- The desk (`trex.desk_entries`) composes a ticket in place of reading a
  draft: the allowlist (`equity_symbol_not_qualified`), the equity entry
  window (`entry_window_closed`) and the exit deadline (a session on or
  after today) before any broker call; then the session guards, the stock
  qualified, a live quote (`quote_unavailable`, `equity_quote_not_realtime`),
  the limit by policy (`mid` rounded up to the cent), the stop below and the
  target above it; intent `manual-<uuid>` (`manual_intent_id`), strategy
  `owner-manual/1` (`MANUAL_STRATEGY_VERSION`), the command's program,
  `equity_draft` valid until the entry window closes. Then the unchanged
  compose path: fresh books, owner facts, preflight and the equity screening
  (allowlist, cash, window, session cap, profile caps), and a normal
  `trex-entry-proposal/1`. Composing never grants a mandate or sends;
  approval stays `approve_entry` on the proposal.
- C2.7: `Blocker` gains `manual_ticket_invalid` (the live price cannot bound
  the ticket: stop not below / target not above the limit, or the exit
  deadline is not a session on or after today), `drafts_dir_unreadable` and
  `draft_unreadable` (`GET /api/ideas`).
- Fixtures: `tests/contracts/fixtures/wave5/command_compose_entry_manual.json`.
  Tests: `tests/contracts/test_wave5_contracts.py` (round trips, the
  unchanged earlier body hash, every misbound or malformed ticket, the new
  members), `tests/contracts/test_wave2_contracts.py` (the W5 set pinned
  disjoint), `tests/unit/test_desk_manual_ticket.py` (the desk end to end:
  composes with the bound exit policy, never grants or sends, refusals,
  idempotency per key). TypeScript: `CommandArgs.compose_entry` gains
  `manual_equity_ticket?`, with `ManualEquityTicket` and
  `manualCandidateId` in `web/src/lib/contracts.ts`.

### 2026-10-21: the desk's manual ticket rules, tightened (Wave 5 review)

No schema or vocabulary change (one new constant). The desk now refuses,
for a manual ticket only (`trex.desk_entries`):

- a `program_id` other than `owner-manual` (`contracts.ideas.MANUAL_PROGRAM_ID`,
  the program `GET /api/ideas` advertises): `candidate_program_mismatch`,
  before any broker call; the proposal binds that program;
- an exit deadline more than `MANUAL_MAX_HOLD_SESSIONS` (20, new in
  `contracts.commands`) NYSE sessions after the entry session, counted on
  the repo calendar: `manual_ticket_invalid` with detail
  `deadline_beyond_max_hold` (`desk_entries.DEADLINE_BEYOND_MAX_HOLD`) and
  the facts `max_hold_sessions`, `max_exit_deadline_session`, before any
  broker call;
- beside the earlier stop-below-limit and target-above-limit rules, a stop
  at or above the live bid (`the stop is not below the bid`) and a target at
  or below the live ask (`the target is not above the ask`):
  `manual_ticket_invalid`.

Qualifying the stock now maps errors as `IbkrSupervisedBroker.preflight`
does: while the session is up, `StockNotQualified` is
`equity_symbol_not_qualified` and any other ValueError or RuntimeError
`order_unbuildable:<error class>` (never the provider text); once the
session reads disconnected the error propagates (`gateway_lost`, exit 6).
Tests: `tests/unit/test_desk_manual_ticket.py`.
