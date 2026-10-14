# Building the isolated research/web release

This procedure prepares a candidate. It does not install a second broker owner,
activate new entries, or qualify paper/live trading. Complete the Phase 0 custody
and canary gates before changing installed services. Preserve the previous release
and all state roots. Do not run these commands in a service-owned checkout.

1. Commit the intended candidate in an isolated worktree. Keep source clean.
2. Run the existing local `scripts/desk_safety_gate.sh` through `host-test` with
   the shared `~/.local/state/trex-desk-gate.lock`. Capture the complete transcript.
   The separate M0 gate and mutation campaign retain their own acceptance rules.
3. Build the private environment and frontend from that same frozen checkout:

   ```sh
   host-build uv sync --frozen > /tmp/trex-release-python.log 2>&1
   host-build npm --prefix web ci > /tmp/trex-release-npm.log 2>&1
   host-build npm --prefix web run check > /tmp/trex-release-web.log 2>&1
   ```

4. Place a private input config outside the source directory. Required fields are
   `source`, `research_workspace`, `paper_workspace`, `paper_catalog`, `desk_store`,
   `desk_state`, `artifacts`, `read_snapshot`, `premarket_runs`, `first_session`,
   `previous_release`, `orders_authorized: false`, and `live_money: false`.
   Use absolute paths; keep `read_snapshot` inside the evidence owner's
   `desk_state` directory, but outside `evidence/`. Keep `premarket_runs` outside
   replaceable releases. `previous_release` names the previous manifest file
   (null only when no compatible previous manifest exists). The initial session
   remains 2026-10-01; the trigger remains 08:00 America/New_York weekdays with
   the runner's NYSE calendar guard.
5. Seal and render from the release's private Python environment:

   ```sh
   .venv/bin/python -m tree_options.trex.release seal --config /private/input.json --out /private/release/config.json
   .venv/bin/python -m tree_options.trex.release verify --config /private/release/config.json
   .venv/bin/python -m tree_options.trex.release render --config /private/release/config.json --out /private/release/units
   ```

The output manifest binds the installed interpreter and package versions, source
head and tree, both dependency locks, all frontend files, and QSL command presence.
The manifest is identity evidence, not a successful test receipt. Keep the full
validation logs and source identity alongside it outside replaceable directories.

After custody gates close, the rendered `*.service`/`*.timer` files target the
existing user services. The `*.conf` files are drop-ins for `trex-web.service` and
`desk-qsl.service`. Reconcile existing drop-in ordering and explicit state roots;
do not assume a new drop-in overrides a later old one. Preserve Family Apps
operator/Tailnet ingress and existing filesystem restrictions. The web process
requires no write access to the evidence directory. The publisher owns snapshot
writes and its timer refreshes every 30 seconds; readers reject age over 120
seconds. Verify initial publication before switching the web service to it.

Test the generated units with `systemd-analyze --user verify` before installing.
Confirm actual process executable, working directory, environment source root,
asset hashes, HTTP evidence status, and absence of evidence-directory write access
in the web process after activation. A healthy process with degraded evidence
must retain specific blockers. The premarket timer's exit 3 is deliberately not
listed as a successful exit; inspect its persisted readiness report.

Rollback selects the previous source/environment/assets and compatible read-model
configuration. It must never restore an old mandate, permit, consumed-command
state, or owner epoch. Reconcile broker facts and retain current journals. An
execution-owner rollback is a separate, position-aware operation; these web and
research templates do not replace the owner.
