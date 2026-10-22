# TREX Waves 3 and 4 frozen cross-lane contracts

Status: FROZEN for Wave 3/4 lane work (branch `w3/contracts`, based on
`wave2/integ` at `4dad0a4`). Code: `src/tree_options/trex/contracts/wave3.py`
and `src/tree_options/trex/contracts/acceptance.py`. TypeScript mirror:
`web/src/lib/contracts.ts` (shapes, labels, helpers) and `web/src/lib/routes.ts`
(route table as data). Tests: `tests/contracts/test_wave3_contracts.py` and
`web/src/lib/contracts.test.ts`, both reading the shared fixtures in
`tests/contracts/fixtures/wave3/`.

Wave 3 is the human product: five destinations (Today, Experiments, Trade
review, Portfolio, Activity & settings) on desktop and phone, owner commands
through Family Apps, the entry-only pause as the primary control, onboarding.
Wave 4 is the paper acceptance instrumentation and the three-program research
pilot. This document fixes what the lanes exchange so about nine lanes can
build in parallel. Nothing here sends an order, grants authority, unlocks live
trading, or touches a running service, the family-apps checkout or a broker.

## Rules for every lane

- Import from `tree_options.trex.contracts.wave3` (and `.acceptance`); never
  redefine these shapes. The package `__init__` is not edited by this lane
  (only new files were allowed); the integration coordinator may re-export.
- Same change rule as Wave 2: a contract changes only additively (a new
  optional field, a new vocabulary member, a new union member). Anything else
  is a new schema version (`.../2`), never an edit of `.../1`.
- `wave3` and `acceptance` import no broker library and no web framework (a
  test pins that `ib_async` and `fastapi` stay unloaded).
- Every Wave 2 shape is reused unchanged: `Actor`, `RawCommandRequest`,
  `CommandAuthority`, `CommandAuthorityRefused`, `ApproveEntryArgs`,
  `CommandKind`, `Blocker`, `trex-command/1`, `trex-proposal-list/1`
  (`GET /api/proposals`) and `trex-portfolio/1` (`GET /api/portfolio`).
- TypeScript changes only together with its Python model and the fixture. The
  fixtures are the contract between the two: Python round-trips them through
  the models; `contracts.test.ts` checks every key list against them; `tsc`
  checks every key list against its type.
- Conventions are Wave 2's: canonical bytes, UTC instants serialized `...Z`,
  whole-second durations (no `timedelta` outside `tree_options/time/`),
  integer cents and micros, `StrictModel` (frozen, `extra="forbid"`).

### Shared-file custody (avoids merge conflicts)

| File | Only this lane edits it |
| --- | --- |
| `src/tree_options/trex_web/app.py` (route wiring) | integration coordinator; lanes ship `attach(app, ...)` in new modules |
| `src/tree_options/trex/contracts/__init__.py`, `commands.py` | coordinator (lane I's `seal_registration` kind lands as a coordinator-approved amendment) |
| `web/src/App.tsx`, `web/src/components/AppShell.tsx`, `web/src/lib/router.ts` | lane E |
| `~/family-apps` (`apps.json`, `src/server.mjs`, `src/actor.mjs`, docs) | lane A, in its own worktree and repo |
| `tests/unit/trex_fakes.py` | lane B (adds a v2 family mode beside the Wave 2 v1 one) |

## Lane map

| Lane | Owns | Produces | Consumes |
| --- | --- | --- | --- |
| A portal (family-apps repo) | `signTrexCommand`, `/trex/` and `/trex-staging/` method rules, key file config | `x-family-actor` command assertions | C3.1, the golden fixture |
| B family authority | `FamilyCommandAuthority`, replay ledger, dispatch, key loading | `Actor{ingress: "family"}` | C3.1, Wave 2 `CommandAuthority` |
| C Today read model | `trex_web/today_view.py`, `GET /api/today` | `trex-today/1` | Wave 2 books, kill files, proposals, `gateway.json`, desk heartbeat, C3.4 summary, calendar |
| D Activity read model | `trex_web/activity_view.py`, `GET /api/activity` | `trex-activity/1` | `commands/`, desk `events.jsonl`, audit files, drill records |
| E web shell and routing | AppShell (desktop nav, phone tab bar, advanced menus), router on `routes.ts`, the control bar | navigation | C3.3, `CONTROL_LABELS` |
| F Today and Trade review pages | approve and reject with countdown, the pause | browser commands | `TodayView`, `ProposalList`, `buildCommandBody`, `secondsLeft` |
| G Portfolio, Activity & settings, onboarding | pages under `#/portfolio`, `#/activity/*`, `#/onboarding` | | `PortfolioView`, `ActivityPage`, `AcceptanceView` |
| H acceptance instrumentation (Wave 4) | session, lifecycle and drill recorders; the compute job; `GET /api/acceptance`; the drill runbook | `trex-acceptance/1`, `trex-acceptance-view/1` | desk heartbeat, reconciliation documents, ledger, commands |
| I research pilot (Wave 4) | registrations, the owner seal, usage metering, budget enforcement, `#/pilot` pages | `trex-pilot-registration/1`, `trex-pilot-usage/1` | C3.5 |

## C3.1 `trex-family-command/1`: the family-signed command assertion

The browser on the phone or desktop reaches TREX only through the Family Apps
portal. For `POST /trex/api/commands` the portal (after its operator session,
exact Origin and size checks) mints a short-lived assertion bound to the exact
request; TREX's family authority verifies it and returns the Wave 2 `Actor`.
The web still only QUEUES commands; the desk owner applies them. No new broker
session owner exists anywhere in this path.

### Wire format (family-apps actor protocol 2, as `signSpeechControl`)

- Header: `x-family-actor` (`ACTOR_HEADER` in `src/actor.mjs`). The portal
  already strips any client-supplied value (`STRIP_REQUEST`).
- Value: `<payload>.<signature>`, where `payload` = unpadded base64url of
  `JSON.stringify(claims)` (compact, UTF-8, insertion order below) and
  `signature` = unpadded base64url of HMAC-SHA256(key, payload as ASCII).
- Claim order (`CLAIM_ORDER`): `signSpeechControl`'s order with `jti`
  appended: `v, sub, aud, iat, exp, method, path, ingress, origin,
  request_id, request_sha256, jti`. The verifier authenticates the payload
  bytes as received, so order matters for the golden fixture, not for
  verification.

| Claim | Value | Rule |
| --- | --- | --- |
| `v` | `2` | a JSON integer; v1 (route-bound) actors never authorize commands |
| `sub` | owner email | trimmed and lower-cased by the portal; must be in `TREX_OWNER_EMAILS` |
| `aud` | `"trex-command"` (`/trex/`) or `"trex-staging-command"` (`/trex-staging/`) | must equal the instance's `TREX_FAMILY_AUDIENCE` (default `trex-command`) |
| `iat`, `exp` | Unix seconds | `iat < exp <= iat + 60`; the portal mints `exp = iat + 60` |
| `method` | `"POST"` | must equal the request method |
| `path` | `"/api/commands"` | the path trex_web sees, see the strip rule |
| `ingress` | `"family"` | |
| `origin` | exact public origin, e.g. `https://pop-os.<tailnet>.ts.net` | the browser's `Origin` the portal checked; in `TREX_FAMILY_ORIGINS`; equal to the forwarded `Origin`; its host and port equal the forwarded `Host` |
| `request_id` | the body's `idempotency_key` | a lower-case UUID |
| `request_sha256` | sha256 hex of the exact body bytes | the portal forwards those bytes unchanged |
| `jti` | 16 random bytes, lower-case hex | single use (replay ledger) |

Path strip rule: the portal strips exactly one app prefix and signs the
upstream pathname: `/trex/api/commands` is forwarded and signed as
`/api/commands` (`/trex-staging/api/commands` likewise). The query string is
never part of `path`; the portal refuses a command POST that has one.

Golden: `tests/contracts/fixtures/wave3/family_command_assertion.json` holds a
TEST-ONLY key (bytes 0..31), the exact body, the claims and the token. Python
rebuilds it with stdlib `json`/`hmac`, the TS test with WebCrypto exactly as
the portal's JavaScript does; lane A golden-checks `signTrexCommand` against it.

### Portal rules (lane A, family-apps)

For each prefix in `PORTAL_ROUTES` (`/trex`, `/trex-staging`), after the
existing operator-only gate:

1. `GET` and `HEAD`: proxied as today, without an assertion.
2. `POST` is allowed only to exactly `<prefix>/api/commands` with an empty
   query; then, in order: exact Origin (`req.headers.origin ===
   byHost(host)`, else 403 `trex-wrong-origin`); body at most 64 KiB
   (`PORTAL_COMMAND_MAX_BYTES`, else 413); key configured (else 503
   `trex-commands-not-configured`); body is a JSON object whose
   `idempotency_key` is a lower-case UUID (else 400 `trex-bad-request`);
   sign, set `x-family-actor`, forward the same body bytes with `Host` and
   `Origin` unchanged.
3. Anything else (another POST path, `PUT`, `DELETE`, `PATCH`, `OPTIONS`,
   a POST with a query): 405 with `Allow: GET, HEAD` (`Allow: GET, HEAD,
   POST` on the exact commands path).

Consequence (intended): the Wave 2 cockpit POSTs (`/api/desk/supervised/*`,
`/api/desk/automation/*`) and the research POSTs (`/api/market/propose`,
`/watch`, `/refresh`, `/api/discovery/scan`, `/backtest`,
`/api/research/compare`, `/api/research/quant/jobs*`, `/api/paper/*`) are no
longer reachable through the portal; they stay loopback-only.

Sketch for lane A (normative behaviour, not code to paste):

```js
export function signTrexCommand(key, {sub, aud, origin, path, body, now=Date.now()}) {
  const request = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(body)); // 400 on failure
  // 400 unless request is a non-array object and request.idempotency_key matches the lower-case UUID pattern
  const iat = Math.floor(now / 1000);
  const claims = {v: 2, sub: sub.trim().toLowerCase(), aud, iat, exp: iat + 60, method: 'POST', path,
    ingress: 'family', origin, request_id: request.idempotency_key,
    request_sha256: createHash('sha256').update(body).digest('hex'), jti: randomBytes(16).toString('hex')};
  const payload = Buffer.from(JSON.stringify(claims), 'utf8').toString('base64url');
  return payload + '.' + createHmac('sha256', key).update(payload, 'ascii').digest('base64url');
}
```

### Key material (by name only; never print, log or commit a value)

- Format: family-apps' pairing-key format, 32 random bytes as standard padded
  base64 (44 characters) in a regular 0600 file, not a symlink, at most 1 KiB;
  ASCII whitespace trimmed; the HMAC key is the 32 decoded bytes
  (`parse_command_key`).
- Portal: private config field `trexCommandKeyPath`, default
  `~/.config/family-apps/trex-command.key` (staging:
  `~/.config/family-apps/trex-staging-command.key`).
- TREX: `TREX_FAMILY_COMMAND_KEY_FILE`, default
  `~/.config/trex/family-command.key`, holding the same bytes.
- A key separate from family-apps' shared `actorSigningKey`: that key is shared
  with other backends (House, Study, Cats), which must not be able to mint TREX
  commands.

### TREX verifier (lane B)

`verify_family_command(raw_request, config=, now=)` is the pure part (in
`wave3.py`); lane B wraps it as a `CommandAuthority` with key loading and the
replay ledger. Refusals raise `CommandAuthorityRefused(reason=
"command_unauthorized", detail=<FamilyRefusal>)`, checked in this order:

`family_ingress_disabled` (no 32-byte key, no owners or no origins) ->
`wrong_peer` (transport peer not loopback) -> `assertion_missing` ->
`assertion_malformed` -> `assertion_bad_signature` (constant-time compare,
before any JSON is parsed) -> `assertion_claims_invalid` (strict JSON, no
duplicate keys, exact claim set and types, lifetime) ->
`assertion_wrong_audience` -> `assertion_not_yet_valid` (`now < iat - 5`) ->
`assertion_expired` (`now >= exp`) -> `assertion_method_mismatch` ->
`assertion_path_mismatch` -> `assertion_origin_not_allowed` ->
`assertion_origin_mismatch` -> `assertion_host_mismatch` ->
`assertion_body_mismatch` -> `assertion_request_id_mismatch` ->
`assertion_sub_not_owner`; then, in lane B, `assertion_replayed` /
`assertion_ledger_unavailable`.

The returned `Actor` is `{sub: claims.sub, ingress: "family", request_id:
<the backend's own request id>, request_sha256: sha256(body)}`, so the Wave 2
`actor_binds_request` check holds unchanged. The claim's `request_id` (the
idempotency key) is not the Actor's `request_id`; it is recorded in the replay
ledger entry.

Dispatch (`authority_for`): a request carrying `x-family-actor` is judged by
the family verifier only, never falling back; any other request goes to the
Wave 2 `LoopbackCommandAuthority`, which refuses forwarded headers and
non-loopback Host/Origin. A forwarded identity without a valid assertion is
therefore always refused.

Configuration: `TREX_OWNER_EMAILS` (comma-separated, lower-cased),
`TREX_FAMILY_ORIGINS` (comma-separated exact `https://host[:port]` origins,
no default port, no path), `TREX_FAMILY_AUDIENCE`, the key file above.

### Replay protection and retries

- Every assertion is single use: before returning the actor, lane B creates
  `<run dir>/authority/assertions/<jti>.json` (`trex-family-assertion-seen/1`)
  with `O_CREAT|O_EXCL`. An existing file refuses `assertion_replayed`; any
  other failure refuses `assertion_ledger_unavailable` (fail closed). Entries
  may be pruned once `exp + 5 s` has passed (the assertion has expired by then).
- `request_id` binds the assertion to one command. A browser retry of the same
  body is a NEW portal assertion (new `jti`) with the same `request_id`; it
  passes the authority and the Wave 2 command store answers `duplicate` with
  the first outcome. A different body under the same key is the store's
  `command_body_mismatch`. The command itself expires within 300 s.

## C3.2 read views

### The null rule (every Wave 3 view)

A null is either UNKNOWN or NOT APPLICABLE, never ambiguous:

- unknown: the enclosing block's `status` is `unavailable` and its
  `unavailable_reason` says why (then every data field of the block is
  null), or a sibling `<field>_unavailable_reason` names it;
- not applicable: fixed by a sibling enum and validated (a kill file's
  `since` is set exactly while it stands);
- a known block has no null data field. Zero, `""` and `[]` are values,
  never stand-ins for unknown. Every key is always on the wire.

Reasons are `Blocker` (Wave 2, including its view reasons such as
`holdings_missing`) or `Wave3Reason` members; anything else refuses.

### `GET /api/today`: `trex-today/1` (`TodayView`)

| Block | Shape | Notes |
| --- | --- | --- |
| `now` | instant | the server's clock for every countdown |
| `session` | `pre_open`/`open`/`after_close`/`no_session`/unavailable; `et_date`, `session`, `opens_at`, `closes_at`, `early_close`, `calendar_sessions_left` | `session` is today's (pre-open, open) or the next one; from the repo calendar |
| `desk_owner` | `healthy`/`stale`/`stopped`/unavailable; `heartbeat_at`, `heartbeat_age_s`, `lock_held`, `exit_owner_ready`; `stale_after_s` | healthy = lock held and heartbeat age at most `stale_after_s` (the exit watch's `HEARTBEAT_STALE_S`, 120); ready iff healthy |
| `gateway` | the gateway watch statuses (`ok`, `starting`, `checking`, `needs_login`, `needs_2fa`, `api_down`, `down`) or unavailable; `detail`, `checked_at`, `age_s` | `gateway.json`; a missing or stale watch is unavailable, never healthy |
| `controls` | `pause_entries`, `halt`, `flatten`: `{name, status: clear/standing/unavailable, unavailable_reason, since, reason}`; `queued`: accepted kill-switch commands not yet applied | the page's primary control is `pause_entries`; HALT is labelled `Emergency stop (also stops exits)` |
| `approvals` | `items` (pending proposals) and `problem_count` | each item repeats `approve_args` (Wave 2 `ApproveEntryArgs`) verbatim, `seconds_to_deadline` at `now`, `precheck_blockers` (advisory; the desk is authoritative); detail: `GET /api/proposals` |
| `positions` | `holdings` (`option_positions`, `equity_positions`: distinct attributed intents) and `risk` (`fresh`/`stale`/unavailable; open loss, day realized loss, settled cash, outside position count) | detail: `GET /api/portfolio` |
| `acceptance` | the C3.4 summary (`AcceptanceSummary.of(document)`) plus the fixed requirements; `live_authorized: false` | |
| `next_events` | time-ordered `{at, kind, label, subject}` | `subject` set exactly for `forced_exit`, `exit_deadline` (intent id) and `approval_deadline` (proposal id) |
| `blockers` | `{code, scope, severity, message, next_step}` | human words beside a machine code |
| `execution_enabled` | `false` | the web never executes |

Countdown rule: a client counts an approval down from the moment it received
the response (`secondsLeft`), never by comparing its own clock with
`send_deadline`.

### `GET /api/activity`: `trex-activity/1` (`ActivityPage`)

Query: `limit` (1..200, default 50), `cursor` (opaque), `source` (repeatable:
`command`, `desk_event`, `audit`, `drill`; none = all). Items are newest
first with unique ids `<source>:<local id>`. Each item has an `actor`
(`{sub, ingress}`: the stamped `issued_by` for commands, `{sub: "trex-desk",
ingress: "system"}` for desk events) or an `actor_unavailable_reason`
(`actor_not_recorded` for old audit lines). `command_key` and
`command_status` are set exactly for commands; `reason` exactly for refused,
failed and duplicate ones. `refs` maps `proposal_id`, `intent_id`,
`program_id`, `drill_id`, `path` to ids. `next_cursor` is set exactly when
`has_more`. Every source reports its status, so an unreadable source shows
as `unavailable`, never as an empty feed.

### `GET /api/acceptance`: `trex-acceptance-view/1`

`{now, status, unavailable_reason, document, live_authorized: false}`;
`document` (C3.4) is present exactly when `ok`, and lane H serves it only
after `verify_acceptance` against the repo calendar. Unavailable reasons:
`acceptance_missing`, `acceptance_invalid`, `acceptance_calendar_mismatch`,
`acceptance_window_mismatch`.

## C3.3 routes (`web/src/lib/routes.ts`)

Hash routes, so the app keeps working at any mount (loopback `/`, the
portal's `/trex/`). First match wins; `resolveHash` never throws.

| Hash | Group | Destination | Renders |
| --- | --- | --- | --- |
| `#/today` (also `""`, `#`, `#/`) | destination | Today | new |
| `#/experiments` | destination | Experiments | existing ExperimentsPage |
| `#/trade-review` | destination | Trade review | new |
| `#/portfolio` | destination | Portfolio | new |
| `#/activity` | destination | Activity & settings | new |
| `#/trade-review/:proposalId` (64 hex) | detail | Trade review | new |
| `#/activity/acceptance`, `#/activity/settings` | detail | Activity & settings | new |
| `#/pilot`, `#/pilot/:programId` | detail | Experiments | new |
| `#/onboarding` | flow | Today | new |
| `#/experiments/:id` | detail | Experiments | existing |
| `#/plans` (was `#/`), `#/plan/:id`, `#/stats` | advanced | Portfolio | existing Plans, plan detail, Performance |
| `#/market`, `#/market/:sym` | advanced | Trade review | existing Market, Symbol |
| `#/discover`, `#/research`, `#/action-model`, `#/quant`, `#/workspace`, `#/trade-floor` | advanced | Experiments | existing views |

Unknown hashes fall back to Today with `fallback: true` (they used to fall
back to Plans). The only pre-Wave-3 hash whose target changes is `#/`, by the
owner's plan; Plans keeps its view at `#/plans`. Phone tab labels are the
`shortLabel`s (`Today`, `Experiments`, `Review`, `Portfolio`, `Activity`).

## C3.4 `trex-acceptance/1` (`contracts.acceptance`)

Phase 5 paper acceptance, computed and never asserted.

Inputs (lane H writes one file each under `TREX_ACCEPTANCE_DIR`):

- `SessionRecord` per session: `heartbeat_minutes` (regular-hours minutes in
  which the desk owner's heartbeat was fresh, i.e. at most
  `HEARTBEAT_STALE_S` old when sampled), `reconciliation_open` (the first
  reconciliation in [open, open + 15 min]), `reconciliation_close` (the last
  in [close - 15 min, close]), `reconciliation_episodes` (not-clean
  stretches with `resolved_at`), `unowned_orders` (broker orders or
  executions on the account that no TREX intent or desk order owns),
  `max_exit_unguarded_s` (the longest time an open desk position had no armed
  exit owner; a standing HALT counts as unguarded), `unavailable` (exactly the
  null metrics, each with its reason), `evidence`.
- `LifecycleRecord` per approved entry: approval, fill quantity and time,
  `exit_by` (`policy`, `flatten`, `manual`, `not_exited`), exit time, broker
  flat time, reconciled time, `commissions_complete`.
- `DrillRecord` per drill id: `relogin`, `restart_mid_position`,
  `gateway_outage`, `duplicate_command`, `backup_restore`, `notification`,
  `unassisted_use`; `status` (`not_run`, `passed`, `failed`), `evidence`
  (`{path, sha256}` relative to the evidence root; the producer verifies the
  hash), `attested_by` (an owner email), `at`.

Rules:

| Rule | Definition |
| --- | --- |
| qualifying session | `100 * heartbeat_minutes >= 99 * regular_minutes` (390 or 210 from the calendar: 387 of 390, 208 of 210); reconciliation clean at the close, every episode resolved by the close, and a not-clean open covered by an episode that started by open + 15 min; `unowned_orders == 0`; `max_exit_unguarded_s <= 300`. Unknown never qualifies. |
| streak | consecutive calendar sessions, ending at the last session closed by `computed_at`; a non-qualifying or unrecorded session (including a trailing one) resets it |
| completed lifecycle | filled >= 1, exited by `policy` or `flatten`, then flat at the broker, then reconciled clean, commissions present for every execution |
| counted lifecycle | completed and inside the current streak (approved on or after its first session, reconciled on or before its last; ET dates) |
| drill passed | `passed`, with evidence, attested by an owner |
| verdict | `met` iff streak >= 10, counted lifecycles >= 10 per asset kind (option, equity), all 7 drills passed |

The document carries its inputs, the calendar slice they were judged against
(`calendar_window`, `calendar_sha256`), the owners, and the derived results
(per-session, per-lifecycle and per-drill reasons, `streak`, `per_kind`,
`drills_passed`, `verdict`, `verdict_reasons`). Validation recomputes every
derived field and refuses any difference, so a hand-set verdict cannot parse.
`verify_acceptance(document, calendar)` refuses another calendar and a
truncated window (a hidden trailing session). `live_authorized` is the
literal `false`: nothing here unlocks live trading; live needs separately
configured limits, account qualification and an explicit activation, under
new schema versions.

## C3.5 `trex-pilot-registration/1` and `trex-pilot-usage/1`

Three programs, one per role: `observation` (the existing QSL observation,
cited through `existing_registration`, never re-registered), `baseline` (an
equity control) and `challenger` (one preregistered equity challenger, which
must name its baseline in `controls`).

Fields: `program_id`, `role`, `title`, `hypothesis`, `universe`
(`asset_kind`, sorted unique symbols), `data_sources` (`name`, `provider`,
`dataset`, `provenance`, `license`, `realtime`), `decision_rule`
(`statement`, `metric`, `minimum_sessions`, `minimum_lifecycles`),
`controls`, `budgets` (`requests_per_day`, `compute_minutes_per_day`,
`storage_mb` in MiB, each within `PILOT_BUDGET_CEILINGS`),
`existing_registration`, `created_at`, `status`, `sealed_by`, `sealed_at`,
`sha256`.

- A `draft` cannot run (`registration_run_blockers` returns
  `registration_not_sealed`). Sealing is an owner act: `sealed_by` must be in
  `TREX_OWNER_EMAILS`; `sha256` = sha256 of the canonical registration
  WITHOUT `status`, `sealed_by`, `sealed_at`, `sha256`. A sealed file is
  written once (`O_EXCL`) and never edited; a change is a new `program_id`.
- The owner seals from the UI through a `seal_registration` command kind
  (args `SealRegistrationArgs{program_id, registration_sha256}`, the hash the
  owner reviewed), added additively to `trex-command/1` by lane I as a
  coordinator-approved amendment; or from a loopback CLI where the owner names
  their email explicitly.
- Budgets are enforced, not advisory: `BudgetUsage` per program per ET day
  (`requests`, `compute_seconds`, `storage_bytes`); unknown usage blocks
  (`budget_usage_unknown`, never read as zero); a budget is exhausted when
  `used >= budget` (`budget_requests_exhausted`,
  `budget_compute_exhausted`, `budget_storage_exhausted`).
- `pilot_roster_problems` checks one program per role and that every control
  names a program in the pilot.

## C3.6 vocabulary

- `Wave3Reason`: unavailable reasons and Today blocker codes Wave 2's
  `Blocker` does not name; additive only, disjoint from `Blocker`.
- `FamilyRefusal`: the family verifier's refusal details, in check order.
- Acceptance reason sets: `SESSION_REASONS`, `LIFECYCLE_REASONS`,
  `DRILL_REASONS`, `VERDICT_REASONS` (`drill_not_passed:<id>`).
- `tests/contracts/fixtures/wave3/vocabulary.json` pins every list for both
  languages.

## C3.7 layout

| Path | Root | Content |
| --- | --- | --- |
| `authority/assertions/<jti>.json` | desk run dir | `trex-family-assertion-seen/1`, `O_EXCL` |
| `sessions/<YYYY-MM-DD>.json`, `lifecycles/<intent_id>.json`, `drills/<drill_id>.json` | `TREX_ACCEPTANCE_DIR` (default `~/.local/state/trex/acceptance`) | acceptance inputs |
| `evidence/` | same | the root every acceptance `EvidenceRef.path` is relative to |
| `acceptance.json` | same | the latest computed document |
| `registrations/<program_id>.draft.json`, `.sealed.json` | `TREX_PILOT_DIR` (default `~/.local/state/trex/pilot`) | registrations |
| `usage/<program_id>/<YYYY-MM-DD>.json` | same | `trex-pilot-usage/1` |

## Defaults and open questions

Each default is in code and holds until the owner rules.

| # | Question | Default chosen |
| --- | --- | --- |
| 1 | Replay key: `request_id` or a nonce? | A `jti` nonce is single use; `request_id` (the idempotency key) binds the body. Making `request_id` single use would turn a browser retry into a 403 instead of the store's `duplicate`. |
| 2 | Shared `actorSigningKey` or a dedicated key? | Dedicated key file per instance (prod and staging), pairing-key format. |
| 3 | Tailnet required for command POSTs (as Speech Lab control)? | No: operator Family session, exact Origin and the request-bound assertion; the phone may be off Tailscale. Open: require it. |
| 4 | Signed actors on GET routes? | None in Wave 3 (the portal's operator gate covers reads). Open: a v1 `trex` read actor. |
| 5 | Legacy and research POSTs through the portal | 405; loopback only. Open: allowlist specific research POSTs. |
| 6 | Staging | Audience `trex-staging-command`, its own key file. |
| 7 | Clock skew | `iat` may lead TREX's clock by 5 s (same host). |
| 8 | Which lifecycles count? | Only those inside the current streak. Alternative: any completed lifecycle in a qualifying session. |
| 9 | Drills against the streak | No exemption: a drill that breaks a session rule resets the streak; schedule drills accordingly (a restart must keep heartbeat loss under 1% of the session, about 3 minutes). Open. |
| 10 | Who attests drills? | An owner email in `TREX_OWNER_EMAILS`. |
| 11 | Reconciliation check windows | 15 minutes after the open and before the close. |
| 12 | Does a standing HALT count as an unguarded exit? | Yes (HALT stops exits). |
| 13 | Pilot budget ceilings | 5,000 requests, 120 compute minutes, 2,048 MiB per program per day; each registration picks its own lower values. |
| 14 | How the owner seals | `seal_registration` command kind (lane I amendment) or loopback CLI with the owner's email. |
| 15 | Labels of `flatten` and `resume_all` | `Close all desk positions`; `Clear emergency stop, close-all and pause`. HALT's wording is the plan's. |
| 16 | Destination of each old view | Plans, plan detail, Performance: Portfolio. Market, Symbol: Trade review. Discover, Research, Action model, Quant, Research and paper, Trade floor: Experiments. |
| 17 | Pilot route | `#/pilot`, not `#/experiments/pilot`, which an experiment id could shadow. |
| 18 | Activity page size | 50 by default, at most 200. |
| 19 | Live trading | Out of scope; `live_authorized` is literally `false` in every acceptance shape. |

### Deliberate deviations from the brief

- `jti` added to the claims, and it (not `request_id`) is the single-use key
  (default 1).
- `v` is `2`, family-apps' request-bound actor protocol; `trex-family-command/1`
  names the TREX claim set.
- The returned `Actor.request_id` is the backend's request id (Wave 2's
  binding rule); the claim's `request_id` is kept in the replay ledger.
- The verifier also checks `Host` against the origin and the audience per
  instance.
- The acceptance document embeds its calendar slice so every parse recomputes
  the verdict; `verify_acceptance` ties the slice to the repo calendar.
- The pilot registration adds `role`, `title`, `created_at` and
  `existing_registration`.
- The contracts package `__init__` is not touched (only new files); import the
  submodules.
- TypeScript mirrors of the Wave 2 payloads (`ProposalList`, `PortfolioView`,
  `CommandBody`, `CommandResult`, `Holding`) are added because no TS type for
  them existed; a Python test pins them to the Wave 2 functions' real output.

## Verification (2026-10-21)

- `tests/contracts/test_wave3_contracts.py` with
  `tests/contracts/test_wave2_contracts.py` (host-work test profile): 503
  passed, 0 failed (255 Wave 3, 248 Wave 2 unchanged). Covered: round-trips
  of every fixture; the golden assertion rebuilt with stdlib; every refusal
  detail in order; tampered claims (lifetime over 60 s, v1, float or string
  version, foreign audience or ingress, uncanonical origin); method, path,
  origin, host, body and request-id mismatches; the null rule per block;
  acceptance arithmetic hand-counted on November 2026 (streak reset by a
  failing, an unrecorded and a trailing session; 99% at 390 and 210 minutes;
  reconciliation, unowned-order and exit-guard rules; per-kind counters;
  drills); tampered verdicts refused; pilot sealing, hash oracle and budget
  blockers; the Wave 2 mirror fixtures regenerated from the Wave 2 code; no
  `timedelta`, no en or em dash, no broker or web import.
- Spot mutants on the Python rules (fresh copy, `-B`, clean baseline): 10 of
  10 killed (99% threshold, 300 s guard, streak reset, counted window, unknown
  exit guard, host check, budget `>=`, expiry `>=`, 60 s lifetime, owner check).
- `web/src/lib/contracts.test.ts` with the existing `router.test.ts`
  (vitest): 22 passed, 0 failed; the family golden rebuilt with WebCrypto.
  `tsc --noEmit` clean; a removed key in `KEYS` fails `tsc` (checked).
- `ruff format --check`, `ruff check` and `mypy` clean on the new Python
  files.

## Amendment log

Every entry is additive (a new optional field, vocabulary member, union
member or helper); nothing frozen above is renamed or removed. The frozen
sections are unchanged; read them with these entries.

### 2026-10-21: `seal_registration` in `trex-command/1` (lane I, coordinator-approved)

Default 14 lands: `CommandKind` gains `seal_registration`
(`SealRegistrationCommand`, args `SealRegistrationArgs{program_id,
registration_sha256}`). `SealRegistrationArgs` moves from `wave3.py` into
`contracts/commands.py` (commands cannot import wave3, which imports
commands); `wave3` re-exports the identical class. The desk applies it as a
file kind (`desk_commands.FILE_KINDS`) through
`research.pilot.seal.apply_seal_command` with the verified actor as sealer.
TypeScript: `CommandArgs.seal_registration`. Tests:
`tests/research/test_pilot_seal_command.py` (no longer skipped) and
`web/src/lib/pilotApi.amendment.test.ts`. The patch file is removed.

### 2026-10-21: C3.7 one acceptance-dir resolver (integration, lanes C, D, H, J)

`ACCEPTANCE_DIR_ENV = "TREX_ACCEPTANCE_DIR"`, `ACCEPTANCE_DIR_DEFAULT =
"~/.local/state/trex/acceptance"` and `acceptance_dir_from_env(env)`
(trimmed; blank means unset). Today, Activity, `GET /api/acceptance`, the
publisher and the alert sweep all resolve through it. Deployment: the
production web drop-in and `trex-deadman.service` set
`TREX_ACCEPTANCE_DIR=@DESK_STATE@/acceptance`, the publisher's own dir
(`tests/unit/test_trex_acceptance_units.py` pins the three agree; staging
sets none).

### 2026-10-21: `acceptance_owners_mismatch` (integration, review P2)

New `Wave3Reason` member (appended; fixture `vocabulary.json` and
`WAVE3_REASONS` in `contracts.ts` updated together) and the pure helper
`acceptance_owners_problem(document_owners, env)`. When `TREX_OWNER_EMAILS`
is configured, `GET /api/acceptance`, Today's acceptance summary and the
alert sweep refuse a document whose `owners` differ from that list (a
malformed list vouches for none); unconfigured means nothing to compare.
Before this, "attested by an owner" was judged only against the document's
own list. Today and the sweep also refuse a document dated more than 300 s
ahead, as the view already did.

### 2026-10-21: portal constants (lane A)

`PORTAL_STAGING_KEY_CONFIG_FIELD = "trexStagingCommandKeyPath"` (one portal
process serves `/trex` and `/trex-staging`) and `PORTAL_REFUSAL_REASONS`,
the `reason` of the portal's own JSON refusals `{"error": <plain words>,
"reason": <code>}` (Cache-Control: private, no-store): `trex-method-not-allowed`
(405, with `Allow`), `trex-wrong-origin` (403), `trex-command-too-large`
(413), `trex-commands-not-configured` (503), `trex-bad-request` (400),
`trex-owner-only` (403, defensive). A non-operator or signed-out caller
gets the portal's HTML 403 page first. Both clients already treat these as
refusals carrying `error`.

Recorded risk on open question 3 (review P2, lane A): the only binding of
a command to "the TREX page" is the exact Origin, and every family app and
Pop Deck share that origin, so script on any co-hosted page (an XSS, a
compromised backend) could have the portal sign an owner command. The
dedicated key separates TREX from other backends cryptographically only.
This is a blocker for any future `live_authorized` change: serve TREX from
its own origin, or add owner step-up (WebAuthn user verification) for
`approve_entry`, `compose_entry` and the resumes.

### 2026-10-21: TypeScript helpers (lanes E and F)

`secondsLeft` accepts `Pick<PendingApproval, 'seconds_to_deadline'>` (a
Today item or the facts built from a `GET /api/proposals` row). Relative
URLs: the SPA fetches `API_PATHS` values without their leading slash
(`commandsApi.relativeUrl`, `ControlBar`'s constants), so every call
resolves under `/` on loopback and `/trex/` through the portal; never a
root-absolute path.

### 2026-10-21: C3.2 blockers Today now emits (integration)

No schema change (existing vocabulary): Today names
`family_commands_disabled` whenever family ingress is disabled (its message
lists the `IngressProblem` codes, never a path or a key) and
`profile_absent`, `profile_invalid` or `profile_limits_missing` from
`<run dir>/profile.json` read by the desk's own `load_profile`. Onboarding
reads the absence of the first in a readable Today as "key installed" and
the profile codes as "To do". The `entries_paused` blocker states whether
exits run now (HALT, desk owner not healthy, unknown) instead of promising
it.

### No-change decisions (integration)

- Lane-local shapes stay lane-local: `trex-pilot-view/1` (`GET /api/pilot`,
  `research/pilot/view.py`, pinned by its fixture), `trex-acceptance-sample/1`
  and `trex-drill-check/1` (only their own lane parses them), the pilot
  reason codes (pattern-valid `ReasonCode`s) and the `usage/<program>/jobs/`
  markers.
- Deferred additive requests, with reasons, are listed in
  `docs/product/WAVE34-INTEGRATION.md` (per-source `NextEvents` status,
  `owner_epoch_unknown`, `acceptance_unreadable`, `acceptance_stale`, an
  Activity feed horizon field, an `attested` actor ingress, a Today profile
  block, TS mirrors of the effect bodies, Blocker members for
  `gateway_unavailable`, `command_queue_too_long`, `queued_command_invalid`).

### 2026-10-21: `GET /api/ideas`, `trex-idea-list/1` (Wave 5, new read schema)

Additive: a new read view, nothing above changes. Code:
`src/tree_options/trex/contracts/ideas.py` (models),
`src/tree_options/trex_web/entry_ideas_view.py` (`attach`, wired in
`trex_web/app.py`; the name `ideas_view.py` was taken by the market Ideas tab,
`GET /api/market/{sym}/ideas`). What the owner may compose; read only,
broker-free, `Cache-Control: no-store`; the null rule of C3.2.

| Block | Shape | Notes |
| --- | --- | --- |
| `now` | instant | countdowns start at the response, as Today's |
| `drafts` | `ok`/`unavailable`; `items`, `blocked`, `problems`, `truncated` | every `trex-entry-proposal-draft/1` under `<run dir>/proposal-drafts/` (the miner's option drafts, `desk.enter_supervised`; any equity draft, `desk_entries.equity_draft`), newest 200 by file time; a missing directory is known and empty; one that cannot be listed (or a link) is `drafts_dir_unreadable`; an unlistable proposals directory is `proposals_dir_unreadable` |
| `drafts.items[]` (`Idea`) | `candidate_id` (the file stem), `asset_kind`, `underlying`, `intent_id`, `program_id`, `strategy_version`, `summary`, `option` / `equity` (exactly one; strikes and limits in cents), `max_loss_cents`, `notional_cents` (equity: the contract arithmetic), `written_at` (file time), `age_s`, `entry_session`, `valid_until`, `seconds_to_expiry`, `status`, `proposal_status`, `proposal_id`, `hints`, `compose_args` | `status`: `used` (a consumed proposal, consumed mandate/2 or outbox entry for the intent) > `proposed` (a pending or approved proposal) > `expired` > `available`. `proposal_status` is the `GET /api/proposals` status of the consumed proposal, else the newest live one, else the newest (`none`: no proposal; `proposal_id` set exactly otherwise). `compose_args` is what `compose_entry` repeats verbatim |
| `drafts.blocked[]` | `candidate_id`, `kind`, `reason`, `written_at` | the miner's `<candidate>.BLOCKED.json`; never composable |
| `drafts.problems[]` | `file`, `reason` | `draft_unreadable` (link, not a file, over 256 KiB, I/O) or `candidate_invalid` (not JSON, not a draft, an invalid effect); never dropped |
| `manual_equity` | `candidate_prefix` (`manual:`), `program_id` (suggested `owner-manual`), `strategy_version` (`owner-manual/1`), `symbols` (the allowlist), `limit_policies` (`ask`, `mid`, `limit_cents`), `max_entries_per_session`, `window_status` (`open`/`closed`/`no_session`), `window_session`, `window_opens_at`, `window_closes_at` (set exactly on a session), `hints` | the terms of a manual ticket (WAVE2-CONTRACTS.md amendment log) |
| `execution_enabled` | `false` | |

Hints are advisory (the desk screens again with the live quote): the stops,
the owner's facts (`owner_fact_blockers`), the capital profile (as the desk
reads it), `outside_regular_session`, the session cap (the outbox count),
`candidate_expired`, `intent_already_used`, `trade_loss_cap_exceeded`, and for
equity `entry_window_closed`, `equity_symbol_not_qualified`,
`equity_notional_exceeds_intended_capital`. Every hint and reason is a
`Blocker` or `Wave3Reason` member.

TypeScript: `IdeaList`, `DraftIdeas`, `Idea`, `IdeaOption`, `IdeaLeg`,
`IdeaEquity`, `BlockedIdea`, `IdeaProblem`, `ManualEquityTerms`,
`IDEAS_PATH` (kept out of `API_PATHS`, which `vocabulary.json` pins for
Wave 3) and their `KEYS` in `web/src/lib/contracts.ts`. Fixtures:
`tests/contracts/fixtures/wave5/idea_list.json` (the document the view
serves for the test world of `tests/unit/test_trex_web_entry_ideas.py`,
pinned) and `idea_list_unavailable.json`. Tests:
`tests/contracts/test_wave5_contracts.py`,
`tests/unit/test_trex_web_entry_ideas.py`,
`tests/desk_safety/test_enter_supervised.py` (the writer's drafts are exactly
what the view lists) and `web/src/lib/contracts.wave5.test.ts`.

### 2026-10-21: `trex-idea-list/1` manual terms state the hold cap (Wave 5 review)

Additive: two new optional fields on `manual_equity` (`ManualEquityTerms`),
nothing renamed or removed. `max_hold_sessions` is the hold cap
(`MANUAL_MAX_HOLD_SESSIONS` = 20, `contracts.commands`, re-exported by
`contracts.ideas`): a manual ticket's exit deadline is at most that many NYSE
sessions after its entry session, and the desk refuses a later one
(`manual_ticket_invalid`, detail `deadline_beyond_max_hold`; WAVE2-CONTRACTS.md
amendment log). `max_exit_deadline_session` is the latest deadline the desk
accepts for a ticket entered today (`trex.clock.last_session_within`, the
desk's own count; the calendar's last session if it ends sooner), null
exactly when today is not a session; when set it is on or after
`window_session` and comes with `max_hold_sessions`. The view always sets
both; a document served before this entry has neither (both parse as
None). The web caps the ticket's date picker (`max`) at it and refuses a
later date before sending. TypeScript: `MANUAL_MAX_HOLD_SESSIONS`,
`MANUAL_PROGRAM_ID`, the two fields on `ManualEquityTerms` and its `KEYS`.
Fixtures: both `wave5/idea_list*.json` carry the fields (hand counted: the
20th session after Thursday 2026-10-22 is Thursday 2026-11-19). Tests:
`tests/contracts/test_wave5_contracts.py` (an older document still parses;
the rules above), `tests/unit/test_trex_web_entry_ideas.py`,
`web/src/lib/contracts.wave5.test.ts`, `web/src/lib/ideasApi.test.ts` (the
shared fixtures through the page's parser) and
`web/src/components/TradeIdeas.test.tsx`.
