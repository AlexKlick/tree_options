# DESK-BT-001 AMENDMENT 1: enter on the next session (PRE-REGISTRATION)

Written 2026-09-23, before DESK-BT-001 has run (no run code exists; the
earliest run date is 2026-10-20) and before any option P&L, placebo draw or
spread haircut was computed. Sealed by `DESK-BT-001-AMENDMENT-1.md.sha256`
in the commit that adds this file.

**Amends:** `docs/desk/DESK-BT-001.md`, sha256
`e3e1d12d75b043c6f344c928290f5f5a813c22a8df327de6c28e2d9943139f77` (its
sidecar). That file's bytes are never edited. **The study is DESK-BT-001 as
amended here: where the two differ, this amendment governs; everything it
does not name stays exactly as written in the original.** Every output
stays labeled MODELED.

## Why (Codex P1-3, 2026-09-23)

Both signals are known only at the signal session's close: XSMOM ranks on
close(t), and a PEAD beat is the close-to-close move of the first
post-report session. The original priced the entry at that same session's
full-day VWAP, i.e. partly with prices traded before the signal existed (a
name crossing +1.5% near the close would be bought at an average of the
day before it qualified). A spread haircut does not remove that
look-ahead. The fix below makes every entry executable.

## Amended rules

- **Signal session D:** unchanged. The signal is observed at D's close
  (XSMOM on the first session of the month; a PEAD beat on the first
  post-report session). Entries are still the signals fired 2024-09-03
  through 2026-08-31.
- **Entry session E0:** the next NYSE session after D (trex calendar;
  2025-01-09 is not a session). Every entry price is the leg's Polygon
  VWAP on E0. Nothing dated D is used to price an entry.
- **Structure selection is anchored to E0:** the expiry is the latest
  monthly with 30 <= DTE <= 60 calendar days AT E0 among those with bars
  on E0; the forward uses the underlying's `adjusted=false` VWAP on E0 and
  DTB3 as of E0; K0 is the strike nearest that forward among strikes with a
  call AND a put bar on E0 (|ln(K0/F)| <= 0.03); strike ranks count
  distinct strikes with bars on E0. S1, S2 and S3 are otherwise as in the
  original.
- **Hold and exit are anchored to E0:** the exit session is the earlier of
  E0 + 20 NYSE sessions and the last session with DTE >= 7; every leg is
  priced at its VWAP on that exit session.
- **Evaluability:** a leg without a bar on E0 or on the exit session makes
  the (entry, structure) NOT_EVALUABLE, with no substitution and no
  carried price; counts are reported (as in the original, now at E0).
- **Placebo is anchored to E0:** the candidates are the names for which
  the same structure is evaluable at E0 and which did not fire the same
  signal on D; the draw is candidate number
  int(sha256("DESK-BT-001-A1|E0|X|structure")[:16], 16) mod n of the
  candidates sorted by name (E0 and X in ISO date / ticker form).
- **Clusters:** the paired placebo test (b) averages within each entry
  session E0 (one E0 per signal session D, so the clusters are the same
  sets as before).
- **Equity comparison (reported, decides nothing):** the executable card on
  the same entry: open(E0) to close(exit) from the split-adjusted panel,
  minus 5 bp.
- **Unchanged:** the six variants, the haircut rule (the first 20 recorded
  chain sessions), commissions, the return per trade, tests (a) to (d),
  the deflated Sharpe (N = 6), T >= 20 and k >= 10, the verdict and
  per-signal rules, the consequences, the earliest run date (2026-10-20)
  and the one-scored-run procedure. The run code records this file's
  sha256 beside the original's.

## Known limit added

- The last entries' exits can fall after the on-disk bars end (about
  2026-09-03); those entries are NOT_EVALUABLE by the evaluability rule,
  as before, and are counted.
