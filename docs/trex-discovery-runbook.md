# trex discovery runbook

The discovery lane scans delayed IBKR put chains into ranked put-DEBIT
spread candidates and serves them on the cockpit's `#/discover` page.
It holds its own gateway session (clientId 74; 71 monitor / 72 enter /
77 library are reserved) and **never places orders**.

## Layout

- Config: `~/.config/trex/discovery.toml` (repo default:
  `deploy/trex/discovery.toml`). NEVER in `plans/` — the plan glob
  adopts TOMLs there.
- Artifacts: `~/.local/state/trex-discovery/`
  - `latest.json` — newest scan (stamp + payload)
  - `runs/<run_id>/scan.json` — history, newest 30 kept
  - `spool/scan.request.<id>` — POSTed scan requests
  - `spool/scan.claim.<id>` — claimed by the runner (atomic)
  - `spool/scan.result` — last request outcome
  - `account.json` — account equity copy (freshest-wins with the monitor's)
- Web API: `GET /api/discovery`, `POST /api/discovery/scan` (202; 503
  when the sandbox blocks the spool).

## Scan-on-demand flow

site button → POST → `scan.request.<uuid>` (the trex-web unit may write
ONLY this directory via ReadWritePaths) → runner claims it (link+unlink:
exactly one winner; claims older than 10 min are reclaimed) → scan →
`latest.json` → `scan.result`. The page polls `/api/discovery` and
shows pending/age from the PAYLOAD (transport staleness is separate).

## Targeting honesty

Probe 2026-09-22 (docs/trex-discovery-probe-2026-09-22.md): delayed
chains flow, delayed modelGreeks do NOT. `target_mode=auto` therefore
degrades delta → %OTM (needs spot; paper has none) → **premium-floor**,
and every artifact stamps `effective_target_mode` + the delta rule as
NOT_APPLICABLE. Explicit `delta` mode with no greeks refuses (NOT_
EVALUABLE rows), never silently degrades.

## Watchlist proposals (LLM)

`llm_provider` is a chain tried in order, default `local,minimax,zai`:
the loopback Qwen 27B (no key, no quota), then MiniMax-M3, then Z.AI
glm-5.3-flash last (its coding plan is shared with every claude-zai
session and Study Forge, and hits the 5-hour wall). Hosted keys come
from the env var NAMES in `discovery/llm.py`: the launcher names
`ANTHROPIC_AUTH_TOKEN_ZAI` / `ANTHROPIC_AUTH_TOKEN_MINIMAX2` first, then
`ZAI_CODING_API_KEY` / `MINIMAX_API_KEY`. The unit loads them from
`~/.claude/.env` (`EnvironmentFile=-`), so rotating a launcher key
rotates this lane too. A missing key only adds a "not set" note to the
run; the chain moves on. Keys never appear in config, logs, notes or
artifacts.

## Commands

```sh
# one scan (gateway must be up on :4002)
uv run --group trex python -m tree_options.trex.discovery --once
# capability report (writes nothing)
uv run --group trex python -m tree_options.trex.discovery --probe --underlying NVDA
# long-running service
systemctl --user start trex-discovery
journalctl --user -u trex-discovery -f
```

## Install (one-time)

```sh
mkdir -p ~/.local/state/trex-discovery/spool   # BEFORE ReadWritePaths unit start
cp deploy/trex/discovery.toml ~/.config/trex/discovery.toml
cp deploy/trex/trex-discovery.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now trex-discovery
# trex-web needs the ReadWritePaths line from deploy/trex/trex-web.service
```

## Traps

- `plans/*.toml` glob: config never in plans/.
- Gateway paper mkt-data budget ~50 lines: the runner caps
  subscriptions (`max_quotes_per_underlying`) and cancels in `finally`;
  a silent all-NaN row masquerades as "no candidates".
- Zero bids outside session hours = no-market → NOT_EVALUABLE rows.
- expirations[0] can be 0DTE and unqualifiable late in the day — expiry
  selection filters to the DTE window first.
- The `.venv` is shared across groups; starting the discovery/monitor
  unit may re-sync it — restart trex-web after group switches.
