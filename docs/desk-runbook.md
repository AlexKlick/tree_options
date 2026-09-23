# Options desk runbook (Wave 0: chain recorder + eod-equity)

Two systemd **user** timers, both `oneshot` in `host-work.slice`, both
idempotent per session. Neither places orders or seals cards.

| job | when (America/New_York) | does |
|---|---|---|
| `desk-chain` | Mon-Fri 17:45, 20:45, 23:45; Mon-Sat 06:30 and 12:30 (catch-up) | records the CBOE delayed chain (calls + puts) for the 35 optionable panel names |
| `desk-eod-equity` | Mon-Fri 16:40, 20:40; Tue-Sat 08:40 | extends the research panel (`fetch_ohlc.py`), computes XSMOM-TOP3 + PEAD beats, writes draft cards, pushes ntfy when a rule fires |

Both run `python -m tree_options.desk <command>` from the main checkout's
`.venv`. Manual runs: add `--session YYYY-MM-DD` (a closed NYSE session)
and/or `--dry-run`.

## Install / verify (operator)

```bash
cp ~/documents/tree_options/deploy/desk/desk-{chain,eod-equity}.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now desk-chain.timer desk-eod-equity.timer
systemctl --user list-timers 'desk-*'
# one manual pass each (record-chains is safe to repeat; eod-equity writes
# the research panel through fetch_ohlc.py, exactly like the old chain)
systemctl --user start desk-chain.service; journalctl --user -u desk-chain -n 20
systemctl --user start desk-eod-equity.service; journalctl --user -u desk-eod-equity -n 20
```

Checks: `artifacts/desk-store/manifest/<D>.json` shows `ok` for >= 33 of
35 symbols; `~/.local/state/trex-desk/signals/<D>.json` exists with
`panel_last_session == <D>`; the first eod-equity run gap-fills the panel
from 2026-09-15 (it stopped on 09-14). fetch_ohlc.py paces the vendor at
5 requests/min, so each session costs ~7.5 min (the first run ~1 h).

## Chain store (`artifacts/desk-store/`, `DESK_STORE` overrides)

- `chains/<D>/<SYM>.json.gz`: schema `desk-chain/1`, columnar
  (`occ exp right strike bid ask bid_size ask_size iv delta gamma theta
  vega rho theo oi volume last last_time`) + a header (session,
  source_as_of, fetched_at, underlying_quote, raw_sha256, n). Never
  rewritten.
- `chains/<D>/<SYM>.conflict.json.gz`: a later payload for the same
  session whose raw hash differs (only fetched with `--recheck`).
- `raw/<D>/<SYM>-<sha256>.json.gz`: vendor bytes, hash-addressed, written
  before the chain is published; newest 20 sessions kept. A recorded
  chain whose raw file vanished inside that window is re-fetched once and
  repaired (identical payload) or reported as a conflict (changed payload).
- `manifest/<D>.json`: per-symbol `ok|exists|conflict|stale|incomplete|missing|invalid|error`.
- `gaps.jsonl`: append-only; a session no run attempted (`sym "*"`), or a
  symbol never recorded before the next session's run (`missing`),
  `invalid`, `error`, `conflict`.

Validation (all must hold): the underlying's last trade is dated D, no
option traded after D, the modal option last-trade date is D, and
`source_as_of` (the payload's naive-UTC `timestamp`) is at or after D 16:15
ET. Completeness: both rights with >= 10 contracts each, >= 50% of rows
and >= 50% of expiries with a bid, else `incomplete` (retryable). The feed
is an overnight EOD snapshot (03:49 UTC observed), so evening slots are
usually `stale` and the 06:30 slot records.

## eod-equity state (`~/.local/state/trex-desk/`, `TREX_DESK_STATE` overrides)

- `stages/<D>/eod-equity.done.json`: the idempotency marker.
- `signals/<D>.json`: `xsmom {is_rebalance_day, fires, top3, scores,
  top3_skip21, conventions_agree, ...}`, `pead` (beats), `pead_evaluated`
  (every report whose first post-report session is D), `provenance`.
- `card-drafts/<D>-xsmom-top3.md`, `card-drafts/<D>-pead-<name>.md`:
  DRAFTS in the card template. Sealing stays manual
  (CRON-paper-engine.md step 6): number, seal time, move into
  `artifacts/paper-trades/`, LEDGER.md row + sha256.
- `outbox/<D>-eod-equity.{json,sending,sent,ambiguous,expired}`: the push
  outbox. `.json` = owed (quiet hours, judged at send time, or a failed
  send; dropped after 4 days); `.sending` is written before ntfy is called;
  `.sent` is the receipt (a rerun never resends). A `.sending` left by a
  crash becomes `.ambiguous`, is logged in `push-ambiguous.jsonl`, shows as
  `ambiguous_pushes=N` in that run's summary line, and is NEVER reposted.
- `logs/eod-equity-<date>.log`: fetch_ohlc.py output.
- `locks/<command>.lock`: one writer per command (both jobs).

The research panel is shared with manual `fetch_ohlc.py` runs: that script
merges under an exclusive flock on `artifacts/paper-trades/ohlc-panel.json.lock`
(unique temp files), and eod-equity reads under the same lock, shared
(exit 3 `panel_locked` after 300 s). Each daily extend re-fetches the last
5 stored sessions in the same request; a stored close off by > 0.5% means
the vendor's split adjustment moved, and that name's full history is
re-fetched and replaced (a `rebase <NAME>` provenance line).

XSMOM ranking convention: `top3` uses close(t)/close(t-273)-1, the
computation behind every PROTOCOL-XSMOM.md row (the research code never
applied the documented 21-session skip). The rule text's
close(t-21)/close(t-273)-1 reading is reported as `top3_skip21`; the
draft card flags it when the two disagree. The operator decides which one
seals. Offsets are fixed NYSE sessions; a name missing any session of the
window is excluded (`data_gaps`, flagged in the draft).

## Exit codes

| code | record-chains | eod-equity |
|---|---|---|
| 0 | >= 90% of symbols recorded (ok/exists/conflict) | done, already done, or not a session |
| 3 | not published (whole) yet (stale/incomplete), or another run holds the lock: timer retries | vendor lag, `--session` before its 16:15 ET cutoff, a held lock, or the panel lock busy |
| 1 | < 90% recorded and nothing retryable | fetch failure, panel missing/incomplete, earnings calendar unreadable, gap > 25 sessions |
| 2 | bad arguments (non-session, unclosed session, bad symbols) | bad arguments |

Units set `SuccessExitStatus=3`, so a retryable run is not a failed unit.

## Long-dated option capture (plan Step 0 item 8)

Daily Polygon bars for the desk's longer-dated options before the free
tier's rolling 2-year window takes them: **ATM +/-2 strikes, calls and puts,
monthly expiries 90-270 calendar DTE, the 35 optionable names**
(`desk/universe.py` CHAIN_UNIVERSE: the panel minus TQQQ/SQQQ), sampled at
**27 as_of dates** from 2024-09-27 to 2026-09-22.

- Cache: `artifacts/massive-cache-desk/`. Captures: `artifacts/desk-longdated-capture/`
  (`masters/`, `bars/`, `spot_proxy.json`, `capture_manifest.json`). The
  load-bearing `artifacts/massive-cache` is only READ (stage 0 copies master
  pages out of it); `artifacts/m4b-*`, `artifacts/bars*` and `data/bars` are
  never touched.
- Monthly = `--bars-expiries monthly-traded`: the third Friday, or the
  session before it when the exchange was closed that Friday. Polygon lists
  April 2025 (Good Friday), June 2026 and June 2027 (Juneteenth) on the
  Thursday; the calendar-only `monthly` filter would drop all three for
  every name.
- Cadence: one as_of every 4 weeks (Fridays from 2024-09-27, the first
  inside the window at launch; the holiday Fridays 2025-07-04 and 2026-07-03
  move to the Thursday before; the last is 2026-09-22, the latest complete
  session). Gaps are 25-29 days against a 181-day 90-270 DTE life, so every
  traded monthly from 2025-01-17 to 2027-06-17 is sampled inside its band
  (interior expiries 6-7 times; `tests/unit/test_desk_longdated_capture.py`
  pins it). Each pick's series runs from its as_of to expiry, so the ATM
  grid is re-anchored every 4 weeks without re-buying a contract's life. A
  2-week cadence would cost about 30% more series (21.0k vs 15.9k on the
  29-name subset) and push the run past about 25k requests.

### Launched 2026-09-23 17:17 ET (job `desk-longdated-capture`)

The job runs a frozen copy of commit `c03ab2f` (`git archive`) at
`artifacts/desk-longdated-capture-code/c03ab2f/`, so a moved or removed
worktree cannot change the code under a multi-day run:

    /home/alexk/.claude/scripts/detach.sh start desk-longdated-capture \
      --cwd /home/alexk/documents/tree_options/artifacts/desk-longdated-capture-code/c03ab2f \
      --timeout 345600 -- \
      /home/alexk/.local/bin/host-work run --profile build --wait 14400 -- \
      nice -n 10 bash /home/alexk/documents/tree_options/artifacts/desk-longdated-capture-code/c03ab2f/scripts/desk_longdated_capture.sh

`host-work` profile `build` (8 GiB cap, 2 GiB reserve) fits the process
(about 2.6 GB resident: every master is held in memory); `benchmark` would
reserve 16 GiB for four days. `scripts/desk_longdated_capture.sh` runs:

| stage | what | wire requests |
|---|---|---|
| 0 | `seed_massive_cache.py`: copy the coverage era's master pages (29 names x 24 as_ofs, 2,694 pages) byte-for-byte into the new cache | 0 |
| 1 | masters + spot for every as_of, `--bars 0`: the manifest note `atm-grid: --bars 0 caps the selection to 0 of N series` gives the exact series count | ~950 (249 masters + 35 spot) |
| 2 | the oldest as_of's bars first (their first bars sit nearest the 2-year edge) | ~1,600 |
| 3 | everything; stage 1-2 pages are cache hits | the rest |

Dry run (cache-only, zero requests, after stage 0): 696/945 masters
complete from the seeded cache, 249 masters and 35 spot series to fetch;
the grid cannot be sized there because no spot close is cached. Offline
sizing with the tool's own `select_atm_grid_bars` on the coverage-era
masters: 17,338 series for 29 names x 23 as_ofs (15,924 with calendar-only
monthlies). Extrapolated to 35 names x 27 as_ofs: **about 23.5k series plus
about 1k master/spot requests, about 24.4k wire requests: roughly 3.3 days
at the 5/min governor (12 s/request)**, inside the 96 h detach timeout. A
bar request that starts before the window edge is truncated to the edge by
the vendor (probed: a 2024-09-13 start returned its first bar on
2024-09-23), never refused, so a late fetch loses days, not series.

### Progress and resume

- Progress: `detach.sh status desk-longdated-capture`; the capture prints
  its manifest only at the end of each stage, so between stages count
  `artifacts/massive-cache-desk/*.json` (2,694 seeded at launch).
- Resume after any interruption (reboot, host-work pressure kill rc 143,
  timeout): re-run the same `detach.sh start ...` command (the name is free
  once the job has ended; it refuses a double start). Every page already
  fetched is a cache hit and costs nothing; stage 0 re-seeds as a no-op.
  Cache writes are atomic (staging file + rename), so a kill never leaves a
  half-written entry.
- A SIGTERM skips the manifest's `finally`: until a re-run finishes,
  `capture_manifest.json` and `bars/` describe the last completed stage,
  not the cache. Stage 3 writes `bars/` only at its end.
- Done: `desk-longdated-capture.done` carries rc; the log ends
  `== done ... seed=0 sizing=0 oldest=0 full=0`. `PARTIAL: x/y masters
  complete` on stderr means some masters errored; read the manifest notes.
- Known limit: a series for a contract still alive at fetch time ends at
  the fetch date and is cached as such; the chain recorder (D1) carries the
  forward data.
