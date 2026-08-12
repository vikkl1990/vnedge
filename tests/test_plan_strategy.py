import pandas as pd

from vnedge.plan import (
    CostModel, PlanStrategy, plan_to_signal_intent, signal_intent_to_plan,
)
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent


class FixedBase(BaseStrategy):
    strategy_id = "fixed"
    warmup_bars = 0

    def __init__(self, sig):
        self._sig = sig

    def prepare(self, candles):
        return candles

    def signal(self, df, index):
        return self._sig


def _df(close=100.0):
    return pd.DataFrame({c: [close] * 3 for c in ("open", "high", "low", "close")}
                        | {"volume": [1.0] * 3})


def test_signal_to_plan_derives_bps():
    plan = signal_intent_to_plan(
        SignalIntent("long", stop_price=99.6, take_profit_price=100.8),
        100.0, CostModel(), decision_tf="1h", time_stop_bars=48)
    assert abs(plan.risk.stop_bps - 40.0) < 0.1     # 40 bps stop
    assert abs(plan.tp1_bps - 80.0) < 0.1           # 80 bps target


def test_plan_to_signal_roundtrip():
    plan = signal_intent_to_plan(
        SignalIntent("long", stop_price=99.6, take_profit_price=100.8),
        100.0, CostModel(), decision_tf="1h", time_stop_bars=48)
    back = plan_to_signal_intent(plan, 100.0)
    assert back.side == "long"
    assert abs(back.stop_price - 99.6) < 0.01 and abs(back.take_profit_price - 100.8) < 0.01


def test_plan_strategy_passes_cost_clearing_signal():
    ps = PlanStrategy(FixedBase(SignalIntent("long", stop_price=99.6, take_profit_price=100.8)))
    out = ps.signal(_df(), 0)                        # 80 bps ≥ 2×17, net 63 > 0
    assert out is not None and out.side == "long" and ps.last_reject is None


def test_plan_strategy_filters_thin_target():
    ps = PlanStrategy(FixedBase(SignalIntent("long", stop_price=99.8, take_profit_price=100.2)))
    out = ps.signal(_df(), 0)                        # 20 bps < 2×17 → gate rejects
    assert out is None and "TP1" in (ps.last_reject or "")


def test_plan_strategy_passes_none_through():
    assert PlanStrategy(FixedBase(None)).signal(_df(), 0) is None
