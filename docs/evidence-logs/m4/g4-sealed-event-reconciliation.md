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

- `quantized = close.quantize(PRICE_TICK)` — a bare `quantize`, the house
  idiom (`options/settlement.py:56` does the same with `FEE_TICK`), whose
  tie rule is the decimal context default **ROUND_HALF_EVEN** (`600.125` →
  `600.12`; `137.175` → `137.18`). The rule is recorded in the code comment
  and stamped in the census payload.
- All four OHLC fields of the flat bar carry `quantized`; the flat shape is
  unchanged.
- Custody: `Lane2World.spot_close_quantized_rows` counts the rows where
  `quantized != close`, and `spot_close_max_quantization_delta`
  (`Decimal | None`, `None` when zero rows quantized) tracks
  `max(abs(close - quantized))` over those rows.
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
2. **No consumer compares a bar close against the surface spot.**
   `dataset.bars` is consumed in the trial only at `options_run.py:1356`
   (`max(bar.session …)` — the world's last session) and `options_run.py:1589`
   (`frozenset({bar.source …})` — dataset provenance). No equality against a
   spot exists anywhere.
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
sub-cent `"0.005"` variant owns the refusal guard, and a flat ≤2dp variant
owns the no-custody-noise path. No real held byte was read, no trial outcome
was computed, and no criterion was evaluated by this remediation: the sealed
verdict remains whatever the NEXT sealed event says.

## 6. Side observation (doc only, no action)

The era masters contain adjusted deliverables: `shares_per_contract` values
**84** (720 rows) and **232** (15,256 rows) alongside the standard 100. The
massive loader's nonstandard-deliverable refusal class already governs what
enters the master; this is a recorded property of the era, not a crash
class, and it is disclosed here so the next packet's census reading is not
surprised by it.

## 7. The forward path

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
   `9b945db3fa3dd92b…`; the runner's existing-registry/artifacts refusals
   enforce this mechanically.

## 8. Verification (this lane, hermetic)

- RED first: `tree_options-logs/g4-price-boundary-red.log` — the pre-fix
  suite reproduces the live crash exactly (BarRecord `decimal_max_places`,
  `Decimal('600.125')`, at `g4_event.py:298/299`); 5 failed, 13 errors.
- GREEN: `tree_options-logs/g4-price-boundary-green.log` — 20/20 in
  `tests/unit/test_g4_event_machinery.py` with the 3dp row present in the
  default bundle.
- Mutants M338-M340 (quantize drop, custody counter zeroed, sub-cent refusal
  dropped) added to the registry in `scripts/mutate.py` (325 → 328); kill
  log `tree_options-logs/g4-price-boundary-mutations.log`.
- `ruff check .` clean; `ruff format` no-op on touched files; `mypy` clean
  (120 source files).
