# TREX research pilot (Wave 4, lane I)

Status: built on `w3/i-pilot` (based on `wave3/base` at `569efd7`). The three
registrations are **drafts**: none is sealed, so none can run. Sealing is an
owner act. Nothing here sends an order, grants a mandate or unlocks live
trading: the pilot is paper research only.

Contract: [WAVE3-CONTRACTS.md](WAVE3-CONTRACTS.md) C3.5 (`trex-pilot-registration/1`,
`trex-pilot-usage/1`) and C3.7 (layout), in `tree_options.trex.contracts.wave3`.
Code: `src/tree_options/research/pilot/` (store, seal, usage, admission, view,
CLI), the admission hook in `src/tree_options/research/runstate/worker.py`,
`GET /api/pilot` in `src/tree_options/trex_web/pilot_view.py`, and the
`#/pilot` page in `web/src/components/PilotPage.tsx`.

## The three programs (drafts in `data/research/pilot/`)

| Program | Role | What it is | Honest data provenance |
| --- | --- | --- | --- |
| `qsl-observation` | observation | Cites the sealed QSL-20260930 pre-registration (`docs/desk/QSL-PREREGISTRATION.md`, sha256 `390606d9...6abc3`, checked by a test against the file bytes). It re-registers nothing. | CBOE delayed chains, one post-close snapshot per session since 2026-09-22, recorded by `desk.chains` (2026-09-23 lost). Delayed, post-close only. |
| `eq-baseline-spy-weekly` | baseline | Control with no signal: whole-share long SPY on the first session of each week, at most 1,000.00 USD notional, 2% stop, out by the third session, owner approves each entry. | TREX session calendar (repo, sha256 in the provenance); IBKR paper quotes read live by the desk, NOT a recorded dataset, entitlement NOT verified, so `realtime: false`. |
| `eq-challenger-spy-reversal` | challenger | Preregistered against the baseline: enter the session after a recorded close at least 1% below the previous one. Same order, sizing, approval and exit policy. | The same plus SPY's post-close underlying quote from the CBOE delayed chain recorder. That is not a point-in-time qualified dataset: the owner should confirm recorder uptime before sealing. |

Decision rule (both equity programs): mean net P&L per completed lifecycle
(C3.4 definition, commissions included, from the reconciled paper ledger),
read once when both have at least 20 completed lifecycles and 60 sessions have
passed since the later seal; the challenger wins only if the difference of
means is above 0 and its 95% bootstrap lower bound (10,000 draws, seed
20261021) is above 0.

Budgets (per program per America/New_York day, each under
`PILOT_BUDGET_CEILINGS`): the observation's are all **zero** (the existing QSL
lane keeps its own timers and owner; no pilot job may run under its id); the
equity programs get 20 requests, 10 compute minutes and 64 MiB.

Open owner questions (defaults are in the drafts; sealing accepts them):

1. `qsl-observation.minimum_sessions` is 67, the measured projection of
   recorded sessions the binding family needs (QSL section 6). The cited rule
   decides by cohorts; the number is there because the field is required.
2. Equity sizing (1,000.00 USD notional), the 2% stop, the third-session
   deadline, the weekly control schedule and the 1% signal threshold.
3. Whether the CBOE recorder's uptime (5 of 6 sessions at the QSL
   measurement) is good enough for the challenger's signal, and whether the
   IBKR paper market data entitlement should be verified before sealing.
4. The budget numbers.

## Store and seal (`research/pilot/store.py`, `seal.py`)

`TREX_PILOT_DIR` (default `~/.local/state/trex/pilot`):

```
registrations/<program_id>.draft.json    editable; never runs
registrations/<program_id>.sealed.json   written once (O_EXCL, 0600), never edited
usage/<program_id>/<YYYY-MM-DD>.json     trex-pilot-usage/1 (ET day)
usage/<program_id>/jobs/<run_id>.json    trex-pilot-job/1: admitted, not yet settled
```

- The sealed file wins over the draft. A file that does not read as that
  program's registration (garbage, a symlink, another program, a sealed
  document in the draft file) is `registration_unreadable`, never skipped.
- A seal requires, in order: no sealed file yet (`registration_already_sealed`),
  a readable draft (`registration_missing` / `registration_unreadable`), the
  hash the owner reviewed equal to `registration_body_sha256(draft)`
  (`registration_sha256_mismatch`), the sealer in `TREX_OWNER_EMAILS`
  (`sealer_not_owner`), every named control already sealed
  (`control_not_sealed:<id>`). After a seal, `write_draft` refuses the program.
  So the order is: observation, baseline, then the challenger.
- From the UI: the `seal_registration` command (below), applied by the desk
  owner with the VERIFIED actor as sealer: `issued_by.sub` must be an owner
  email, so the loopback label `loopback-operator` can never seal.
- From a loopback shell, the owner names their email:

```
python -m tree_options.research.pilot import-drafts data/research/pilot
python -m tree_options.research.pilot list            # shows review_sha256 per program
python -m tree_options.research.pilot seal --program-id qsl-observation \
    --sha256 <review_sha256> --owner-email <owner email in TREX_OWNER_EMAILS>
```

## Usage metering (`research/pilot/usage.py`)

- `requests`: one per admitted job, charged BEFORE the job starts.
- `compute_seconds`: the job's monotonic compute time in whole seconds,
  rounded up, charged when it ends (completed or failed).
- `storage_bytes`: the UTF-8 length of the canonical JSON (sorted keys,
  compact) of the result record the job published; 0 when it published none.
  Files a job writes elsewhere (for example `quant-jobs/<run_id>/`) are not
  metered yet.
- All charged to the America/New_York day of admission.
- Unknown is never zero: an unreadable, invalid or foreign day file reads
  `budget_usage_unknown:<unreadable|invalid|mismatch>` and nothing can be
  charged on top of it; an open job marker reads
  `budget_usage_unknown:job_in_flight` (its compute is not metered yet), so a
  program runs one job at a time. A day with no file and no marker had no
  admission (the meter is its only writer and writes first), so it reads zero.
- A worker that dies mid-job leaves the marker. When the run is requeued and
  re-admitted, the wall time since its admission is charged as compute (an
  over-count, the safe direction). A job that will not be re-run is settled by
  hand: `python -m tree_options.research.pilot release --program-id ID
  --run-id RUN --compute-seconds N`.

## Admission (`research/pilot/admission.py`, `runstate/worker.py`)

A research run record that carries `pilot_program_id` meets
`PilotAdmission.admit` in `ResearchWorker._process`, under the queue control
lock, before it is claimed. Blockers are `registration_run_blockers` (C3.5:
`registration_not_sealed`, `registration_sealer_not_owner`,
`budget_usage_unknown`, `budget_usage_mismatch`, `budget_*_exhausted` at
`used >= budget`), plus `registration_missing`, `registration_unreadable`,
`pilot_program_invalid` and `pilot_admission_unavailable` (the admission could
not judge: the run never starts). A refused run is terminal: `status`
`failed`, `error_code` `pilot_admission_refused`, `admission_blockers`, and
nothing is charged. An admitted run gains `pilot_day`, `pilot_admitted_at`
and `pilot_registration_sha256`. With no admission passed to the worker, pilot
runs use `PilotAdmission.from_env()` (`TREX_PILOT_DIR`, `TREX_OWNER_EMAILS`).
Runs without the field never meet the hook (existing QSL and challenge work is
unchanged). Editing `worker.py` changes `engine_identity_sha` (the worker is
in `_ENGINE_MODULES`), as every worker change does: a rerun is a new run.

## `GET /api/pilot` (`trex-pilot-view/1`, lane-local)

`{schema, now, status, unavailable_reason, programs, roster_problems,
owners_configured, seal_command_available, execution_enabled: false,
live_authorized: false}`; each program row `{program_id, role, state
(draft|sealed|unavailable), unavailable_reason, registration, review_sha256,
usage, usage_unavailable_reason, run_blockers}`. Programs in role order.
Null rule as C3.2, validated by `research.pilot.view.PilotView`. The fixture
`tests/research/fixtures/pilot/pilot_view.json` is the served view and the
TypeScript mirror's contract (`web/src/lib/pilotApi.ts`).

## `#/pilot` and `#/pilot/:programId`

`PilotPanel` (content only) and `PilotPage` (inside the current `AppShell`).
The list shows each program's role, sealed or draft state, hypothesis,
today's usage against each budget (unknown usage as unavailable with its
reason, never 0) and why it cannot run. The detail adds the decision rule,
universe, controls, every data source with provenance, license and realtime
flag, and for a draft the seal: the review hash, an explicit "I reviewed this
registration and its hash" checkbox, and a `Seal registration` button that
queues one `seal_registration` command (never sent automatically, disabled
while a control is unsealed). Until the server knows the command kind
(`seal_command_available` false) the page shows the loopback CLI line
instead.

## Coordinator wiring (lane I does not edit these files)

1. `src/tree_options/trex_web/app.py`, beside `attach_portfolio(...)`:

   ```python
   from tree_options.trex_web.pilot_view import attach as attach_pilot

   attach_pilot(app, clock=clock)
   ```

2. Lane E router: `pilot` renders `<PilotPage />` (or `<PilotPanel />` in the
   new shell), `pilot-program` renders `<PilotPage programId={param} />`
   (import from `./components/PilotPage`).
3. The `seal_registration` amendment: APPLIED by the Wave 3/4 integration
   (the patch file is removed; see WAVE34-INTEGRATION.md). Additive: the kind
   and `SealRegistrationCommand` in `contracts/commands.py`,
   `SealRegistrationArgs` moved there and re-exported unchanged by
   `contracts/wave3.py`, the desk applying it as a file kind in
   `trex/desk_commands.py`, `CommandArgs.seal_registration` in
   `web/src/lib/contracts.ts`. `tests/research/test_pilot_seal_command.py`
   now runs the full web-queue to desk-apply path.
4. The desk owner's unit needs `TREX_OWNER_EMAILS` (and `TREX_PILOT_DIR` if
   not the default) in its environment and write access to the pilot dir;
   without them a seal is refused (`sealer_not_owner`) or fails (`apply_error`).
5. A producer that submits pilot jobs sets `pilot_program_id` on the run
   record (for example an optional field in `quant_jobs.enqueue`); none does
   yet.
