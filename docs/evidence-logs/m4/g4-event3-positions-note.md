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

Stamped sources: `artifacts/g4-sealed-runs/<event-3 run>/artifacts/`
trial payloads (`n_positions`, `no_in_band_strike` counters), summarized
in `m4-g4-sealed-gate-plan.md` §4 and the event-4 evidence
(`docs/evidence-logs/m4/m4-g4-sealed-gate.md` history at that head).
