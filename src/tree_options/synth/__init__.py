"""Synthetic world generation (M2 packet §3.B).

The generator is a VENDOR, not a shortcut: it emits vendor-shaped rows
through the unchanged M1 ingest → manifest → authority pipeline, so the
point-in-time machinery is exercised for real on generated data with
known ground truth. Determinism is the contract — the same WorldSpec
produces the byte-identical payload on any machine (stdlib-only, no
wall clock, no set-iteration order in output paths).

The truth sidecar (synth.truth) carries the generating parameters and
planted effects. It must never be imported outside tree_options.synth.*
— enforced by an AST scan test — so ground truth is structurally
unreachable from feature construction.
"""

from tree_options.synth.generate import PROVIDER, GeneratedWorld, generate_world
from tree_options.synth.spec import ActionRates, AlphaSpec, WorldSpec

__all__ = [
    "PROVIDER",
    "ActionRates",
    "AlphaSpec",
    "GeneratedWorld",
    "WorldSpec",
    "generate_world",
]
