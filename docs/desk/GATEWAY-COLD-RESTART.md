# Gateway cold restart — the Monday-morning operator checklist

The supervised paper desk (entries, and EVERY protective exit its book
holds — ruling 2b) lives only while the IB Gateway container is logged
in. Once a week the gateway FORCES a full re-login by design. This page
is what the desk operator checks afterwards. The machine-side detail
(config table, probe semantics) lives in
[docs/trex-gateway-runbook.md](../trex-gateway-runbook.md); the desk's own
surface is [SUPERVISED-PAPER-PATH.md](SUPERVISED-PAPER-PATH.md).

## What happens, unattended (no action needed when it goes right)

- **Sunday 12:00 ET** IBC performs the weekly cold restart
  (`TWS_COLD_RESTART=12:00`, `TZ=America/New_York` in
  `deploy/trex/docker-compose.yml`): a full logout + fresh login, chosen
  for a sociable 2FA hour. This is a REAL re-authentication — it needs
  the phone (IBKR Mobile push). `FA_AUTHCODE` is NOT used in this repo;
  there is no code-based second factor configured, so a missed push means
  a human.
- An unanswered 2FA prompt does not stall: `TWOFA_TIMEOUT_ACTION=restart`
  + `RELOGIN_AFTER_TWOFA_TIMEOUT=yes` make IBC restart and re-prompt.
- `trex-gateway-watch` (60 s timer) watches the whole thing. It
  auto-restarts the container for `needs_login` / `api_down` at most
  **4 times per 24 h with a 2 h cooldown** (IBKR locks accounts after
  repeated attempts), then falls back to notify-only — the notification
  ends with `Auto-retry: N left today`. The restart ledger
  (`~/.local/state/trex/gateway.json`) self-expires after 24 h, so
  Monday's budget is fresh; a restart that turns out unnecessary (the API
  answered on recheck, or the ledger was unwritable) is refunded, not
  spent.
- The desk and monitor units re-attach on their own: both start behind
  `--wait-api`, so they wait for a real handshake instead of crash-looping.

## What the phone says (pre-alert, missed notice, 2FA)

All three are pushes from `trex-gateway-watch` through the normal notify
path (`trex.notify`), with the usual priority and quiet hours
(`trex.alert_policy`). None of them starts anything or spends restart budget.

| Push | When | What it asks |
| --- | --- | --- |
| **"cold restart in 1 hour"** (`cold_restart_prealert`) | the first tick from **11:00 ET** Sunday | be near the phone at 12:00 ET for the 2FA push |
| **"cold restart just happened"** (same action, late form) | the first tick between 12:00 and 13:00 ET if the watch was not running at 11:00 | same: approve the 2FA push if it is still waiting |
| **"cold restart missed"** (`cold_restart_missed`) | the first tick **at or after 13:00 ET** when the watch's previous tick was before 11:00 ET (it slept through the whole window) | the watch was down from X to Y; confirm the gateway re-login went through (status line below) |

- **One per Sunday.** Every form books the Sunday in `gateway.json`
  (`cold_restart_notified_for`), so the 60 s ticks never repeat it. Ticks
  after 13:00 ET never send a pre-alert; a host that was off for the whole
  window gets the missed notice instead. On 2026-10-18 the host was down
  from Saturday 03:05 MDT to Sunday 18:48 MDT and the cold restart went by
  with no push at all. That is the case this notice exists for.
- **The missed notice also logs a `watch_gap` event** in `gateway.json`
  (`events`, with the gap's `from`/`to` epochs). It needs a previous tick
  on record: a fresh or unreadable ledger proves no gap and stays silent.
- **Quiet hours or a failed push keep it owed.** The gap waits in
  `cold_restart_gap` and goes out on the first loud tick (after the 5 min
  retry backoff if the push failed), still naming the original gap.
- **2FA expectations.** The cold restart is a full logout and fresh login.
  It always needs an IBKR Mobile approval; there is no code-based second
  factor here. An unanswered prompt re-prompts on its own
  (`TWOFA_TIMEOUT_ACTION=restart`, `RELOGIN_AFTER_TWOFA_TIMEOUT=yes`),
  and while it waits the watch reports `needs_2fa` ("approve IB Gateway
  2FA"). It never auto-restarts the container for `needs_2fa`. If the API
  is still down 15 minutes after 12:00 ET, open the VNC login from the
  cockpit banner. After a missed notice, check the status line below. `ok`
  means the re-login happened unattended. Anything else is a human login.

## Monday morning (or any morning after a forced re-login)

1. **Status, one line:**
   `cat ~/.local/state/trex/gateway.json | jq '{status, since, detail, restarts_left}'`
   — `api_ok` / no `needs_login`, `needs_2fa` or `api_down`. `restarts_left`
   tells you how much of the day's auto-retry budget survived the weekend.
2. **If it says `needs_2fa`**: approve the push in IBKR Mobile (or open the
   VNC login URL the notification carries). Watchdogs handle the rest.
3. **The desk is back only when it says so:**
   `python -m tree_options.trex.desk_cli status` — the `owner.json`
   heartbeat is current and `kill_files` is empty (a stale HALT/FLATTEN
   from Friday is a desk that trades nothing; clear it deliberately).
4. **The book re-adopted**: `desk_cli status` book section shows every
   structure with the status you expect (OPEN positions still OPEN, not
   held); `journalctl --user -u trex-desk -u trex-gateway-watch --since -12h`
   for the re-login window. An exit that filled while the desk was down
   resolves from broker evidence on the first tick (see the gap-review
   table in SUPERVISED-PAPER-PATH.md).
5. **Re-grant if you want entries**: a restarted desk process has a new
   owner epoch — the old mandate is dead by design. Follow arming step 3
   (grant by policy or by hand).
6. **Sanity, changes nothing**:
   `uv run --group trex --no-sync python -m tree_options.trex.gateway_watch --dry-run`.

## Invariants this page relies on (do not "fix" casually)

- The cold-restart hour and the 2FA re-prompt behavior are compose
  settings; changing them is an owner decision, not a lane tidy-up.
- The gateway watch restart budget (4/day, 2 h apart) is an account-lock
  guard: never raise it to "make Monday quieter".
- An exit that cannot be explained from broker evidence is HELD, never
  force-closed, so a half-observed Monday can never write a wrong price
  into the book.
