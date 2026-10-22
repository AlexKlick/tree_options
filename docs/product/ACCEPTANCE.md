# TREX paper acceptance: recorder, document and drill runbook

Status: Wave 4 lane H (branch `w3/h-acceptance`). Code:
`src/tree_options/trex/acceptance.py` (recorder and compute job),
`src/tree_options/trex/drills.py` (drill checks and the owner's record),
`src/tree_options/trex_web/acceptance_view.py` (`GET /api/acceptance`). Units:
`deploy/product/trex-acceptance-publish.{service,timer}.in`. Contract:
`docs/product/WAVE3-CONTRACTS.md` C3.4 (`trex-acceptance/1`) and C3.2
(`trex-acceptance-view/1`).

Acceptance is a PAPER milestone. Whatever it says, `live_authorized` is the
literal `false`: nothing here unlocks live trading, grants a mandate, places an
order or connects to a broker. The verdict is computed from evidence on every
run and recomputed again whenever the document is read; a hand-edited verdict
cannot parse.

## What has to be true (C3.4, summarized)

- **10 qualifying sessions in a row**, ending at the last closed session. A
  session qualifies when the desk owner's heartbeat was fresh (at most 120 s
  old) in at least 99% of the regular-hours minutes (387 of 390, 208 of 210 on
  an early close), reconciliation was clean at the close with every not-clean
  stretch resolved inside the session, no unowned broker order filled, and no
  open position went more than 300 s without an armed exit owner (a standing
  HALT counts as unguarded). An unrecorded or unknown session resets the streak.
- **10 counted lifecycles per asset kind** (option and equity): an approved
  entry that filled, exited by policy or by the close-all, was then seen flat at
  the broker, reconciled clean, with every commission booked; inside the
  current streak.
- **All 7 drills passed**, each with evidence and attested by an owner email.

## Where things live

`TREX_ACCEPTANCE_DIR` (the release unit sets `<desk_state>/acceptance`; the
contract default is `~/.local/state/trex/acceptance`):

| Path | Written by | Content |
| --- | --- | --- |
| `evidence/samples/<ET date>.jsonl` | recorder, append-only | one `trex-acceptance-sample/1` line per run: heartbeat, exposure, HALT, the reconciliation document's status/as_of/sha256/mismatches, the gateway watch status |
| `sessions/<date>.json` | compute job, once, 5 minutes after the close | `SessionRecord` (derived; deletable) |
| `evidence/sessions/<date>/samples.jsonl` | compute job | the immutable in-session samples the record cites |
| `lifecycles/<intent>.json`, `evidence/lifecycles/<intent>/facts.json` | compute job | `LifecycleRecord` per consumed per-trade mandate, and the ledger slice, mandate and desk exit events it was built from |
| `drills/<drill>.json` | the owner, through `trex.drills record` | `DrillRecord` |
| `evidence/drills/<drill>/` | `trex.drills check` / `record --copy` | check reports, copied evidence, `history.jsonl` of replaced records |
| `acceptance.json` | compute job, atomically | the latest `trex-acceptance/1` |

Every `EvidenceRef.path` is relative to `evidence/`, and its sha256 was taken
by the producer from the bytes it wrote or read.

Sources (read-only): the desk run dir (`TREX_DESK_RUN_DIR`: `book.json`,
`HALT`, `events.jsonl`, `ledger/events.jsonl`, `ledger/reconciliation.json`,
`proposals/`, `commands/`), the supervised dir (`TREX_SUPERVISED_DIR`:
`mandates/*.consumed.json`) and the gateway watch (`TREX_GATEWAY_STATE`,
default `~/.local/state/trex/gateway.json`).

## Install (owner steps; agents never enable units)

1. Render the release as usual (`docs/product/RELEASE.md`); the new pair is
   `trex-acceptance-publish.service` and `.timer`. Its only command is
   `tree_options.trex.acceptance publish`, behind the release identity gate.
2. Create the write root once (the unit's only `ReadWritePaths`):
   `install -d -m 0700 <desk_state>/acceptance`.
3. Name the owners in `~/.config/trex/acceptance.env` (mode 0600):
   `TREX_OWNER_EMAILS=you@example.com` (comma-separated, lower-cased on read).
   Without it the job still samples but writes no document (exit 3).
4. Point the web at the same directory: `TREX_ACCEPTANCE_DIR=<desk_state>/acceptance`
   on `trex-web` (production only; staging keeps its own or none, and then shows
   `acceptance_missing`).
5. Enable the timer. It runs 20 seconds after each run ends, so every
   regular-hours minute gets two or more samples. It samples on session days from
   one hour before the open to one hour after the close; otherwise it only
   recomputes.

The first sample is the install date: the window starts at the first session
that has a sample file. A session the recorder only partly saw does not qualify,
which is the honest answer.

## Day to day

- `GET /trex/api/acceptance` (Activity & settings, Acceptance) shows the
  verified document, or `unavailable` with one of `acceptance_missing`,
  `acceptance_invalid`, `acceptance_calendar_mismatch`,
  `acceptance_window_mismatch`, or (Wave 3/4 integration, when
  `TREX_OWNER_EMAILS` is set for the web) `acceptance_owners_mismatch`: a
  document whose `owners` differ from that list is never served. Today and
  the alert sweep refuse the same documents.
- From a terminal: `python -m tree_options.trex.acceptance show` prints the
  summary; `publish --no-sample` recomputes without sampling.
- Exit codes: 0 published; 3 degraded (the `problems` list names each one, for
  example `ledger_unreadable`, `drill_record_invalid:<drill>`,
  `drill_evidence_invalid:<drill>` (a stored `passed` record that cites no ok
  machine check of the same drill: counted as not run, the write path's rule
  applied at compute time too), `samples_invalid_lines:<date>`,
  `owners_not_configured`); 2 refused
  configuration (`owners_invalid`); 1 failure.
- Rebuild: delete `sessions/` (or run `publish --no-sample --rebuild`) and every
  session record is re-derived from `evidence/samples/`. Never edit a record by
  hand; `--rebuild` replaces it.

How each session metric is measured:

- `heartbeat_minutes`: minutes with at least one sample, every sample of the
  minute seeing `book.json`'s heartbeat at most 120 s old. An unsampled minute
  does not count. The recorder never probes the runtime lock (a probe could race
  the desk's own start).
- reconciliation: each sample contributes the document's own status at its
  `as_of` if it was at most 120 s old, else `unavailable` at the sample time. The
  open check is the first point in the first 15 minutes, the close check the last
  point in the final 15 minutes.
- `unowned_orders`: distinct broker orders with an outside fill in the desk's
  ledger since the previous session's close. Unfilled outside orders are not
  visible to a broker-free recorder.
- `max_exit_unguarded_s`: the longest stretch with exposure (or an unreadable
  book) and no fresh heartbeat or a standing HALT; an unsampled stretch over 90 s
  next to exposure counts as unguarded.

## The drills

Every drill follows the same three steps:

1. Do the drill. Note the start and end instants (UTC or with an offset).
2. Check it: `python -m tree_options.trex.drills check <drill> --since <start>
   --until <end>` (exit 0 when every requirement held, 3 otherwise). It writes
   `evidence/drills/<drill>/<stamp>-check.json` and prints its `path`.
3. Record it: `python -m tree_options.trex.drills record <drill> --status
   passed --attested-by <your email> --evidence <check path> [--copy
   <screenshot or log>] --note "<what happened>"`. `passed` is refused unless
   one cited file is an `ok` check of the same drill; the attester must be in
   `TREX_OWNER_EMAILS`. Record `--status failed` (with evidence) when a drill
   failed; re-run and re-record later (the old record goes to `history.jsonl`).

Schedule drills so they do not break a streak session (default 9: no exemption).
The heartbeat budget is 1% of the session, about 3 minutes; the exit guard limit
is 300 s.

| Drill | Do | The check requires | Attach |
| --- | --- | --- | --- |
| `relogin` | Let the gateway ask for a login (or log out), log back in from the phone | gateway watch to `needs_login`/`needs_2fa`, back to `ok`, a desk `gateway_connected` after the prompt | a note of how long it took |
| `restart_mid_position` | With a position open in a session, restart the desk owner | a sample with exposure before the restart (`gateway_connected` with `first: true`), a fresh un-halted owner after it, longest unguarded stretch at most 300 s | none needed |
| `gateway_outage` | Stop the gateway API (outside the session if possible), bring it back | gateway watch to `api_down`/`down`, back to `ok`, a desk reconnect after | the alert you received |
| `duplicate_command` | Submit the same command twice (double tap) | a command answered by the desk in the window, no command applied twice | the browser's second answer (`duplicate`); the web does not store it |
| `backup_restore` | Restore the latest backup into a scratch dir (`docs/product/RESTORE.md`) | `--restore-dir <scratch>` holds a `trex-desk-restore/1` receipt that restored files, dated inside the window (copied into the evidence) | none needed |
| `notification` | Trigger an alert (for example during the gateway outage drill) | a desk event the runtime pushes, or a gateway watch alarm, in the window | a screenshot of the push and its arrival time |
| `unassisted_use` | Use the product from the phone alone for a session: review, approve or pause | a command issued through the Family portal (`ingress: family`) applied in the window | a note that nobody helped |

## Defaults chosen here (until the owner rules)

| # | Question | Default |
| --- | --- | --- |
| H1 | Which approvals are lifecycles? | Consumed per-trade mandates (`mandates/<intent>.consumed.json`): the approval minted its permit. An approval that expired unsent sent nothing. |
| H2 | `exit_by` source | The desk's `closed` event reason for the closing structure, else its last `exit_begin`; an exit nobody explains is `manual` (never counted). A close through an untagged order leaves the intent open in the ledger (`not_exited`). |
| H3 | Commissions | Pending on the execution's ET date, missing (`false`) after it: IBKR reports only the current session. |
| H4 | Minute rule | A minute counts only if every sample in it saw a fresh heartbeat. |
| H5 | Settle time | A session record is persisted 5 minutes after the close; until then it is rebuilt every run. |
| H6 | Future-dated documents | The view refuses a document dated more than 300 s ahead of its clock (`acceptance_invalid`). |
| H7 | Release command | `tree_options.trex.acceptance publish` is added to `release.COMMANDS` (coordinator-approved amendment). |
