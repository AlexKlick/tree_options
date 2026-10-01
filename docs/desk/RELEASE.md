# TREX premarket release procedure (owner-installed, research-only)

How the pinned web/premarket release under `~/.local/state/trex-premarket/` is
cut — used for `cc91b783` (2026-09-30) and repinned through `a6f6d7d`,
`af44b3c`, `fa3c6c2`, `f8d7600` (2026-10-01). Nothing is automated by the repo:
every step runs by hand on the host, and steps marked MANUAL touch files
systemd owns. No step places orders: the runner asserts `orders_authorized=
false` and `live_money=false` before and during every run.

A release is `~/.local/state/trex-premarket/releases/<short-sha>/`:

- `source/` — a DETACHED, CLEAN git worktree of the main checkout at the pin
- `source/.venv/` — a private uv venv, frozen at release time
- `premarket.py` — the 08:00 ET morning runner AND the ExecStartPre `--check` gate
- `smoke.py` — the staging HTTP rehearsal (loopback :8091)
- `test_premarket.py` — the runner's own unit tests
- `config.json` (mode 0600) — the pin and the paths (fields in step 6)

## Steps

1. Pick the commit in the main checkout (merged, gate green). `short=$(git
   rev-parse --short=8 HEAD)`; keep the full 40-hex sha for `config.json`.
2. Create the worktree: `git -C /home/alexk/documents/tree_options worktree add
   --detach ~/.local/state/trex-premarket/releases/$short/source <full-sha>`.
   `git status --porcelain` inside it must stay empty forever after — the
   runner's `verify()` aborts on any dirt.
3. Private venv, from inside `source/`: `env -u UV_PROJECT_ENVIRONMENT
   UV_PROJECT_ENVIRONMENT=$PWD/.venv uv sync --frozen` (the default dev group
   includes the trex-web deps). Never re-sync an existing release.
4. Frontend assets: `src/tree_options/trex_web/static/` is gitignored build
   output. Copy the three built files (`index.html`, `assets/index-*.css`,
   `assets/index-*.js`) from the current web build (or the previous release's
   `source/`) into the new worktree, and record `sha256sum` of each.
5. Carry the runner forward: copy `premarket.py`, `smoke.py`,
   `test_premarket.py` from the previous release directory. They live outside
   git; edit only when the contract changes.
6. Write `config.json` (then `chmod 600`):
   - `release_head` — the full sha; `verify()` requires source HEAD to equal it
   - `source` — absolute path to the `source/` worktree
   - `static_sha256` — served-file hash map; enforced by `--check`, asserted
     over HTTP by `smoke.py`
   - `research_workspace`, `paper_workspace`, `paper_catalog`, `desk_store`,
     `desk_state`, `artifacts` — the state paths the runner inspects; they
     mirror the drop-in's `Environment=` lines
   - `timezone`, `trigger` — documentation of the 08:00 ET weekday schedule
     (the installed timer is the real schedule)
   - `first_session` — first NYSE session the runner may act on; earlier
     triggers exit `SKIPPED_BEFORE_FIRST_SESSION`
   - `orders_authorized`, `live_money` — MUST be `false`; `verify()` hard-fails
     otherwise (the research-only contract)
7. Rehearse on STAGING FIRST: `~/.local/state/trex-staging/release` is its own
   worktree + venv at the same sha. Point a copy of `config.json`'s `source`
   at it and run `smoke.py`: it serves 127.0.0.1:8091 with
   `TREX_RESEARCH_WORKER=0`, asserts the three asset hashes over HTTP, the four
   read APIs, the one $1,000,000 / 29-sleeve allocation with
   `execution_authorized=false`, and that a forwarded mutation POST gets 403.
   The receipt is one JSON `PASS` line; stop on anything else.
8. Run the gate yourself once: `<release>/source/.venv/bin/python <release>/
   premarket.py --check` must print `"status": "PASS"`.
9. MANUAL — edit `~/.config/systemd/user/trex-web.service.d/90-premarket-workspace.conf`:
   all four release paths (WorkingDirectory, PYTHONPATH, ExecStartPre,
   ExecStart) AND the `[Unit]` Description, which names a release too (found
   stale on 2026-10-01: it said "pinned a6f6d7d" while the paths said
   f8d7600). Then `systemctl --user daemon-reload`.
10. MANUAL — `systemctl --user restart trex-web.service`. (The 08:00 runner
    also restarts it when the running owner's `/proc/<pid>/cwd` differs from
    the release, and reuses it when it already matches.)
11. Verify the served surface: `curl -fsS http://127.0.0.1:8090/api/research/
    quant/datasets` shows `controls_enabled=true`, `execution_authorized=false`,
    `live_money=false`; `journalctl --user -u trex-web.service` shows the
    ExecStartPre PASS.
12. MANUAL when repinning the morning runner too: `~/.config/systemd/user/
    trex-premarket.service` pins the release path in four places of its own
    (still at `cc91b783` on 2026-10-01 while the web drop-in was at f8d7600 —
    repin both together or the 08:00 job verifies the OLD release), then
    `daemon-reload`.
13. Receipts: every runner mode (`--check/--prepare/--preview/--run`) writes a
    0600 JSON receipt under `~/.local/state/trex-premarket/runs/` plus
    `latest.json`; the morning run's steady state is
    `RESEARCH_STARTED_BROKER_BLOCKED` — success, not an alarm.

A release is immutable: repin by cutting a NEW directory, never by editing a
shipped one. The staging smoke is loopback HTTP evidence only — no browser,
provider, or broker proof — and the venv's frozen lock is part of the pin.
