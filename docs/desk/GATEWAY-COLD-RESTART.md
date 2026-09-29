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
