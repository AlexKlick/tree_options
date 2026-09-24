# trex_web — read-only cockpit (SPA + JSON API)

Loopback FastAPI surface serving the trex execution lane's persisted state:
a React SPA (`web/`) for people and a JSON API for the SPA. **Broker-free** —
never imports `ib_async`. Designed to run in parallel with the live
`trex-monitor` service without competing for the IB Gateway session.

## Routes

| Path | Returns |
| --- | --- |
| `/` | Built SPA shell (`static/index.html`); 503 operator fallback page when the bundle is missing |
| `/assets/*` | Built SPA assets (root StaticFiles mount, registered last) |
| `/plan/{plan_id}` | Legacy-bookmark shim: inline script rewrites to `./../#/plan/<id>` (never an HTTP redirect — an absolute Location would escape the portal prefix) |
| `/api/plans` | Plan summaries (no marks data) |
| `/api/plans/{plan_id}` | Plan detail: specs, runbook, per-structure state (incl. server-computed `realized_pnl`), marks, numeric book summary, payoff series, decimated P&L history, trailing events |
| `/api/market` | Market snapshot: `market.json` symbols + watchlist + proposals |
| `/api/market/{sym}` | Symbol detail: quote + ~1y daily closes + news (discovery cache envelopes, ages disclosed) |
| `/api/market/{sym}/history` | Long-term OHLCV from the desk panel (5y split-adjusted, `?range=1y\|3y\|5y\|max&max_points=`, row-decimated, ETag/304; `points:null` + `error` when the writer holds the panel lock, `in_panel:false` pre-backfill) |
| `/api/market/{sym}/options` | Recorded options surface (nightly chain ATM slice + features cards + iv_rank + 2y IV30 history) plus `live` viewchain when warmed (`?window=&max_expiries=`) |
| `/api/market/{sym}/ideas` | Advisory idea context: signals slice (xsmom rank/top3, PEAD, next report), desk-mine queue deals, paper positions, sealed cards, research lines, and the `protocol` block (`allowed_direction` from `desk.signals` — the UI's direction chips derive from it, nothing else) |
| `/api/market/refresh` `POST` | Spool a forced market refresh (warms quote/bars/news/viewchain for the symbols) |
| `/plan/{plan_id}/book.json` | Raw `book.json` for debugging |
| `/plan/{plan_id}/events.jsonl` | Raw `events.jsonl` |
| `/health` | JSON liveness probe |

The SPA hash-routes (`#/plan/<id>`, `#/market[/{sym}]`), polls
`/api/plans[/{id}]` + the symbol endpoints every 15s/60s (paused while
hidden), and formats everything client-side (ET via `Intl` timeZone, USD
signs) except the prebuilt `+_usd` payoff labels. The symbol page's
Ideas tab is advisory-only by protocol: direction chips render solely
for `ALLOWED_DIRECTION` members; queue deals carry the miner's
`PROPOSED` status verbatim; research lines are non-directional display.

## Build & deploy

```sh
# one-time + after dependency changes (node lives in interactive shells)
cd web && npm ci

# build the SPA into src/tree_options/trex_web/static/ (gitignored)
cd web && npm run build

# client-only changes: no restart needed (StaticFiles stats per request)
# Python changes:
systemctl --user restart trex-web
```

Frontend gate: `cd web && npm run check` (tsc --noEmit, vitest, vite build).
Python gate stays the release authority: `ruff check src tests`, `mypy`
(with `--group trex-web`), `pytest`.

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
- `~/.local/state/trex/<plan_id>/marks.json` — monitor marks + bounded history
- `~/documents/tree_options/plans/*.toml` — operator-authored plans

Runtime deps: `fastapi`, `uvicorn`, `httpx`, `anyio` (no `jinja2` — the
Jinja panel was retired in the SPA cutover; chart math is pure Python in
`payoff.py`, render-space is the SPA's).

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
The portal strips the `/trex` prefix, forces `Cache-Control: private,
no-store`, and strips accept-encoding — so the SPA is **relative-only**
(fetches, links, favicon; Vite `base: './'`), single-chunk, no dynamic
imports. Root-absolute URLs would escape to the wrong app.
