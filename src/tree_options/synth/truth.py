"""World truth sidecar (M2 packet §3.B): the generating parameters and
planted effects of a world, for EVALUATION ONLY.

Import boundary (enforced by test_truth_sidecar_import_boundary): no
module outside tree_options.synth.* may import this package's truth
module — ground truth must be structurally unreachable from feature
construction. M2 evaluation code will import it through a deliberate,
reviewed seam when that campaign begins.
"""

from __future__ import annotations

from datetime import date

from tree_options.schemas.common import IdStr, StrictModel
from tree_options.synth.spec import ActionRates, AlphaSpec

LIFECYCLE_KINDS = (
    "ipo",
    "rename",
    "split",
    "reverse_split",
    "stock_dividend",
    "cash_dividend",
    "merger",
    "bankruptcy_11",
    "voluntary_delisting",
    "coverage_lapse",
)


class LifecycleEvent(StrictModel):
    session: date
    security_id: IdStr
    kind: str  # one of LIFECYCLE_KINDS


class WorldTruth(StrictModel):
    world_id: IdStr
    kind: str  # "null" | "alpha"
    seed: int
    sectors: tuple[str, ...]
    rates: ActionRates
    alpha: AlphaSpec | None
    events: tuple[LifecycleEvent, ...]
    recycled_tickers: tuple[str, ...]
    sector_of: dict[str, str]
