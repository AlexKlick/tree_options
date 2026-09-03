# The owed POSITIONS=0 observation (event-3 analysis addendum)

2026-09-03. The successor packet's hygiene item: "the POSITIONS=0
observation belongs in the event-3 analysis." The event-3 analysis lives in
`docs/m4-g4-sealed-gate-plan.md` §4's criterion-2 amendment block (owner
ruling 2026-09-02) — but that document's BYTES are hash-bound into every
sealed packet (`criteria_source_document_sha256`, verified at
`verify_sealed_inputs`), so the note cannot be edited into it: a prose
addendum there refuses the typed input bundle for every future preflight
(this packet proved it live — the first M406 kill run's restoration suite
failed 40+ g4-family tests on exactly that refusal, before the note was
moved here). The amendment block itself is history; this file is the
addendum's home.

## The observation

Event-3 (2026-09-02, bars era, verdict FAIL 5/6) stamped `n_positions: 0`
in BOTH arms. The criterion-2 amendment block records the root cause
(zero candidates ever reached the filter: the derivation's underlying
spot was the Friday-only v1 proxy, every T+1-visible Thursday cell refused
"no spot proxy", every selected name died at the strike pick —
`no_in_band_strike` 312/312, faithfully counted). The cascade's stamped
end state was: zero candidates → zero positions → zero fills.

Two consequences worth naming:

1. **Criterion 3 passed event-3 trivially.** With no fills there was no
   participation to violate — the pooled (then per-trial) participation
   check was never exercised until event-4 put 271 fills on the book.
   Event-4's only criterion-3 failure (the cross-arm pooled double-count,
   COST 3+3 vs a 4-volume bar) was therefore invisible at event-3.
2. **The sidecar fix's before/after is positional:** event-3's 0
   positions vs event-4's 90/91 positions (arm A/B) and 271 fills — the
   v2-daily derivation spot didn't just improve the counts, it took the
   book from structurally empty to exercised.

Stamped sources: event-3 ran 2026-09-02 at gated head `60e9ff1b00fbe595`
under sealed run `1e6a64502498c5198ce4c5e1f4c07b064e5e7d2f2fb91f9335c4c1ee5e3da73c`
(workspace `artifacts/g4-sealed-runs/1e6a64502498c5198ce4c5e1f4c07b064e5e7d2f2fb91f9335c4c1ee5e3da73c/`,
untracked by design — artifacts are truth, the DB is a rebuildable index);
the two stamped trial payloads are
`…/artifacts/trials/m3-massive-derived/AAPL+…/4a395d6c841b-a-r1.json`
(sha256 `abb9d8b143251c1dfd6becb4f372f00fcc8fc3f80ba20d11dc4618dc85226dba`)
and `…/4a395d6c841b-b-r1.json`
(sha256 `cb16d6803f4c5c5d60c262ae97fafffb8ec1274fa9a3c3bd4f0dd91626b57d`),
each carrying `n_positions: 0` and the `no_in_band_strike` counters. The
summary is `…/artifacts/sealed-gate-summary.json`; the criteria text is
`docs/m4-g4-sealed-gate-plan.md` §4 (packet-hash-bound, byte-frozen) and
the event-4 evidence is `docs/evidence-logs/m4/m4-g4-sealed-gate.md`
(history at that head).
