# Options desk runbook (Wave 0: chains + eod-equity; Wave 1: indices + events)

Four systemd **user** timers, all `oneshot` in `host-work.slice`, all
idempotent. None places orders or seals cards.

| job | when (America/New_York) | does |
|---|---|---|
| `desk-chain` | Mon-Fri 17:45, 20:45, 23:45; Mon-Sat 06:30 and 12:30 (catch-up) | records the CBOE delayed chain (calls + puts) for the 35 optionable panel names |
| `desk-eod-equity` | Mon-Fri 16:40, 20:40; Tue-Sat 08:40 | extends the research panel (`fetch_ohlc.py`), computes XSMOM-TOP3 + PEAD beats, writes draft cards, pushes ntfy when a rule fires |
| `desk-indices` | Mon-Fri 19:00 | CBOE index histories (VIX VIX9D VIX1D VIX3M VIX6M VIX1Y VVIX SKEW VXN RVX GVZ VXAPL VXAZN VXGOG) + FRED DTB3 |
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
  whenever a vendor **revision** (a changed or dropped past row) replaces
  it; `changes.jsonl` logs every such row (`old`, `new`, `backup`).
- `provenance.jsonl`: one line per source per run (`fetched_at`, `sha256`,
  `rows`, `last_date`, `status`, `lagging`).
- A payload that ends before the stored history, or drops more than 3
  stored rows, is `invalid` and the store is kept (truncated body).
- Every request is time-bounded (30 s) and isolated per source. A source
  a run could not refresh gets a `gaps.jsonl` line (`at`, `source`,
  `status`, `detail`). A transport failure (timeout, 5xx) is a soft
  `error`: exit 3, and the next run re-fetches the whole history. FRED
  stalls requests that carry no `Accept` header (two probes on 2026-09-23
  each hung ~25 s; with `Accept: */*` it answered in 0.5 s), so DTB3 is
  fetched with a plain tool User-Agent plus `Accept: */*`.
- Lag: CBOE sources must carry the latest completed session, DTB3 may
  trail it by one (FRED publishes a day late). CBOE rewrote its files at
  ~21:50 ET for 09-22, so the 19:00 slot usually exits 3 (`lagging`) and
  stores through the prior session; the next evening catches up. Add a
  morning slot to the timer if same-night freshness ever matters.

## Events (`update-events`, weekly; `seal-macro`, operator)

Macro calendar: `data/desk/events/macro-2026-2027.json` (tracked) +
`.sha256` + `MACRO-SEALS.md` (append-only provenance; `DESK_EVENTS_DIR`
overrides). `events.load_macro` refuses a file whose hash does not match.

- FOMC: parsed from the Fed's calendar page (16 meetings 2026-2027, `sep`
  flags the projection meetings). `update-events` re-reads the page every
  Saturday; a difference writes `~/.local/state/trex-desk/events/macro-drift.json`
  and exits 1 (review, then reseal).
- CPI, NFP: **empty; operator/agent entry required** (bls.gov answers
  scripts with 403; nothing is entered from memory). Add
  `{"date", "source", "entered_by"}` items to the arrays by hand, then
  reseal in a worktree and commit:
  `python -m tree_options.desk seal-macro --from 2026-01-01 --to 2027-12-31`
  (fetches the Fed page; `--fomc-html FILE --fetched-on D` uses a saved
  copy). Hand items are carried; a new MACRO-SEALS.md row is appended.
- OpEx (third Friday, else the session before; 2026-06-18 for Juneteenth)
  and VIX expiry (30 days before the next month's OpEx; 2026-05-19) are
  computed on the trex NYSE calendar.

Earnings timing: `artifacts/paper-trades/earnings-timing.json`
(`{name: {date: {timing, source, fetched_at, status}}}`, `DESK_PAPER_DIR`
overrides). The sealed `earnings-calendar.json` next to it is never
written.

- `estimated`: Nasdaq's calendar for the next 65 sessions
  (`time-pre-market` bmo, `time-after-hours` amc, `time-not-supplied`
  unknown). An estimate the vendor stops listing on a day it answered with
  rows is dropped; an empty answer drops nothing. Estimated dates only
  ever BLOCK a trade (`upcoming_earnings(...).blocker_only`), never
  trigger PEAD.
- `confirmed`: EDGAR 8-K item 2.02 since 2021, timed by
  `acceptanceDateTime` (EDGAR's digits are Eastern despite the "Z"):
  before 09:30 bmo, from 16:00 amc, else unknown. Needs `DESK_SEC_UA`
  (`~/.config/trex/desk-sec.env`); unset, EDGAR is skipped and the line
  says `sec_ua_missing`. A run where > 10% of acceptance times fall outside
  EDGAR's 06:00-22:00 ET hours is dropped whole (`edgar=tz_suspect`,
  exit 1). A confirmed entry is never downgraded to an estimate.
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
| 0 | every source stored and current | all fetched, macro seal and Fed page agree (`sec_ua_missing` alone is 0) |
| 3 | a vendor lags the latest session, or a transport `error` (timeout/5xx) left a soft gap; or the lock is held | some Nasdaq days / EDGAR names failed, or the Fed page was unreachable |
| 1 | a vendor file is gone (404, `missing`) or bad/shrunk (`invalid`), or nothing was stored | macro drift, broken seal, unreadable Fed page, `tz_suspect`, no Nasdaq day answered, timing file unwritable |
| 2 | bad `--sources` | bad `--horizon` |

Units set `SuccessExitStatus=3`, so a retryable run is not a failed unit.
