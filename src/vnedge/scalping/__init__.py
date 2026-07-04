"""Scalping hot-path primitives.

This package deliberately does not place orders. It provides the state,
feature, strategy-interface, stop, and risk-gate building blocks a future
event-driven scalper loop can compose while still routing every order through
the existing PreTradeRiskGateway and OrderManager.
"""

from vnedge.scalping.features import IncrementalFeatureEngine, ScalperFeatures
from vnedge.scalping.microstructure import (
    MarketMicroState,
    PrivateStreamState,
    TopOfBook,
    TradeTick,
)
from vnedge.scalping.risk import (
    ScalperRiskConfig,
    ScalperRiskDecision,
    ScalperRiskGateway,
    ScalperRiskLimits,
)
from vnedge.scalping.strategy import (
    BaseScalperStrategy,
    CancelIntent,
    QuoteIntent,
    ScalperDecisionContext,
)
from vnedge.scalping.tick_stop import StopRegistration, TickStopEngine

__all__ = [
    "BaseScalperStrategy",
    "CancelIntent",
    "IncrementalFeatureEngine",
    "MarketMicroState",
    "PrivateStreamState",
    "QuoteIntent",
    "ScalperDecisionContext",
    "ScalperFeatures",
    "ScalperRiskConfig",
    "ScalperRiskDecision",
    "ScalperRiskGateway",
    "ScalperRiskLimits",
    "StopRegistration",
    "TickStopEngine",
    "TopOfBook",
    "TradeTick",
]
