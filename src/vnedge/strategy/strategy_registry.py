"""Small, explicit strategy registry and capital-permission choke point."""

from __future__ import annotations

from vnedge.strategy.base_strategy import BaseStrategy
from vnedge.strategy.crypto_trend_atr_margin import CryptoTrendAtrMargin
from vnedge.strategy.fee_wall_momentum_observer import FeeWallMomentumObserver
from vnedge.strategy.funding_mean_reversion import FundingMeanReversion
from vnedge.strategy.funding_squeeze_continuation import FundingSqueezeContinuation
from vnedge.strategy.measurement_only import MeasurementOnly
from vnedge.strategy.panic_reversal import PanicReversal
from vnedge.strategy.range_expansion_observer import RangeExpansionObserver
from vnedge.strategy.range_expansion_observer_v2 import RangeExpansionObserverV2
from vnedge.strategy.range_expansion_observer_v3 import RangeExpansionObserverV3
from vnedge.strategy.range_expansion_observer_v4 import RangeExpansionObserverV4
from vnedge.strategy.squeeze_expansion_breakout import SqueezeExpansionBreakout
from vnedge.strategy.squeeze_expansion_breakout_v3 import SqueezeExpansionBreakoutV3
from vnedge.strategy.squeeze_expansion_breakout_v4 import SqueezeExpansionBreakoutV4
from vnedge.strategy.structure_bos_1h import StructureBos1H
from vnedge.strategy.structure_bos_15m_trigger_v2 import StructureBos15mTriggerV2
from vnedge.strategy.structure_bos_15m_trigger_v3 import StructureBos15mTriggerV3
from vnedge.strategy.trend_continuation import TrendContinuation
from vnedge.strategy.vol_expansion_breakout import VolatilityExpansionBreakout

STRATEGIES: dict[str, type[BaseStrategy]] = {
    MeasurementOnly.strategy_id: MeasurementOnly,
    FeeWallMomentumObserver.strategy_id: FeeWallMomentumObserver,
    CryptoTrendAtrMargin.strategy_id: CryptoTrendAtrMargin,
    TrendContinuation.strategy_id: TrendContinuation,
    FundingMeanReversion.strategy_id: FundingMeanReversion,
    VolatilityExpansionBreakout.strategy_id: VolatilityExpansionBreakout,
    PanicReversal.strategy_id: PanicReversal,
    RangeExpansionObserver.strategy_id: RangeExpansionObserver,
    RangeExpansionObserverV2.strategy_id: RangeExpansionObserverV2,
    RangeExpansionObserverV3.strategy_id: RangeExpansionObserverV3,
    RangeExpansionObserverV4.strategy_id: RangeExpansionObserverV4,
    FundingSqueezeContinuation.strategy_id: FundingSqueezeContinuation,
    StructureBos1H.strategy_id: StructureBos1H,
    StructureBos15mTriggerV2.strategy_id: StructureBos15mTriggerV2,
    StructureBos15mTriggerV3.strategy_id: StructureBos15mTriggerV3,
    SqueezeExpansionBreakout.strategy_id: SqueezeExpansionBreakout,
    SqueezeExpansionBreakoutV3.strategy_id: SqueezeExpansionBreakoutV3,
    SqueezeExpansionBreakoutV4.strategy_id: SqueezeExpansionBreakoutV4,
}

# Observation and pre-registered candidates are deliberately non-capital even
# though they use the same feed/DQ/Time-Machine/snapshot machinery as an
# executable lane. Promotion requires a separate reviewed allowlist change.
RESEARCH_ONLY: frozenset[str] = frozenset(
    {
        MeasurementOnly.strategy_id,
        RangeExpansionObserver.strategy_id,
        RangeExpansionObserverV2.strategy_id,
        RangeExpansionObserverV3.strategy_id,
        RangeExpansionObserverV4.strategy_id,
        StructureBos1H.strategy_id,
        StructureBos15mTriggerV2.strategy_id,
        StructureBos15mTriggerV3.strategy_id,
        FeeWallMomentumObserver.strategy_id,
        SqueezeExpansionBreakout.strategy_id,
        SqueezeExpansionBreakoutV3.strategy_id,
        SqueezeExpansionBreakoutV4.strategy_id,
    }
)

# Failed forward paper; retained only so historical evidence can be replayed.
KILLED: frozenset[str] = frozenset({FundingMeanReversion.strategy_id})

# Capital permission is an explicit promotion decision, not the absence of a
# kill decision. The safe default is deliberately empty after the 2026-08 edge
# investigation. Adding an ID here requires its reviewed, pre-registered OOS
# and paper evidence in the same change.
CAPITAL_APPROVED: frozenset[str] = frozenset()

# Separate, non-capital permission for strategies allowed to emit virtual
# intents in a SHADOW lane.  This is intentionally narrower than
# ``RESEARCH_ONLY``: being research-only does not automatically grant a live
# public-data observation process.  Killed strategies can never enter it.
SHADOW_OBSERVE: frozenset[str] = frozenset(
    {
        StructureBos1H.strategy_id,
        StructureBos15mTriggerV2.strategy_id,
        StructureBos15mTriggerV3.strategy_id,
        RangeExpansionObserver.strategy_id,
        RangeExpansionObserverV2.strategy_id,
        RangeExpansionObserverV3.strategy_id,
        RangeExpansionObserverV4.strategy_id,
        FeeWallMomentumObserver.strategy_id,
        SqueezeExpansionBreakout.strategy_id,
        SqueezeExpansionBreakoutV3.strategy_id,
        SqueezeExpansionBreakoutV4.strategy_id,
    }
)


def is_shadow_observe_eligible(strategy_id: str) -> bool:
    """True only for an explicitly allowlisted, registered, non-killed strategy."""
    return (
        strategy_id in STRATEGIES
        and strategy_id in SHADOW_OBSERVE
        and strategy_id not in KILLED
    )


def is_capital_eligible(strategy_id: str) -> bool:
    """True only for a registered strategy with explicit capital approval."""
    return (
        strategy_id in STRATEGIES
        and strategy_id in CAPITAL_APPROVED
        and strategy_id not in RESEARCH_ONLY
        and strategy_id not in KILLED
    )


def capital_denial_reason(strategy_id: str) -> str | None:
    """Explain a capital denial; ``None`` means the explicit allowlist grants it."""
    if strategy_id not in STRATEGIES:
        return "unknown strategy"
    if strategy_id in KILLED:
        return "strategy is killed"
    if strategy_id in RESEARCH_ONLY:
        return "strategy is research/measurement only"
    if strategy_id not in CAPITAL_APPROVED:
        return "strategy has no explicit capital approval"
    return None


def get_strategy_class(strategy_id: str) -> type[BaseStrategy]:
    try:
        return STRATEGIES[strategy_id]
    except KeyError:
        raise KeyError(
            f"unknown strategy '{strategy_id}' — registered: {sorted(STRATEGIES)}"
        ) from None
