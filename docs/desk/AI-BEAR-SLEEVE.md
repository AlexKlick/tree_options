# AI-BEAR thesis sleeve: the Phase 3 design for P2 lanes A and B

**Status:** DESIGN, with the limits ruled by the owner on 2026-11-09 (see the
last section). Nothing here is built. It lands with the P2 lanes
(`docs/product/P2-PLAN.md`) after P0 closes, as additions to lanes A and B,
not as a parallel lane. Owner decisions 2026-11-09: buy-side debit spreads
only, a separate thesis sleeve, and paper only. Live trading stays
unauthorized.

## What the evidence says before any ticket exists

- **AI-BEAR-001 FAILED** on 1997–2026 (`docs/desk/AI-BEAR-001-results.md`).
  No rule in this sleeve is a promoted strategy. Paper tickets here are the
  owner's discretion.
- **AI-BEAR-002 is forward-only.** Its verdict per sleeve is PENDING until
  20 closed cards (`docs/desk/AI-BEAR-002.md`). The ticket view should show
  both verdicts, read from `GET /api/aibear`, next to the review.

## Lane A: the capital policy gains a sleeve

`trex-capital-policy/1` gets an optional `sleeves` map, edited through the
same `update_policy` compare-and-swap. When the map is absent, every ticket
is in the default sleeve and today's limits apply unchanged.

```json
"sleeves": {
  "aibear": {
    "max_trade_loss_usd": "500",
    "max_open_loss_usd": "2500",
    "short_beta_delta_floor_usd": "-1000",
    "long_beta_delta_cap_usd": "250",
    "allowed_names": ["TSLA", "NVDA", "AVGO", "AMD", "PLTR", "META", "MSFT", "QQQ", "SMH", "SOXX"],
    "allowed_kinds": ["debit_vertical"],
    "max_dte_days": 450
  }
}
```

The rails rules:

- **The beta-delta check runs per sleeve.**
  - Today's check is `abs(after) <= cap` (`desk/rails.py:728`), and it stays
    exactly that for the default sleeve.
  - The `aibear` sleeve checks
    `short_beta_delta_floor_usd <= after <= long_beta_delta_cap_usd` on its
    own positions.
  - The book-wide $5,000 max loss (`rails.py:173-187`) is unchanged and binds
    the sum of all sleeves.
- **A sleeve's max open loss counts only its own open debits.** The
  account-wide ≤ 2 positions per underlying rule is unchanged.
- **Lowering a sleeve limit below its current exposure blocks additions and
  never liquidates** (lane A's existing rule).
- **The host profile** (`desk-paper/profile.json`) gets a strategy version
  `aibear-thesis/1` beside `operational-canary/1`, with the same numbers.
- **The quantity-1 rule stays** for the first acceptance lifecycles.

## Lane B: the manual debit vertical carries the sleeve

`ManualDebitVerticalTicket` gains `sleeve: "default" | "aibear"`. For
`aibear`:

- `right` may be P or C.
- The underlying must be in `allowed_names`.
- The expiry may be up to `max_dte_days` (no other DTE cap exists today:
  `trex/plan.py:322-331`).
- Exit templates, which the owner can edit before approving:

  | Template | Take-profit | Time | Stop-loss |
  |---|---|---|---|
  | core (put, ~365 DTE) | 0.90 × width | close at 120 DTE left | none: defined risk, the debit is the stop |
  | falls (put, ~45 DTE) | 0.70 × width | 25 sessions, or 10 DTE left | — |
  | relief (call, ~45 DTE) | 0.70 × width | 20 sessions, or 10 DTE left | — |

- **Regime stops are not exit-engine rules.** The engine knows touch,
  breach, take-profit, stop-loss and the time stop (`trex/engine.py:302-345`);
  "the state turned BULL with a rising 200-day MA" is not one of them.
  - The monitor flags such a state change on `/api/aibear`, and the owner
    closes the trade with lane C's selected close.
  - Putting regime stops into the engine is Phase 4 work, and only after a
    FORWARD-PASS.

## Constraints the owner should know

- **One option structure per underlying** (`supervised_canary.py:205-206`).
  A core spread and a tactical spread cannot both be open on QQQ or on SMH.
  - Practical split: core on single names, tactical on QQQ, SMH or SOXX.
  - Relaxing the rule is a separate owner ruling.
- **Rolling** is a close plus a new approved ticket (`plan.py:5-7`).
- **Credit spreads stay blocked** (`credit_open_not_supported_v1`). The owner
  chose buy-side only.

## Owner ruling (2026-11-09): the proposed numbers are adopted

The owner approved these numbers as proposed on 2026-11-09. They are the
values lane A writes into the `aibear` sleeve and the host profile's
`aibear-thesis/1`. Nothing is enforced yet:

- the sleeve, the per-sleeve beta-delta check and the ticket field are not
  built;
- they land with P2 lanes A and B after P0 closes;
- until then no `aibear` ticket can exist, and every limit in force today
  is unchanged.

Live trading stays unauthorized.

| Field | Ruled | Why |
|---|---|---|
| `max_trade_loss_usd` | 500 | today's per-trade rail; one core spread at quantity 1 is about $500–1,500 of debit on these names, so some names need narrower strikes |
| `max_open_loss_usd` | 2,500 | half the $5,000 paper book |
| `short_beta_delta_floor_usd` | −1,000 | about 4–6 put spreads on high-beta names |
| `long_beta_delta_cap_usd` | 250 | unchanged (relief call spreads) |
| `max_dte_days` | 450 | the core2 window |
