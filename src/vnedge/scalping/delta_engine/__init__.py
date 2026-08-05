"""Delta India multi-timeframe scalper research engine.

The package is deliberately execution-neutral.  It creates causal signal
plans, journals every decision, and can adapt a selected plan into the
existing risk gateway.  It never submits an order itself.
"""

from vnedge.scalping.delta_engine.candle_store import MultiTimeframeCandleStore
from vnedge.scalping.delta_engine.context import MarketContextBuilder
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel, FeeBreakdown
from vnedge.scalping.delta_engine.scanners import (
    MomentumBurstScanner,
    OrderFlowImbalanceFadeScanner,
    Scanner,
)
from vnedge.scalping.delta_engine.signal_generator import (
    DeltaScalperSignalGenerator,
    EngineDecision,
    ScalperRiskAdapter,
)
from vnedge.scalping.delta_engine.types import (
    Candle,
    L2Confirmation,
    MarketContext,
    Regime,
    Side,
    SignalCandidate,
)

__all__ = [
    "Candle",
    "DeltaFeeModel",
    "DeltaScalperSignalGenerator",
    "EngineDecision",
    "FeeBreakdown",
    "L2Confirmation",
    "MarketContext",
    "MarketContextBuilder",
    "MomentumBurstScanner",
    "MultiTimeframeCandleStore",
    "OrderFlowImbalanceFadeScanner",
    "Regime",
    "ScalperRiskAdapter",
    "Scanner",
    "Side",
    "SignalCandidate",
]
