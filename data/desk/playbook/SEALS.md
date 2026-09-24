# Desk playbook: append-only seal log

Each row seals one playbook version (sha256 of the file bytes, also in the
``.sha256`` sidecar). ``tree_options.desk.playbook.load_playbook`` refuses a
file whose bytes do not match its sidecar or whose sha256 has no row here.
Rows are only ever appended; never edit or delete a row.

| sealed (UTC) | file | sha256 | rows | basis |
|---|---|---|---|---|
| 2026-09-24T02:23:37Z | v1.toml | bc69e250a9da181016e925f382657ef2b0db6de2630e93c709b7cb7e406e6cd1 | rows=9 active=7 | v1 written from plan D5 and the Wave 2 playbook brief; sealed BEFORE any condition was computed on real data (the chain store held one session); percentile cut-offs, min history 120 and warm-up NOT_EVALUABLE fixed here; feat/desk-w2-playbook |
