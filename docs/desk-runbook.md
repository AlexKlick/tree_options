# Options desk runbook (Wave 0: chains + eod-equity; Wave 1: indices + events)

Four systemd **user** timers, all `oneshot` in `host-work.slice`, all
idempotent. None places orders or seals cards.

| job | when (America/New_York) | does |
|---|---|---|
| `desk-chain` | Mon-Fri 17:45, 20:45, 23:45; Mon-Sat 06:30 and 12:30 (catch-up) | records the CBOE delayed chain (calls + puts) for the 35 optionable panel names |
| `desk-eod-equity` | Mon-Fri 16:40, 20:40; Tue-Sat 08:40 | extends the research panel (`fetch_ohlc.py`), computes XSMOM-TOP3 + PEAD beats, writes draft cards, pushes ntfy when a rule fires |
| `desk-indices` | Tue-Sat 06:40 | CBOE index histories (VIX VIX9D VIX1D VIX3M VIX6M VIX1Y VVIX SKEW VXN RVX GVZ VXAPL VXAZN VXGOG) + FRED DTB3 |
| `desk-events` | Sat 10:00 | earnings timing (Nasdaq estimates; EDGAR 8-K 2.02 with `DESK_SEC_UA`) + the sealed macro calendar's Fed-page check |

All run `python -m tree_options.desk <command>` from the main checkout's
`.venv`. Manual runs: add `--session YYYY-MM-DD` (a closed NYSE session;
chains/eod-equity) and/or `--dry-run`.

## Install / verify (operator)

Wave 1 units (files only until the operator installs them):

```bash
cp ~/documents/tree_options/deploy/desk/desk-{indices,events}.{service,timer} ~/.config/systemd/user/
# optional, for EDGAR confirmations (one line, never committed, never printed):
#   ~/.config/trex/desk-sec.env:  DESK_SEC_UA=<name> <contact e-mail>
systemctl --user daemon-reload
systemctl --user enable --now desk-indices.timer desk-events.timer
systemctl --user start desk-indices.service; journalctl --user -u desk-indices -n 20
systemctl --user start desk-events.service; journalctl --user -u desk-events -n 20
```

Wave 0 units:

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
option traded after D, the modal option last-trade date is D,
`source_as_of` (the payload's naive-UTC `timestamp`) is at or after the
symbol's **options close** on D, and the underlying last traded within 5
minutes of the equity close. Completeness: both rights with >= 10
contracts each, >= 50% of rows and >= 50% of expiries with a bid, else
`incomplete` (retryable). The feed is an overnight EOD snapshot (03:49 UTC
observed), so evening slots are usually `stale` and the 06:30 slot records.

Options close per symbol (`universe.LATE_CLOSE_OPTIONS`, `sessions.options_close`):

- 16:15 ET for the late-close classes SPY QQQ IWM SMH SOXX XLE XLF XLV GLD
  (the Nasdaq "Options Market Hours" 9:30-16:15 list, fetched 2026-09-23;
  the 2026-09-22 chains show SPY QQQ IWM XLE XLF options printing until
  16:14:59), and for any symbol whose options printed after the equity close;
- 16:00 ET for every other name (single stocks);
- early-close sessions use the calendar's close: 13:00, and 13:15 for the
  late-close classes.

XLV on 2026-09-22 stays refused, correctly: its snapshot was stamped 16:01:18
ET, before XLV's 16:15 options close, and its content was frozen at 15:46
(underlying last trade 15:46:14; CBOE still served that payload on 09-23 at
16:55 ET). XLV becomes `missing` for 09-22 once the next session records.

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

## Index histories (`artifacts/desk-store/indices/`, `DESK_STORE` overrides)

Sources (all 14 CBOE URLs verified 2026-09-23 with one ranged GET each):
`https://cdn.cboe.com/api/global/us_indices/daily_prices/<X>_History.csv`
and `https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3`.

- `<X>.csv`: `date,open,high,low,close`, ISO dates, the vendor's value
  strings verbatim (never floats). VVIX, SKEW and GVZ serve one value a
  day: close only, open/high/low empty. DTB3 keeps FRED's no-rate days as
  an empty close (e.g. 2026-09-07), never filled.
- `<X>.<D>.bak` (`-2`, `-3`, ... on the same day): the prior file, kept
  whenever a vendor **revision** (a changed past value) replaces it;
  `changes.jsonl` logs every such row (`old`, `new`, `backup`).
- `provenance.jsonl`: one line per fetched source per run (`fetched_at`,
  `sha256`, `rows`, `last_date`, `status`, `lagging`).
- Refused (`invalid`, exit 1, the stored file kept whole): a payload that
  drops ANY stored date or ends before the stored history (a truncated
  body), and one with a row dated after the fetch's ET date. To accept an
  intentional vendor deletion, move the stored `<X>.csv` aside; the next
  run records the history as `new`.
- A body shorter than its `Content-Length` is refused before parsing
  (`IncompleteBody`, a transport `error`): a cut connection never becomes
  a "revision".
- Every request is time-bounded (30 s) and isolated per source. A source
  a run could not refresh gets a `gaps.jsonl` line (`at`, `source`,
  `status`, `detail`). A transport failure (timeout, 5xx) is a soft
  `error`: exit 3, and the next run re-fetches the whole history. FRED
  stalls requests that carry no `Accept` header (two probes on 2026-09-23
  each hung ~25 s; with `Accept: */*` it answered in 0.5 s), so DTB3 is
  fetched with a plain tool User-Agent plus `Accept: */*`.
- Lag: CBOE sources must carry the latest completed session, DTB3 may
  trail it by one (FRED publishes a day late). CBOE rewrote its files at
  ~21:50 ET for 09-22, so the timer runs at 06:40 ET, Tue-Sat (an evening
  slot only ever saw the prior session). A source already stored through
  the latest completed NYSE session (within its lag) is `current` and not
  fetched, so the morning after an exchange holiday makes no request;
  `--force` fetches anyway.

## Events (`update-events`, weekly; `seal-macro`, operator)

Macro calendar: `data/desk/events/macro-2026-2027.json` (tracked) +
`.sha256` + `MACRO-SEALS.md` (append-only provenance; `DESK_EVENTS_DIR`
overrides). `events.load_macro` refuses a file whose hash does not match.

- FOMC: parsed from the Fed's calendar page (16 meetings 2026-2027, `sep`
  flags the projection meetings). A year panel without its footer (a cut
  page) does not parse, and every year the range touches must show all 8
  scheduled meetings, so a truncated page can never be sealed.
  `update-events` re-reads the page every Saturday; a difference writes
  `~/.local/state/trex-desk/events/macro-drift.json` and exits 1 (review,
  then reseal).
- CPI, NFP 2026: 12 + 12 release dates (08:30 ET, with reference month),
  hand-entered 2026-09-23 from bls.gov (`schedule/news_release/cpi.htm`,
  `empsit.htm` and `schedule/2026/home.htm`: 24/24 agree; bls.gov answers
  scripts with 403, so these are read by a person/agent, never fetched).
  2027: **not yet published by BLS**; the file's `todo` says so and
  `events.macro_gaps()` returns `cpi 2027`, `nfp 2027`. To add them: put
  `{"date", "source", "entered_by"}` items (optional `period` YYYY-MM,
  `time_et` HH:MM) into the arrays by hand, then reseal in a worktree and
  commit:
  `python -m tree_options.desk seal-macro --from 2026-01-01 --to 2027-12-31`
  (fetches the Fed page; `--fomc-html FILE --fetched-on D` uses a saved
  copy; `--gap-note TEXT` words the todo for years still missing). Hand
  items are carried; a new MACRO-SEALS.md row is appended. NFP 2026-04-03
  is Good Friday: a release on an exchange holiday.
- OpEx (third Friday, else the session before; 2026-06-18 for Juneteenth)
  and VIX expiry (30 days before the next month's OpEx; 2026-05-19) are
  computed on the trex NYSE calendar.

Earnings timing: `artifacts/paper-trades/earnings-timing.json`
(`{name: {date: {timing, source, fetched_at, status}}}`, `DESK_PAPER_DIR`
overrides). The sealed `earnings-calendar.json` next to it is never
written.

- `estimated`: Nasdaq's calendar for the next 65 sessions
  (`time-pre-market` bmo, `time-after-hours` amc, `time-not-supplied`
  unknown). A day counts as answered only with `status.rCode` 200 and a
  `data.rows` field (`null` is the real empty day); an application error
  or schema drift is a failed day. An estimate the vendor stops listing on
  a day it answered with rows is dropped; an empty answer drops nothing.
  Estimated dates only ever BLOCK a trade
  (`upcoming_earnings(...).blocker_only`), never trigger PEAD.
- `confirmed`: EDGAR 8-K item 2.02 since 2021, timed by
  `acceptanceDateTime` (EDGAR's digits are Eastern despite the "Z"):
  before 09:30 bmo, from the session's actual close (16:00, 13:00 on an
  early close) amc, else unknown. Needs `DESK_SEC_UA`
  (`~/.config/trex/desk-sec.env`); unset, EDGAR is skipped and the line
  says `sec_ua_missing`. A run where > 10% of acceptance times fall outside
  EDGAR's 06:00-22:00 ET hours (any batch size) is dropped whole
  (`edgar=tz_suspect`, exit 1). A tracked name without a CIK, a skipped or
  failed submissions page, or an unreadable stamp makes the run `partial`
  (exit 3); no resolvable name at all is `error`. A confirmed entry is
  never downgraded to an estimate.
- Readers: `events.upcoming_earnings(name, from, to)`,
  `events.macro_events(from, to)` (raises outside the sealed range),
  `events.etf_holding_reports(etf, from, to)` (SMH SOXX XLE XLV XLF QQQ;
  `uncovered` lists holdings with no earnings data).

## Exit codes

| code | record-chains | eod-equity |
|---|---|---|
| 0 | >= 90% of symbols recorded (ok/exists/conflict) | done, already done, or not a session |
| 3 | not published (whole) yet (stale/incomplete), or another run holds the lock: timer retries | vendor lag, `--session` before its 16:15 ET cutoff, a held lock, or the panel lock busy |
| 1 | < 90% recorded and nothing retryable | fetch failure, panel missing/incomplete, earnings calendar unreadable, gap > 25 sessions |
| 2 | bad arguments (non-session, unclosed session, bad symbols) | bad arguments |

| code | record-indices | update-events |
|---|---|---|
| 0 | every source stored and current (incl. skipped as `current`) | all fetched, macro seal and Fed page agree (`sec_ua_missing` alone is 0) |
| 3 | a vendor lags the latest session, or a transport `error` (timeout/5xx/short body) left a soft gap, even for every source; or the lock is held | some Nasdaq days failed, EDGAR `partial`/`error`, or the Fed page was unreachable |
| 1 | a vendor file is gone (404, `missing`) or bad/shrunk/future-dated (`invalid`), or an empty invocation | macro drift, broken seal, unreadable Fed page, `tz_suspect`, no Nasdaq day answered, timing file unwritable |
| 2 | bad `--sources` | bad `--horizon` |

Units set `SuccessExitStatus=3`, so a retryable run is not a failed unit.

## Econometrics: IV history, forecasts, features (Wave 1 D2/D4; manual, no timer yet)

These commands read inputs only; they write under `DESK_STORE`.

- **Inputs:** the chain store, the panel (under its shared flock), the sealed earnings calendar, `earnings-timing.json`, `indices/<X>.csv` and `indices/DTB3.csv` in the stored format written by `record-indices`, and the read-only Polygon cache.
- **Studies:** `docs/desk/IVHIST-001.md` and `docs/desk/FORECAST-001.md`, with their `-results.md` files.
- **Resources:** run heavy passes under `host-work` (profile `test`). `ivhist-build` scans the cache in about 3 minutes with a peak of about 0.5 GB.

```bash
# the IV history (after record-indices has stored DTB3), then its benchmark
python -m tree_options.desk ivhist-build          # -> iv-history/vwap_atm.json
python -m tree_options.desk ivhist-001            # -> iv-history/IVHIST-001-verdict.json
# a session's surface features (after record-chains; needs the panel through D)
python -m tree_options.desk features --session 2026-09-22   # -> features/<D>.json
# FORECAST-001 scoring (the forward monitoring re-uses it) and its disclosed
# earnings-coverage sensitivity figure
python -m tree_options.desk forecast-001 [--out DIR]
python -m tree_options.desk forecast-001 --coverage-sensitivity [--out DIR]
```

**Features: what to read.**
- `forecast.source` follows `har.FORECAST_001_VERDICT` (PASS, so `har`).
- `forecast.schedule` is one of `complete`, `incomplete`, `unavailable` or `n/a`. A reporter whose known report dates, sealed plus `earnings-timing.json`, do not reach past the 20-session window is `degraded`: its HAR VRP is withheld (`vrp_withheld`), and `vrp_rv22` is still shown. Run `update-events` so the timing file carries the Nasdaq estimates.
- `iv_rank.history_label` carries the IVHIST-001 label. It is `unvalidated` when the verdict file scored a different `vwap_atm.json` than the one loaded: a rebuilt history needs a fresh `ivhist-001`.
- `earnings.implied_move` is null, with a reason, when the two-expiry split is infeasible or a second report falls before the second expiry.

**Sessions** come from the trex calendar. Confirmed closures such as 2025-01-09 are dropped there, through `closure_overrides` in `scripts/gen_trex_calendar.py`. A missing bar is never read as a closure.

**The sealed IVHIST-001 run** read raw vendor snapshots. To reproduce it, point `DESK_STORE/indices` at those files and add `--raw-snapshots` to `ivhist-build` and `ivhist-001`.

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
  timeout) FROM THE CURRENT CODE, not the `c03ab2f` copy. The launched copy
  predates the Codex review fixes: the resolved-path write guard, stopping
  when the seeder refuses, and the 13 s cooldown before every wire stage.
  Its dirs are not symlinks, and a 429 at a stage transition is retried
  (4 attempts, each at least 12 s after the last). Freeze the fixed commit
  and start it under the same name (free once the job has ended; it
  refuses a double start):

      SHA=$(git -C /home/alexk/documents/tree_options rev-parse --short HEAD)  # once merged
      SNAP=/home/alexk/documents/tree_options/artifacts/desk-longdated-capture-code/$SHA
      mkdir -p "$SNAP" && git -C /home/alexk/documents/tree_options archive "$SHA" | tar -x -C "$SNAP"
      /home/alexk/.claude/scripts/detach.sh start desk-longdated-capture --cwd "$SNAP" \
        --timeout 345600 -- /home/alexk/.local/bin/host-work run --profile build --wait 14400 -- \
        nice -n 10 bash "$SNAP/scripts/desk_longdated_capture.sh"

  (Before the merge, archive the branch commit from the worktree the same
  way.) Every page already fetched is a cache hit and costs nothing; stage 0
  re-seeds as a no-op. Cache writes are atomic (staging file + rename), so
  a kill never leaves a half-written cache entry. The trex calendar fix
  (2025-01-09 closure override) does not touch the capture: its only
  calendar use is the monthly-traded third-Friday check.
- The CACHE is the durable truth. The exported `masters/`, `bars/`,
  `spot_proxy.json` and `capture_manifest.json` are plain (non-atomic)
  writes: after a kill they can be stale or torn until a re-run rebuilds
  them from the cache. A SIGTERM also skips the manifest's `finally`, and
  stage 3 writes `bars/` only at its end.
- Done: `desk-longdated-capture.done` carries rc; the log ends
  `== done ... seed=0 sizing=0 oldest=0 full=0`. `PARTIAL: x/y masters
  complete` on stderr means some masters errored; read the manifest notes.
- Known limit: a series for a contract still alive at fetch time ends at
  the fetch date and is cached as such; the chain recorder (D1) carries the
  forward data.

## Playbook and conditions (Wave 2 D5; no timer yet)

- `data/desk/playbook/v2.toml` is the ACTIVE version; `v1.toml` stays as
  sealed history. Each has a `.sha256` sidecar and exactly one row in the
  append-only `SEALS.md`, and its digest is pinned in
  `desk.playbook.APPROVED`; `load_playbook()` refuses any mismatch (a
  coordinated edit of TOML + sidecar + log still fails). Never edit a
  version: write `v3.toml`, seal it once from a worktree with
  `desk.playbook.seal_playbook(path, basis=...)` (it validates, refuses
  other bytes under a sealed name, appends the row), then pin its digest in
  `APPROVED` and move `ACTIVE_FILE` in the same reviewed change.
  `DESK_PLAYBOOK_DIR` overrides the directory (tests pin it to tmp).
- v2 vs v1: R1 (XSMOM call debit spread) may match while the vol state is
  NOT_EVALUABLE (operator ruling 2026-09-23); PEAD incremental drift 0
  (Codex P2-4); R6 declares the actual-strike protective rule
  (`desk.playbook.require_protective`, Codex P2-5).
- `desk.regime.conditions_at(...)` computes each name's conditions for a
  session from the features files (`DESK_STORE/features/<D>.json`), the
  signals file, the stored VIX/VIX3M, the report schedule and the sealed
  macro calendar; `match_rows` / `regime_doc` give the rows each name meets
  and why the others are unmet. It is pure: the pipeline wiring comes with
  the miner.
- Vol state warm-up: `NOT_EVALUABLE` until a name has 120 evaluable
  chain-source sessions in the trailing 252 (2027-03-16 at the earliest
  with no recorder gaps); R2 and R4 match nothing until then, R6 waits on
  the same warm-up for its steep-contango percentile, and R1 matches with
  a note (v2). A features document counts only when its own `session`,
  chain provenance, forecast horizon (20) and `fit_through` (before the
  session) check out.
- `docs/desk/DESK-BT-001.md` + `DESK-BT-001-AMENDMENT-1.md` (entry on the
  next session after the signal) are sealed and NOT run: earliest run
  2026-10-20 (after 20 recorded chain sessions).
