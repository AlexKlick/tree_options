"""TREX Research Lab (RL) — see
``~/pop-deck-uploads/2026-09/TREX-Research-Lab-Design-and-Build-Handoff-29a9aa.md``.

This package implements RL-1 (catalog + historical comparisons + evidence
drill-down). RL-2 (scenario branching) and RL-3 (calibrated outlook) live
in later milestones and are NOT implemented here.

HARD BOUNDARY (enforced by ``tests/research/test_boundaries.py`` AST check):

    No module under ``tree_options.research`` may import
    ``tree_options.trex.ibkr``, ``.monitor``, ``.gateway_watch`` or ``.enter``.
    The research lane is broker-free and never writes the broker book,
    the monitor's ``book.json``, or the live desk evidence audit chain.
    Research writes only to its own workspace (see ``research.paths``).

This package reads the desk evidence store (via ``EvidenceStore(readonly=True)``)
for shadow-proxy and audit-trail provenance — read-only, never mutating.
"""
