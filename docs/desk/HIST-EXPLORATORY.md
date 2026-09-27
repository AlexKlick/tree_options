# Exploratory historical options replay

This lane reads cached Massive/Polygon daily option VWAPs and the desk's
historical equity panel. It is separate from the sealed `DESK-BT-001` study:
the haircut is a scenario input, the cache may be incomplete, and no result
here changes a desk verdict, capital mandate, or broker execution setting.

Run from this checkout with the host benchmark admission launcher and full
log capture. Set `DESK_STORE` and `DESK_PAPER_DIR` to the owning data root
when running from a worktree:

```bash
DESK_STORE=/home/alexk/documents/tree_options/artifacts/desk-store \
DESK_PAPER_DIR=/home/alexk/documents/tree_options/artifacts/paper-trades \
~/.local/bin/host-benchmark .venv/bin/python scripts/run_desk_historical_replay.py \
  --cache /home/alexk/documents/tree_options/artifacts/massive-cache \
  --cache /home/alexk/documents/tree_options/artifacts/massive-cache-desk \
  --start 2025-01-01 --end 2025-06-30 --min-dte 30 --max-dte 90 \
  --max-loss 300 > /tmp/trex-historical-replay.log 2>&1
tail -n 40 /tmp/trex-historical-replay.log
```

Use `--names` to choose from the desk's 35-name chain universe; the default
includes all 35. Use `--signals` to select `xsmom_top3`, `pead_beat`, or both.
Use `--structures` to choose `long_call`, `call_debit`, and `put_credit`.
Use `--all-expiries` to include available weeklies; the default uses traded
monthlies. The data reader accepts 7–365 entry DTE and does not substitute
another contract when a leg is missing. One or more `--cache` directories
can be supplied; conflicting duplicate prices are dropped.

The signal and contract selection use only bars available at session D's
close. The exact selected legs are modeled as opened at the next session's
VWAP and closed at the earlier of 20 NYSE sessions or
the last session with at least 7 calendar DTE. Both sides pay the configured
per-leg VWAP haircut and $0.65 commission per leg at entry and exit. The
default 1% haircut is an assumption, not measured spread evidence. A trade
whose modeled maximum loss exceeds `--max-loss` is excluded and counted.

Each run writes a distinct JSON file under
`DESK_STORE/evaluations/historical-replay/` by default. It records the panel
and earnings hashes, the digest and file count of each cache set's consumed
responses, every evaluable modeled row, and counts of missing or excluded
rows. `GET /api/desk/historical-replays` projects the 12 newest summaries to
the Action model cockpit; it does not launch jobs or expose a trade action.

Win rates and returns are **modeled on the evaluable subset only**. Cached
daily VWAPs are neither executable quotes nor IBKR fills; missing bars can
bias the subset. Contract selection sees only contracts with decision-day
trade bars, and the sealed earnings calendar has no historical
announcement-time vintages. Overlapping trades are counted independently, so aggregate
P&L is not a $5,000 portfolio path and the $1,500 combined open-risk limit
is not tested here. This lane supplies research evidence for later governed
proposal review, not a measured live edge or broker authorization.
