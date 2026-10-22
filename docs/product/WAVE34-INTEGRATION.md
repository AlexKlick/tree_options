# TREX Waves 3 and 4: integration record

Branch `w3/integration`, based on `wave3/integ` at `50f5ff6` (Waves 1 and 2,
the frozen Wave 3/4 contracts, the nine merged TREX lanes B to J and the cash
tag hotfix). Lane A (the family-apps portal signer) lives in the family-apps
repo (branch `feat/trex-signed-commands-20261021`) and is not changed here.

Paper only. The owner approves every entry; exits run automatically; the
pause never stops exits; nothing here authorizes live trading
(`live_authorized` stays the literal `false`). Nothing was deployed,
restarted or installed; no broker, live service (:8090, :8091), serving
checkout, family-apps checkout, systemd unit or live state was touched.

Contract amendments are logged in `WAVE3-CONTRACTS.md` (Amendment log) and,
for the one C2.7 member, in `WAVE2-CONTRACTS.md`.

## 1. Wiring dispositions

| Lane | Item | Disposition |
| --- | --- | --- |
| A | No tree_options code; deployment steps and post-deploy checks for the portal | Not ours (family-apps). Recorded for the owner (section 6). |
| A | Origins, audience, key files for TREX | Done on the TREX side by name: the production web drop-in reads an optional `~/.config/trex/web.env` (`TREX_OWNER_EMAILS`, `TREX_FAMILY_ORIGINS`, `TREX_FAMILY_AUDIENCE`, `TREX_FAMILY_COMMAND_KEY_FILE`, `TREX_PILOT_DIR`); creating it is an owner step. |
| B | `create_app` defaults to `dispatching_authority(run_dir=resolve_run_dir, clock=command_clock)` | Done (`trex_web/app.py`); an `x-family-actor` request is judged by the family verifier only, never a loopback fallback. Test: `test_create_app_defaults_to_the_dispatching_authority`. |
| B | `__main__` runs uvicorn with `proxy_headers=False` | Done; test `test_the_server_never_trusts_forwarded_headers`. |
| B | `family_ingress_status()` for Today's `family_commands_disabled` | Done: `today_view.attach(..., family_ingress=...)`, wired in `app.py` to `family_ingress_status().problems`. |
| B | Replay-ledger prune | Deferred: optional hygiene (one small file per family command); `FamilyCommandAuthority.prune()` exists. Pruning at `create_app` would touch the run dir of every test app; schedule it on the desk owner's tick or a timer later. |
| C | `attach_today(app, run_dir, supervised_dir, clock, gateway_state=lambda: gateway_state_path)` | Done (plus `family_ingress`). |
| C | Timer reader | Not wired, by design (no `systemctl` from the web). |
| C | README route list | Done (`trex_web/README.md`). |
| C/H | One acceptance root for every reader | Done: `wave3.acceptance_dir_from_env`, used by Today, Activity, `GET /api/acceptance`, the publisher and the sweep. |
| D | `attach_activity(app, run_dir=resolve_run_dir, clock=clock)` | Done. |
| E | `WAVE3_PAGES`: every stub replaced by the F/G/I component | Done (`web/src/App.tsx`): Today, Trade review index and detail, Portfolio, Activity (feed, acceptance, settings), onboarding, pilot index and program. |
| E | `PlanDetail` back links to `#/plans` | Done (+ `PlanDetail.test.tsx`). |
| E | `index.html` title | Done ("TREX paper desk"). |
| F | Lane F senders share lane E's outbox | Deferred (see risks): both senders keep one key per request; unifying them is a UX/code consolidation, not a correctness fix. |
| F | Phone tab bar vs the bottom sheet / sticky decision row | Done (review P2, below). |
| G | Pages wired; onboarding depends on Today's `family_commands_disabled` and profile blockers | Done: Today emits both (no schema change). |
| H | `attach_acceptance(app, clock=clock)` | Done. |
| H | `release.COMMANDS` `tree_options.trex.acceptance publish` | Already merged with lane H; kept. |
| H | `trex-web-release.conf.in`: `TREX_ACCEPTANCE_DIR=@DESK_STATE@/acceptance` | Done; staging sets none. |
| I | `attach_pilot(app, clock=clock)` | Done. |
| I | `seal_registration` amendment (`pilot-seal-command.patch`) | Done (applied, patch file removed; `test_pilot_seal_command.py` now runs). |
| I | Router: `pilot` and `pilot-program` render `PilotPage` | Done. |
| I | Desk-owner unit env and pilot-dir write access; producer `pilot_program_id`; seeding the store | Owner/unit steps (section 6); no producer sets `pilot_program_id` yet. |
| I | Experiments index ids `equity-control`/`equity-challenger` | Deferred: an Experiments-index content change, not wiring. |
| J | `release.COMMANDS` `tree_options.trex.deadman` | Done; RELEASE.md lists both new units and commands. |
| J | `trex-deadman.service.in` reads the publisher's acceptance dir | Done (`TREX_ACCEPTANCE_DIR=@DESK_STATE@/acceptance`, optional `acceptance.env` for `TREX_OWNER_EMAILS`); cross-unit test. |
| J | Drop `reconciliation_required` from the desk's raw pushes | Deferred to the owner: until the dead-man unit is installed the raw push is the only one; after, both arrive (decide then). |
| - | `desk_runtime` wires `ExitQuoteWatch` and pushes `exit_quote_blind` | Done: the runtime owns the watch, records the event once per structure per session, pushes it urgent; `DeskEvent.EXIT_QUOTE_BLIND` added (C2.7); `drills.ALERTING_DESK_EVENTS` follows. The sweep and the desk then both push it (as for reconciliation). |
| - | Contracts package re-exports | Done: `contracts/__init__.py` re-exports the `seal_registration` command classes and the `wave3` and `acceptance` submodules. |

## 2. Contract issues

| Lane | Issue | Decision |
| --- | --- | --- |
| A | Staging key config field | Added `PORTAL_STAGING_KEY_CONFIG_FIELD`. |
| A | Portal refusal body and codes | Added `PORTAL_REFUSAL_REASONS` (with `trex-command-too-large`). |
| A | Shared-origin signing | Recorded on open question 3 as a blocker for any live change (amendment log). |
| B | Ingress cause on the 403 wire | No change: the cause is now visible on Today (`family_commands_disabled` lists the problem codes). |
| B | `peer_host` honesty | Fixed by `proxy_headers=False`. |
| C | Per-source `NextEvents` status | Deferred: needs a new optional block; one bad source hides the list (shown unavailable, never wrong). |
| C | `owner_epoch_unknown`, `acceptance_unreadable` | Deferred: existing reasons are honest if coarse (`acceptance_invalid`; the precheck is skipped). |
| C | `holdings_account_mismatch` mapping | No change (unreachable; documented by lane C). |
| C/H | Heartbeat source alignment | No change needed: both read `book.json` `heartbeat`. |
| D | `attested` actor ingress, feed horizon field, `activity_source_missing` | Deferred: additive fields with UI work; the module docstring documents the horizon and drills name their attester in words. |
| E/F | Relative API URL helper | No code change: documented (amendment log); `commandsApi.relativeUrl` and `ControlBar` already strip the slash; the e2e check found no root-absolute API call. |
| E | `CONTROL_LABELS` wording | No change (frozen labels kept). |
| F | `secondsLeft` takes `Pick<..., 'seconds_to_deadline'>` | Added; the ApprovalSheet cast is gone. |
| F | TS mirrors of the effect bodies | Deferred: lane F narrows at run time and shows raw JSON otherwise; mirrors need fixtures for both effect schemas. |
| F | `gateway_unavailable`, `command_queue_too_long`, `queued_command_invalid` as `Blocker` members | Deferred: a C2.7 amendment with AST-pinned producer tests; lane F words the plain strings, pinned to their Python sources. |
| G | Positive profile / family / approvals-applied facts | Partly: Today now emits the profile and family blockers G reads; a positive profile block and `approvals_applied_total` are deferred (new optional blocks). |
| H | `acceptance_stale` | Deferred: needs an owner-chosen freshness bound. |
| H | Future-dated documents, evidence formats lane-local | Kept (Today and the sweep now apply the same 300 s rule); formats stay lane-local. |
| I | `trex-pilot-view/1` adoption | No change: lane-local, pinned by its fixture. |
| I | `SealRegistrationArgs` move | Approved and applied. |
| I | Lane-local reason codes, job markers, metering units, evidence root, seal rule | No change: documented in PILOT.md, pattern-valid. |
| J | Shared acceptance-dir resolver | Added (`acceptance_dir_from_env`). |

## 3. Review P2 findings and cross-lane risks

Fixed (each with a test that fails without the fix: the Python ones are
registered mutants, the web ones were checked by scratch web mutants):

| Finding | Fix | Evidence |
| --- | --- | --- |
| F: Today's Pause/Resume mints a new key after an unconfirmed send | Pressing again re-sends the same prepared request (key and bytes) while it is unexpired | `TodayPage.test.tsx` "re-sends the SAME request"; web mutant 4 killed |
| F: sticky decision row under the phone tab bar | `.shell-phone` row at `bottom: 57px + safe area`, `z-index: 21` | e2e hit test at 390x844: the Approve button is hit, not the tab bar |
| E: control bar pushes Today's approvals below the first screen | Phone (compact) bar: the emergency stop and close-all sit behind "More controls" (a standing stop's clear control stays in view) | e2e: approvals card top 726 px, tab bar 787 px; `ControlBar.test.tsx` compact tests; web mutant 3 killed |
| F/E/G/C: exit-state text promised exits beside HALT or a stopped owner | Conditional text everywhere it states the present (Today tiles, `entries_paused` blocker, control bar "blocked by"), policy wording elsewhere ("pausing never stops exits") | `test_the_pause_blocker_states_whether_exits_run_now`, `TodayPage`/`ControlBar` tests; W34-M5, web mutants 2 and 5 |
| E: the outbox resent an expired kept body | A kept body is reused only with at least 5 s of life left | `ControlBar.test.tsx` expiry tests; web mutant 1 |
| H: GET /api/acceptance did not check owners | `acceptance_owners_mismatch` in the view, Today and the sweep | W34-M6, W34-M8 |
| H: the publisher counted hand-written passed drills | A stored passed drill needs an ok check of the same drill (`drill_evidence_invalid:<drill>`) | W34-M11 |
| J: a sweep that lost its alert book still pinged | No ping (exit 1) when the book was not written | W34-M9; `test_main_with_an_unwritable_alert_book_sends_no_ping` |
| J: dead-man read another acceptance dir | Unit env set; cross-unit test | `test_every_production_reader_reads_the_directory_the_publisher_writes` |
| D: a terminal label shaped `family:<sub>` shown as a Family actor | An audit label is an actor only when a stored, body-hash-bound command by that actor proves it | W34-M10 |
| Risk: no error boundary | `PageErrorBoundary` keeps the shell and control bar when a page throws | `App.boundary.test.tsx`; web mutant 6 |
| Risk: lane E's App tests asserted stubs; G's Portfolio test collided with the control bar | Tests updated to the wired pages | `App.test.tsx`, `PortfolioPage.test.tsx` |
| Risk: release.COMMANDS edited by two lanes | One integration commit | `test_trex_product_release.py` in the broad run |
| e2e: legacy `#/research` tables overflowed a phone by 132 px | Both tables in `TableScroll` | e2e at 390x844 |

Deferred, with reasons:

| Finding / risk | Why deferred |
| --- | --- |
| A: shared origin can drive owner commands | Portal/topology change (own origin or WebAuthn step-up), outside this repo; recorded as a blocker for any live change. |
| H: stored session and lifecycle records still trusted at compute time | Re-deriving sessions from samples and re-hashing lifecycle facts is a publisher redesign; drills (the cheap, highest-value part) are fixed. `--rebuild` re-derives sessions. |
| I: pilot admission cannot write the pilot dir under trex-web's sandbox | Owner decision: grant `ReadWritePaths` for `<pilot dir>/usage` (against the release policy) or move metering to a process that owns it. Dormant: no producer sets `pilot_program_id`. Fails closed. |
| J: per-process push suppression when the book is unwritable; reconciliation persist anchored to the document time | The dead-man now alarms externally in that state; the in-process dedupe is a second line. |
| Two command senders (E's outbox, F's `useCommand`) and two Pause controls on Today | Both are now correct (one key per request, conditional text); merging them and keeping one Pause per page is a UX consolidation for the owner. |
| B: authentication runs synchronously inside the async route | Milliseconds per family command at the owner's rate. |
| C: `/proc/locks` probe edge cases (PID namespaces, btrfs) | Read "stopped": the alarming side. |
| D: one corrupt log line marks its source unavailable | Strict by design ("a partial list would undercount"). |
| Deadman unit memory caps unmeasured; acceptance sampling cadence | Measure on the first supervised day. |

## 4. Mutants

`scripts/mutate.py`, one new `MUTANTS.extend` block (30 entries): 16 lane
proposals (B 2, C 2, D 3, H 2, I 3, J 4; re-prefixed by lane) and 14
integration mutants (W34-M1..M14). Harness run (`--only <30 ids> --jobs 2`,
host-work test profile): 30 KILLED, each by its owner test; restoration
full-suite pass. Evidence: `~/.local/state/trex-wave3/evidence/w34-mut.{json,md,log}`.
No lane proposal was dropped. Lane A's twelve mutants are JavaScript in
family-apps; lanes E, F and G proposed web mutants; the integration's eight
web mutants (scratch copy, owner test must fail): 8 KILLED
(`~/.local/state/trex-wave3/evidence/w34-webmut-*`).

## 5. Verification

Logs: `~/.local/state/trex-wave3/evidence/` (`w34-broad.log`,
`w34-web.log`, `w34-mypy.log`, `w34-ruff.log`, the mutant reports).

- Python broad run (host-work test profile, `-W error`, `-p no:cacheprovider`):
  `tests/contracts`, `tests/soak`, `tests/desk_safety`, `tests/research`,
  `tests/guards` and the 162 `tests/unit` files that touch `trex`,
  `trex_web`, `desk` or `research.pilot`: 6766 passed, 0 failed, 0 skipped
  (128 s).
- Web `npm run check` (tsc, vitest, vite build, bundle check): 61 files,
  527 tests passed; single relative chunk.
- `ruff format --check`, `ruff check` (src, tests, scripts) and the
  configured `mypy` (385 source files): clean.
- End to end (no live services; fixture state under a temp root and a temp
  HOME; `trex_web` from this worktree on 127.0.0.1:18890 with
  `TREX_WORKSPACE_CONTROLS=1`; Playwright via host-browser): 15 pages
  (Today, Trade review index and detail, Portfolio, Activity feed,
  acceptance and settings, onboarding, pilot, and the legacy `#/market`,
  `#/action-model`, `#/research`, `#/stats`, `#/discover` plus an unknown
  hash) at 1280x800 and 390x844: 0 console errors, 0 horizontal overflow,
  no unknown shown as 0, every heading right, no stub, every API call
  relative. Approve on Trade review posted `approve_entry` (202 accepted)
  and showed "Queued for the desk owner"; Today's Pause posted
  `pause_entries` (202 accepted). Screenshots and `e2e-summary.json` in
  `~/.local/state/trex-wave3/e2e/`. `#/plans` was left out on purpose
  (`GET /api/plans` TCP-probes the IB Gateway port). A first run with
  `#/experiments` logged the browser's resource line for its designed 503
  (no experiment publication in the fixture); that page was replaced by two
  other aliases.

## 6. Owner steps (none done here)

1. Family ingress: create the key pair (lane A's deploy steps), then
   `~/.config/trex/web.env` (0600) with `TREX_OWNER_EMAILS`,
   `TREX_FAMILY_ORIGINS`, `TREX_FAMILY_AUDIENCE` (and
   `TREX_FAMILY_COMMAND_KEY_FILE` if not the default). Staging needs its own
   audience and key file. Until then Today shows
   `family_commands_disabled` and family commands are refused.
2. Reseal and re-render the release (COMMANDS changed), then install
   `trex-acceptance-publish` and `trex-deadman` timers and
   `~/.config/trex/deadman.env` / `acceptance.env` as ACCEPTANCE.md and
   RELEASE.md describe.
3. Pilot: the desk owner unit needs `TREX_OWNER_EMAILS`, `TREX_PILOT_DIR`
   and write access to the pilot registrations dir before a UI seal can
   apply; decide where pilot metering runs (section 3).
4. After the dead-man unit runs, decide whether the desk keeps its raw
   `reconciliation_required` and `exit_quote_blind` pushes beside the
   worded sweep alerts.
5. Lane A post-deploy checks (browser gate, check:live, check-public, a
   real Google login) remain owner-run.
