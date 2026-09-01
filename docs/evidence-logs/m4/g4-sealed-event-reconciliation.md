# M4-G4 sealed-event reconciliation — the consumed run of 2026-08-31

- date: 2026-08-31, branch `m4/g4-price-boundary-20260831` (from `main` at
  `c03cc6c`), remediation lane for the one-shot M4-G4 sealed event that
  consumed itself and crashed before any trial ran.
- status of the event: **CONSUMED / VERDICT UNKNOWN / RECONCILIATION_REQUIRED**.
  The owner ruling is pending; nothing in this lane re-runs it.

## 1. The consumed run (history, never re-run)

The authority ledger (`artifacts/g4-authority/ledger.jsonl`, extent
2,743 bytes, format 2) carries exactly two records, both for
`sealed_run_id 9b945db3fa3dd92bb9e65dc0bc4a522c7b30fc52f67b8f561ccff0781ef07230`:

| record | sha256 | epoch (UTC) |
| --- | --- | --- |
| APPROVAL | `468e2cf8d658add66943e510d6b41a181697535819c328fc4b7f240b001277b7` | 1788208442 (2026-08-31 20:34:02) |
| CONSUMPTION | `bb6a616b94865c7655a82a6e3d0723f6c1126c7d6775ea60d567ed9edb840380` | 1788208443 (2026-08-31 20:34:03) |

Both records bind the same identity: declared head
`c03cc6cc2883c747785c575513e3c8bdebb74704`, protocol 0.2.2
(`22c782313865fadd37fd18a3ff95ac449dc4e9740102f54f828219c354614521`),
verified packet
`a893f622a6425740ad8ea6ba7e493e6d0a75dfdaabfc8bb8337817d135461545`,
criteria artifact `c870fcd7…`, lane-1 manifest `5bc87e9e…`, lane-2 manifest
`1732e1d0…`, calendar decision `73f78f43…`, runner `m4-g4-runner/1`. The
approval ratified the Agenda-D fold geometry (min_train 40, val 12, test 13,
roll 13, embargo 2, label_horizon 5 — grid Fridays) and pre-committed to the
one-shot discipline: the verdict is recorded verbatim whatever it is, and a
FAIL means a remediation packet plus a NEW pre-declared gate, never an
in-place re-run.

What actually executed: **0 trials**. The CONSUMPTION record was appended
before the runner, the runner entered `build_lane2_world`, and the machinery
raised before a single arm ran — so no verdict was evaluated and none is
inferred. The lane-1 census artifact retained at
`artifacts/g4-sealed/lane1-adapter-census.json` (7,630 contracts, session
2023-08-25) is the only output that predates the crash; it is not a verdict.

## 2. The crash and the root cause

```
File ".../src/tree_options/trials/g4_event.py", line 298, in build_lane2_world
    dataset_bars = tuple(
  File ".../g4_event.py", line 299, in <genexpr>
    BarRecord(
pydantic_core._pydantic_core.ValidationError: 4 validation errors for BarRecord
open/high/low/close: Decimal input should have no more than 2 decimal places
[type=decimal_max_places, input_value=Decimal('417.125'), input_type=Decimal]
```

`build_lane2_world` fabricates the flat dataset bars from the lane-2
spot-proxy v1 closes (`open=high=low=close=close`, `source
"spot-proxy/declared"`), and `BarRecord`'s price fields are the shared
`Price` type (`src/tree_options/schemas/common.py:31`:
`Field(gt=0, decimal_places=2, max_digits=12)`). Real vendor closes
sometimes carry a THIRD decimal, so the real wire refused at the schema the
moment a 3dp close reached it. The machinery was authored and tested only
against synthetic captures whose closes were all ≤2dp — fixture-only
machinery cannot see real-wire precision, which is exactly the class the
sealed event exists to surface.

## 3. The read-only census of the exact HELD bytes (authoritative)

| source | rows | sub-cent (3dp) precision | constraint downstream |
| --- | --- | --- | --- |
| `spot_proxy.json` v1 (lane-2 spot proxy) | 2,871 rows, 29 names | **8 rows** (close exponent −3) | closes feed the flat `BarRecord` bars — the crash site |
| spot-proxy-v2 sidecar | 14,181 rows | 24 rows | dollar-volume arithmetic only — no Decimal-places constraint; disclosure only |
| masters `strike_price` | 10,049,160 rows across 3,045 files | **zero** (exponents only −2/−1/0) | `OptionContract.strike` is NOT a divergence site |

v1 close-exponent distribution: −3 → 8 rows, −2 → 2,564, −1 → 246, 0 → 53.
The 8 sub-cent closes: ADBE 2025-05-16 `417.125`; AMD `137.175`, `96.645`;
AVGO `136.995`, `370.825`; META `716.915`, `608.745`; NFLX `1241.475`.
Maximum HALF-UP quantization delta **0.005**. All 2,871 closes are positive;
minimum 18.89 — so the sub-cent refusal guard (below) never fires on this
corpus. With closes cent-quantized, all 2,871 `BarRecord`s construct.

## 4. The fix: quantize at the bar boundary, never widen the shared type

`build_lane2_world` now quantizes every spot-proxy close to cents AT THE
`BarRecord` BOUNDARY, reusing the existing `PRICE_TICK`
(`src/tree_options/schemas/common.py:13`, `Decimal("0.01")`):

- `quantized = close.quantize(PRICE_TICK, rounding=ROUND_HALF_EVEN)` — an
  EXPLICIT tie rule (Codex round-1 P1: a bare `quantize` inherits the
  mutable decimal CONTEXT's rounding, so a stamped payload's ties could
  depend on ambient process state). Ties resolve to the even cent:
  `600.125` → `600.12`, `137.175` → `137.18`.
- The quantize runs ONLY on a close whose wire exponent exceeds the cent
  tick (`exponent < -2`). Rows already on the grid pass through as the
  ORIGINAL `Decimal` object — bit-identical, no representation rewrite (an
  exponent-0 `6E+2` stays `6E+2`, never `600.00`), zero custody.
- All four OHLC fields of the flat bar carry `quantized`; the flat shape is
  unchanged.
- Custody: `Lane2World.spot_close_quantized_rows` counts every row the
  boundary REWRITES (including a trailing-zero `200.130`, whose value does
  not move — disclosed as rewritten with delta `0.000`), and
  `spot_close_max_quantization_delta` (`Decimal | None`, `None` when
  nothing was rewritten) tracks the largest VALUE movement among them.
- Fail-closed guard: a quantized value `<= 0` (a positive sub-cent close
  quantizes to `0.00`, which `Price`'s `gt=0` can never carry) raises a
  `ValueError` naming the underlying, the session, and the original token —
  never a silent drop, never a floor to zero.
- The census payload (`lane2_census_payload`) stamps a
  `spot_close_quantization` block — `rows_quantized`, `max_delta`, `tick`,
  `rule` — with the Decimal facts stringified per the stamped-payload idiom.

Why the boundary and NOT the shared `Price` type: `Price`'s 2dp invariant is
a repo-wide schema decision (every fill, premium, strike, and settlement
already lives on the cent grid), and widening a shared invariant to
accommodate one fabrication site would re-price the meaning of every `Price`
in the codebase. The divergence is local to ONE fabrication: the flat
dataset bar is a synthesized convenience view of the spot proxy, not vendor
truth. Three facts make the boundary quantization safe rather than lossy:

1. **The surface keeps the vendor-exact closes.** `spot` feeds
   `VwapPitSurface` verbatim; `spot_mid_as_of` still returns the un-truncated
   3dp token. Nothing on the read surface is rounded.
2. **No consumer compares a bar close against the surface spot** (verified:
   the decision reads are `surface.decision_close`, the exercise elections
   read the surface — a bar close and a surface close never meet in an
   equality anywhere). The bar closes' actual consumers: the world's session
   span and dataset provenance (`options_run.py:1356`, `:1589`), the
   backtest's bar index and settlement reference bars
   (`backtest/options.py:347`, `:511`), and the generic equity lane's bar
   map (`trials/run.py:427`, not on the G4 options path).
3. **Settlement stays consistent.** `mint_settlement` takes its
   `settlement_price` from the authoritative `BarRecord`
   (`options/settlement.py:47,70`), so any settlement struck against a
   lane-2 underlying bar settles on the same cent-quantized close the bar
   carries — one convention end to end, never a 3dp close meeting a 2dp
   price.

## 5. Fixture shape fidelity (this remediation observed no outcome)

The machinery tests now carry the real wire shape: the lane-2 fixture's
`spot_proxy.json` carries ONE 3dp close (`"600.125"` on SPY 2024-06-14 — a
synthetic VALUE in the real precision CLASS of ADBE's `417.125`), and the
default module bundle builds every end-to-end machinery test (both arms, the
gate CLI, the production runner, the clean-clone replay) against it. A
sub-cent `"0.005"` variant owns the refusal guard, a flat ≤2dp variant owns
the no-custody-noise path, and multi-row / trailing-zero variants own the
max-delta aggregation and the rewritten-but-unmoved disclosure. No REAL held
byte was read and no REAL-data trial outcome, sealed criterion verdict, or
sealed-gate verdict was computed or observed by this remediation (the
machinery tests evaluate SYNTHETIC fixture trials and criteria only): the
sealed verdict remains whatever the NEXT sealed event says.

## 5b. Criterion 6 now binds the report to the live registry and the sealed head (Codex rounds 1–3)

Adding registry entries without re-running the full campaign would have let
criterion 6 certify a stale N/N report: the evaluator read only the report's
self-declared totals. `_criterion_mutation_campaign` now takes the LIVE
registry's id set AND content digest AND the sealed head, and FAILs when the
report omits registry mutants (stale), carries ids foreign to or duplicated
in the registry, declares a total inconsistent with its own entries, counts
KILLED from its own entries rather than trusting the declared totals
(round-2 probe: an all-SURVIVED report with N/N totals previously PASSED),
lacks the matching registry digest (the mutation runner stamps
`registry_digest` — sha256 over the canonical MUTANTS list, exposed as
`mutate.registry_digest()` on the producer side and recomputed by
`live_mutation_registry` on the consumer side, pinned together by a
machinery test), was generated at a different head than the sealed one
(`--head` passed by `m0_gate.sh`, stamped into the report — the registry can
be identical across commits while the guarded code moved), or when the
registry was not supplied at all — never a silent skip.

The report's provenance is additionally protected OUTSIDE the evaluation:
`preflight_gate_auxiliaries` (called by the production runner and the sealed
CLI BEFORE the one-shot event runs) refuses an unloadable registry or an
unparseable report with nothing created and nothing consumed — an exception
at evaluation time can no longer burn the sealed workspace or, under the
seal, the CONSUMPTION. TRUST BOUNDARY (stated plainly): criterion 6
certifies that the report is internally consistent and bound to this head's
registry and commit — it does not and cannot prove the campaign physically
executed; that attestation lives in the gate pipeline and the evidence
chain, exactly as before.

## 6. Side observation (doc only, no action)

The era masters contain adjusted deliverables: `shares_per_contract` values
**84** (720 rows) and **232** (15,256 rows) alongside the standard 100. The
massive loader's nonstandard-deliverable refusal class already governs what
enters the master; this is a recorded property of the era, not a crash
class, and it is disclosed here so the next packet's census reading is not
surprised by it.

## 7. The forward path (and the two authority blockers a successor lane must clear)

1. **New head** — this remediation lands on
   `m4/g4-price-boundary-20260831` and merges to `main` (owner merges
   NORMAL).
2. **New packet** — `scripts/g4_seal.py` verify + packet build at the new
   head over the SAME retained era bytes (they are unchanged; only the
   machinery moved).
3. **NEW pre-declared gate** — a fresh owner declaration + approval at the
   new head, a fresh `sealed_run_id`, a fresh evidence triple. The consumed
   pair (APPROVAL `468e2cf8…` / CONSUMPTION `bb6a616b…`) stays in the ledger
   as history: the one-shot discipline is never satisfied by re-running the
   consumed run, and the 2026-08-31 approval's own terms (remediation packet
   plus a NEW pre-declared gate) are the ruling being followed.
4. **Never re-run** the consumed `sealed_run_id`
   `9b945db3fa3dd92b…`.

Two authority-side facts (verified in source during the Codex round-1
review) mean the successor event does NOT simply run — a follow-up
successor-enablement lane is required first, with its own review:

- **Content identity collides.** `content_identity()`
   (`src/tree_options/seal/identity.py:52-58`) deliberately blanks the
   checkout-bound fields (`code_sha`, packet hash) so "a fresh checkout of
   the same research content is not fresh one-shot authority", and
   `_check_authority` (`scripts/g4_seal.py:303`) refuses any new execution
   whose content id matches an existing CONSUMPTION. This remediation
   changes no content-bearing input (same era bytes, protocol, criteria,
   runner version), so the successor event's content id equals the consumed
   run's `0a1ea282…` and would refuse. The design has no lane for
   "consumed, no verdict, owner-ordered remediation" — the successor lane
   must add an explicit, owner-issued RECONCILIATION record that re-arms
   authority for exactly this case (naming the consumed record and the
   remediation), never a weakening of the content-identity scoping.
- **The sealed workspace paths are fixed and now occupied.**
   `production_gate_paths` (`src/tree_options/seal/g4_gate.py`) pins
   `artifacts/g4-sealed.db`, `artifacts/g4-sealed/`, and
   `artifacts/g4-sealed-scratch/`, and the machinery refuses to reuse any
   that exist (`g4_event.py` "refusing to reuse sealed …"). The crashed run
   left all three. The successor lane must make the workspace run-scoped
   (fresh paths per `sealed_run_id`, the crashed run's partials preserved
   untouched as history).

## 8. Verification (this lane, hermetic)

- RED first: `tree_options-logs/g4-price-boundary-red.log` — the pre-fix
  suite reproduces the live crash exactly (BarRecord `decimal_max_places`,
  `Decimal('600.125')`, at `g4_event.py:298/299`); 5 failed, 13 errors.
- GREEN: `tree_options-logs/g4-price-boundary-green.log` — 20/20 in
  `tests/unit/test_g4_event_machinery.py` with the 3dp row present in the
  default bundle.
- Mutants M338-M344 (quantize drop, custody counter zeroed, sub-cent refusal
  dropped, max-delta comparator flipped, exponent gate dropped, criterion-6
  stale-report binding dropped, criterion-6 registry-absence silenced) added
  to the registry in `scripts/mutate.py` (325 → 332); kill logs
  `tree_options-logs/g4-price-boundary-mutations{,2,3}.log`.
- `ruff check .` clean; `ruff format` no-op on touched files; `mypy` clean
  (120 source files); machinery suite green (23 tests after round-1
  remediation, see the verify logs below).
- Codex round 1 (gpt-5.6-sol, adversarial brief): 3 P0 / 2 P1 / 3 P2, all
  8 verified in source. The 6 in-lane findings are fixed here (explicit
  rounding, exponent-gated rewrite, representation-exact quiet path,
  max-delta guard + mutant, criterion-6 live-registry binding + mutants,
  doc corrections); the 2 authority-side P0s (content-identity collision,
  fixed sealed paths) are recorded in §7 for the successor-enablement lane.
- Codex round 2 (same reviewer, delta-focused): 2 P0 / 2 P2, all 4 verified
  in source (the forged all-SURVIVED report and the duplicate-id report
  both reproduced as PASS before the fix). Fixed: criterion 6 counts KILLED
  from the entries and rejects duplicate ids; the report must carry the
  mutation runner's `registry_digest` matching the live registry; the CLI
  derives the registry BEFORE the one-shot runs; M344's mutant now kills
  behaviorally (None-safe else) instead of by TypeError; this verification
  section rewritten to point at the post-round-2 evidence
  (`tree_options-logs/g4-price-boundary-verify5.log` and
  `-mutations3.log`).
- Codex round 3: 2 P0 / 1 P2, all verified. Fixed: the report is bound to
  the SEALED head (`mutate.py --head`, stamped by `m0_gate.sh`, required ==
  the sealed head by criterion 6 — closes the registry-identical-but-code-
  moved staleness class); `preflight_gate_auxiliaries` runs in the
  PRODUCTION RUNNER and the CLI before the one-shot event (a malformed
  report or unloadable registry refuses with nothing created — Codex's
  probe of the runner path raising after the event ran); the digest
  producer/consumer pair is pinned by a machinery test
  (`mutate.registry_digest()` == the gate's recompute) plus a
  wrong-nonempty-digest case. Mutants M345 (runner preflight dropped) and
  M346 (head binding dropped); registry 325 → 334; 9/9 KILLED with
  restoration (`tree_options-logs/g4-price-boundary-{verify7,mutations4}.log`).
  The residual — a deliberate forger who reads the code can fabricate a
  self-consistent, digest- and head-correct report — is a stated trust
  boundary (§5b), not a defect criterion 6 can close: unsigned JSON cannot
  prove execution; that attestation lives in the gate pipeline and the
  evidence chain.
- Codex round 4: 3 P0, all verified and both gate-level probes reproduced
  (a `restoration_suite_passed` of the STRING `"false"` PASSED — truthy
  under `bool()`; a `total` of `"not-an-int"` raises `ValueError` only at
  evaluation time). Fixed: `validate_mutation_report` strict-shape-validates
  every field the evaluation casts or branches on (refused at preflight,
  before anything the event creates; the era census's absence joins the
  preflight); the criterion reads `restoration_suite_passed is True`;
  `execute_sealed_run` calls the RUNNER's new `preflight()` AFTER the
  authority cross-join and BEFORE the CONSUMPTION append (a refusal now
  costs nothing — no consumption, no workspace, the approval intact; the
  round-3 in-runner preflight alone could not protect the LEDGER because
  the append precedes the runner); and `evaluate_and_record` re-validates
  the report at load so a file that changes shape between preflight and
  evaluation (the TOCTOU residue) FAILs as a verdict, never raises after
  consumption. Mutants M347 (execute preflight dropped) and M348
  (restoration flag coerced); registry 325 → 336; 11/11 KILLED with
  restoration (`tree_options-logs/g4-price-boundary-{verify9,mutations5}.log`).
