"""Initial strategy catalog metadata for integration with Research Lab."""

from tree_options.strategy_lab.contracts import StrategyDefinition

STRATEGIES: tuple[StrategyDefinition, ...] = (
    StrategyDefinition(
        strategy_id="equal_weight_us_equities",
        version="1",
        family="control",
        registration="reference_baseline",
        required_inputs=("point_in_time_universe", "prices"),
        description="Equal-weight frozen-universe control.",
    ),
    StrategyDefinition(
        strategy_id="momentum_12_1",
        version="1",
        family="momentum",
        registration="reference_baseline",
        required_inputs=("monthly_prices_t_minus_12", "monthly_prices_t_minus_1"),
        description="Cross-sectional 12-minus-1 momentum percentile.",
    ),
    StrategyDefinition(
        strategy_id="hqm_1_3_6_12",
        version="1",
        family="momentum",
        registration="reference_baseline",
        required_inputs=("returns_1m", "returns_3m", "returns_6m", "returns_12m"),
        description="Mean of four return-horizon cross-sectional percentiles.",
    ),
    StrategyDefinition(
        strategy_id="robust_value_5metric",
        version="1",
        family="value",
        registration="reference_baseline",
        data_status="requires_pit_ratio_reconstruction",
        required_inputs=("pe", "pb", "ps", "ev_ebitda", "ev_gross_profit"),
        description=(
            "Five-metric point-in-time value percentile composite; current Massive "
            "ratios is latest-only, so historical runs require reconstructed or alternate PIT ratios."
        ),
    ),
    StrategyDefinition(
        strategy_id="cluster_rsi_factor",
        version="1",
        family="unsupervised",
        registration="exploratory",
        required_inputs=("technical_features", "lagged_returns", "lagged_factor_betas"),
        description="Deterministic K-means research lane with semantic centroid selection.",
    ),
    StrategyDefinition(
        strategy_id="sentiment_engagement",
        version="1",
        family="alternative_data",
        registration="exploratory",
        data_status="data_gated_not_run",
        required_inputs=("point_in_time_social_observations",),
        description="Monthly engagement-ranking experiment; provider-neutral.",
    ),
    StrategyDefinition(
        strategy_id="garch_intraday_contrarian",
        version="1",
        family="volatility_intraday",
        registration="exploratory",
        data_status="data_gated_not_run",
        required_inputs=("daily_returns", "intraday_bars"),
        description="Rolling GARCH + intraday RSI/Bollinger contrarian experiment.",
    ),
)


def by_id(strategy_id: str) -> StrategyDefinition:
    for strategy in STRATEGIES:
        if strategy.strategy_id == strategy_id:
            return strategy
    raise KeyError(strategy_id)
