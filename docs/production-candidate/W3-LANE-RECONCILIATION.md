# Wave 3 lane reconciliation (candidate vs the four local lanes)

Date: 2026-09-25. Candidate: `desk-wave3-safety-rc1` (applied verbatim on
`ab493d7`; see the candidate commit for patch/archive sha256 provenance).

The four Wave 3 branches reported as "pushed but not landed" by the original
session were never on origin (verified by `git ls-remote` 2026-09-25). All four
exist locally, one commit each, and are now pushed to origin for custody;
durable bundles live outside the repository in
`~/documents/tree_options-custody/` (`SHA256SUMS` recorded).

**The candidate is the single coherent implementation of every shared file**
(`desk/__main__.py`, `trex/engine.py`, `trex/enter.py`, `trex/monitor.py`,
`desk/shadows.py`, `desk/scorecards.py`, `desk/enter.py`). Nothing from the
lanes is merged; only the ports listed below were taken, as tests and
documentation, never source hunks.

| Lane (hash, fork point) | True additive diff | Verdict | Ports taken |
|---|---|---|---|
| `fix/legacy-late-fill-price` `7e7d82c` (off `ab493d7`) | `trex/enter.py` +21, `trex/monitor.py` +29, 2 behavioral tests | **Superseded.** Same defect, same fix shape; the candidate's guards are stricter (`avg.is_finite() and avg > 0`, `.get()`-based notional, explicit `filled == filled_qty` elif) and its tests are stronger (parametrized prior-book state; restart-checkpoint clearing; `changed=True` reporting on the exit side) | none |
| `feat/desk-assignment-exit` `a5ca17d` (off `ab493d7`) | `trex/engine.py` +85, 10 engine tests | **Superseded.** The candidate implements the same rule as a pure heuristic but: validates `prev_session` against the session calendar, normalizes the strike in the short-call key (Decimal display precision cannot change identity), and fail-closes on non-finite/negative mids (the lane fail-closes only on a missing mid) | engine docstring rule renumbering (the candidate inserted the rule without renumbering); ordering pins (expiry-safety/breach preemption, fires-before-take-profit, working-exit keeps reason); literal key wire-format pin; zero-dividend stand-down case |
| `feat/desk-w3-enter` `f96275d` (off `ab493d7`) | `desk/enter.py` +295 (spec spool + admissions.jsonl + os.link claims + ntfy), `paths.py` `DESK_PAPER_DIR` run dir, 293 test lines | **Rejected per INTEGRATION_REVIEW §2.B.** A shadow spec must not enter an executable spool a future runtime could consume; `DESK_PAPER_DIR` already names the research paper-trades panel (namespace collision; the candidate uses `TREX_DESK_RUN_DIR`). The candidate's `desk/contracts.py` covers every validation idea the lane had (money-string discipline, max-loss recompute from the capped structure, deadline calendar/expiry-buffer checks, id/deal_id agreement, valid_until expiry, rank order, HALT/AUTO_OFF as blockers) | none |
| `feat/desk-w3-shadows` `b6c5b26` (fork `4fc4bfa`, 19 commits back — diffing against `main` shows ~17,300 spurious deletions) | `desk/shadows.py` +532, `desk/scorecards.py` +208, `__main__` +45, desk-shadow units, runbook +59, ~600 test lines | **Superseded per INTEGRATION_REVIEW §2.C/§2.D.** The lane resolves missing deadline data at the latest earlier recorded session (`fallback: true`) and treats an on-disk future chain as proof the deadline passed; it also computes promotion/retire/pause rules and weekly t-statistics. The candidate censors missing/conflicted deadline outcomes, bounds evaluation by the clock, keeps `promotion_ready: false`, separates gross/assumed-commissions/net, and shows the 20-resolved count as a sample floor only. Its cohort key is richer (adds tier + policy lineage to underlying/row/week) | fresh runbook prose for the candidate's own commands/units (the lane's runbook section documents the superseded JSON/fallback design and was not ported) |

## Custody record

- Bundles: `~/documents/tree_options-custody/{tree_options-main-ab493d7,feat-desk-w3-shadows-b6c5b26,fix-legacy-late-fill-price-7e7d82c,feat-desk-assignment-exit-a5ca17d,feat-desk-w3-enter-f96275d}.bundle` (all `git bundle verify` clean; the set is self-contained through the shadows fork point).
- Remote: all four branches pushed to origin 2026-09-25; local branches and worktrees preserved untouched.
- The `desk-w3-shadows` worktree hosts a live codex session; it was read only via the main repository's object database.

## Scoring semantics the lanes computed and the candidate deliberately does not

Recorded here so the difference is a decision, not an accident: fallback
resolutions, weekly clustered t-statistics, retire/pause triggers, and
`slippage_first_mark_dollars` (the candidate records first marks but does not
label the difference "slippage" — it also contains market movement). Any future
promotion study must re-derive these under a registered estimand with a fixed
forward window; the lane's numbers must not be back-filled into the evidence
store.

## Independent review round (codex, 2026-09-25) — disposition

One bounded round on the full integ diff; transcript in the campaign state
logs. Four findings, each verified against the code before action:

| # | Finding | Verdict | Action |
|---|---|---|---|
| 1 | CRITICAL: a historical `--as-of` scorecard could be censored by a quality event discovered after that date (quality filtered by session, marks by discovery time) | REAL | Fixed: `EvidenceStore.all_at()` pairs payloads with the audit commit time; `build_scorecards` filters quality by discovery time under `as_of`. Regression: `test_historical_card_does_not_use_quality_events_discovered_after_requested_session` (red pre-fix) |
| 2 | HIGH: a delayed price for a replacement order can be checkpointed without entering the aggregate fill price (`st.filled_qty > filled` with `entry_fill` None) | **Reopened by the round-2 audit (R2-02): real, still-tracked incomplete-price coverage.** The E5 boundary legitimately covers only corrections arriving after an order LEAVES the tracked set; the reproduced cases keep the replacement in `self.orders`, where the old merge conflated filled quantity with price coverage (2 unpriced fills then a cumulative average recorded 1.50, not 0.50; unknown earlier-order costs became fabricated whole-position averages) | **Fixed in round 2:** both legacy runners now merge through `engine.drain`, whose unpriced-quantity accounting (already the desk runtime's landed law) keeps `entry_fill`/`exit_fill` as averages over PRICED packages only, with `*_unpriced_qty` explicit; consumers (marks, realized P&L, take-profit, stop ticks) stand down or disclose while coverage is incomplete. Complete retired-order/execution-ID recovery remains E5 (Gate 2) |
| 3 | HIGH: a nonterminal entry price revision was not persisted to `book.json` (enter saves only on terminal order status, but reloads the book every cycle) | REAL — **but the fix was revision-only: the round-2 audit (R2-01) reproduced ordinary nonterminal PARTIAL FILLS still being lost across the real reload cycle** (the round-1 regression ticked without ever calling `_sync_book_from_disk`, so it never exercised the boundary its name describes) | Round 1: `Enterer._fill_revised` flag. **Round 2 (complete):** every accounting mutation persists before the next reload; order checkpoints live only in the durable book (the in-memory precedence dicts are gone), so a failed save self-heals when the broker's cumulative report reconciles against the durable checkpoint |
| 4 | MEDIUM: an explicit future `--as-of` censored outcomes that have not happened yet | REAL (semantics) — **but the clamp implementation moved the KNOWLEDGE cutoff backward with the horizon (round-2 audit R2-03): a deadline quote fetched on D+1 became invisible to `--as-of D+1`, censoring an already-known outcome; an explicit date also turned a no-evaluation store into `ok`** | Round 1: clamp to the evaluated horizon. **Round 2:** requested as-of (knowledge cutoff, never moved backward), outcome horizon (bounded by evaluated coverage) and the cutoff filter are separate fields (`as_of_requested`, `as_of_session`, `quote_knowledge_cutoff`); `no_evaluation` is returned whenever no evaluation exists, regardless of a supplied date |

No findings in evidence-store transactionality/backup, `--armed` refusal or
preview-to-execution paths, GET-only web routes, or shared-environment safety.

## Round-2 audit corrections (2026-09-25, TREX-Round2-Audit-and-Handoff)

Independent round-2 audit against `bedced9` reproduced three code defects
(R2-01 fill custody, R2-02 cost coverage, R2-03 scorecard cutoffs — all
verified red-first here as `tests/desk_safety/test_round2_regressions.py`,
15 failing at `bedced9`) and relabeled Gate 1 "deployed / acceptance
pending". Dispositions above record the reopened/annotated round-1 rows;
the history is retained, not rewritten. The round-2 fixes route both legacy
runners through `engine.drain` (the accounting law the desk runtime already
used), keep checkpoints only in the durable book, and gate every consumer
of a side average on complete price coverage. An exit-side sibling of
R2-01 was found and fixed in the same pass: `_refresh_exit` replaced a
cancelled exit order without merging its final fills (enter's
`_place_after_cancel` always merged; the exit path did not). The audit's
suggested per-lane branches were folded into one integration worktree as
separate commits (single integrator; gate and review run once on the
whole corrective diff).
