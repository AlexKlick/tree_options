"""Read-only HTTP status panel for the trex execution lane.

This package is intentionally broker-free: it only reads ``book.json``,
``events.jsonl`` and the operator-authored plan TOML. It never imports
``ib_async`` and never opens a socket to the IB Gateway. The web service
can be deployed in parallel with the live ``trex-monitor`` service without
competing for the broker session.

Routes
    /                       plan list (cards)
    /plan/{plan_id}         plan detail (structures + events)
    /plan/{plan_id}/book.json   raw book.json
    /plan/{plan_id}/events.jsonl  raw events log
    /health                 JSON liveness probe

The CLI entrypoint is :func:`tree_options.trex_web.__main__.main`.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
