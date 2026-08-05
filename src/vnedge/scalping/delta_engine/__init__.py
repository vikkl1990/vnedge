"""Delta India multi-timeframe scalper research engine.

The package is deliberately execution-neutral.  It creates causal signal
plans, journals every decision, and can adapt a selected plan into the
existing risk gateway.  It never submits an order itself.
"""

from vnedge.scalping.delta_engine.architecture import architecture_manifest
from vnedge.scalping.delta_engine.candle_store import MultiTimeframeCandleStore
from vnedge.scalping.delta_engine.context import MarketContextBuilder
from vnedge.scalping.delta_engine.factory import (
    DeltaScalperAssembly,
    build_delta_scalper_assembly,
)
from vnedge.scalping.delta_engine.fee_model import DeltaFeeModel, FeeBreakdown
from vnedge.scalping.delta_engine.flow_store import (
    ChannelSequenceTracker,
    FlowSnapshot,
    L2TradeFlowStore,
    SequenceHealth,
)
from vnedge.scalping.delta_engine.forward_tracker import (
    ForwardOutcome,
    ForwardOutcomeTracker,
)
from vnedge.scalping.delta_engine.scanners import (
    MomentumBurstScanner,
    OrderFlowImbalanceFadeScanner,
    Scanner,
)
from vnedge.scalping.delta_engine.signal_generator import (
    DeltaScalperSignalGenerator,
    EngineDecision,
    PipelineStage,
    ScalperRiskAdapter,
)
from vnedge.scalping.delta_engine.types import (
    Candle,
    ExitPath,
    L2Confirmation,
    MarketContext,
    Regime,
    Side,
    SignalCandidate,
)
from vnedge.scalping.delta_engine.validation import (
    RobustValidationReport,
    fee_sensitivity,
    robust_validation_report,
    untouched_window_summary,
)

__all__ = [
    "Candle",
    "ChannelSequenceTracker",
    "DeltaFeeModel",
    "DeltaScalperAssembly",
    "DeltaScalperSignalGenerator",
    "EngineDecision",
    "ExitPath",
    "FeeBreakdown",
    "FlowSnapshot",
    "ForwardOutcome",
    "ForwardOutcomeTracker",
    "L2Confirmation",
    "L2TradeFlowStore",
    "MarketContext",
    "MarketContextBuilder",
    "MomentumBurstScanner",
    "MultiTimeframeCandleStore",
    "OrderFlowImbalanceFadeScanner",
    "PipelineStage",
    "Regime",
    "RobustValidationReport",
    "ScalperRiskAdapter",
    "Scanner",
    "SequenceHealth",
    "Side",
    "SignalCandidate",
    "architecture_manifest",
    "build_delta_scalper_assembly",
    "fee_sensitivity",
    "robust_validation_report",
    "untouched_window_summary",
]
