# tree_options

Point-in-time equity-signal research implemented through liquid, defined-risk
option positions. **M0–M3 complete; M4 (real data) in flight:** a Cboe EOD
adapter (quotes-bearing, sealed-era capable) plus a Massive (Polygon)
free-tier lane for structural options coverage. No model training and no
real-data backtest results exist in this repo yet, by design (synthetic-era
structural artifacts — worlds, overlays, and their mechanism-validating
backtests — do exist). The local gate is the only CI. M4 plan:
`docs/m4-real-data-plan.md`; Massive operator runbook:
`docs/m4-massive-runbook.md`.

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
- `src/tree_options/data/` — real-data ingest: Cboe EOD adapter + Massive
  (Polygon) free-tier client (see `docs/m4-real-data-plan.md`)
- `docs/m0-evidence.md` — commands, counts, mutation table, remaining decisions

## Status

M0, M1, M2, and M3 are complete and gated; the protocol is 0.2.0 (amended
once, at G3 — vwap quote kind, volume-flow liquidity regime, model-derived
delta provenance — merged via PR #12, `1f7c388`; no invariant weakened).
M4 — real data — is in flight: the Cboe EOD adapter shakedown (M4-A) is green
on main, and the Massive (Polygon) free-tier structural coverage era (M4-B)
has begun (operator runbook: `docs/m4-massive-runbook.md`). The options
purchase lane is CLOSED (G2, 2026-08-21: the owner ruled "no money spent —
find a workaround"; the free tier's daily aggregates plus model-implied
greeks carry the quote-bearing lane), and the sealed real-data gate (G4)
remains open.
