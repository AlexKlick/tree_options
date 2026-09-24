# trex IB Gateway runbook

The paper book's exit machine (trex-monitor) and discovery both talk to
the IB Gateway container `trex-ib-gateway` (IBC + gateway 10.51,
`deploy/trex/docker-compose.yml`) on `127.0.0.1:4002`. If the gateway is
logged out, open positions have **no exit machine**.

## Log in by hand

1. On a tailnet device open
   **https://pop-os.tail2b3f82.ts.net:6080/vnc.html?path=vnc**
   (or `…:6080/` and press Connect). easy-novnc serves its socket at `/vnc`;
   noVNC's default `/websockify` 404s and shows "Failed to connect to
   server".
2. The password it asks for is the **screen (VNC) password**,
   `VNC_SERVER_PASSWORD` in `~/.config/trex/ib.env`, not the IBKR one. Read
   it in your own terminal; never paste it into a chat.
3. In the gateway window: approve 2FA in IBKR Mobile, or pick **Paper
   Trading** and log in. The monitor starts within ~5 s of the API
   answering (its `ExecStartPre` waits for it); discovery reconnects
   within 2 minutes.

## Automation (2026-09-23)

| Layer | What it does |
|---|---|
| `ib.env` `TWS_USERID` | the name the image renders into IBC's `IbLoginId`. Setting only `TWS_USERNAME` gave IBC a blank user: every automated login from 09-18 to 09-23 stalled on the open dialog |
| compose `AUTO_RESTART_TIME=11:45 PM` | daily restart **keeps the session** (no re-auth). Without it the gateway logged OFF daily at 11:45 PM UTC (19:45 ET). The Gateway's own zone is `jts.ini` `TimeZone`, seeded once from `TIME_ZONE` and persisted in the `ibgw-settings` volume (ours: `Africa/Abidjan` = GMT), so the restart lands at 19:45 EDT or 23:45 ET: check the first one in the log |
| compose `TZ=America/New_York`, `TWS_COLD_RESTART=12:00` | IBKR's weekly full re-login: IBC's cold restart reads the JVM zone (`TZ`), so Sunday noon ET (a sociable 2FA hour) |
| compose `TWOFA_TIMEOUT_ACTION=restart`, `RELOGIN_AFTER_TWOFA_TIMEOUT=yes` | an unanswered 2FA prompt re-prompts instead of stalling |
| compose `EXISTING_SESSION_DETECTED_ACTION=primary` | no silent stall on an "existing session" dialog |
| `trex-gateway-watch.timer` (every 60 s) | real IB handshake probe, IBC login phase, x11vnc; writes `~/.local/state/trex/gateway.json`; restarts x11vnc when missing (5 min backoff; never when VNC has no password configured); restarts the container when a login is stuck ≥ 10 min or the API is silent ≥ 5 min (**max 1 per 2 h and 4 per 24 h**: IBKR can lock accounts after repeated login attempts); pushes on entering a bad state, every 4 h while it lasts, and on recovery (a failed push retries every 5 min) |
| container-restart safety | the attempt is written to the ledger **before** docker acts (unwritable ledger = no restart); the API is re-probed first; no restart when the log read failed or the ledger was unreadable (held for 2 h); a worst-case run fits the unit's 240 s deadline |
| trex-monitor `ExecStartPre=… --wait-api 300` | no crash loop against a logged-out gateway (1,276 restarts on 09-23) |
| trex-monitor exits (code 6) on a lost connection | a drop inside a tick used to be swallowed, leaving a running but disconnected exit machine; systemd now restarts it behind `--wait-api` |
| cockpit banner (`GET /api/gateway`) | every page: what is wrong, since when, retries left, the login link; a silent watchdog or unreadable status shows as "health unknown" (with any old alarm as "last known"), never as healthy |

States: `ok` · `starting` (login in progress, < 10 min) · `checking` (API
missed, < 5 min: no action) · `needs_login` · `needs_2fa` · `api_down`
(API silent ≥ 5 min) · `down` (container stopped: never auto-started, it
may be deliberate) · `unknown`.

## Exit machine (trex-monitor) watch

The gateway can be fine while the book is unguarded: the monitor dead, or
alive with every tick failing (caught each 20 s poll; systemd and the
heartbeat still look healthy). `trex-exit-watch.timer` (every 60 s,
`trex/exit_watch.py`) looks only at run dirs with exposure (a structure
entering, open or exiting) and reads `book.json` (heartbeat) plus the
monitor's `monitor.json` (tick outcome, error class only).

States: `idle` (no open positions) · `ok` · `waiting_for_gateway` (the
monitor is down or failing because of the gateway: the gateway watchdog is
alarming, or the gateway isn't ok for ≥ 2 min and the outage is < 15 min;
no second push) · `monitor_down` (heartbeat > 2 min old or book.json
unreadable, and the gateway not to blame; a silent gateway watchdog never
takes the blame) · `monitor_failing` (beating but `monitor.json` absent or
older than 3 min, any hour; or in market hours 3 failed ticks in a row or
none good for 3 min; the last good tick survives monitor restarts) ·
`touch_blind` (the monitor is healthy, but inside the touch window, from
the open + 16 min to the calendar close, early closes included, an open
position's underlying has had no accepted spot for ≥ 10 min: its touch
exit can't fire; time-stop and expiry exits still work. Push: "trex: touch
exit blind", ticker symbols only) · `touch_suspended` (a blind incident
the session ended without a price: held silently, never a recovery; "trex:
touch exit back" goes out only once a price arrives or the blind position
closes). Detection only: the monitor's restarts are systemd's
(`Restart=on-failure`). Banner: `GET /api/exit-machine`.

Deploy after the monitor runs code that writes `monitor.json`, or a
monitor without it alarms as `monitor_failing` after 3 minutes.

**Touch-exit spot (`trex/spot.py`).** The only source is the Polygon stock
snapshot (`~/.config/tree_options/polygon.key`, 15 min delayed intraday).
IBKR stock prices are never used: the paper account has no equity quotes,
`close` is the prior session's, and `ticker.time` moves on every bid/ask
tick so it can't date `last` (revisit once live quotes arrive and
`lastTimestamp`, tick 45, is verified). A bar counts only if it is dated
inside the current session's regular hours (premarket and after-hours
prints never fire a touch) and is ≤ 20 min old, so each day's first price
arrives about 16 min after the open. Fetches run in background workers,
never in the exit loop: one request in flight per symbol, 60 s between
attempts, 10 s total per request, OPEN positions' underlyings only. No
accepted spot = no touch decision. `marks.json` carries `spot_sources`
(`px`, `source`, `as_of`, `age_s`) next to `spots`; `monitor.json` carries
`touch_guarded`, `spot_ok` and `spot_blind` (`{symbol: since}`, counted
only inside the touch window, kept across restarts).

**Calendar horizon.** trex reads its own session calendar,
`data/calendar/trex/nyse_sessions_2018_01_02_2028_12_29.json` (the sealed
protocol calendar ends 2026-12-31; past a calendar's last session the
monitor sees no sessions and places no exits). `monitor.json` carries
`calendar_last_session` and `calendar_horizon_warn` (true inside the last 60
sessions; the watchdog copies it onto the book row). Regenerate with
`scripts/gen_trex_calendar.py` (see its docstring for the pinned build env).
Unscheduled closures the pinned exchange-calendars 4.5.2 does not know are
declared in its `CLOSURE_OVERRIDES` (today: 2025-01-09, the Carter day of
mourning) and recorded in the payload's `closure_overrides`; the sealed
protocol calendar still lists that day, so research walking it must treat
it as a non-session itself.

**FLATTEN and working entries.** IBKR lets only the placing clientId cancel
an order (error 10147), and the monitor (clientId 71) never sees the entry
runner's (72) BUYs. So under FLATTEN, `trex.enter` cancels its own working
entries on its next cycle (15 s), closes PLANNED structures and enters
nothing new; a partly filled entry goes OPEN and the monitor sells it. The
monitor leaves `enter_working` structures alone (event
`flatten_waits_for_entry_runner`). If the entry runner is not running,
start it: it adopts its working BUYs and cancels them on its first cycle.
An entry whose order filled or died while the runner was down is settled
from broker evidence (today's executions of that order, or a flat
account); if that is inconclusive it stays `enter_working` with the event
`entry_unresolved`: check the account by hand. HALT also stops the entry
runner placing or repricing.

**Shared book.** Both runners write `book.json` under an exclusive lock
(`book.json.lock`), each keeping the other's structures (entry lane vs
exit lane), never moving a structure backwards (CLOSED stays closed), and
never overwriting an unreadable book.

## Phone push (ntfy)

`~/.config/trex/notify.env` (chmod 600, never committed):

```
NTFY_URL=https://ntfy.sh/<random topic>
# optional; the defaults shown
QUIET_HOURS=22:00-07:00
OPERATOR_TZ=America/Denver
```

Subscribe to that topic in the ntfy app. The topic is a capability:
anyone with it can read and post. Push text carries no URLs, hostnames,
account ids or amounts. Without the file the watchdogs are banner-only.
Comments go on their own lines (values are taken verbatim).

When a push goes out (`trex/alert_policy.py`, both watchdogs):

| When | Priority | Reminders |
|---|---|---|
| market hours (NYSE session, 09:00-16:15 ET) with open positions | high | hourly |
| quiet hours (`QUIET_HOURS`, operator time; `off` disables) | held: sent when they end if still bad (07:00 MDT = 09:00 ET) | held |
| any other time | default | every 4 h |

A recovery ("… back") is sent only for an alarm that was delivered, and
waits out quiet hours too. A failed push retries every 5 min. Self-heal
(x11vnc, capped container restarts) never waits for daylight.

## Commands

```sh
# what the watchdog sees right now (changes nothing, sends nothing)
uv run --group trex --no-sync python -m tree_options.trex.gateway_watch --dry-run
uv run --group trex --no-sync python -m tree_options.trex.exit_watch --dry-run
cat ~/.local/state/trex/gateway.json | jq '{status, since, detail, restarts_left}'
cat ~/.local/state/trex/exit_watch.json | jq '{status, since, detail, books}'
systemctl --user list-timers 'trex-*-watch.timer'
journalctl --user -u trex-gateway-watch -u trex-exit-watch --since -1h
# apply compose/env changes (logs the gateway out; IBC logs back in)
docker compose -f deploy/trex/docker-compose.yml up -d
```

## Known traps

- `docker compose up -d` after a compose/env change **recreates** the
  container: a fresh login (maybe 2FA). Do it outside market hours.
- x11vnc can lose a boot race with Xvfb (`XOpenDisplay(":1") failed`)
  and exit for good; the watchdog restarts it.
- The compose healthcheck only opens the socket; the socat bridge accepts
  even while logged out. The watchdog's handshake probe is the truth.
