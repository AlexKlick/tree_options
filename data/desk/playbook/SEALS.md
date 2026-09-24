# Desk playbook: append-only seal log

Each row seals one playbook version (sha256 of the file bytes, also in the
``.sha256`` sidecar). ``tree_options.desk.playbook.load_playbook`` refuses a
file whose bytes do not match its sidecar or whose sha256 has no row here.
Rows are only ever appended; never edit or delete a row.

| sealed (UTC) | file | sha256 | rows | basis |
|---|---|---|---|---|
| 2026-09-24T02:23:37Z | v1.toml | bc69e250a9da181016e925f382657ef2b0db6de2630e93c709b7cb7e406e6cd1 | rows=9 active=7 | v1 written from plan D5 and the Wave 2 playbook brief; sealed BEFORE any condition was computed on real data (the chain store held one session); percentile cut-offs, min history 120 and warm-up NOT_EVALUABLE fixed here; feat/desk-w2-playbook |
| 2026-09-24T02:42:24Z | v2.toml | 0d8964989b76423e2db742225b41a6ba82c6932c746c761ecfc3a3893706759f | rows=9 active=7 | v2 (active; v1 stays byte-for-byte as sealed history): operator ruling 2026-09-23 (debit spreads skip it): R1, the XSMOM call debit spread, may match while the vol state is NOT_EVALUABLE because a vertical long and short legs largely cancel vega; an evaluable state still gates R1 (cheap or fair); R2, R4, R6 keep waiting; R3 never vol-gated. Codex P2-4: PEAD incremental drift 0 until a date- and beta-matched excess is pre-registered (raw 3.89 pct kept as description). Codex P2-5: R6 declares protective_strike_rule (actual strikes, not delta ranges). Nothing else differs from v1; feat/desk-w2-playbook |
