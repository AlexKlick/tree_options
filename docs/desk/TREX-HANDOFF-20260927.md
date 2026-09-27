# TREX continuation handoff, 2026-09-27

## Objective and custody

Continue the governed TREX action-model integration toward a supervised IBKR
paper-trading path. The current pull request is a research and proposal surface;
it does not authorize or send orders. The source packet is
`/home/alexk/pop-deck-uploads/2026-09/TREX-Governed-Adaptive-Action-Model-20260926-9472c2.zip`.

- Repository: `/home/alexk/documents/tree_options`.
- Isolated worktree: `/home/alexk/documents/tree_options-worktrees/governed-action-foundation`.
- Branch: `feat/governed-action-foundation`, based on `main` at
  `8a9d073c1ce8beb640464f5ca691d6e183d84502`.
- Implementation head before this handoff: `498cdeab0dda65d9c62d988faea2b01bd1b3bd7c`.
  The worktree was clean and that head matched `origin/feat/governed-action-foundation`.
  Run `git rev-parse HEAD` for the handoff commit's exact head.
- Draft PR: <https://github.com/AlexKlick/tree_options/pull/42>. It was open,
  mergeable, and had no hosted status checks when inspected. No merge or deploy
  occurred.

## Completed work

- Added a validated, proposal-only action graph and read-only Action Model
  cockpit view. `execution_authorized` remains false. Capital profiles use
  intended capital separately from paper-account equity. The modelled target
  is $5,000, with a $300 maximum loss per trade and $1,500 combined open-loss
  reservation. The paper canary is screened against a bound account, owner
  epoch, vertical geometry, price, and risk policy; screening is not a permit.
- Added exploratory daily-bar replay, overlap-aware portfolio scenarios, a
  blind historical model-selection exercise, source-checked strategy and
  systems-graph reports, and read-only API summaries. The blind exercise used
  Z.ai, Z.ai Flash, and MiniMax; its single split and daily VWAP proxies do
  not establish an edge or fill quality. See
  `docs/desk/HISTORICAL-MODEL-GAME-20260927.md` and
  `docs/desk/STRATEGY-REVIEW-20260927.md`.
- Added a minute-bar historical action graph with eight scheduled decisions
  per regular session and two overlapping three-month windows. The frozen
  local bundle has 72/72 selected option series and 339,457 traded-minute
  bars from 2026-05-25 through 2026-09-25. The source bundle, selection,
  one-snapshot three-model pilot, and blind-game scoreboard exist under
  `/home/alexk/documents/tree_options/artifacts/desk-store/evaluations/`.
  The windows overlap by 43 sessions, so their results are not independent.
  See `docs/desk/INTRADAY-ACTION-GRAPH-20260927.md` for hashes and limitations.
- Guarded the minute-bar wire capture against simultaneous use of the shared
  Massive structural capture. Added replay source-custody and month-end
  window checks in the final Python-only iteration.

## Evidence and limits

- `/tmp/trex-pr-focused-tests.log`: 45 passed, 0 failed, exit 0.
- `/tmp/trex-pr-iteration-tests.log`: 9 passed, 0 failed, exit 0 after the
  source-custody and month-end changes.
- `/tmp/trex-pr-iteration-ruff.log`: `All checks passed!`, exit 0.
- `/tmp/trex-pr-web-check.log`: 186 passed across 37 files, TypeScript check,
  production build, and bundle check, exit 0. This ran before the later
  Python-only correction; it is not an exact-head browser or deployment proof.
- `/tmp/trex-pr-rolling-sweep.log` contains only `queued at position 8`.
  It is not a completed benchmark or pass/fail result. A later guard status
  check showed no active or queued jobs; no full-window sweep artifact was
  verified. Do not report a strategy winner from the pilot or older replay.
- Raw minute bars are traded-minute closes, not synchronized executable
  bid/ask quotes or two-leg fills. No historical spread costs, slippage,
  assignment, live stops, broker-paper outcome, or high-win probability was
  established. No IBKR order was sent.

## Host memory and RAID5 follow-up

The later Pop Deck screenshot showed about 57% RAM used. The fleet collector
calculates this from `MemTotal - MemAvailable`; a live inspection found about
27 GiB available and about 16 GiB swap occupied. zram is the first swap tier
(priority 1000); the existing 4 GiB encrypted root swap is priority -2.
`host-work` uses an 8 GiB host reserve, a 32 GiB batch cap, and a 16 GiB RAM
plus 2 GiB swap benchmark profile. The earlier sweep queued while other
admitted jobs held reservations. The guard was not changed.

The RAID5 array is `/dev/md0`, ext4, mounted at
`/media/alexk/RAID5_Storage`, healthy with all 3 members present and about
892 GiB free (44% used during inspection). It does not currently back swap.
The signed observer's `DEGRADED` tile reflects root-capacity and backup
verification warnings, not proof of RAM exhaustion; the root filesystem was
77% used by `df` at inspection. No RAID5 swap file, crypt mapping, unit,
`/etc/crypttab`, or `/etc/fstab` change was made.

The user requested a dedicated RAID5 swap overflow pool. Activation needs
root privileges. In this agent session `sudo -n true` exited 1 with
`The "no new privileges" flag is set`, so live setup is blocked. Next
operator-root work should use a fixed-size, encrypted, low-priority pool
behind zram and existing encrypted swap, verify mount/array identity before
activation, and test boot/shutdown ordering and rollback. Disk swap is an
overflow safety tier; it does not increase RAM or by itself satisfy the
benchmark admission rule. Do not create an unencrypted swap file on RAID5.

## Next actions

1. Recheck branch/worktree ownership, `git status --short --branch`, active
   jobs, and `/home/alexk/.local/bin/host-work status --json`. Keep the
   structural capture and other agents' work untouched.
2. Run the two-window, five-policy replay once under the required
   `~/.local/bin/host-benchmark` wrapper, each output at a new path beneath
   `.../evaluations/intraday-graph/`; capture each full command log under
   `/tmp/` and read its final summary. The input bundle is
   `/home/alexk/documents/tree_options/artifacts/desk-store/evaluations/intraday-graph/20260927-v1/minute-bars-4mo-expanded.json` and the
   runner is `scripts/run_desk_intraday_graph.py` with `--start 2026-05-26
   --end 2026-09-24 --policy <policy> --out <new-path>`. Use `--help` for
   exact CLI arguments. Preserve any admission exit 75 as blocked.
3. Examine coverage, exclusions, overlapping-window dependence, and per
   policy risk results before discussing a strategy. Acquire historical
   executable quotes and more untouched periods before any promotion claim.
4. Build the supervised paper path around the existing `trex.ibkr`,
   `trex.enter`, `trex.monitor`, and execution record owners. Require an
   expiring account-bound mandate, durable intent and outbox, fresh quote and
   risk checks, and reconciliation of uncertain submission and fills.
5. Prepare and activate RAID5 overflow swap only in an unrestricted root
   session with encrypted storage, lower priority, bounded size, and explicit
   live/boot verification. Keep the existing zram and cryptswap intact.

No production deployment, merge, browser acceptance, live provider/model
retest, broker-paper trial, or RAID5 swap activation is claimed here.
