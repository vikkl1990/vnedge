"""Small, explicit strategy registry and capital-permission choke point."""

from __future__ import annotations

from vnedge.strategy.base_strategy import BaseStrategy
from vnedge.strategy.crypto_trend_atr_margin import CryptoTrendAtrMargin
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion
from vnedge.strategy.funding_squeeze_continuation import FundingSqueezeContinuation
from vnedge.strategy.measurement_only import MeasurementOnly
from vnedge.strategy.panic_reversal import PanicReversal
from vnedge.strategy.trend_continuation import TrendContinuation
from vnedge.strategy.vol_expansion_breakout import VolatilityExpansionBreakout

STRATEGIES: dict[str, type[BaseStrategy]] = {
    MeasurementOnly.strategy_id: MeasurementOnly,
    CryptoTrendAtrMargin.strategy_id: CryptoTrendAtrMargin,
    TrendContinuation.strategy_id: TrendContinuation,
    FundingMeanReversion.strategy_id: FundingMeanReversion,
    VolatilityExpansionBreakout.strategy_id: VolatilityExpansionBreakout,
    PanicReversal.strategy_id: PanicReversal,
    FundingSqueezeContinuation.strategy_id: FundingSqueezeContinuation,
}

# Observation is deliberately non-capital even though it uses the same
# feed/DQ/Time-Machine/snapshot machinery as an executable lane.
RESEARCH_ONLY: frozenset[str] = frozenset({MeasurementOnly.strategy_id})

# Failed forward paper; retained only so historical evidence can be replayed.
KILLED: frozenset[str] = frozenset({FundingMeanReversion.strategy_id})


def is_capital_eligible(strategy_id: str) -> bool:
    """True only for a known, registered, non-killed executable."""
    return (
        strategy_id in STRATEGIES
        and strategy_id not in RESEARCH_ONLY
        and strategy_id not in KILLED
    )


def get_strategy_class(strategy_id: str) -> type[BaseStrategy]:
    try:
        return STRATEGIES[strategy_id]
    except KeyError:
        raise KeyError(
            f"unknown strategy '{strategy_id}' — registered: {sorted(STRATEGIES)}"
        ) from None
