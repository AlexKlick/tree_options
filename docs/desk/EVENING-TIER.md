# The evening observation tier (chains/clock=evening/)

One post-close observation per session per name, taken at **18:05 ET on
weekdays** (`deploy/desk/desk-chain-evening.{service,timer}`, SHIPS
UNINSTALLED like its siblings).

- **Namespace.** `chains/clock=evening/<D>/<SYM>.json.gz` and
  `manifest/clock=evening/<D>.json`, header `clock: "evening"` — the same
  per-tier layout the A2 clock tier uses (`store.chain_path` /
  `store.manifest_path` with `clock="evening"`), additive to `desk-chain/1`
  with no schema bump. The eod namespaces (`chains/<D>/`,
  `manifest/<D>.json`, `gaps.jsonl`, `raw/`) and every `clock=<HH:MM>/`
  namespace are untouched by an evening run. `clock-coverage` shows the
  evening row naturally (it globs `manifest/clock=*/`).
- **Freshness.** The payload's own `source_as_of` must be **at or after
  the session close (16:00 ET, early-close-aware via the calendar)** of
  that session date. This mirrors the eod tier's post-close validation
  (`store.validate`): dated-D options evidence, the underlying last traded
  within the close tolerance, and the completeness floors all still apply;
  the only difference is the boundary — the session close itself, not the
  per-class options close.
- **Single pass.** The evening run is one paced sweep, no SPY probe, no
  stale-retry passes (those are clock-tiers-only: they exist to catch a
  publication rolling 1–4 min behind a 10-minute clock window; by evening
  there is no window to race). A stale evening verdict is a frozen feed,
  and the overnight eod recorder remains the safety net.

## Why (the persistent late cohort)

Day-1 clock-tier data (2026-10-01) shows a cohort — CRM, DIS, LLY, PEP,
PG, V, XLE, XLV — stale at **every** one of the eight decision clocks:
their CBOE delayed publications never roll inside any `[clock,
clock+10 min)` window, so the clock tier's validate branch refuses them
all day by design. By evening those same publications have fully rolled.

## What it is NOT

- **Not a clock backfill.** Backfilling a clock namespace from a later
  fetch would write post-window data into a point-in-time namespace the
  store's validate branch refuses by design. The evening tier keeps its
  own namespace and its own observation time instead.
- **Not decision-time data.** 18:05 ET is after the close; an evening
  observation is post-close evidence about the settled session, never an
  input to that session's decisions. The decision-time record stays the
  eight clock tiers.

## Ops

`python -m tree_options.desk record-chains --clock evening` (timer
`desk-chain-evening.timer`, Mon..Fri 18:05 America/New_York,
`Persistent=true` — a machine that was asleep fires late, and a
later-evening fire still satisfies the at-or-after-close rule for the
same session, so the catch-up stays honest). exit 0 when ≥ 90% recorded,
3 on stale names (benign, see above), 1 otherwise. Coverage:
`clock-coverage --session <D>` prints the `clock evening: ok N stale M`
row alongside the clock rows.
