# systemd user-unit templates — DO NOT INSTALL

`tree-options-era.service` is a **template** for a possible future era
supervisor. It exists to make the restart discipline explicit and
reviewable; it is NOT installed, and installing or enabling it is an
**owner authorization decision**, never a closeout step. As of 2026-08-23
no tree-options systemd unit exists on any host — that is the intended
state, and the closeout test suite asserts the absence of this unit under
`~/.config/systemd/user/`.

## Why `Restart=no` is load-bearing

A killed, OOM'd, or timed-out process is **UNKNOWN** — never success,
never FAILED, never blind-retried (`docs/m4-closeout-runbook.md` rule
0.1). A supervisor that auto-restarts would convert every UNKNOWN into a
silent second attempt. For one-shot authority — the G4 sealed event — a
restart-after-unknown could double-spend the only consumption. `Restart=no`
keeps every incident visible and classification in operator hands.

## Why logs point at `artifacts/`, never `/tmp`

`/tmp` is wiped on reboot and already orphaned this campaign's era once
(2026-08-22: pass-1's log and marker died with a reboot). The template
appends stdout/stderr to a durable path under the repo's `artifacts/`
tree, so the evidence survives whatever happened to the process.

## The manual procedure this would replace (authoritative until enabled)

1. `uv run --frozen python scripts/era_status.py --json` — read-only
   classification of the run.
2. `UNKNOWN_RESUMABLE` (capture/inspection lanes only) → resume the
   wrapper by hand; the content-addressed cache makes it free.
3. `UNKNOWN_RECONCILIATION_REQUIRED` (sealed lane) → owner decision;
   nothing automatic.

Full recovery tables: `docs/m4-closeout-runbook.md` §2.

## Installation gate

If the owner ever authorizes supervised eras: copy the unit to
`~/.config/systemd/user/`, edit `WorkingDirectory`/`ExecStart`/log paths
for the specific era, keep `Restart=no`, and record the authorization in
the campaign evidence log before `systemctl --user enable`. Enabling
without that recorded owner authorization is prohibited.
