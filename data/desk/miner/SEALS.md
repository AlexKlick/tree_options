# Desk deal-miner selection rules: append-only seal log

Each row seals one version (sha256 of the file bytes, also in the
``.sha256`` sidecar). ``tree_options.desk.selection.load_config`` refuses a
file whose bytes do not match its sidecar, whose sha256 is not pinned in
``APPROVED``, or that has no row (or more than one) here. Rows are only
ever appended; never edit or delete a row.

| sealed (UTC) | file | sha256 | status | basis |
|---|---|---|---|---|
| 2026-09-24T04:44:55Z | v1.toml | f34c96fc23bd73a75bbc932b6c053b9134e6a2a96da535f075c666697e5f826b | PROPOSED | miner lane Wave 2 Stage B (feat/desk-w2-miner): PROPOSED selection rules, pending an operator ruling before E6 goes live |
