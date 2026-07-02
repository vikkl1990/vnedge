"""Strategy registry — the single place strategies are looked up by name.

Later milestones (approval workflow, live config) reference strategies by
registry name only, so an approved strategy is always a specific, importable
class — never an ad-hoc script.
"""

from __future__ import annotations

from vnedge.strategy.base_strategy import BaseStrategy
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion
from vnedge.strategy.trend_continuation import TrendContinuation

STRATEGIES: dict[str, type[BaseStrategy]] = {
    TrendContinuation.strategy_id: TrendContinuation,
    FundingMeanReversion.strategy_id: FundingMeanReversion,
}


def get_strategy_class(strategy_id: str) -> type[BaseStrategy]:
    try:
        return STRATEGIES[strategy_id]
    except KeyError:
        raise KeyError(
            f"unknown strategy '{strategy_id}' — registered: {sorted(STRATEGIES)}"
        ) from None
