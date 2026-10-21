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
   `experiment_sources`, `experiment_snapshot`,
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

Version 2 also requires an existing experiment-source JSON config outside the
release, and an experiment snapshot path inside `desk_state`. Its config hash is
sealed with the release. See [EXPERIMENTS.md](EXPERIMENTS.md) for the source keys.
Version 1 manifests are preserved as history; do not rewrite an old manifest to
claim version 2 identity.

The output manifest binds the installed interpreter and package versions, source
head and tree, both dependency locks, all frontend files, QSL and experiment publisher command presence, and the
experiment-source configuration hash. `supported_commands` lists every command a
rendered unit may run, now including `tree_options.desk shadows` and
`tree_options.desk desk-enter`. A manifest sealed before that change will not
verify under the new code; reseal it rather than editing it.
The manifest is identity evidence, not a successful test receipt. Keep the full
validation logs and source identity alongside it outside replaceable directories.

Render is pure: it verifies the manifest, resolves every template, and checks
that every unit command line runs `-m <command>` from `supported_commands`
(`release_template_command_unbound` otherwise). Nothing is written unless every
template passes, and it never calls `systemctl`. Installing is a separate,
owner-coordinated step.

After custody gates close, the rendered `*.service`/`*.timer` files target the
existing user services. Each `<unit>-release.conf` is a drop-in for one existing
unit. Together they cover every unit that ran outside the serving checkout on
2026-10-21 (see [INVENTORY.md](INVENTORY.md)):

| Drop-in | Base unit | Live source on 2026-10-21 |
| --- | --- | --- |
| `trex-web-release.conf` | `trex-web.service` (:8090) | release `0ec5acb` via `90-premarket-workspace.conf` |
| `trex-web-staging-release.conf` | `trex-web-staging.service` (:8091) | `~/.local/state/trex-staging/release` (`0ec5acb`) |
| `desk-qsl-release.conf` | `desk-qsl.service` | release `cc91b783` |
| `desk-evidence-release.conf` | `desk-evidence.service` | untracked copy `desk-wave3-safety-rc2` |
| `desk-admission-preview-release.conf` | `desk-admission-preview.service` | untracked copy `desk-wave3-safety-rc2` |

`trex-premarket.service` is a full rendered unit. The other ~20 `trex-*`/`desk-*`
units still run from the serving checkout with `PYTHONPATH=src`. No release
template covers them yet.

Rules every drop-in follows (the tests in `tests/unit/test_trex_product_release.py`
enforce them):

- Code is pinned to the release. `WorkingDirectory`, `PYTHONPATH` and every
  command use `<source>` and `<source>/.venv/bin/python`. `TREX_RELEASE_DIR`
  names the directory that holds the sealed `config.json`.
- `ExecStartPre=`, `ExecStart=` and `ExecStartPost=` are reset before they are
  set. A oneshot drop-in that does not reset them adds to the base unit's
  commands, so the old release would run first. The base `desk-qsl.service` and
  `trex-web.service` also carry an `ExecStartPre` that checks an older release.
- The only start gate is `tree_options.trex.premarket --check` from the same
  release (`verify_manifest`). A failed check fails the start.
  `desk-admission-preview` gets `TimeoutStartSec=120` because its 45 s base
  budget was sized for the preview alone.
- Sandboxed units never launch through `host-test`/`host-work`. That wrapper
  writes a payload under `/run/user`, which `ProtectHome=read-only` makes
  read-only, and its transient unit would not keep the caller's sandbox. Memory
  is capped with `Slice=host-work.slice` and `MemoryHigh`/`MemoryMax`. The two
  publisher units borrow the desk-evidence caps; the experiment publisher's cap
  has not been measured. Only the unsandboxed premarket runner keeps `host-test`.
- Desk drop-ins may add `ReadWritePaths` only inside the release's `desk_state`.
  The web drop-ins add none.
- The staging drop-in pins code only. It sets no state variable and no
  `ReadWritePaths`, so the base unit's staging roots stay in force. Never point
  staging at the release's production state.
- No command passes `--armed`. Admission stays preview-only.

Install each drop-in as `~/.config/systemd/user/<unit>.service.d/95-release.conf`
so it sorts after the existing `90-premarket-workspace.conf`. Drop-ins apply in
name order. Single-value settings and same-named `Environment` variables from the
later file win. List settings such as `ReadWritePaths` add up across drop-ins
unless reset with an empty assignment, so remove a superseded drop-in when you
switch. Reconcile existing drop-in ordering and explicit state
roots; do not assume a new drop-in overrides a later old one. Preserve Family Apps
operator/Tailnet ingress and existing filesystem restrictions. The web process
requires no write access to the evidence directory. The publisher owns snapshot
writes and its timer refreshes every 30 seconds; readers reject age over 120
seconds. Verify initial publication before switching the web service to it.

Test the generated units with `systemd-analyze --user verify` before installing.
Run the read-only inventory ([INVENTORY.md](INVENTORY.md)) before and after an
install. Each switched unit should then report `release` with the sealed
`release_head`, and no unit should report `untracked_copy`.
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
