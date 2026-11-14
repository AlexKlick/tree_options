# DESK-BT-001 CLARIFICATIONS (pre-run, narrowing-only)

Written 2026-11-14, before DESK-BT-001 has run: no scored run exists, no
`DESK-BT-001.{json,md}` exists under any `DESK_STORE/evaluations/`, and the
run code had not been written when the readiness review closed. Sealed by
`DESK-BT-001-clarifications.md.sha256` in the commit that adds this file.

**Spec of record (quoted scope):** `docs/desk/DESK-BT-001.md`, sha256
`e3e1d12d75b043c6f344c928290f5f5a813c22a8df327de6c28e2d9943139f77`, as
amended by `docs/desk/DESK-BT-001-AMENDMENT-1.md`, sha256
`b4e2626aeb5d75f808b47941f07a357ef2c4fdcd947a2e06b5c3c89b71ef1f86`
("where the two differ, this amendment governs; everything it does not name
stays exactly as written"). Those bytes are never edited here.

**Device invoked (verbatim from the original, "Earliest run date and
procedure"):** "An ambiguity found while writing it is resolved in a
separately sealed `DESK-BT-001-clarifications.md` BEFORE the run, and a
clarification may only narrow a rule, never choose between outcomes."

**Every item below is narrowing-only.** Each pins exactly one mechanical
reading of an ambiguous phrase in the spec of record, or records an as-run
provenance duty; none selects between outcomes, adds, removes or re-grids a
window, structure, exit rule, haircut rule, placebo draw or test. Where a
reading could conceivably change a number, the reading pinned is the letter
of the spec's own words, not the alternative. Evidence base: the O1
execution readiness memo of 2026-11-14
(`~/.local/state/trex-strategy-fanout-20261114/wave1/o1-deskbt001-readiness.md`),
whose session arithmetic, input inventory and divergence list (§2, §4, §5)
this document pins; all serving-checkout facts cited there were re-verified
the same day (33 recorded chain sessions; first 20 end 2026-10-20; the
2026-09-23 manifest reads 37x `status: "missing"`; the 2026-10-29 manifest
reads 37x `missing`).

**Status of the window (disclosed, not a rule edit):** the earliest-run
condition is met as of 2026-10-21 -- one session later than the letter's
"2026-10-20", because 2026-09-23 is permanently missing (A6). These
clarifications are therefore before-the-run, not before-the-window; the
prereg's own BEFORE-THE-RUN clause is the only authority they claim.

## A1. Strike-rank basis for S2/S3 (narrowing the rank set)

Quoted: "Strike ranks count distinct strikes with bars on D" (original),
"strike ranks count distinct strikes with bars on E0" (Amendment 1); "S2
call debit spread: buy the K0 call, sell the call 2 ranks above K0"; "S3
bull put credit spread: sell the put 1 rank below K0, buy the put 3 ranks
below K0"; "A leg without a bar on E0 or on the exit session makes the
(entry, structure) NOT_EVALUABLE".

Ambiguity: ranks among ALL distinct strikes of the chosen expiry with any
bar on E0 (call or put), or among strikes with a bar of the leg's own right
(the exploratory `historical_replay._legs` convention)?

Clarified (the letter of the spec): **ranks are computed over all distinct
strikes of the chosen expiry with ANY bar on E0, sorted ascending; "n ranks
above/below K0" means n positions in that list.** The strike so selected
must then carry a bar of the leg's right on E0 (and on the exit session);
if it does not, the (entry, structure) is NOT_EVALUABLE under the leg rule
-- no substitution, no per-right re-ranking. K0 itself keeps its own rule
(nearest F among strikes with a call AND a put bar on E0, ties to the lower
strike). This is narrowing: it fixes which contracts the sealed words
already named; it does not choose between strikes the words left equally
valid.

## A2. Placebo candidate pool and hash-string structure token

Quoted (Amendment 1): "the candidates are the names for which the same
structure is evaluable at E0 and which did not fire the same signal on D;
the draw is candidate number int(sha256(\"DESK-BT-001-A1|E0|X|structure\")[:16],
16) mod n of the candidates sorted by name (E0 and X in ISO date / ticker
form)"; "No candidate: the pair is dropped (counted)."

Clarified, four mechanical pins:

1. **Pool:** "the names" = the 29 names with on-disk option bars in
   `artifacts/massive-cache` -- AAPL ADBE AMD AMZN AVGO COST CRM DIS GOOGL
   HD INTC IWM JPM KO LLY MA META MSFT NFLX NVDA PEP PG QCOM QQQ SPY TSLA
   UNH V XOM (the original 29; SPY/QQQ/IWM included). Names without bars
   cannot be structure-evaluable and so never appear anyway; the pin keeps
   the pool from drifting with later captures.
2. **"did not fire the same signal on D":** XSMOM -- names not in that
   month's `desk.signals.xsmom_top3` top-3 on D; PEAD -- names with no beat
   (`pead_beats` fires) on D. The signal name X itself is excluded by this
   condition.
3. **Structure token:** the `structure` field of the hash string is
   literally one of `S1`, `S2`, `S3`; `E0` is the ISO date `YYYY-MM-DD` of
   the entry session; `X` is the signal name's ticker. The string hashed is
   exactly `DESK-BT-001-A1|<E0>|<X>|<S1|S2|S3>` (pipe-separated, UTF-8).
4. **The placebo trade is the same structure on the drawn name at the same
   E0**, priced by the identical machinery: its own expiry/forward/K0/ranks
   on its own bars, its own exit session, its own per-name haircut h, the
   same commissions. A dropped pair (no candidate) removes the pair from
   test (b) only; the signal trade itself stays in (a), (c) and (d), whose
   T counts all evaluable signal trades.

## A3. Haircut chain-symbol filter: standard roots and monthly expiries

Quoted: "over the first 20 recorded chain sessions, the contracts of the
name with a monthly expiry at 30 <= DTE <= 60, |ln(K/F)| <= 0.10, bid > 0
and ask >= bid".

Clarified (the filter is symbol-shape-silent as written; these pins close
that hole without touching any threshold):

1. **Standard roots only:** a chain row counts only if its OCC symbol
   matches the standard shape of the recorded underlying
   (`desk.aibear.monitor.standard_occ`: `<underlying><YYMMDD><C|P><strike
   x1000 in 8 digits>`), the same filter `Chain` already applies. The
   B1VDA/B1SLA-style adjunct rows that appear from 2026-11-05 sit outside
   the first 20 sessions, but the filter is pinned for shape, not date.
2. **Monthly expiry** has the option-reader's definition
   (`desk.ivhist.is_monthly_expiry_session`): the third Friday, or the last
   session before it when that Friday is not a session.
3. **The haircut's F** uses the spec's own forward construction with the
   chain session's data: F = S · exp(r · tau / 365), S = the chain doc's
   underlying quote close for that session, r = DTB3 as of that session
   (latest observation dated on or before it), tau = calendar days to the
   row's expiry.

## A4. Snapshot pins for growing inputs (recording duties; refusal rules)

Duties, not rule changes. Static inputs are pinned by exact sha256 and the
runner refuses any mismatch:

| Input | Pin (sha256) |
|---|---|
| `docs/desk/DESK-BT-001.md` | `e3e1d12d75b043c6f344c928290f5f5a813c22a8df327de6c28e2d9943139f77` |
| `docs/desk/DESK-BT-001-AMENDMENT-1.md` | `b4e2626aeb5d75f808b47941f07a357ef2c4fdcd947a2e06b5c3c89b71ef1f86` |
| `data/desk/playbook/v1.toml` | `bc69e250a9da181016e925f382657ef2b0db6de2630e93c709b7cb7e406e6cd1` |
| `data/calendar/trex/nyse_sessions_2018_01_02_2028_12_29.json` | `5c038a891c6ea99580b670518402fef7849ffd7a0f53d268fabdf1263f005f6b` (sidecar-verified by the calendar loader) |
| `desk-universe.toml` | `5f79bc3fec2c078e4b4f17e8f3e20d942d12952b105d431f97bbf5935be94066` (xsmom.tradables must number 36) |

Growing inputs are pinned by **consumed-slice digests** -- the runner
recomputes each slice at run time and refuses on mismatch; the full-file
sha256 of each is recorded in the run stamp as context (files grow; the
consumed history must not move). Slices and pins as measured on the serving
checkout, 2026-11-14:

| Input | Consumed slice | Slice sha256 | Full-file sha256 (context) |
|---|---|---|---|
| `artifacts/paper-trades/ohlc-panel.json` | rows dated <= 2026-09-03, canonical JSON of the parsed rows (sort_keys, compact separators) | `42ee7cd174a3748b0ea23bbc0268ee636d15d5ad3b2cc5fb70cc9e8f24a382e6` | `9fbde932dfade93e701167f16ac30aed3476eb7379d0530e867315a8b04c4b93` |
| `artifacts/desk-store/indices/DTB3.csv` | raw CSV lines dated <= 2026-10-20 (header kept, newline-joined) | `4374f7741896985eeb8e255ef5c04a53390242d91173c1e2f362f28495f6a708` | `cedc47f42bbbe14b3dd4f8f6f0a26a4bf2f192329cd839d16b85381908dd4cb9` |
| `artifacts/paper-trades/earnings-calendar.json` | entries with report date <= 2026-08-31, canonical JSON | `e096a1ec2c099dbdc5a1ed6403c458e9f02d7e1dde527463d44c758e60fc26dc` | `916569ad1f81bcf8a074fb8594fc1de26290210eff79a1e92fef7131888c0383` |

The cutoffs are the consumption bounds, not new rules: signals consume
panel rows and earnings reports only inside the 2024-09-03..2026-08-31
entry window (a report dated after 2026-08-31 cannot have its first
post-report session inside the window); the equity card's close(exit) and
the DTB3 entries read no row dated after 2026-09-03 (the bar scan itself
stops at 2026-09-03, so no evaluable exit can sit later); the haircut's
chain forward reads DTB3 only through 2026-10-20, the 20th recorded
session. The runner additionally refuses if it would read any panel row
dated after 2026-09-03. The panel grows nightly and its full-file sha will
differ at run time; that is expected and only the slice digest binds.

## A5. Cache scope and the actual max bar session

Quoted: "the Polygon daily bars in `artifacts/massive-cache` (the
IVHIST-001 cache and reader rules...)".

Clarified: **option bars come from `artifacts/massive-cache` ONLY.**
`massive-cache-desk` (90-270 DTE capture) and `massive-cache-purged-0904`
are excluded; no multi-cache merge is performed (the exploratory
`merge_scans` pattern is explicitly not used). The scan runs under the
sealed IVHIST-001 window 2024-08-26..2026-09-03 with reader DTE bounds
7..60 (the entry rule's 30..60 plus the exit floor of 7) and
monthly expiries only; the run reports the cache's input digest, file
count and **the actual maximum bar session present in the scan** -- the
amendment's "about 2026-09-03" is an estimate, and the true end date
decides the last entries' evaluability; it is reported, never assumed. The
count of entries NOT_EVALUABLE because the exit session has no bars (the
amendment's declared known limit) is reported beside it.

## A6. Miss-count reading: the one-session shift

Quoted: "if no session is missed the 20th is 2026-10-19..., so the earliest
run date is 2026-10-20, later by one session for every session the recorder
misses (the manifests decide)."

Clarified (reading, no rule change): a "missed session" counts only inside
the first 20 recorded sessions. **2026-09-23 is permanently missing** (its
manifest reads 37x `status: "missing"`; CBOE served the 09-22 snapshot all
day), which shifts the earliest run date from 2026-10-20 to **2026-10-21**.
The 2026-10-26..2026-10-30 recorder outage ("no recorder run for this
session"; recording resumed 2026-11-02) falls after the 20th recorded
session and shifts nothing -- the haircut reads only the first 20. The first
20 recorded sessions, in order, are:

2026-09-22, 2026-09-24, 2026-09-25, 2026-09-28, 2026-09-29, 2026-09-30,
2026-10-01, 2026-10-02, 2026-10-05, 2026-10-06, 2026-10-07, 2026-10-08,
2026-10-09, 2026-10-12, 2026-10-13, 2026-10-14, 2026-10-15, 2026-10-16,
2026-10-19, **2026-10-20**.

The runner pins this exact list plus the aggregate sha256 of the chain
manifests through 2026-10-20 (21 manifest files, the 20 recorded sessions
plus the 09-23 miss that decided the shift;
`9ec48aa44fa71fcd131bcbf7d5178ae9691ed616b995fb8e0e1a0aba9ae97ca7`, the
sha256 over each manifest's name and bytes in sorted order) and refuses a
store whose first twenty differ. The earliest-run condition -- at least 20
recorded sessions and a run date after the 20th -- is enforced by the
runner, not assumed.

## A7. Disclosed exploratory adjacency (context, not a rule edit)

An exploratory modeled replay of the same two signals crossed with similar
structures exists and was viewed after the seal: `historical_replay.py`
(scenario haircut 1%, a 300 USD max-loss cap that EXCLUDES trades, spot
moneyness, per-right strike ranks, decision-day contract selection),
documented in `HIST-EXPLORATORY.md` and `STRATEGY-REVIEW-20260927.md`, run
full-window on 2026-09-27. It evaluates none of the sealed bars, applies
none of the sealed rules, and its trade cap has no counterpart here. The
desk has therefore seen adjacent modeled option P&L for these signals; the
run report's context section states the no-peeking claim in exactly this
weakened form: nothing computed by the sealed runner -- no option bar
priced for a signal entry, no placebo drawn, no spread haircut measured
under the sealed rule -- existed before this study's seal on 2026-09-23
beyond the disclosed exploratory lane.

## No now-false rules found

The readiness review found no contradiction inside the sealed rules
themselves: entry/exit, cost, placebo and test text remain implementable as
written, and the amendment's known limit (late exits past the bar end) is
reported, not corrected. Three near-miss divergences in the exploratory
scaffold (`historical_replay.py`) are corrected to the seal in the runner
and are NOT carried over: moneyness tested on the forward F (not spot),
strike ranks on distinct strikes with bars (not per-right), and the
measured haircut h (not a scenario input).
