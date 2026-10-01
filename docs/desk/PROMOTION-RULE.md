# PROMOTION-RULE — the registered ADVISORY-LIVE promotion rule

Sealed **2026-10-01, before the 19:00 MDT nightly game** (before any digest
carried paired columns; before any standings file existed). Registration is
an operator act on a pre-registered artifact; this file is that artifact.
Amendments after seal require a NEW seal with a NEW sha256 and reset the
accumulation windows below.

A policy (model or rules) may be promoted to **ADVISORY-LIVE** — its choices
displayed as advice on real entry requests, never binding, never executing —
only when ALL of the following hold over nightly challenge digests:

1. **Sample**: the accumulating standings total **≥500 boards across ≥20
   distinct sessions**, with **no digest window overlapping the registration
   sample** (registration sample = every digest that existed at seal time:
   the six digests through `20261001T162152Z`).
2. **Beats doing nothing**: summed closed-capital proxy strictly above the
   **no_trade** baseline under the same capital/open-cap constraints
   (5000/300/1500), on the paired bars of clause 3.
3. **Significance**: the per-session **paired difference vs no_trade has
   CI95 > 0** (bootstrap, `longrun.paired`, the digest's `vs_no_trade`
   column).
4. **Beats a trivial picker**: the **first_row control FAILS clause 3**
   (its own vs-no-trade CI does not exclude 0) in the same windows. An arm
   that only beats do-nothing by beating first-row-of-the-board is not
   skill (defect #47; measured-cost verdict 2026-09-30).
5. **Capital**: worst minimum closed capital **≥ 4500** across the sample.
6. **Reliability**: model-failure rate **< 5%** across the sample.
7. **Multiplicity**: every threshold claim above survives **Holm** across
   all tested arms in the same standings.

**Euthanasia clause**: if after **40 sessions** of standings accumulation no
arm satisfies clauses 2–4, the nightly game is retired to the digest
archive, `desk-challenge.timer` is disabled, and this program is closed.

Mechanics: the nightly digest carries the paired columns
(`vs_no_trade`, `vs_first_row`) per policy; `challenge standings` accumulates
them; `challenge rule-check` prints each clause's status against the
standings. Promotion itself remains the operator's explicit act; no code
path promotes anything (`best_advisory` returns `promoted: false` by
construction).

Nothing about this rule is a claim that any arm will pass it.
