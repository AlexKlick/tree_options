"""The Wave 4 research pilot (lane I): registrations, the owner seal, usage
metering and budget enforcement at job admission.

Contract: docs/product/WAVE3-CONTRACTS.md C3.5 / C3.7
(``trex-pilot-registration/1``, ``trex-pilot-usage/1``) in
``tree_options.trex.contracts.wave3``; product notes: docs/product/PILOT.md.

- ``store``: ``registrations/<program_id>.draft.json`` / ``.sealed.json``
  under ``TREX_PILOT_DIR`` and the owner seal (write once).
- ``usage``: one ``trex-pilot-usage/1`` counter per program per ET day.
- ``admission``: the ``ResearchWorker`` admission hook for runs that carry
  ``pilot_program_id``.
- ``seal``: the ``seal_registration`` command's apply step.
- ``view``: ``trex-pilot-view/1`` for ``GET /api/pilot`` (``trex_web.pilot_view``).
- ``cli``: ``python -m tree_options.research.pilot`` (loopback).

Paper research only: nothing here sends an order, grants a mandate or
authorizes live trading. Kept import-light (the worker imports this module).
"""

from __future__ import annotations

from typing import Final

__all__ = ["PILOT_PROGRAM_FIELD"]

#: The run-record field naming the pilot program a research run is charged to.
PILOT_PROGRAM_FIELD: Final = "pilot_program_id"
