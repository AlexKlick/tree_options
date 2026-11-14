# SQQQ-HEDGE-001: beta-hygiene hedge twin of the XSMOM-TOP3 card book (PRE-REGISTRATION)

Written 2026-11-14, before any hedge twin exists and before any hedge
outcome data exists anywhere (RT-4 verified: no hedge cell in any
`artifacts/paper-trades/*.md`). Sealed by `SQQQ-HEDGE-001.md.sha256` (the
sha256 of this file, recorded in the commit that adds it). PAPER-ONLY.
Monitors/timers are owner-gated deployments, out of scope here.

**Gross-exposure disclosure (header, per RT-4 revision 6):** the twin's gross
exposure is <= 4x book notional at the β̂=3 cap (never reached in-sample; max
≈ $21.4k short-index-equivalent vs $7,500 long); net market exposure =
(β̂ − min(β̂, 3)) x $7,500 >= 0 — the twin is NEVER net short the market.

This resolves the ledger's queued future work — RESEARCH-LEDGER **L212-217**
(DESK-CHECKS 2026-09-12: book-vs-SPY monthly correlation +0.75, "the risk is
dominated by equity beta ... a pre-registered SQQQ-hedged proxy is queued as
future work"). First of family (m=1): the only prior "hedged" text anywhere
is options-friction wording (SWEEP-001/002) and one incidental seal phrase
(RT-4 §2).

Pinned at seal (sha256, first 16 hex):
- Card 8, twin card #1 (sealed 2026-11-14, AMD/LLY/INTC, 2026-11-02 close ->
  2026-12-01 close): `c70acd6883b8b27f89436154f1f080a52143b4911d31b7dbe3408bc070eb1891`
- panel `artifacts/paper-trades/ohlc-panel.json` @ 2026-11-13: `9fbde932dfade93e…`
- wave-1 spec `~/.local/state/trex-strategy-fanout-20261114/wave1/n1-universes.md` WP2: `7ce1db9a2feed270…`
- red-team verdict `…/wave2/n1-2-sqqq-proxy-redteam.md` (RT-4, MAJOR-REVISION —
  all six required revisions applied here): `a2ff2e98a76fbdb2…`

## The job: measurement, not yield

The book's risk is equity beta (+0.75 monthly SPY correlation). The
market-neutral L-S spread is the true alpha but is not long-only-tradeable.
A SQQQ overlay bought alongside each XSMOM-TOP3 card is the
long-only-permissible approximation, and its job is to measure how much of
the card series is beta — a **beta estimator, never "market-neutral alpha"**
(it does NOT express the banned short-momentum leg; it strips index beta).
Toxicity precedent on file: PROTOCOL-XSMOM 2023-01, SQQQ as a top-3 momentum
SIGNAL leg, **−$831 in one month** — a signal-leg outcome, not a hedge
experiment (RT-4 §2); this proxy never ranks, never signals, only hedges.

## The rule (deterministic twin, paper-only)

At each XSMOM-TOP3 card seal (36-name sealed rule; if WP1's XU-62 successor
ever re-seals, the twin rides that book unchanged):

```
beta_hat   = OLS slope of the card's equal-weight top-3 basket daily returns
             on QQQ daily returns over the trailing 60 sessions ending at the
             session BEFORE entry (panel closes; names missing any session ->
             use the basket of names with full windows, require >= 2 of 3,
             else NO HEDGE)
h_notional = min( max(beta_hat, 0) , 3.0 ) / 3.0  *  $7,500   # SQQQ dollars to BUY
             (beta_hat <= 0 -> no hedge; cap = full $7,500)
entry      = buy h_notional of SQQQ at the card entry close; exit at the card exit close
costs      = 5bp RT on the SQQQ leg (same convention as card legs), crossing at closes
twin card  = (unhedged card P&L) + (SQQQ leg P&L) - 5bp RT on h_notional
```

Info set: panel closes strictly before entry (beta_hat), card entry/exit
closes, nothing else; no vol_state, no chains. Declared basis: hedging an
SPY-correlated book with a QQQ instrument (measured QQQ-on-SPY 20-session
beta 1.30, corr 0.932) leaves a deliberate tech-vs-market residual — itself
one of the readouts.

### The β-cap, stated as verified fact (RT-4 §1)

β̂ reconstructed at all 46 historical XSMOM-TOP3 month-start entries (sealed
36-name roster, EW basket, 60-session OLS ending the session before entry):
**min 0.18, p10 1.11, median 1.63, p90 2.50, max 2.85; β̂ > 3 in 0/46; β̂ <= 0
(no-hedge branch) in 0/46; mean h/7500 = 0.56 (≈ $4,200)**. The cap is an
a-priori prudence bound (hedge notional <= book notional) that the panel era
never reaches; the no-hedge branch is equally inert. Nothing was selectable
post-hoc: no hedge outcome data existed when the rule was fixed.

## Costs, and the regime table that must travel with every artifact (RT-4 revisions 3-4)

- **5bp RT on SQQQ is a CONVENTION, not a measurement** (RT-4 revision 4): no
  SQQQ spread corpus exists anywhere (SQQQ is `no_options_expression`; the
  measured desk corpus is CBOE option quotes — 18,783 on IWM/QQQ/SPY). Sanity
  arithmetic: SQQQ last panel close $42.46; a penny spread is ~4.7bp RT, so
  5bp ($3.75 on $7,500) is plausible-to-mildly-conservative for one of the
  most liquid ETFs. At paper level fees are 2.8% of the per-card gap noise.
- **Daily-reset decay (measured):** a −3x daily-reset ETF's log-growth ≈
  −3x QQQ log-growth − 6σ²(QQQ) per day; measured QQQ daily σ 1.46%
  full-panel / 1.36% last-252 → drag ≈ 0.13%/day ≈ 32%/yr (27.8%/yr at
  last-252 vol), before the ~0.95%/yr expense ratio.
- **The 20-session tracking gap — pooled numbers AND the sub-era table
  (RT-4 revision 3).** Pooled (1,270 overlapping windows, 2021-09..2026-11):
  mean +0.06%, median +0.08%, p10 −3.35% / p90 +3.83%, extremes ±17.8%,
  47.9% of windows underperform linear −3x. **The pooled mean is a regime
  cancellation and must not be quoted without this table:**

| era | mean 20-sess gap | sd | mechanism |
|---|---|---|---|
| 2021-09..2022-12 (zero-rate) | **−1.10%** | 3.69% | pure variance drag |
| 2023-2024 (high-rate) | **+0.78%** | 2.20% | daily replication gap runs +2.6bp/day ≈ **+6.5%/yr SQQQ financing tailwind** at 4-5% policy rates, offsetting the drag |
| 2025-2026 | **+0.03%** | 4.39% | includes the 2025-04-09 SQQQ −35.3% day |

  The hedge's **mean cost Y is regime-dependent** — "financing-vs-drag net,
  CI from the sub-era table, plus $3.75 fees" — NOT a stable "decay + fees"
  constant (the wave-1 wording was mis-signed; corrected here). A forward
  zero-rate regime restores ~−1.1% per 20 sessions (~−$46/card at mean h) of
  pure drag. Hold-to-exit (no daily rebalance) is declared: the twin
  measures the tradeable version, decay and all, dispersion ±3.5% ≈ ±0.5
  beta units per card.

## Readouts: exactly two judged claims (RT-4 revision 1 — everything else demoted)

1. **Variance / information-ratio readout:** paired per-card variance
   reduction of the twin series vs the unhedged series, with permutation CI
   (expected cut ≈ 50%: twin sd ≈ $694 vs unhedged $986, from gap-sd $134 at
   mean h + β̂-estimation error ≈ $98), plus drawdown change. **Marginal, and
   says so:** at n=18 paired variance-ratio SE(ln ratio) ≈ 0.37 → **z ≈ 1.9**,
   one-sided p ≈ 0.03, power ~60-65% IF the forward effect is at in-sample
   strength; if R² degrades to 0.35, power collapses (z ≈ 1.2). **MDE: at
   n=20, 80% power detects only a ~58%-or-larger variance cut; the
   deliverable is a point estimate with a wide CI (roughly 10-75%), not a
   significance claim** (RT-4 revision 5).
2. **Gross beta-hygiene readout:** regression β of twin card P&L on QQQ
   same-window return ≈ 0 (with CI), reported beside the unhedged β̂.

**DEMOTE — explicitly NOT a readout (RT-4 revision 1, with the arithmetic):**
the wave-1 "comparator 2" (α̂ recovery vs the L-S spread, and the beta-share
CI). SE(twin mean) at n=18 is **$164** vs the marked-down alpha effect
$112.50/card; the twin's 95% CI on recovered alpha spans **±$330 — from
"the hedge destroys $220/card" to "alpha is 4x the XU expectation"**,
comfortably covering both "all of it is beta" and "none of it is beta".
Separating those two hypotheses at 95% needs a CI half-width <= $112.50 →
**n ≈ 152 monthly cards ≈ 12.7 years**. The α̂/β̂ split and beta-share are
reported, if at all, as explicitly-unpowered descriptive intervals carrying
this paragraph — never judged, never cited as a finding.

## Placebo battery (RT-4 revision 2 — fixed primary null, declared before the first card)

- **PRIMARY null (declared now): the constant-notional twin at the sample
  mean h (≈ $4,200)** — the null that actually tests "estimated β̂ carries
  information beyond a fixed average hedge." A random-notional hedge still
  loads −3x QQQ at its mean size and would pass for any same-direction
  sizing, including a constant (RT-4 §3); the wave-1 U(0, cap) primary is
  therefore demoted.
- **SECONDARY: same-notional random-sign / date-shuffled pairing** (preserves
  notional scale exactly, destroys direction and timing) for any directional
  claim.
- **TERTIARY (dispersion band only): the wave-1 U(0, cap) random-notional
  draws**, seeded 1,000, on the same dates.
- Sign-flip permutation (10,000 draws) on paired hedged−unhedged differences
  for directional claims; A/A determinism twin (β̂ is a pure function of the
  panel — re-derivation must reproduce byte-identically).

## Bars, clocks, machinery

- **Clocks:** the incumbent book's own — read at the WP1 trigger's 18/20
  valid sealed XSMOM card clocks, **counting from Card 8 (card #1 of record,
  resolves 2026-12-01)**; twin #1 = Card 8's twin; report-only before that;
  ONE evaluation. Mean twin P&L > 0 is NOT required (the twin can
  legitimately lose the decay premium in a bull tape — that is the
  measurement working). The only promotion-shaped claims are the two readout
  forms above, with CIs.
- **Machinery (owner-gated; out of scope here):** no repo change needed to
  start — a research-lane script in the `resolve_card.py` pattern, reading
  sealed card files + panel closes, writing a parallel twin-card ledger with
  provenance. It must never edit sealed card text; the sealer is blind to
  twin state when sealing (the twin reads, never writes, the card chain).

## Banned-list cross-check

`short_any` / `xsmom_short_leg` are banned as DIRECTION signals
(`signals.py`); the twin only ever BUYS SQQQ, fired by the allowed
`xsmom_top3` card, never ranks, never signals, never selects a name, never
touches exits. Net exposure >= 0 (header disclosure): beta-neutral at the
in-sample β̂ range, never beyond. Economically an L-S-shaped structure —
which is why any real adoption (beyond this paper artifact) is an operator
ruling of its own, quarantined here. Auto-reject tripwire: any use of the
twin to short momentum names, to rank, or to size a direction trade = the
banned family.

## What was seen before sealing

- The 46 PROTOCOL-XSMOM months (mean +$288, sd $986) — descriptive prior
  ONLY; outcomes burned by the XSMOM family; never evidence here.
- The SQQQ/QQQ instrument characterization above (decay, tracking
  dispersion, sub-era gaps, replication slope −2.973 / corr −0.9985) —
  instrument facts, legitimately reusable with the regime table attached.
- Card 8's entry window (2026-11-02) predates this seal: entry-leg closes
  and the panel needed for its β̂ were observable at sealing. Nothing was
  selectable — the rule is deterministic at entry, the β̂-cap and no-hedge
  branch are provably inert in-sample (0/46, 0/46), and Card 8's OUTCOME
  (2026-12-01 close) did not exist at seal. No hedge outcome data existed
  anywhere when the rule was fixed (RT-4 §1-2).
- The −$831 (2023-01) and +154 (2023-02) SQQQ signal-leg rows, TQQQ legs
  2023-12..2024-12 — the leveraged-ETF momentum toxicity precedent, quoted
  as motivation for why the twin never ranks.
