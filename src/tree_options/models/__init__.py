"""Models (M2-proper workstream D)."""

from tree_options.models.determinism import force_single_threaded_blas

# Package import is the earliest reliable interception point: importing a
# submodule executes this initializer first.  Pin before pipeline imports
# numpy, including for callers that import RidgePipeline directly.
force_single_threaded_blas()

from tree_options.models.pipeline import FitRow, ObsRow, RidgePipeline, ScoreRow  # noqa: E402

__all__ = ["FitRow", "ObsRow", "RidgePipeline", "ScoreRow"]
