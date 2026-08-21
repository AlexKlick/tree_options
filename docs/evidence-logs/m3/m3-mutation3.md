| mutant | verdict | invariant |
|---|---|---|
| M01-availability-boundary | KILLED | INV-03/04 inclusive at-close availability |
| M02-availability-gutted | KILLED | INV-03/04 future data rejected |
| M03-same-close-instant | KILLED | INV-10 instant-level same-close |
| M04-same-close-ordinal | KILLED | INV-10 ordinal-level same-close |
| M05-decision-instant-not-close | KILLED | decision_at pinned to session close |
| M06-contract-unknown-at-decision | KILLED | contract known at decision time |
| M07-execution-not-session | KILLED | fills only on real sessions |
| M08-execution-instant-mismatch | KILLED | execution instant inside labeled session |
| M09-nonstandard-deliverable-accepted | KILLED | multiplier never silently 100 |
| M10-side-size-inverted | KILLED | buy uses ask size, sell bid size |
| M11-size-fraction-gutted | KILLED | displayed-size fraction enforced |
| M12-unmarketable-limit-gutted | KILLED | unmarketable limit rejected |
| M13-price-rounding-flipped | KILLED | conservative tick rounding |
| M14-quote-age-gutted | KILLED | stale quote rejected |
| M15-future-quote-gutted | KILLED | quote from the future rejected |
| M16-crossed-gutted | KILLED | crossed quote rejected |
| M17-locked-gutted | KILLED | locked quote rejected |
| M18-nonpositive-gutted | KILLED | nonpositive side rejected |
| M19-quote-selection-reaches-back | KILLED | quote selection monotone in time |
| M20-naive-timestamp-accepted | KILLED | naive datetimes rejected |
| M21-signed-cash-flipped | KILLED | signed cash direction |
| M22-fees-zeroed | KILLED | fees charged |
| M23-duplicate-fill-accepted | KILLED | duplicate fill rejected |
| M24-ledger-underflow-gutted | KILLED | ledger underflow rejected |
| M25-independent-oracle-broken | KILLED | independent replay oracle |
| M26-embargo-checker-gutted | KILLED | INV-06 embargo checked |
| M27-anchor-checker-gutted | KILLED | INV-05 anchored train |
| M28-coverage-checker-gutted | KILLED | INV-05 test blocks disjoint |
| M29-session-grouping-gutted | KILLED | INV-05 same-session grouping |
| M30-budget-cap-off-by-one | KILLED | INV-13 32-cap exact |
| M31-duplicate-trial-accepted | KILLED | INV-13 duplicate id rejected |
| M32-outcome-cas-gutted | KILLED | REGISTERED->RUNNING->outcome ordering |
| M33-scope-json-blanked | KILLED | stored scope_json round-trips to the presented scope |
| M34-candidate-future-input-accepted | KILLED | future-available inputs rejected |
| M35-candidate-acceptance-gutted | KILLED | FAIL/NOT_EVALUABLE block acceptance |
| M36-dte-gutted | KILLED | DTE band enforced |
| M37-protocol-hash-constant | KILLED | INV-01 protocol forks on semantic edit |
| M38-calendar-checksum-ignored | KILLED | calendar tamper fails closed |
| M39-decision-close-trusts-caller | KILLED | decision close is calendar-derived, not caller-stamped |
| M40-duplicate-order-accepted | KILLED | one order mints one fill unless explicit partial sequence |
| M41-lot-basis-stale | KILLED | partial closes reduce lot basis |
| M42-scope-derivation-ignored | KILLED | scope hash derives from presented TrialScope |
| M43-security-future-mapping-visible | KILLED | point-in-time security master hides future mappings (anchor re-pinned M2: sector_on now carries the same single-line shape) |
| M44-volume-bypass-restored | KILLED | supplied volume always evaluated |
| M45-off-tick-ask-accepted | KILLED | off-tick ask rejected |
| M47-off-tick-bid-accepted | KILLED | off-tick bid rejected |
| M46-fit-on-eval-undetected | KILLED | INV-07 fit-on-train-only detected |
| M48-partial-remaining-unbounded | KILLED | partial chains bounded by REMAINING quantity (overfill occurs if unbounded) |
| M49-security-record-future-visible | KILLED | master record invisible before its own available_at |
| M50-deliverable-action-id-ignored | KILLED | standard deliverable carries no action provenance |
| M51-snapshot-incoherence-accepted | KILLED | snapshot fields must agree with the contract object |
| M52-finite-listing-end-never-honored | KILLED | finite listing_end with no delisting is honored once passed |
| M53-order-rebind-accepted | KILLED | an order_id is bound to the order that first minted a fill |
| M54-cap-type-gutted | KILLED | scope cap is an integer commitment (NaN cap would disable the cap) |
| M55-cap-storage-replaced | KILLED | the constructed cap is the stored cap (read-only storage fidelity) |
| M56-protocol-cap-lax | KILLED | protocol cap field is strict (bool/str/float never coerce into the commitment) |
| M57-cap-revalidation-gutted | KILLED | tampered cap fails closed at the enforcement point (registration refuses) |
| M58-supplied-budget-dropped | KILLED | the registry enforces the SUPPLIED budget (a tightened cap cannot be swapped for the default) |
| M59-commitment-equality-gutted | KILLED | the live cap must equal the cap COMMITTED to storage (in-range loosening refuses) |
| M60-commitment-read-misses | KILLED | the recorded commitment is READ at every registration (a missed read re-opens loosening via a swapped budget; the mutant EXECUTES a valid query that matches no row — placeholder and binding counts unchanged — rather than crashing, so a kill is behavioral only) |
| M61-migration-backfill-empty | KILLED | a scope populated before the commitment table is COMMITTED at open (no backfill re-opens its first post-upgrade registration) |
| M62-duplicate-bar-accepted | KILLED | M1-E duplicate (security, session) bars are rejected (duplicates inflate panels) |
| M63-split-discontinuity-gutted | KILLED | M1-E an overnight factor at/beyond the split bounds requires a covering action (unrepresented splits corrupt labels) |
| M64-manifest-content-gutted | KILLED | M1-D the manifest content hash is bound to the bars (a post-ingest row swap must not survive verification) |
| M65-current-ticker-join-accepted | KILLED | M1-C ticker resolution is point-in-time (a mapping announced after as_of is invisible — the current-ticker join is refused) |
| M66-future-bar-visible | KILLED | M1-C the authority never returns a bar published after decision_at (future data is invisible at the read gate); anchor re-pinned 2026-08-19 — the M2-proper C lane replaced the linear filter with the monotone bisect fast path, same leak on the new shape |
| M67-universe-survivorship-gutted | KILLED | M1-C universe membership is point-in-time (delisted names leave, pre-IPO names never enter — no current-survivor filtering) |
| M68-master-content-gutted | KILLED | M1-D the manifest content hash binds the MASTER records (a post-ingest listing swap must not survive verification) |
| M69-resolver-record-visibility-gutted | KILLED | M1-C a mapping inside a not-yet-knowable master record is invisible (record.available_at gates the whole record) |
| M70-snapshot-rebind-accepted | KILLED | M1-D the outer snapshot id cannot be rebound post-ingest (outer, manifest, and per-row ids must agree) |
| M71-sector-leak-window-open | KILLED | M2-A sector classifications are availability-gated: a reclassification between effective_from and available_at must stay invisible |
| M72-seed-stream-shifted | KILLED | M2-B/C a world is pinned to its registered seed: byte-exact registry reproduction fails if stream seeding drifts |
| M73-alpha-injection-gutted | KILLED | M2-B the planted effect actually moves closes: an alpha world must differ from its same-seed null world |
| M74-publication-hour-shifted | KILLED | M2-B every row publishes at the spec's fixed 23:00 UTC instant (the availability gates key on it; round-1 P2-2 re-pinned from hour+1, which crashed construction instead of testing detection) |
| M75-recycle-truth-gutted | KILLED | M2-B the truth sidecar records ticker recycling (the fixture's INV-08 scenario) |
| M76-initial-cohort-unlisted | KILLED | M2-B the initial cohort lists on the first session: registry worlds reproduce only with the listed universe |
| M77-bankruptcy-bound-over-gate | KILLED | M2-B/E terminal crash losses stay under the 2x undeclared-discontinuity quality gate |
| M78-split-override-not-exact | KILLED | M2-B/E split sessions derive the close exactly from the declared ratio (ratio-match quality gate) |
| M79-split-floor-suppression-gutted | KILLED | M2 round-1 P1-2: ratio events that would floor-clamp the close are suppressed (the floor exists), so any accepted spec generates a gate-clean world |
| M80-session-return-clamp-removed | KILLED | M2 round-1: undeclared overnight moves are clamped strictly inside the 2x discontinuity gate bound |
| M81-min-close-floor-removed | KILLED | M2 round-2 P1-1: closes never quantize below $1.00, where cent rounding is too small to land on the 0.5x/2x gate bounds |
| M83-application-guard-gutted | KILLED | M2 round-3 P1-1: ratio events are decided at APPLICATION time against the actual session price — never canceled-blind at announcement |
| M84-alpha-drift-wall-removed | KILLED | M2 round-4 P1-1: cumulative alpha drift is walled so the alpha-vs-base wobble and every combined session factor stay strictly inside the gate bounds and the ratio-match tolerance |
| M85-label-window-extended | KILLED | M2-B the label window is exactly H sessions after the base bar (b+1, b+H); extending it changes the label value |
| M86-staleness-skip-gutted | KILLED | M2-B a stale base bar (last visible bar older than d-1) yields NO label, never a lookback-through-the-gap label |
| M87-total-return-gutted | KILLED | M2-B split/reverse/stock-dividend labels are total-return adjusted (uniform n/d wealth multiplier); raw closes would corrupt every in-window action |
| M88-cash-dividend-dropped | KILLED | M2-B cash dividends inside the window are held unreinvested and enter the label value |
| M89-window-gap-gutted | KILLED | M2-B a gap inside the label window (lapse/delisting) yields NO label, never a span-the-gap label |
| M90-non-monotone-publication-accepted | KILLED | M2-B the two-pointer visibility walk fails closed when publication order does not follow session order |
| M91-contiguity-gutted | KILLED | M2-C calendar-contiguous lookbacks only: a lapse inside the window makes the feature absent, never imputed across the gap |
| M92-momentum-horizon-shifted | KILLED | M2-C mom_H is log(c_b / c_{b-H}) over exactly H+1 aligned bars; a shifted window changes every momentum value |
| M93-dol-vol-mean-denominator-shifted | KILLED | M2-C dol_vol_20 is the mean over exactly the 20 aligned bars |
| M94-fit-guard-registration-gutted | KILLED | M2-D fit-once and fit-session registration are guard-enforced INSIDE the pipeline (INV-07 discharged) |
| M95-fit-eval-disjointness-gutted | KILLED | M2-D scoring a pipeline on sessions it was fitted on is detected and refused at score time |
| M96-score-standardizer-leaks-eval-stats | KILLED | M2-D the standardizer carries TRAIN statistics only; recomputing them on eval rows is leakage |
| M97-blas-pin-gutted | KILLED | M2-D single-threaded BLAS is forced for every knob; multi-thread reduction order breaks byte-identical determinism |
| M98-average-rank-ties-gutted | KILLED | M2-E Spearman coefficients use deterministic average ranks for ties |
| M99-unevaluable-fabricated-zero | KILLED | M2-E unevaluable cross-sections are None, never a fabricated zero that would dilute pooled statistics |
| M100-binomial-boundary-term-dropped | KILLED | M2-E the exact binomial upper tail includes its boundary term P[X = successes]; the FP threshold is exact |
| M101-t-statistic-population-variance | KILLED | M2-E the one-sample t uses sample variance (ddof=1) |
| M102-fold-filter-gutted | KILLED | M2-F folds whose test blocks run past the world's session range are dropped whole, never half-counted |
| M103-fail-through-gutted | KILLED | M2-F a trial never ends in limbo: any execution error marks the trial FAILED before re-raising |
| M104-next-open-shifted-to-same-session | KILLED | M2-G decisions execute at the NEXT session's open, never the decision session's own open (look-ahead) |
| M105-fee-zeroed | KILLED | M2-G the campaign-fixed 5bp/side fee is charged on every ordinary trade |
| M106-conversion-ratio-inverted | KILLED | M2-G corporate-action conversion fills use the exact n/d share multiplier; value preservation is asserted by the ledger oracle |
| M107-unavailable-slot-promotes | KILLED | M2-G an unavailable top-quintile name keeps its slice in cash; promoting a lower-ranked name would silently change the registered strategy |
| M108-t1-receipt-shifted-same-day | KILLED | M3-A option files publish 09:00 ET on the NEXT session (T+1); a same-day receipt leaks session-t facts to a close(t) decision |
| M109-received-stamped-as-exchange | KILLED | M3-A quote receipt is the T+1 publication wall, never the intraday exchange stamp (receipt >= exchange) |
| M110-eligible-window-unbounded | KILLED | M3-A eligibility ranks on the bounded trailing 20-bar median dollar volume (the pre-declared 'eligible-set window' mutant: an unbounded window drifts the eligible set away from the independent oracle) |
| M111-put-delta-sign | KILLED | M3-A put |delta| = |N(d1) - 1|; using the call delta breaks the strike-band selection and put-call parity |
| M112-spread-halves-swapped | KILLED | M3-A the ask adds the half-spread, the bid subtracts it; swapping crosses the market |
| M113-zero-bid-floor-removed | KILLED | M3-A re-anchored (round 2): the defensive max(...,0)/never-locked guards are unreachable (the half-spread is proportional to mid); the LIVE zero-bid seam is the bid's tick-FLOOR rounding - sub-cent deep-wing bids quantize DOWN to 0.00, which is the bulk of the tail the gate's rejection criterion exercises |
| M114-oi-plumbed-from-wrong-instant | KILLED | M3-B every candidate input is AsOf-wrapped at the file's receipt instant; OI stamped a session ahead is future-available and must go NOT_EVALUABLE |
| M115-volume-applicability-excuses-missing | KILLED | M3-B an absent contract's missing volume is NOT_EVALUABLE - the applicability flag must never excuse a missing input (filter F4) |
| M116-settlement-pays-strike-side-swapped | KILLED | M3-C a CALL settles at max(S - K, 0); the put-side intrinsic credits the wrong side |
| M117-settlement-skips-lot-removal | KILLED | M3-C apply_settlement closes FIFO lots; leaving them open corrupts quantity and realized PnL |
| M118-settlement-kind-swapped | KILLED | M3-E expiry settlements are counted as expiries; miscounting them as terminals hides the machinery oracle |
| M119-conservation-oracle-drops-settlements | KILLED | M3-C the replay oracle independently recomputes and includes settlement cash; dropping it would certify a broken book |
| M120-force-close-missed | KILLED | M3-E a ratio action announced mid-hold forces the position closed at the next window |
| M121-execution-cancellation-dropped | KILLED | M3-D orders whose underlying had an action announced overnight are cancelled at execution, never filled blind |
| M122-exit-same-session | KILLED | M3-E arm A exits on the 4th session after entry at the 10:00 window - never same-session |
| M123-mark-at-ask | KILLED | M3-E open positions are marked at the strictly-knowable file(t-1) EOD BID, never the ask |
| M124-inv11-fraction-inverted | KILLED | INV-11 buy-side improvement rounds UP (conservative for the taker), never down |
| M125-sizing-ignores-fees | KILLED | M3-D whole-contract sizing includes per-contract fees; ignoring them overspends the budget |
| M126-quintile-over-full-universe | KILLED | M3-D the cut is quintiles (top -> calls, bottom -> puts), never the full cross-section |
| M127-dte-in-sessions | KILLED | M3-D the expiry pick targets 45 CALENDAR days' DTE (re-anchored round 2 to the nearest-target key itself — widening only the band was dominated by the days-based tie-break and semantically equivalent) |
| M128-future-file-visible | KILLED | M3-B a file is visible exactly from its receipt instant; an always-visible file leaks the future chain |
| M129-dead-contract-tradable | KILLED | M3 a dead (expired) contract must never trade; re-anchored (round 2) to the listing-window guard - for standard contracts (listing_end == expiration) it is the guard that actually fires; the explicit expired_on check is unreachable behind it (the M0 test now pins the specific rejection code) |
| M130-spans-earnings-fed-none | KILLED | M3-B spans_earnings is fed AsOf(False, receipt) - the worlds contain no earnings; None collapses every candidate to NOT_EVALUABLE (empty backtest) |
| M131-election-visibility-extends-to-close | KILLED | M3-E the election consumes only actions visible by the 10:00 window; extending visibility to the close leaks same-evening announcements |
| M132-exercise-ignores-style-guard | KILLED | M3-C european contracts have no early-exercise right; the mint must refuse |
| M133-settlement-priced-at-strike-not-close | KILLED | M3-C the settlement strikes at the reference bar's CLOSE; recording the strike breaks the oracle's independent cash recomputation |
| M134-election-ignores-dividend-branch | KILLED | M3-C branch (a): a call elects when the visible dividend dominates the file(t-1) time value |

totals: {'KILLED': 133}  total=133
restoration full-suite pass: False
