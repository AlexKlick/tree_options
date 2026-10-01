# Measured cost corpus: the overall figures and the poisoned-mean decomposition

This file exists because two figure families the exit packet quotes were
carried only in campaign session memory and PR-prose context, not in any
durable artifact. They are recorded here with their source and derivation
so `CAMPAIGN-EXIT-20260930.md` and `RESTART-THRESHOLD.md` cite on-disk
ground. All figures derive from ONE corpus:

- **Source**: `~/.local/state/trex-strategy-sweep-20260930/chains.json` —
  184 CBOE delayed-quote chain files, **612,371 quote rows**, sessions
  2026-09-22..2026-09-29 (09-23 permanently missing).
- **Tradeable universe**: IWM/QQQ/SPY, 7 ≤ dte ≤ 60, volume > 0, oi > 0,
  |delta| ≤ 0.70 — **n = 18,783 rows**. This n is pinned in code:
  `cost.py CostProvenance.measured_corpus()` (`n_rows=18_783`, docstring
  "612,371 rows, 184 CBOE chain files").
- **Convention**: full quoted spread (ask − bid) per share; the 2-leg
  round trip = 4 fills × (half spread) × 100 multiplier + commissions —
  the same convention `SpreadCostModel.measured()` charges.

## Overall figures (tradeable universe, n = 18,783)

| statistic | full spread/share | 2-leg round trip | vs flat $14.60 |
|---|---|---|---|
| median | $0.030 | $8.60 | 0.59x |
| mean | $0.066 | $15.76 | 1.08x |
| p75 | $0.070 | $16.60 | 1.14x |
| p90 | $0.160 | $34.60 | 2.37x |

Median full spread by |delta| bucket: 0.00–0.10 $0.02 · 0.10–0.20 $0.03 ·
0.20–0.35 $0.05 · 0.35–0.50 $0.06 · 0.50–0.70 $0.19.
By DTE band: 7–21 $0.03 · 22–45 $0.04 · 46–60 $0.05.

## The poisoned-mean decomposition (why the corpus mean was never usable)

Over the WHOLE 612,371-quote corpus (no tradeable filter), rows with full
spread ≥ $1.00 are **9.2% of rows** and carry median |delta| **0.93** with
10-lot displayed size — deep-ITM quotes the desk does not trade. They
contribute **768% of the mean** spread (i.e. the mean of the whole corpus
is ~8.7x the mean of the tradeable universe). The "cost is 5.6x optimistic"
claim died on exactly this: it quoted the corpus mean against a model that
trades the tradeable universe.

## Re-derivation

Both families are recomputable from the corpus with the repo's own
machinery (offline builder over `chains.json`; the measured marginals in
`cost.py` are the transcription of the same corpus, and `n=18,783` is
asserted by `CostProvenance.measured_corpus()` and its tests). Session
provenance: computed 2026-09-30 in the measured-cost audit (two
independent re-derivations agreed exactly); transcribed here 2026-10-01 by
the exit-packet critic followup.
