# Desk owner backup and restore

This runbook covers the supervised desk owner (`trex-desk.service`, IBKR
clientId 83) and its two state roots:

- the run dir `~/.local/state/trex/desk-paper` (`TREX_DESK_RUN_DIR`):
  `book.json`, `events.jsonl`, `specs/`, `requests/`, `owner.json`,
  `monitor.json`, `profile.json` and the kill files;
- the supervised state `~/.local/state/trex/supervised` (`TREX_SUPERVISED_DIR`):
  the mandate, `permits/`, `outbox/`, `journal/`.

Nothing here contacts a broker, grants a mandate, or starts, stops or
installs a service. Restoring into a live root is refused by the tool.

## Why the owner takes its own backups

The owner holds the run-dir lock (`runtime.lock`) and the account fences for
its whole life. A copy made from outside reads files while the owner may be
writing them, so it is only an observation copy (the 2026-10-14 baseline
copies are of this kind). A consistent backup is taken inside the owner: the
beat runs single-threaded under the runtime lock, and the supervised root is
copied under its own lock (`supervised/.lock`, the same lock that grant,
revoke and permit writes take).

The hook runs only in an owner process that contains this code. The owner
that is running now predates it. Until the operator restarts the owner onto
a release that contains it, there is no consistent backup. Do not restart
the owner just for this: the restart is a separate, position-aware owner
action.

## When backups are taken

- **Scheduled.** On the first beat at or after 16:30 ET on each NYSE session
  day. Beats run outside 09:30-16:15 ET; the hook never backs up inside that
  window.
- **On request.** `touch ~/.local/state/trex/desk-paper/BACKUP-REQUEST`.
  The next beat outside 09:30-16:15 ET consumes the file with an atomic
  rename and takes a backup. During the session the request waits. On days
  without a session, beats run all day and a request is honoured at once.
- **Where.** `~/.local/state/trex/desk-backups/<UTC stamp>-<owner epoch>/`.
  The default is the run dir's parent plus `desk-backups`; `DeskRuntime`
  takes `backup_root=` to change it. The newest 14 backups are kept.
  Directories the tool did not name are never deleted.
- **Shape.** `run/` and `supervised/` trees plus `manifest.json`
  (`trex-desk-backup/1`). The manifest lists each file's path, sha256 and
  size, the owner epoch, `taken_at` and the source roots. Lock files, `*.tmp`
  files and the request file are left out. Each backup is written into a
  hidden `.partial-*` directory, fsynced, then renamed into place, so a
  backup directory is either complete or absent.
- **Evidence.** `events.jsonl` records `backup_taken` (trigger, backup name,
  file count, bytes, owner epoch) or `backup_failed` (trigger, error class,
  reason code; never the error message). `backup_failed` also sends a
  default-priority push. A failure never stops the owner loop. A failed
  scheduled backup is not retried every beat: there is one scheduled attempt
  per session day per owner process. To retry, touch `BACKUP-REQUEST`.

When NO owner is running (the unit is stopped, for example during an owner
migration), the operator can take a backup directly. The command holds the
runtime lock itself. It refuses with `owner_running` if an owner holds the
lock; in that case, use `BACKUP-REQUEST` instead.

```sh
PYTHONPATH=src .venv/bin/python -m tree_options.trex.desk_backup snapshot
```

Run every command in this runbook from a checkout with its own private
environment, with `HOME` set to the operator's home. The tool writes only
into the backup root (for `snapshot`) or into the scratch destination (for
`restore`).

## Verify a backup

```sh
PYTHONPATH=src .venv/bin/python -m tree_options.trex.desk_backup verify \
  ~/.local/state/trex/desk-backups/<backup>
```

The command prints the owner epoch, `taken_at` and the file count. It exits
2 with `refused: <reason>` on any problem:

- a missing or changed file (size or sha256);
- a file the manifest does not list;
- an unsafe or out-of-tree path in the manifest;
- a manifest that is not `trex-desk-backup/1`.

## Restore rehearsal (scratch only)

A rehearsal checks that a backup can be restored and that its book is
complete. It never touches a live root and never starts an owner on the host.
A second owner with clientId 83 would collide with the real one at IBKR and
break the one-owner rule. The recovery path itself (a fresh owner over the
restored book, against a broker) is rehearsed hermetically by
`tests/soak/test_desk_restore_rehearsal.py` with a fake broker.

1. Pick the newest backup and `verify` it.
2. Restore into a new, empty directory OUTSIDE `~/.local/state/trex`, for
   example `~/trex-restore-scratch/20261021`:

   ```sh
   PYTHONPATH=src .venv/bin/python -m tree_options.trex.desk_backup restore \
     ~/.local/state/trex/desk-backups/<backup> --into ~/trex-restore-scratch/20261021
   ```

   The tool refuses (exit 2, nothing written) when the destination is not
   empty, or when it is, is inside, or contains any of these roots:
   - the run dir or the supervised dir (given with `--run-dir` and
     `--supervised-dir`, and their defaults);
   - the backup root;
   - the source roots recorded in the manifest;
   - any `--live-root`.

   Symlinks are resolved before the check.
3. Read `<scratch>/RESTORE.json`: the backup, its owner epoch, the restored
   and quarantined file lists, and the steps required before any entry.
4. Compare `<scratch>/run/book.json` with the broker's view of the bound
   account. Use read-only tools only, for example the desk's status pages.

The restored layout:

| Path | Contents |
| --- | --- |
| `run/` | `book.json`, `events.jsonl`, `specs/`, `monitor.json`, `profile.json`, `HALT` if present |
| `supervised/` | `outbox/`, `journal/`, the supervised `HALT` and `mandate.revoked.json` if present |
| `archive/run/`, `archive/supervised/` | the quarantined files, at their original relative paths |
| `RESTORE.json` | the restore receipt (`trex-desk-restore/1`) |

## What is quarantined

These files are MOVED into `archive/` and are never active after a restore:

| File | Why it can never be active again |
| --- | --- |
| `run/owner.json` | It names the old owner epoch. Every owner start mints a new epoch and writes its own file. |
| `run/requests/*` (unclaimed, `.claimed`, `.result`) | These are consumed command state. An unclaimed request restored into the inbox would be processed by the next owner as a new entry. |
| `run/FLATTEN`, `run/RESOLVE-FLAT-*` (including `.applied`) | These are operator commands about a past moment. A restored FLATTEN would close positions; a restored RESOLVE-FLAT would close a book entry unpriced. |
| `supervised/mandate.json`, `supervised/mandate.expired-*.json` | These are old authority. A mandate is bound to the epoch it was granted for. |
| `supervised/permits/*` (issued and consumed) | These are single-use authority for old intents. |

These stay active, on purpose:

- `book.json`, `specs/` and `events.jsonl` are what the exit owner knows
  about open positions. The legs-held guard checks them against the broker
  before any close.
- `outbox/` and `journal/` are the intent history. An unresolved intent
  still blocks a new intent on the same package, and reconciliation reads
  it. Never delete current journals.
- `profile.json` is the operator's capital profile. It does nothing without
  a mandate bound to its digest.
- `monitor.json` is stale health, so the exit watchdog does not mistake a
  restored state for a live owner.
- Restrictions stay as they were: the run-dir `HALT`, the supervised `HALT`
  and `mandate.revoked.json`. Restoring a restriction fails safe. Remove one
  by hand only after review.

## Never

- Never restore into, over, or around a live root, and never copy anything
  from `archive/` back into an active root.
- Never restore an old mandate, permit, consumed-command state or owner
  epoch. An old mandate copied back is refused anyway
  (`mandate_owner_mismatch`, because the new owner has a new epoch). Do not
  work around the refusal.
- Never hand-edit `book.json` to match the broker. The owner reconciles it
  from broker evidence, and anything it cannot explain raises an alert.
- Never start a scratch-restored owner on the host next to the live one.

## Putting a restored state into service

Do this only when the live roots are lost or corrupt. It is an owner
replacement: position-aware, Alex-in-the-loop, and separate from this
runbook's rehearsal.

1. The operator stops the desk owner. Confirm that the runtime lock is free:
   `desk_backup snapshot` must not refuse with `owner_running`.
2. Preserve what remains of the live roots. Move them aside; never delete
   them.
3. Reconcile the broker. Compare the open orders and positions in the bound
   account (orders tagged `trex:sup:` and `trex:desk:`) with the structures
   in the restored `run/book.json`.
4. Move `<scratch>/run` into the run dir and `<scratch>/supervised` into the
   supervised dir. Leave `archive/` and `RESTORE.json` in the scratch
   directory as evidence.
5. The operator starts the owner. It writes a NEW `owner.json` epoch. Its
   first ticks rebuild working state from the book and the broker. Exits run
   without a mandate (ruling 2b), behind the legs-held and exit guards, which
   refuse and alert on any mismatch.
6. Entries stay refused (`mandate_absent`) until the operator grants a NEW
   mandate for the NEW epoch. Take the epoch from the new `owner.json` and
   the digest from `python -m tree_options.trex.supervised_desk
   profile-digest`, then run `python -m tree_options.trex.supervised grant
   --owner-epoch <new epoch> ...`.

`tests/soak/test_desk_restore_rehearsal.py` exercises steps 3 to 6 end to
end against a fake broker. It checks these outcomes:

- the reconciled book equals the backup;
- the old mandate is refused for its epoch;
- the unclaimed evening request is never processed;
- entries resume only after a new mandate;
- the time-stop exit is still placed from the restored book.
