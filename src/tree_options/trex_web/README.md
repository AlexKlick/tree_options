# trex_web — read-only HTTP status panel

Loopback FastAPI surface that renders the trex execution lane's persisted
state. **Broker-free** — never imports `ib_async`. Designed to run in
parallel with the live `trex-monitor` service without competing for the
IB Gateway session.

## Routes

| Path | Returns |
| --- | --- |
| `/` | Plan list (cards, one per TOML in `plans/`) |
| `/plan/{plan_id}` | Plan detail: structures, events, heartbeat |
| `/plan/{plan_id}/book.json` | Raw `book.json` for debugging |
| `/plan/{plan_id}/events.jsonl` | Raw `events.jsonl` |
| `/health` | JSON liveness probe |

## CLI

```sh
uv run --group trex-web python -m tree_options.trex_web \
  --host 127.0.0.1 --port 8090
```

Env overrides:
- `TREX_STATE` — state root (default `~/.local/state/trex`)
- `TREX_PLANS_DIR` — plans dir (default `~/documents/tree_options/plans`)

The systemd --user unit lives at `deploy/trex/trex-web.service` and is
deployed under `~/.config/systemd/user/trex-web.service`.

## What it reads

- `~/.local/state/trex/<plan_id>/book.json` — atomic snapshot per structure
- `~/.local/state/trex/<plan_id>/events.jsonl` — append-only trade log
- `~/documents/tree_options/plans/*.toml` — operator-authored plans

It does **not** import `ib_async`; the runtime deps are `fastapi`,
`uvicorn`, `jinja2`, `httpx`, `anyio`. The `trex-web` uv dependency group
is broker-free so the test gate stays green without an IB Gateway.

## Why a separate web lane

The trex plan's EV lives in the exit discipline (touch-exit + time stops),
not the entry — the monitor + entry runner architecture makes the exit
machine the precondition of entry. The web lane is a pure observer; it
does not influence orders, even indirectly. The trex CLI surface
(`monitor`, `enter`) is for execution; this is for visibility.

## Family-apps wiring

The service sits behind the shared HTTPS ingress at `/trex/`. Authentication
is owned by the family portal: anonymous traffic gets a 403 sign-in prompt
with `returnTo=/trex/...` so the post-login redirect lands on the cockpit.