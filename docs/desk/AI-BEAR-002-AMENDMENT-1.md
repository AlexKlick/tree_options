# AI-BEAR-002 AMENDMENT 1: monitor mechanics and like-for-like controls (PRE-REGISTRATION)

Written 2026-11-09 (evening), before AI-BEAR-002's first signal session
(the 2026-11-10 close). No forward card exists. Sealed by
`AI-BEAR-002-AMENDMENT-1.md.sha256` in the commit that adds this file.

**Amends:**

- `docs/desk/AI-BEAR-002.md`, sha256 `0f62a2f02e3578e6aede2b871888eacb2f4763d773920191b2217a99b025784f`;
- its parameters `data/desk/aibear/AI-BEAR-002.params.json`, sha256
  `a20dfa060a30cdcdfec8ce196d9d4ad84de772581f1ccc4c5c81b39c5217f282`.

Neither file's bytes are edited. **The study is AI-BEAR-002 as amended
here.** Where the two differ, this amendment governs, and everything it does
not name stays as written.

The monitor now loads `data/desk/aibear/AI-BEAR-002-A1.params.json`. That
file is the original parameters plus an `amendment` block, and its digest
is pinned as `SEALED_FORWARD_PARAMS_SHA256`.

## Why

An independent correctness review of the sealed code (2026-11-09) found
six defects. Each one would have biased the forward record or let it be
read selectively. None changes a sleeve's entry or exit rule.

## Amended rules

1. **Every session counts (review finding 1).**
   - The monitor processes every panel session (QQQ's dates), not only
     sessions with a recorded chain. 6 of 34 sessions since 2026-09-22 have
     no chain.
   - On a session without a chain, signals, regime stops and time stops
     still run.
   - The take-profit needs a mark from that session.
   - A fill waits for the next session with a chain, for at most 3
     sessions (`fill_wait_sessions`). After that the card is `unfillable`
     (recorded and counted).
   - Exits keep the original 3-session wait, then intrinsic value.
2. **Exit quotes are one-sided, as the fill rule already was (finding 2).**
   - An exit or mark needs the long leg's **bid** and the short leg's
     **ask**.
   - A bid of 0 on a live row (ask > 0) is a real bid, not a missing quote.
   - Fills still need the long's ask and a positive short bid.
3. **Controls are drawn inside the sleeve's regime context (finding 3).**
   - Under the original rule, a control drawn on any day almost never
     carried the episode-low stop (relief2), and on BULL days it stopped out
     at once (core2). F3 would have compared different exits, not timing.
   - Controls are now drawn (probability 0.05, same seed) only on sessions
     in the sleeve's context (`control_context`):

     | Sleeve | Context |
     |---|---|
     | core2 | state BEAR or RELIEF |
     | falls | state BEAR |
     | relief2 | a CAPITULATION close within the last 20 sessions |

   - A relief2 control therefore gets the rule's stop: the lowest
     CAPITULATION close in that window.
   - F3 now asks whether **this trigger** beats a random entry **in the same
     regime**.
4. **One LLM read per session, and no-op reruns (finding 4).**
   - A run for a session that is already processed (a holiday morning, or
     a manual rerun) changes nothing: the book, the day document and
     `llm.jsonl` are left as they are.
   - A session never gets a second LLM read, which would carry later
     headlines.
5. **Split guard (finding 5).**
   - At fill, the card records the panel's close for the fill session.
   - If a later panel prints that same historical close more than 5 %
     differently, the panel has been re-adjusted for a corporate action.
     The card's strikes and stop are then in stale units.
   - The card becomes `void_corporate_action`. It is counted but has no R.
6. **The verdict is fixed once reached (finding 6).**
   - F1–F3 are computed on the **first 20 closed rule cards** in exit order
     (exit session, then card id). The comparison set is the control cards
     closed on or before the 20th card's exit session.
   - The verdict and its `verdict_session` never change after that. Later
     cards appear only in a running total.

## Not changed

Regime states, sleeve entry and exit rules, structures, strike and expiry
rules, fill and exit price sides, the control probability and seed, the
20-card bar, α = 0.10, and the first signal session (2026-11-10).
