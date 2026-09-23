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

## Phone push (ntfy)

`~/.config/trex/notify.env` (chmod 600, never committed):

```
NTFY_URL=https://ntfy.sh/<random topic>
```

Subscribe to that topic in the ntfy app. The topic is a capability:
anyone with it can read and post. Push text carries no URLs, hostnames,
account ids or amounts. Without the file the watchdog is banner-only.

## Commands

```sh
# what the watchdog sees right now (changes nothing, sends nothing)
uv run --group trex --no-sync python -m tree_options.trex.gateway_watch --dry-run
cat ~/.local/state/trex/gateway.json | jq '{status, since, detail, restarts_left}'
systemctl --user list-timers trex-gateway-watch.timer
journalctl --user -u trex-gateway-watch --since -1h
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
