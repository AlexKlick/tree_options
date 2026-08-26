# M4 — protocol 0.2.1 ratification package (2026-08-26)

Branch `m4/protocol-021-20260826` (from main `6334d08`). The four
owner-ratified decisions (FIXED, owner decision 2026-08-26):

1. `flow_min_session_volume = 100` as an **owner_deviation** bound to the
   exit-5 census `43b0b040ea3c7936fc08e6b1028ce446e46c99f44ca1d87da9fec02099e12e14`
   (evidence base 214 bars: in-band n=61, below-10 1/61, below-100 17/61,
   pooled 44/214; continuity with `min_same_day_volume: 100`).
2. Holdout = **window A**: exactly the 13 dates 2026-05-08, 05-15, 05-22,
   05-29, 06-05, 06-12, 06-26, 07-10, 07-17, 07-24, 07-31, 08-07, 08-14,
   scoped to lane-2 evaluation folds, bound to the same census.
3. Calendar = **repo-generated-calendar** (the protocol-declared committed
   checksummed NYSE fixture), NOT a weekend-only wall.
4. Criterion 4 = **keep 50 both lanes + STRICT per-lane class map**
   (lane 1 firing parse refusals only, zero-bid reported-not-counted;
   lane 2 zero-volume-bar + MassiveDerivationError + master-row refusals +
   session_volume_flow below-min FAIL; no_bar NOT_EVALUABLE
   disclosed-not-counted, ~32% by construction).

The derivation path and the EXACT-fact gate stay REFUSED against the
exit-5 census; the builder admission opens owner_deviation provenance
only (proven by tests in both directions).

## Commit series (one logical commit each)

| step | commit | outcome |
| --- | --- | --- |
| (a) scoped admission + criterion-2 wording | `db7f353` | wholeness gate admits the INCOMPLETE census for owner_deviation only; derivation still refused (scoped `StaleCensusError`), EXACT-fact gate untouched; plan §4 criterion-2 re-read + `sealed-criteria.json` re-transcribed (source `8fff037e878e…`) |
| (b) holdout schema addition | `68965f2` | `tree_options.protocol.holdout` single source; `_render_schema_addition_proposal` renders the enumeration for the ratified census only (placeholder otherwise) |
| (c) calendar landing | `1113035` | `tree_options.seal.runner` production wiring (config digest over the protocol-declared fixture, checkout-independent, live re-derived); `g4_seal` preflight wires only an empty registry, execute stays unwired; `data/g4/calendar-decision.json` authored (content `28775e955138…`) |
| (d) criterion-4 class map | `fdbab19` | dated pre-run §4 amendment + `sealed-criteria.json` regeneration (source `74c71f1ff135…`) |
| (e) amendment build | `5ba1f8a` | builder exit 0 (record commit; packet below) |
| (f) this evidence row | (this commit) | evidence + suites |

## Amendment packet (builder exit 0, `landed: false`)

`artifacts/amendment/43b0b040ea3c/` (gitignored on-disk evidence):

- `amendment-packet.json` — `f0e6d27abf067d23ed160e47c5778b305e2186b7194412c4713297f3a9fb486c`
- `protocol-0.2.1-proposed.yaml` — `130643627cce333ff366553baaf25ad82245cc7e7315eaa94e54d887c9a5082d`
- `schema-addition-proposal.yaml` — `40f64d4270c60c29292f6d6b35b2d0c4f6c17a9ead1f556729b20aefeecbc313` (carries the window-A enumeration)
- `amendment-diff.md` — `fa6e5615dc33a02d7815932ece9b173746373fed6be5b54507156a3540803cef`

Builder inputs (owner-authored, outside the repo):
`/home/alexk/documents/tree_options-logs/021-owner-values.json`
(`0bac84059d40…`) and `/home/alexk/documents/tree_options-logs/021-ratified-rules.json`
(`8cab0b77df7f…`, the single `R-UNIVERSE-GRID-ACKNOWLEDGMENT` noop,
expression 3045). The proposed protocol loads through today's loader:
0.2.1, 2 amendment records, `protocol_hash d26276a9aa63…`,
version-completeness validator PASS.

**Named next action (apply)**: the owner lands
`artifacts/amendment/43b0b040ea3c/protocol-0.2.1-proposed.yaml` into
`research_protocol.yaml` as the recorded 0.2.1 amendment (the
`PENDING-OWNER-RATIFICATION` date resolves to 2026-08-26) — exactly the
G3 pattern (packet drafted → owner-ratified → applied as its own PR);
research_protocol.yaml changes ONLY through that recorded-apply step.
The `final_holdout_window` schema addition lands separately per its
landing contract in the proposal.

## Full command captures

`/home/alexk/documents/tree_options-logs/`:

- `021-branch-setup.log` — branch created from main `6334d08`
- `021-a-red.log` — RED-first admission tests (4 failed: admission refused
  pre-fix; 1 passed: EXACT-gate independence)
- `021-a-format.log`, `021-a-format2.log`, `021-a-criteria-regen.log`,
  `021-a-green.log` (91 passed), `021-a-mypy.log`, `021-a-commit.log`
- `021-b-format*.log`, `021-b-ruff-fix.log`, `021-b-green.log`
  (96 passed), `021-b-commit.log`
- `021-c-calendar-artifact.log`, `021-c-ruff.log`, `021-c-format2.log`,
  `021-c-format3.log`, `021-c-green.log` (71 passed), `021-c-commit.log`
- `021-d-criteria-regen.log`, `021-d-format.log`, `021-d-green.log`
  (62 passed), `021-d-commit.log`
- `021-e-build.log` — the amendment build (exit 0) + packet hashes +
  loader verification; `021-e-commit.log`
- `021-suite.log` — the final suites at the head before this evidence
  commit: focused touched files **141 passed**; `ruff format --check`
  215 files already formatted; `ruff check` all passed; `mypy` clean
  (112 files); full suite `pytest -W error` **1669 passed in 106.78s**,
  0 failed / 0 errors / 0 skipped
