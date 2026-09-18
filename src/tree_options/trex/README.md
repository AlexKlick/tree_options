# trex — defined-risk execution lane (IBKR)

Mechanical execution of an operator-authored, defined-risk options plan.
The exit machine is the trade: the plan's EV lives in the touch-exit and
time-stop discipline, so **no entry is ever placed unless the monitor is
provably alive** (fresh heartbeat + held flock).

This lane is deliberately outside the research protocol packages. It
encodes a plan; it never derives one. There is no code path that widens a
structure, adds contracts, or sells anything naked.

## Pieces

| Piece | File | What it owns |
| --- | --- | --- |
| Plan models | `plan.py` | TOML -> validated `TradePlan`; caps fail closed |
| Decision core | `engine.py` | pure `decide()`: entry ladder, touch-exit, time stops |
| Book state | `state.py` | legal transitions, atomic `book.json`, append-only `events.jsonl` |
| Broker adapter | `ibkr.py` | ib_async I/O: NBBO-from-legs, BAG combos, order adoption |
| Exit machine | `monitor.py` | systemd service; flock + heartbeat; kill files |
| Entry runner | `enter.py` | arm-gated entries; reprice-at-mid; abort on window close |

State lives in `~/.local/state/trex/<plan-id>/` (override: `TREX_STATE`).

## The exit discipline, as code

- **Entry** (window 09:45-12:00 ET on `entry_date` only): combo limit at
  min(NBBO mid, cap); reprice at fresh mid every 90s; after 4 cycles cross
  to min(ask, cap); after the window, abort. Never above the cap.
- **Touch-exit**: spot at/below the long strike -> sell the spread at mid;
  reprice every cycle; after 3 cycles go to the bid; after 15:45 ET on the
  deadline day, reprice at the fresh bid every cycle until flat (IBKR takes
  no market orders on combos).
- **Time stops**: Oct-9 legs flatten from 09:45 ET Oct 9; Nov-6 legs from
  09:45 ET Nov 6. Expiry is a hard backstop that never gets reached.
- **Kill files** in the run dir: `FLATTEN` = exit everything now;
  `HALT` = place no new orders (working exits finish).

Partial fills are first-class: exits are sized to *filled* quantity, not
plan quantity, and an aborted entry that partially filled becomes OPEN
(flattened by the monitor, never abandoned).

## Paper runbook (Friday 2026-09-18, times MDT = ET-2)

```bash
# --- once, tonight ---
cp deploy/trex/ib.env.example ~/.config/trex/ib.env   # fill paper creds, chmod 600
docker compose -f deploy/trex/docker-compose.yml up -d   # paper gateway, :4002
mkdir -p ~/.config/systemd/user
cp deploy/trex/trex-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload

# --- before 07:30 MDT (09:30 ET) ---
docker compose -f deploy/trex/docker-compose.yml ps        # gateway healthy
cd ~/documents/tree_options
uv run --group trex python -m tree_options.trex.monitor --plan plans/2026-09-18.toml --dry-run
systemctl --user start trex-monitor                        # ARM the exit machine
journalctl --user -u trex-monitor -f                       # watch heartbeats

# --- after 09:45 ET: enter (refuses to run if the monitor is down) ---
uv run --group trex python -m tree_options.trex.enter --plan plans/2026-09-18.toml

# --- flatten everything, now ---
touch ~/.local/state/trex/putspread-20260918/FLATTEN
```

Order status survives restarts: both runners adopt their working combo
orders from the broker on boot (`openTrades` matching by leg conIds).

## Not in scope

Holiday calendars (this book crosses none), live mode hard rail above
$5,000 committed (edit the plan, don't raise the rail), and any notion of
signals. The retired semi-reversion lane stays retired; trex only executes
what the operator writes down first.
