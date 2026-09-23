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
