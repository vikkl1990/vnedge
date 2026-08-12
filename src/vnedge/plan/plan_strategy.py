"""PlanStrategy — route any BaseStrategy through the cost-aware TradePlan contract.

The base strategy provides the economic logic (direction, stop, target). This
wrapper re-expresses each signal as a cost-priced TradePlan and emits a tradable
SignalIntent ONLY when the hard cost gate passes (expected net bps > 0 and TP1
clears a safety multiple of round-trip cost). That is exactly "rewrite funding_mr
onto the contract" — same logic, plus cost discipline. Orders still go through
the unchanged PreTradeRiskGateway; nothing here places one.
"""
from __future__ import annotations

import pandas as pd

from vnedge.plan.adapters import plan_to_signal_intent, signal_intent_to_plan
from vnedge.plan.cost_model import CostModel
from vnedge.plan.trade_plan import plan_gate
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent


class PlanStrategy(BaseStrategy):
    def __init__(
        self,
        base: BaseStrategy,
        cost_model: CostModel | None = None,
        *,
        decision_tf: str = "1h",
        time_stop_bars: int = 48,
        funding_bps: float = 0.0,
    ) -> None:
        self.base = base
        self.cost_model = cost_model or CostModel()
        self.decision_tf = decision_tf
        self.time_stop_bars = time_stop_bars
        self.funding_bps = funding_bps
        self.strategy_id = f"{getattr(base, 'strategy_id', 'base')}_plan_v1"
        self.warmup_bars = base.warmup_bars
        self.last_plan = None
        self.last_reject: str | None = None

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return self.base.prepare(candles)

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        sig = self.base.signal(df, index)
        if sig is None:
            return None
        close = float(df["close"].iloc[index])
        plan = signal_intent_to_plan(
            sig, close, self.cost_model,
            decision_tf=self.decision_tf, time_stop_bars=self.time_stop_bars,
            funding_bps=self.funding_bps, source=self.strategy_id,
        )
        if plan is None:
            self.last_reject = "targetless"
            return None
        self.last_plan = plan
        ok, reasons = plan_gate(plan, self.cost_model)
        if not ok:
            self.last_reject = "; ".join(reasons)   # cost gate filtered this setup
            return None
        self.last_reject = None
        return plan_to_signal_intent(plan, close)
