# TREX unit and release inventory (read-only)

`scripts/trex_inventory.py` answers one question before any release work: which
code does each `trex-*`/`desk-*` user unit actually run? It only reads. It does
not start, stop, reload or enable units, fetch, or change a worktree. It writes
to one path at most: `--out`.

```sh
HOME=/home/alexk .venv/bin/python -B scripts/trex_inventory.py            # text table
HOME=/home/alexk .venv/bin/python -B scripts/trex_inventory.py --json     # JSON on stdout
HOME=/home/alexk .venv/bin/python -B scripts/trex_inventory.py --out /tmp/trex-inventory.json
```

Exit codes: 0 means complete. 3 means degraded: a probe failed, it is listed
under `errors`, and the rest of the inventory is still reported. 2 means usage
error, or `--out` points inside the serving checkout or a release directory. That
check runs before any probe.

## What it reads

- `systemctl --user list-unit-files`, `list-units --all`, `list-timers --all -o json`
  and `show -p ...` for `trex-*`/`desk-*`. These are list and show verbs only.
- `git --no-optional-locks -C <serving checkout> worktree list --porcelain`.
- `git --no-optional-locks -C <serving checkout> branch --no-merged origin/main
  --format=...`. This compares against the local `origin/main` ref as of the last
  fetch. The inventory never fetches.
- `git --no-optional-locks -C <dir> rev-parse HEAD`, only for a directory that
  has its own `.git` and is not a registered worktree. Git is never run inside an
  untracked copy, because it would climb to an enclosing repository (the home
  directory is one) and report that repository's identity.
- Directory listings of the release parents
  (`~/.local/state/trex-premarket/releases`, `~/.local/share/trex/releases`) and
  of the staging release `~/.local/state/trex-staging/release`.

Before running, each probe goes through `check_read_only`. Only those exact
shapes pass, and any other command raises `inventory_command_not_read_only`.

Secrets: environment values are recorded only when they are absolute paths under
names that do not look secret (`TOKEN`, `SECRET`, `PASSW`, `API_KEY`, `AUTH`,
`_KEY`, ...). Environment files are listed by name only and never opened. Command
lines are reduced to the program and the `-m module [subcommand]` or script path.

## Schema `trex-inventory/1`

| Field | Meaning |
| --- | --- |
| `units[]` | One entry per service. `source.kind` is `serving_checkout`, `release` (own `.git`; `head`, and `branch` when attached), `untracked_copy` (no `.git`, no identity), `missing` or `unknown`. `source.basis` gives the deciding setting: `PYTHONPATH`, then the interpreter's `.venv`, then `WorkingDirectory`. `source.consistent` is false when these name different roots (`!` in the table). The entry also lists drop-ins, environment file names, `exec`, state paths, `ReadWritePaths`, triggering timers, the next fire, and `release_template` (`dropin`/`unit`/null from `deploy/product`). |
| `timers[]` | Target unit, active and unit-file state, `next_fire_utc`/`last_fire_utc` from `list-timers`, `OnCalendar` and monotonic specs, and the target's source kind. |
| `state_roots[]` | Every state path a unit names (non-secret path variables and `ReadWritePaths`), with the units, the variable names, whether it exists, and whether it is inside the serving checkout. |
| `worktrees[]` | Registered worktrees of the serving checkout with `role`: `serving_checkout`, `release` (inside a release directory) or `worktree`. |
| `unpushed_branches[]` | Local branches with no upstream that are not merged into `origin/main`. |
| `release_dirs[]` | Every scanned release directory, plus any root outside the serving checkout that a unit runs. Each has its source root, tracked/head, and the units that refer to it by any path (including an `ExecStartPre` gate script). |
| `summary` | Counts by source kind, mixed-source units, untracked copies in use, and unreferenced release directories. |

## Snapshot 2026-10-21 (read-only run, `/tmp/trex-inventory-20261021.json`)

Generated 2026-10-21T20:01:12Z from this branch, with 0 probe errors.

- 28 services: 22 on the serving checkout (`main` at `c0010040ac01`), 4 on
  releases, 2 on an untracked copy. No unit had mixed sources.
  - `trex-web` (:8090) and `trex-premarket` ran release `0ec5acb99856` from
    `~/.local/state/trex-premarket/releases/0ec5acb/source`. `trex-web` reached it
    through `90-premarket-workspace.conf`, with `automation.conf` on top.
  - `trex-web-staging` (:8091) ran `0ec5acb99856` from
    `~/.local/state/trex-staging/release`.
  - `desk-qsl` ran release `cc91b783e843`.
  - `desk-evidence` and `desk-admission-preview` ran the untracked copy
    `~/.local/share/trex/releases/desk-wave3-safety-rc2`, which has no `.git` and
    no recorded sha.
  - `trex-monitor` has an `override.conf` drop-in.
  - `desk-forward-minutes` was in the `failed` state.
- 23 timers. `desk-qsl.timer` has unit-file state `disabled` but was active, with
  its next fire at 23:15Z. `desk-lab.timer` and `desk-lab-overnight.timer` were
  disabled and inactive.
- 9 release directories, 5 of them unreferenced: `desk-wave3-safety-rc1`,
  `a6f6d7d`, `af44b3c`, `f8d7600` and `fa3c6c2`.
- 20 registered worktrees: the serving checkout, 7 release worktrees and 12 lane
  worktrees.
- 24 unpushed branches (no upstream, not merged into `origin/main`).
- 24 state paths. Three are inside the serving checkout, so a release switch must
  leave them where they are: `artifacts/desk-store`, used by 10 units through
  `DESK_STORE`; `artifacts/desk-store/features`, through `ReadWritePaths`; and
  `artifacts/paper-trades`, through `DESK_PAPER_DIR`.
  `~/.local/state/trex-quant-datasets` is named by 2 units but does not exist.

Release templates now cover all six units that ran outside the serving checkout
([RELEASE.md](RELEASE.md)). None of them is installed.
