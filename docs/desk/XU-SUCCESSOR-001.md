# XU-SUCCESSOR-001: the XU-62 successor trigger for XSMOM-TOP3 (PRE-REGISTRATION, forward-only)

Written 2026-11-14 and sealed the same day (owner-approved registration,
TREX strategy fan-out CP1). PAPER-ONLY. This is a sealed trigger document
plus the minimal spec detail that makes its reads un-shoppable; it deploys
nothing — no monitor, no timer, no code. Every machinery item is §6
plumbing, flagged for separately reviewed, owner-gated changes.

Sealed at body sha256 `d779816ff7e1f09eae63df4441d19db96a6d82d317477566beea91e81cca5144`
— the sha256 of every line below the `<!-- body -->` marker (reproduce:
`tail -n +20 docs/desk/XU-SUCCESSOR-001.md | sha256sum`). The sidecar
`XU-SUCCESSOR-001.md.sha256` carries the sha256 of the whole committed
file (`sha256sum -c` format); the commit and the sidecar are authoritative.
Corrections are separate `XU-SUCCESSOR-001-AMENDMENT-n.md` files with their
own sidecars — sealed text is never edited. Any re-parameterization of the
bars, roster or construction below restarts under a new pre-registration
id.

<!-- body -->

## 1. The question, and why this can be sealed today

The sealed XSMOM-TOP3 rule's evidence is partly a curated-universe artifact
(XU-XSMOM: the 60-skip5 forms invert on the broad universe; the classic
construction survives attenuated to ~25-80% of curated magnitudes). The
ledger pre-committed the expanded roster as the successor universe —
verbatim, RESEARCH-LEDGER.md L33-34:

> Universe amendment (36 -> XU-62) is POST-HOC: rejected for the sealed
> rule's first 20 cards; pre-registered as the successor universe if the
> curated cards disappoint.

"Disappoint" was never defined. This document defines it as an observable,
one-read trigger, sealed before a single sealed card of the series exists
beyond Card 8 (sealed this morning, still open until 2026-12-01), so the
condition cannot launder the already-observed XU comparison.

### 1.1 What was seen before sealing (disclosure)

- The 2026-10-01 rebalance draft (INTC/AMD/SOXX, exit 2026-10-29) was never
  sealed and never can be: its resolution data has existed for weeks. Its
  visible outcome was a disaster (INTC 120 → 59.97, AMD 615.73 → 466.84 by
  2026-11-02). It does not count toward anything here and no threshold in
  this trigger is tuned on it — but anyone reading the drafts namespace
  sequentially has seen it, and this registration says so.
- Before Card 8 there were ZERO sealed XSMOM-TOP3 cards. Verified 2026-11-14
  by the red team (RT-2, read-only): `artifacts/paper-trades/LEDGER.md`
  carried seven sealed-card rows, all non-xsmom; no `*-xsmom-top3.md`
  sealed card file existed; the only xsmom cards anywhere were the two
  unsealed drafts in `~/.local/state/trex-desk/card-drafts/`.
- Card 8 — the 2026-11-02 rebalance AMD/LLY/INTC — was sealed 2026-11-14,
  the same day as this registration, with a full LATE SEAL disclosure in
  the card (§2.3). It is card #1 of record (§1.2).
- The XU-62 panel's entire history is burned as evaluation evidence (§5).
  The +$112.50/card expectation used in §3's power math is an
  in-sample-derived number used for honesty only, never as a bar.

### 1.2 The chain of record

The trigger's clock runs on the sealed 36-name rule's own card series.
Card #1 of record is **Card 8**:
`artifacts/paper-trades/2026-12-01-xsmom-top3.md`, sha256
`c70acd6883b8b27f89436154f1f080a52143b4911d31b7dbe3408bc070eb1891`
(LEDGER.md row "Card 8 — XSMOM-TOP3 monthly AMD+LLY+INTC (sealed
2026-11-14)"): AMD 466.84 / LLY 1410.99 / INTC 59.97, 3 × $2,500 LONG,
2026-11-02 close → 2026-12-01 close (hold-20). The card's SEAL NOTE
discloses the late seal — drafted 2026-11-02 by the engine, sealed
2026-11-14 owner-approved during the entries-paused period; interim
sessions 11-03..11-13 elapsed pre-seal; the exit outcome did not exist at
seal; entries were fixed mechanically by the 2026-11-02 signal and the
sealer exercised no name discretion. That disclosure is part of the record
this trigger anchors to. Card #n is the n-th valid sealed card from Card 8
onward.

The ranking convention is pinned by reference and may not be re-litigated
at any future seal: `close(t)/close(t-273)-1` (273 NYSE sessions, latest
month included), per the 2026-09-23 operator ruling
(CRON-paper-engine.md step 6 CORRECTED note; Card 8 SEAL NOTE; the desk's
`signals/<D>.json` carries it as `xsmom.top3`). The untested skip-21
variant (`top3_skip21`) is record-only.

## 2. The trigger (exactly one read)

### 2.1 Unit: a valid sealed card

A **valid sealed card** is an XSMOM-TOP3 monthly card of the sealed 36-name
rule (the `[xsmom].tradables` roster pinned in `desk-universe.toml` and by
`tests/unit/test_desk_universe.py::test_xsmom_tradables_is_the_sealed_roster`)
that:

1. is sealed per CRON-paper-engine.md step 6 — card file
   `artifacts/paper-trades/<exit-date>-xsmom-top3.md` + LEDGER.md row + the
   card file's sha256 — with parameters frozen at seal;
2. was sealed before its exit outcome existed (§2.3 fixes the late-seal
   boundary; a card first sealed after its exit outcome exists can never
   be valid — the 2026-10-01 class);
3. is not voided at read time (§2.2).

Honest NO-TRADE months and REFUSED drafts (§2.4) do not count and extend
the calendar.

### 2.2 Void grounds (pre-named classes only; nothing else voids)

The program has voided resolved sealed cards before — cards 3/4/5, LEDGER.md
SIGNAL-ENGINE AUDIT: "NOT valid forward-test instances of their rules and
MUST NOT count toward the promotion gate's ≥20-cards-per-rule requirement."
A card of this series is void — excluded from the count, the clock
extending to the next valid sealed card, so "the 18th card" moves with it —
only for these pre-named defect classes:

- **ENGINE-DEFECT** — the card was produced or resolved by engine code
  later shown defective in a way that changed its entries, exits or P&L
  (the cards-3/4/5 class);
- **DATA-DEFECT** — a sealed input (panel close, calendar row) was wrong
  and materially moved the card's entries or P&L;
- **SEAL-TIMING BREACH** — the seal is shown to postdate the card's exit
  outcome.

A void never restates P&L (the 3/4/5 precedent: entries and exits were
real; the card simply stops counting). Voids are logged in Appendix A
(append-only: card, class, date, evidence pointer). Operator discretion is
NOT a void ground, and neither is: the card's P&L; roster or
ranking-convention regret (§1.2 pins the ruling); discovering a better
construction after resolution; or wanting the clock to move. A card that
cannot be voided under a named class counts, whatever anyone thinks of it.

### 2.3 Seal discipline (every draft sealed or refused)

Sealing stays the human/agent step (CRON step 6; the eod timer only writes
drafts). To make "the 18th valid sealed card" non-shoppable:

- **On-time seal**: sealed before any post-entry-session data exists.
- **Late seal**: sealed after interim sessions exist but before the exit
  outcome exists — VALID only with an in-card LATE SEAL disclosure stating
  that entries were fixed mechanically by the signal, that the sealer
  exercised no name discretion, and that the exit outcome did not exist at
  seal, with the owner approval recorded. Card 8 is the precedent and the
  template.
- **Never-sealable**: first sealed after the exit outcome exists. Such a
  card cannot enter the series at any time for any reason.
- Every timer draft for an xsmom rebalance must be either sealed or
  recorded as REFUSED with one of the pre-enumerated reasons:
  `EARNINGS-BLOCK` / `NAME-HELD` / `NO-TRADE` / `DEFECT`, auditable from
  the timestamped drafts namespace. A drafted-but-unsealed month with no
  recorded refusal is a protocol breach under this registration.

### 2.4 READ-1 (the sole read)

At the resolution of the 18th valid sealed card:

> **DISAPPOINTMENT** iff cumulative net P&L over the first 18 valid sealed
> cards (Card 8 onward) is ≤ $0.

Net P&L is the cards' own sealed house convention ($2,500/leg × 3 legs,
$7,500 notional, 5bp RT, exit at the 20th-session close). This inherits —
does not re-tune — the ledger's standing kill-switch text
(RESEARCH-LEDGER.md L219-221, pre-registered before forward card #1):

> XSMOM-TOP3 pauses on trailing-6-cards mean <= 0 AND cumulative <=
> -$1,500; retires at 18 cards with cumulative <= 0. [...] Valid cards
> only (corrected engine, sealed before entry).

READ-1's bar coincides with the retire bar (inheritance, not a new bar).
A pause does NOT itself confirm disappointment — it defers to READ-1.
Interim card P&L is report-only (the existing Friday roll-up) and may not
fire, pause, re-size or void anything; an open card's partial P&L cannot
materially move a fixed 18-card cumulative threshold. There is exactly one
read; if READ-1 does not fire (cumulative > $0 at card 18), the successor
question closes (§4.4).

### 2.5 READ-2 is dropped — the promotion gate is structurally unavailable, recorded as a standing fact

The wave-1 draft's READ-2 ("DISAPPOINTMENT iff the sealed rule does not
clear its pre-registered promotion evaluation for any reason") is
foreordained under the machinery as it exists and is NOT registered.
`artifacts/paper-trades/promotion.py` G1 reads `**YES**` rows from
EXIT-GRID.md's promotion-eligible sections (MR, R3f+up; the R3f-down, CONT
and earnings sections are report-only). There is no xsmom section;
XSMOM-EXITGRID's registered result is 0/26 variants pass; and G2 counts
cards only for G1 winners. The sealed xsmom rule therefore can never clear
promotion under the code as it stands — a conditional READ-2 would fire at
card 20 with probability 1, and the "incumbent promoted" branch would be
dead code. This registration instead:

1. records that structural impossibility as a **standing fact at seal** —
   not a future read, not a trigger condition;
2. makes **READ-1 the sole trigger** of successor evaluation;
3. does NOT authorize amending EXIT-GRID.md or promotion.py to create an
   xsmom G1 path — that is the post-hoc evaluation-rule change the program
   bans. Pre-registering an xsmom G1 criterion inside this trigger was
   considered and rejected: every candidate either re-promotes one of the
   0/26 failed XSMOM-EXITGRID variants or invents a new bar scored on
   burned history. It cannot be stated without inventing new bars, so it
   is not stated. A future, separately sealed xsmom promotion
   pre-registration is not prejudged by this paragraph; none exists at
   seal.

The accumulation fact that survives, stated honestly: the rule's sealed
cards still count toward PROMOTION-RULE.md's ≥20-forward-cards requirement
(promotion.py G2's own count); under current machinery that is necessary,
not sufficient, because G1 cannot clear for xsmom.

## 3. Power honesty (the trigger is a gate-opener, not a verdict)

Measured inputs (wave-1 N1-1 §1.5; recomputed and confirmed by red team
RT-2 §4): PROTOCOL in-sample 46 months mean +$288, sd $986; forward σ
assumption $800-1,000/card; at n = 18, SE(mean) ≈ $190-233.

| true mean per card | P(READ-1 fires: cum ≤ 0 over 18) |
|---|---|
| $0 | 0.500 |
| +$112.50 (XU-scale marked-down: +1.5% on $7,500) | **0.314** = Φ(−112.5/232) |
| +$288 (protocol in-sample mean) | 0.108 |

Stated plainly: under the marked-down expectation there is roughly a 31%
chance the trigger fires spuriously. That is accepted because the trigger
only OPENS A GATE: a DISAPPOINTMENT buys the shadow book its single
pre-declared evaluation (§4.4), never a roster switch. At zero true mean
the shadow book clears its own one-sided bar with probability ≈ 2.5%, so
the joint risk of spurious-fire → accidental roster swap is bounded by the
shadow book's bars. The successor bar realistically requires a true mean
≥ ~$350/card to clear. 18-20 monthly cards detect gross failure or gross
success and nothing in between; the design accepts that and refuses
magnitude claims in the middle.

## 4. The shadow book (opens only after plumbing; digest-only by machinery)

### 4.1 Construction (identical rule, different roster)

Identical construction to the sealed rule — `close(t)/close(t-273)-1`,
top 3, $2,500/leg, hold 20, 5bp RT, same seal discipline (§2.3) — on the
roster of exactly the 62 names of `artifacts/paper-trades/ohlc-panel-xu.json`
(sha256 `3753dcb862eafd67cb9635116b5cb66ed056891dd8251cb4c73ad25a036c0a5a`).
The roster REPLACES the 36; it does not augment them (a union roster has
no evidence anywhere). PLTR/SPCX are NOT added (no XU cells; SPCX has 108
sessions). WBA stays on the roster but can never rank (dead series → the
code's own `gap` exclusion); months with <30 rankable names are honest
no-trade. Ranking inputs: the trailing 273 sessions may reach into the
frozen panel period — lookback inputs dated ≤ seal are PIT-clean; the
in-sample taint (§5) applies to evaluation windows, not lookback inputs.

### 4.2 Opening sequencing (pinned)

The shadow book opens at the FIRST monthly seal after BOTH §6 plumbing
items (panel extension fetch, item 1; shadow drafter, item 2) are live —
nominally 2026-12-01. If either slips, the opening is the next monthly
seal after both land; the missed months are recorded in Appendix A and
nothing is backfilled (no synthetic shadow cards, no repaired pairing).
§6 is follow-up plumbing that this document does not implement.

### 4.3 Digest-only fence (mechanical, not procedural)

The shadow book may write exactly:

1. per-month drafts and sealed card files under its own namespace
   (`artifacts/paper-trades/xu-shadow/`), with its own ledger file
   carrying per-card rows and card sha256s — never the incumbent
   LEDGER.md;
2. per-card digest lines in that namespace's own outputs.

Nothing else. Before READ-1 fires:

- **no aggregate** (count beyond the roster, mean, sum, cumulative,
  quantile or plot) over shadow cards may be computed, written or surfaced
  by any desk code path — the digest/roll-up builders carry NO code path
  over the XU namespace (assertable by unit test; the test belongs to the
  §6 item-2 change);
- day-doc digests SUPPRESS XU card P&L lines until a read fires;
- any pre-read aggregate computation is a **protocol breach** under this
  registration, logged in Appendix A.

What this fence honestly buys (red-team correction, adopted): per-card
P&L is inherently and unavoidably visible — resolutions print per-card
blocks and anyone can mentally average. The fence keeps ~20 months of
accumulation from becoming 20 aggregated REPORTS; the load-bearing
protection is that every bar and threshold in §4.5 was fixed before card
#1 of the shadow series exists and cannot move. Both halves are required;
neither alone suffices.

**The read rule**: aggregation happens exactly once — at READ-1, and on
DISAPPOINTMENT at the shadow book's single evaluation — by the named
one-shot script `scripts/desk/xu-successor-read.py` (§6 item 3), which
refuses to run twice. No other code path may compute the READ-1 cumulative
or any shadow aggregate. (The incumbent book's existing Friday roll-up
keeps its report-only running totals; the READ-1 DECISION is still
computed once, by the one-shot script.)

### 4.4 On DISAPPOINTMENT / on no DISAPPOINTMENT

- **DISAPPOINTMENT**: the incumbent stops taking new cards (its own
  kill-switches already say so — §2.4's retire bar fires with READ-1), and
  the shadow book is evaluated ONCE against §4.5's bars. If it clears
  them, the re-seal is ONE reviewed change: `desk-universe.toml
  [xsmom].tradables` 36 → 62 names plus the pin in
  `tests/unit/test_desk_universe.py::test_xsmom_tradables_is_the_sealed_roster`,
  plus the §6 panel/history plumbing. If the shadow book also fails, the
  momentum card class ends — no third universe.
- **No DISAPPOINTMENT** (cumulative > $0 at card 18): the incumbent
  continues under its own registered kill-switches; the successor question
  closes; the shadow book is reported descriptively (one appendix table;
  means now allowed) and the 36-name roster stays sealed.

### 4.5 Bars and nulls (pre-declared; evaluated once, only on DISAPPOINTMENT)

Nulls and placebos:

1. **Random-3 placebo**: each shadow card month, draw 3 names uniformly
   from that month's rankable set (seed 20261114, 1,000 draws). The draw
   pool INCLUDES the top-3 names themselves — the correct null for a
   roster claim (pinned). Placebo legs carry identical costs and data:
   close-only panel, $2,500/leg, hold-20, 5bp RT.
2. **Incumbent-paired contrast**: per-calendar-month difference
   (shadow − incumbent) over common months, month-clustered bootstrap CI.
   The rosters are disjoint by construction (XU-XSMOM: Dow 30 + S&P 100
   minus the 37, with zero reference to how any of them traded), so the
   contrast is clean.
3. **A/A determinism twin**: `signals.xsmom_rank` is pure; re-running a
   sealed month must reproduce its top-3 byte-identically (mechanical
   check, no model; also covers the §1.2 convention pin).

Bars (all must hold; one evaluation):

- n ≥ 18 valid shadow cards;
- mean net > 0, one-sided sign-flip p < 0.05 (10,000 draws);
- 95% CI-low > 0 vs the random-3 null;
- paired mean difference vs incumbent > 0 (month-clustered bootstrap);
- no single dropped month flips the sign.

Holm correction within the 2-claim family {incumbent verdict, successor
verdict}. Any re-parameterization restarts under a new pre-registration id.

## 5. Burned history and banned forms (the fence)

- The XU-62 panel (`ohlc-panel-xu.json`, 62 names, 2021-09-13..2026-09-11,
  split-adjusted, price-return; WBA ends 2025-08-27, delisted in-window and
  kept — the survivorship-honest choice; BK ends 2026-05-20, an unexplained
  truncation that must be resolved before the §6 fetch) is FROZEN at
  2026-09-11 and was consumed in-sample through that date by: (a) the
  XU-XSMOM frozen grid — 48 cells ({60,120,252}×{5,21}×{20,60}, tercile AND
  top-3) both eras, plus the 60-skip5-h60 L-S spread; (b) XSMOM-12-1's
  XU-62 comparator legs, 16 cells; (c) the T-spin contamination enumeration
  (32 top-pick slots). Every historical XU-62 outcome window is therefore
  burned: **no successor claim may cite XU-62 backtest cells as
  confirmatory evidence** — descriptive prior only.
- Banned-list cross-check: the successor is the ALLOWED `xsmom_top3`
  direction signal on a different roster. It is not `xsmom_60skip5`
  (banned), not `xsmom_short_leg` (banned; the book stays long-only top-3),
  and not `mom_top_tercile`/`ts_momentum` (banned forms untouched). No new
  direction signal enters `signals.py`; the roster is data, not a rule.

## 6. PLUMBING (follow-up; each item a separately reviewed, owner-gated change; NOT implemented by this document)

1. **XU panel extension fetch** — extend `ohlc-panel-xu.json` past
   2026-09-11 via a research-lane script in the `fetch_ohlc.py` pattern
   (daily merge + full-tail, provenance lines, flock). Preconditions:
   resolve the BK truncation (2026-05-20; possible ticker change, not yet
   investigated) and pin the vendor symbol map (the BRK.B convention is
   already known). Do NOT add the 62 to `desk-universe.toml [panel]` for
   this purpose — that would silently enroll 62 names in the CBOE chain
   recorder (per-session quota/time cost) which the equity-only shadow
   book does not need.
2. **Shadow-card drafter** — `signals.xsmom_rank(..., names=...)` already
   accepts an arbitrary roster; a small eod-equity extension or
   research-lane twin drafts the monthly XU-62 card to a SEPARATE drafts
   namespace (`~/.local/state/trex-desk/card-drafts-xu/`) so the sealed
   book's queue is never mixed. The §4.3 enforcement (no aggregate code
   path over the XU namespace, digest suppression, and the unit test
   asserting both) belongs in this change's review checklist.
3. **One-shot read script** — `scripts/desk/xu-successor-read.py`: computes
   the READ-1 cumulative over the valid sealed cards of record, decides
   READ-1, and on DISAPPOINTMENT performs the shadow book's single §4.5
   evaluation; writes one report artifact plus a completion marker (its
   own sha) into the state namespace and refuses a second run.
   Consolidator-supervised; written and reviewed before the 18th valid
   card.
4. **The re-seal change itself** (only on trigger + shadow success):
   `desk-universe.toml [xsmom].tradables` 36 → 62 and the pin in
   `tests/unit/test_desk_universe.py` in ONE change. A `[panel]` addition
   for the 62 (with `[options_close]` decisions) is ANOTHER change, only if
   options expressions on XU names are ever wanted — the successor book is
   equity-only, so it is not carried along.

None of these is bundled with a rule change; timer/unit deployment is
owner-gated and out of scope here.

## 7. Consequences (fixed now)

- PAPER-ONLY: nothing in this document arms the canary, a mandate, or the
  supervised lane, and no scoring code ships with it.
- A DISAPPOINTMENT plus a shadow-book PASS is still only an owner-ruling
  request to make §6 item 4; nothing promotes by code.
- At most two promotion claims reach the operator in any 30-day window
  (the 2026-09-30 pattern, inherited).
- Kill-switch inheritance: the incumbent's pre-existing mid-stream
  kill-switches (§2.4) run untouched; a pause defers to READ-1; the retire
  bar and READ-1 coincide.
- If the incumbent retires under its own registered rules before the 18th
  valid sealed card for any reason other than the §2.4 bars, that event is
  recorded in Appendix A and the successor question is decided by the
  owner on the record — no automatic roster change, and no rule is
  invented here for that path.

## Appendix A — void / refusal / opening log (append-only; empty at seal)

| date | card / month | event | class / reason | evidence |
|---|---|---|---|---|
| — | — | — | — | — |
