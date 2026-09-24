# IVHIST-001 results: VWAP 30-day ATM IV vs the CBOE vol indices

- Pre-registration: `docs/desk/IVHIST-001.md` (sha256 `9c426c21...ed82`), sealed in `84e3cc8` before any IV was solved. Both runs re-verified the hash.
- **Status: PROVISIONAL, pending an operator ruling.** Run 2 gives PARTIAL: one of 6 evaluable pairs passes (RVX~IWM), and GVZ~GLD is NOT_EVALUABLE, as declared.
  - Run 2 replaced run 1, which was defective (see below). The sealed pre-registration does not provide for replacing a defective run automatically, so run 2 is not the verdict of record until the operator rules.
  - Until then, every downstream use of run 2's labels is provisional too. That includes IWM's `ok`, and with it FORECAST-001's IV-squared cells and IV-blend decision.
- Machine-readable output: `docs/desk/IVHIST-001-verdict.json`, a copy of `DESK_STORE/iv-history/IVHIST-001-verdict.json`.

## Runs, stated plainly

Both runs used the same inputs:
- Polygon cache: `artifacts/massive-cache`, read-only.
- CBOE CSV snapshot and DTB3: the sha256s in the pre-registration, re-verified.
- Code run under `host-work --profile test`; the working tree was clean at each run's head.

| run | code | result | status |
|---|---|---|---|
| 1 | `7aa3425` | every pair NOT_EVALUABLE (n = 12 to 14), overall FAIL by the literal rule | **defective**, kept in the lane state `run1/` with sha256s |
| 2 | `c339f03` | the table below | **provisional** (operator ruling pending) |

**What was wrong in run 1.** The scan typed the underlying (stock) bars with the option-bar parser, `massive_options.parse_daily_bars`. That parser refuses a whole body when:
- a volume is fractional (Polygon stock volumes are fractional from 2026-02-23, for example SPY `v: 90558087.165861`), or
- a VWAP lies outside [low, high] (the stock daily VWAP includes extended-hours trades, for example ADBE on 2024-09-12, an after-close earnings day).

148 of 176 spot bodies were refused. The declared spot, the underlying's adjusted=false daily VWAP, was therefore unreadable on almost every session.

**The fix** (`c339f03`, with a test) reads only `t` and `vw` from stock bodies. It changes none of the spot definition, strikes, DTE bounds, averaging, extrapolation, rate or bar.

**It did change one filter, and that is a policy change** (Codex P2-8).
- **What changed.** Before `c339f03`, two cached stock files for the same session conflicted when any field of the bar differed. `c339f03` compared only the VWAP.
- **Faithfulness to the sealed wording.** The pre-registration says: "If cached files disagree on D, D is NOT_EVALUABLE for that name."
  - A VWAP-only reading can be argued: the VWAP is the only field the method consumes, so files that agree on it yield the same spot.
  - The whole-bar reading is the literal one. "Disagree" names no field, and a file that disagrees on the rest of the bar casts doubt on its VWAP.
  - So `c339f03` narrowed the rule relative to its most natural reading.
- **Effect: none on this cache.**

  | measure | count |
  |---|---|
  | stock (ticker, session) cells in the window | 15,240 |
  | cells cached in more than one file | 14,529 |
  | conflicts, VWAP-only rule | 17 |
  | conflicts, whole-bar rule | 17 (the same 17 sessions) |
  | cells agreeing on VWAP but differing elsewhere | 0 |

- **Restored.** `7dfb767` compares the whole bar again (tested).
- **Run 2's history is byte-identical under the restored rule;** see the consistency check.

**Readers.** Run 2 read the raw vendor snapshots: CBOE `<X>_History.csv` and the raw FRED `DTB3.csv`, with the sha256s pinned in the pre-registration.
- Live runs now read the market lane's stored format (`indices/<X>.csv`, ISO dates, `date,open,high,low,close`) with its own reader.
- The raw snapshots are read only through the explicit `--raw-snapshots` adapters (Codex P1-1).

**Run 1's numbers, verbatim:**

| pair | n | median bias | corr |
|---|---|---|---|
| VIX~SPY | 12 | -3.10 | 0.703 |
| VXN~QQQ | 12 | -1.95 | 0.661 |
| RVX~IWM | 13 | -3.26 | 0.594 |
| VXAPL~AAPL | 14 | -2.63 | 0.810 |
| VXAZN~AMZN | 14 | -2.45 | 0.949 |
| VXGOG~GOOGL | 13 | -3.65 | 0.887 |

Run 1 covered only 2024-08-28..2024-09-13, extrapolated days only. This was seen before run 2. **Operator ruling needed:** accept run 2 as the verdict of record, or rule the defective run 1 (FAIL) binding and send this to IVHIST-002.

**Calendar.** 2025-01-09 was listed by both static calendars, but the NYSE was closed and no name has a bar.
- **At the runs** (`7aa3425`), the desk treated any calendar session that no name has a bar for as a non-session. That gave the history 508 sessions per name, not 509.
- **Now** (`7dfb767`, after merging the side lane's corrected trex calendar), sessions come from that calendar, which drops 2025-01-09 through a declared `closure_overrides` entry. The inference from data is gone (Codex P1-2), so an ordinary missing bar stays a missing observation.
- **Same result.** 2025-01-09 was the only such day, and the history is byte-identical; see the consistency check.

## Consistency check after the Codex fixes (not a re-score)

The consistency check reran IVHIST-001 at `7dfb767` into TEMP paths.

**What `7dfb767` changes:**
- sessions come from the corrected trex calendar, with no closure inference
- stock duplicates conflict on the whole bar
- the stored-format readers, with the raw snapshots read through the `--raw-snapshots` adapter

**What it held fixed:** the inputs of run 2, meaning the same massive-cache (read-only), the same raw CBOE and FRED snapshots and the same `indices_dir` string. It ran under `host-work`. The job's log is `identity-7dfb767/` in the lane state.

**Result:**

| file | sha256 at run 2 | sha256 at `7dfb767` | result |
|---|---|---|---|
| `vwap_atm.json` | `105aaabecbc2a2ae...` | `105aaabecbc2a2ae...` | **byte-identical** |
| `IVHIST-001-verdict.json` | as committed | as committed (`cmp`) | **byte-identical** |

The build logged 508 sessions: the corrected calendar holds 508 in the window, where run 2 held 509 with one inferred away. Evaluable name-sessions, 13,610, and spot conflicts, 17, are unchanged. The verdict stays PARTIAL and provisional; nothing was re-scored.

## Pair metrics (run 2)

Bias is 100 * iv30 - index, in vol points. The bar: n >= 250, |median bias| <= 2.0 and correlation >= 0.85.

| pair | n | median bias (vol pts) | corr (levels) | status | mean bias | median abs diff | corr (daily changes) | extrapolated share | interpolated-only n / median bias / corr |
|---|---|---|---|---|---|---|---|---|---|
| RVX~IWM | 456 | -1.51 | 0.944 | PASS | -1.57 | 1.53 | 0.615 | 0.469 | 242 / -1.44 / 0.926 |
| VIX~SPY | 463 | -2.91 | 0.941 | FAIL | -3.20 | 2.91 | 0.655 | 0.456 | 252 / -2.82 / 0.934 |
| VXN~QQQ | 463 | -2.10 | 0.946 | FAIL | -2.16 | 2.12 | 0.598 | 0.490 | 236 / -1.97 / 0.949 |
| VXAPL~AAPL | 477 | -2.57 | 0.962 | FAIL | -2.76 | 2.57 | 0.696 | 0.459 | 258 / -2.60 / 0.968 |
| VXAZN~AMZN | 475 | -2.35 | 0.959 | FAIL | -2.50 | 2.45 | 0.764 | 0.491 | 242 / -2.30 / 0.963 |
| VXGOG~GOOGL | 469 | -2.46 | 0.933 | FAIL | -2.64 | 2.48 | 0.681 | 0.520 | 225 / -2.67 / 0.918 |
| GVZ~GLD | 0 | n/a | n/a | NOT_EVALUABLE (no GLD option bars on disk; declared) | n/a | n/a | n/a | n/a | n/a |

The informational columns decide nothing. For example, VXN~QQQ's interpolated-only median bias of -1.97 does not change its FAIL.

**Reading.** Every pair clears the correlation bar (0.933 to 0.962). Every failure is on bias alone: the ATM IV runs 2.1 to 2.9 vol points below the index. This is the declared structural risk. CBOE's indices are model-free variance-swap rates that integrate the wings; an ATM IV does not. The single-stock pairs fail too, so the single-stock generalization rule fails.

## Labels (fixed by the pre-registration)

| label | names |
|---|---|
| `ok` | IWM |
| `low-fidelity` | SPY, QQQ, AAPL, AMZN, GOOGL (benchmarked, failed); ADBE, AMD, AVGO, COST, CRM, DIS, HD, INTC, JPM, KO, LLY, MA, META, MSFT, NFLX, NVDA, PEP, PG, QCOM, TSLA, UNH, V, XOM (unbenchmarked single stocks, because VXAPL/VXAZN/VXGOG did not all pass) |
| `not-evaluable` | GLD, SMH, SOXX, XLE, XLF, XLV (no option bars on disk; TQQQ/SQQQ are outside the 35-name build) |

**Consequences (pre-registered).** `low-fidelity` histories are written and may be displayed; `desk features` carries the label beside every IV rank. They are excluded from:
- FORECAST-001's IV-squared benchmark and IV blend (only IWM enters there)
- any rule that thresholds IV rank or IV level

This holds until a successor IVHIST-002 passes. Nothing here is re-gridded.

## Coverage (run 2, 2024-08-26..2026-09-03, 508 sessions per name)

- Evaluable: 13,610 name-sessions (6,891 interpolated, 6,719 extrapolated).
- NOT_EVALUABLE:
  - 610: no expiry brackets 30 days, and none lies in 15..60 days
  - 495: no option bars (17 per name)
  - 17: conflicting unadjusted spot bars across cache files (ADBE 2, COST 3, LLY 3, UNH 3, DIS/HD/JPM/MA/META/PG 1 each)
- Scan:
  - 16,028 option bodies, 434,599 in-window monthly bars
  - 175 spot bodies read, 3 adjusted=true spot bodies skipped, 46 adjusted-root tickers (XOM1) skipped, 11,979 contract masters ignored, 0 bodies refused

## Observations (not decisions)

- **Extended-hours spot.** The declared spot, the stock daily VWAP, includes extended-hours trades. On after-close earnings days it is not synchronous with the regular-hours option VWAP. This is a known contamination of those sessions.
- **Heavy extrapolation.** About half the evaluable days are `extrapolated`, from a single expiry 15..60 days out. The on-disk capture is monthlies only.
- **A possible successor, only as a new pre-registration (IVHIST-002):** benchmark the same bars against the indices with a variance-swap-style estimator, or an explicitly declared ATM-to-variance-swap adjustment. That would be sealed before it runs.
