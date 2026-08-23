# M4-G4 — sealed real-data gate plan (pre-declared)

Status: DRAFT (authored at G3, per `docs/m4-real-data-plan.md` §2: "separate
plan at G3"). No sealed run has started. Every `PENDING` slot below is filled
by a named prerequisite BEFORE the run; none may be filled after first look.

## 1. Purpose and scope

One sealed validation event over BOTH real worlds under protocol 0.2.x,
following the M3 discipline exactly (`scripts/run_m3_sealed_gate.py`):
pre-declared numbered criteria evaluated ONLY from stamped payload files,
verdict recorded verbatim, no re-run inside the campaign regardless of
outcome, mutation campaign + clean-clone determinism at the sealed head.

- **Lane 1 — `cboe-option-eod/1`** (the M4-A real adapter): the retained
  Cboe sample session(s). Honest limit, declared up front: the purchase lane
  is CLOSED (G2 $0 ruling), so this lane seals the ADAPTER + two-snapshot
  semantics on the data that exists — one session, SPY/TSLA/^SPX — not a
  coverage claim.
- **Lane 2 — `massive-derived-free/1`** (the M4-B/M4-C era): the coverage-era
  masters + atm-grid bars. This is the primary lane: the volume-flow
  liquidity regime, the derived-delta provenance gate, and the vwap fill
  path (bars leg) on real Massive-derived data.

## 2. Prerequisites (all blocking; each is a named commit/PR)

1. **PR #12 merged** (G3 apply, protocol 0.2.0 implementation).
2. **Coverage era COMPLETE** (3,150 masters; ERA_EXIT=0) and the era-results
   PR (`m4/era-results-20260824`) merged with its §4 census.
3. **Protocol 0.2.1 amendment** (owner-ratified): sets the two PENDING-era
   values from the §4 census — `flow_min_session_volume` (int ≥ 1) and the
   holdout window declaration. Both land as a recorded amendment, never a
   silent edit; the version-completeness validator enforces the record.
4. **Bars era landed** (atm-grid, ~5.5k series; wrapper per the custody
   cron's standing authorization) — REQUIRED for criterion 3. Recommended
   sequencing: a single sealed event AFTER the bars era, keeping the one-shot
   discipline intact (the alternative — G4a masters now, G4b bars later —
   splits the seal and doubles the mutation surface; not recommended).
5. **Holiday calendar decision** (M4-A flagged gap): the wall is
   weekend-only. Either adopt the generated repo calendar
   (`scripts/gen_calendar.py`) for the sealed run or declare the
   weekend-only wall as accepted for this gate. Must be declared BEFORE the
   run; PENDING.

## 3. Seal mechanics

- **Inputs frozen**: every sealed artifact verified by its typed manifest
  pair before the run (`verify_massive_capture_manifest` with
  `capture_version='m4b-capture/1'` for the era; the Cboe manifest for lane
  1). Any verify failure = gate does not start (this is not a criterion; it
  is a precondition).
- **Head declared**: the sealed run executes at one declared commit on
  `main`; the commit hash is written into the evidence log before the run.
- **One-shot**: the gate script runs once. Verdict PASS or FAIL is recorded
  verbatim. A FAIL triggers a remediation packet + a NEW pre-declared gate
  (the M3 correction-run pattern), never an in-place re-run.
- **Evidence**: `docs/evidence-logs/m4/m4-g4-sealed-gate.{md,json,log}` +
  the stamped payload files under `artifacts/` (slimmed reports per house
  rule: counts + samples, full lists elided).

## 4. Pre-declared criteria (evaluated from stamped payloads only)

1. `manifest_integrity` — lane 2: every master + bar file passes the typed
   manifest verify (zero silent drops); lane 1: the Cboe manifest verifies.
   Count of verified series is reported, target = the era's stamped counts.
2. `candidate_discipline` — every accepted candidate carries a delta
   provenance in the protocol's accepted set for its regime; the
   volume-flow threshold in the stamped run equals the 0.2.1 census value
   EXACTLY (any drift = FAIL); every volume-flow decision discloses the
   dropped inputs (NOT_APPLICABLE rows present, OI values withheld).
3. `fill_discipline` (bars leg) — every stamped fill executes strictly
   after its decision session, against a bar received by the execution
   instant AND belonging to the session immediately before the execution
   session (ordinal difference exactly 1); cumulative participation per
   (contract, bar session) never exceeds the bar's observed volume.
4. `rejection_paths_live` — zero-volume/unfillable and provenance-refusal
   paths fire ≥ 50 times pooled per lane (a degenerate all-pass run proves
   nothing; PENDING owner calibration at 0.2.1 ratification if the real
   refusal rate makes 50 unreachable — must be re-set BEFORE the run).
5. `determinism` — a clean-clone replay of the sealed run reproduces the
   stamped payload hashes byte-identically (the M3 cleanclone pattern).
6. `mutation_campaign` — at the sealed head: full suite green, registry
   N/N KILLED (including M171–M179), restoration TRUE. The gate script's
   own verdict logic is covered by at least one mutant.

## 5. Explicit non-goals

- No vendor purchase, no new capability classes (the $0 ruling stands).
- `spans_earnings` remains a declared gap (no events source) — stamped
  NOT_EVALUABLE, never silently False.
- No parameter search on the sealed data: the threshold and holdout come
   from the census, and criterion 2 pins the run to them.
