"""synth_options: the v2 option-chain overlay over frozen v1 equity worlds.

Import boundary: NOTHING under `tree_options.synth_options` may import
`tree_options.synth` in any form (enforced by an AST scan test mirroring
the v1 truth-sidecar boundary) — the overlay consumes the parent world's
ingested records, never the equity generator itself. Stdlib-only: the
registry pin hashes every byte of this package and it must stay
importable without numpy.
"""

from tree_options.synth_options.generate import (
    GeneratedOptionOverlay,
    OptionChainEntry,
    OptionDayFile,
    OptionQuoteSnapshot,
    contract_id_of,
    generate_overlay,
    is_quarterly_expiry,
    strike_ladder,
)
from tree_options.synth_options.spec import OptionsOverlaySpec
from tree_options.synth_options.truth import OptionsOverlayTruth

__all__ = [
    "GeneratedOptionOverlay",
    "OptionChainEntry",
    "OptionDayFile",
    "OptionQuoteSnapshot",
    "OptionsOverlaySpec",
    "OptionsOverlayTruth",
    "contract_id_of",
    "generate_overlay",
    "is_quarterly_expiry",
    "strike_ladder",
]
