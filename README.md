# tree_options

Point-in-time equity-signal research implemented through liquid, defined-risk
option positions. **M0 milestone only: protocol and invariant harness.** No
vendor data ingestion, no model training, no backtest results exist in this
repo yet, by design.

The engineering priority is not model sophistication. It is preventing future
information, historical-universe reconstruction errors, fantasy fills, and
trial-selection bias from entering a result. Every guard in this repo fails
closed: when data is missing, stale, crossed, or from the future, the answer
is a rejection with a reason code — never a silent drop.

## The 14 invariants

The frozen protocol (`research_protocol.yaml`) enumerates them as INV-01..INV-14.
Summary: point-in-time provenance on every feature; `available_at <= decision_at`;
filing/publication timing (never fiscal period end) for fundamentals and insider
filings; same-session rows stay in the same split; train labels purged against
test intervals; all fitting inside training data; delisted securities in the
historical universe; only contracts that existed at decision time; no same-close
fills from close-derived features; executable-side fills (long entry at ask,
exit at bid); exact cash/position conservation; trials registered before
outcomes are viewed; artifacts stamped with git SHA + hashes + trial ID.

## Gate (local — this repo has no CI and will never have any)

```bash
uv sync --locked
uv run ruff check src tests scripts
uv run pytest
uv run python scripts/mutate.py   # every listed mutant must be KILLED
bash scripts/m0_gate.sh           # all of the above, logs teed to /tmp
```

The mutation harness applies a fixed list of targeted defects (inverted fill
side, removed purge, gutted crossed-quote check, ...) and proves the suite
fails for each one. A mutant that survives means the tests are too weak.

## Layout

- `research_protocol.yaml` — frozen protocol; single source of truth
- `src/tree_options/protocol/` — typed loader, canonical hash, artifact stamping
- `src/tree_options/time/` — session calendar (no naive business-day arithmetic)
- `src/tree_options/schemas/` — Pydantic records (security, feature, contract,
  quote, order, fill, position, trial)
- `src/tree_options/guards/` — availability join gate + fill engine
- `src/tree_options/splitting/` — purged anchored-expanding walk-forward folds
- `src/tree_options/ledger/` — fee models + conservation-checked ledgers
- `src/tree_options/candidates/` — §9.2 candidate predicates (fail-closed)
- `src/tree_options/registry/` — trial registry (register-before-outcome, 32-cap)
- `docs/m0-evidence.md` — commands, counts, mutation table, remaining decisions

## Status

M0 in progress. Later milestones (point-in-time dataset, baselines, option
backtester, adversarial evaluation, paper adapter) are out of scope until the
M0 gate is green and the protocol version is frozen.
