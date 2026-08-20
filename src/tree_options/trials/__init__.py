"""Trials (M2-proper workstream F; M3 options era)."""

from tree_options.trials.options_run import (
    OptionsSplitOverride,
    OptionsTrialResult,
    run_options_trial,
)
from tree_options.trials.run import (
    DEV_TRIAL_CONFIGS,
    DevTrialConfig,
    SplitOverride,
    TrialResult,
    run_trial,
)

__all__ = [
    "DEV_TRIAL_CONFIGS",
    "DevTrialConfig",
    "OptionsSplitOverride",
    "OptionsTrialResult",
    "SplitOverride",
    "TrialResult",
    "run_options_trial",
    "run_trial",
]
