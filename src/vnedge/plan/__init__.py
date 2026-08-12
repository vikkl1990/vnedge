"""Unified, cost-aware trade-plan contract.

Every candidate — rule-based or model — emits a ``TradePlan`` with explicit,
bps-defined entry and exit and a single cost model. The hard gate is simple:
if expected net bps after ALL costs is <= 0, or TP1 cannot clear a safety
multiple of round-trip cost, the plan never reaches the gateway. This layer
enforces cost-aware profit capture; it does NOT claim bps targets create edge.
Nothing here places an order — plans are converted to SignalIntent and pass the
existing PreTradeRiskGateway unchanged.
"""
from vnedge.plan.cost_model import CostModel, CostModelConfig
from vnedge.plan.trade_plan import (
    AISpec, CostSpec, EntrySpec, ProfitSpec, RiskSpec, Target, TradePlan,
    bps_frac, plan_gate, stop_price, target_price,
)
from vnedge.plan.entry_engine import EntryEngine, EntryResult
from vnedge.plan.exit_engine import ExitEngine, ExitEvent
from vnedge.plan.adapters import plan_to_signal_intent, signal_intent_to_plan
from vnedge.plan.plan_strategy import PlanStrategy

__all__ = [
    "CostModel", "CostModelConfig",
    "TradePlan", "EntrySpec", "RiskSpec", "ProfitSpec", "Target", "CostSpec", "AISpec",
    "plan_gate", "bps_frac", "target_price", "stop_price",
    "EntryEngine", "EntryResult", "ExitEngine", "ExitEvent",
    "signal_intent_to_plan", "plan_to_signal_intent", "PlanStrategy",
]
