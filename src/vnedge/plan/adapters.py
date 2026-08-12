"""Adapters between the TradePlan contract and the existing SignalIntent path.

`signal_intent_to_plan` re-expresses any strategy's SignalIntent as a cost-priced
TradePlan (deriving bps from its stop/target); `plan_to_signal_intent` converts a
plan back to the SignalIntent the existing PreTradeRiskGateway consumes. Nothing
here bypasses the gateway — a plan only becomes a tradable intent after the hard
cost gate passes.
"""
from __future__ import annotations

from vnedge.plan.cost_model import CostModel
from vnedge.plan.trade_plan import (
    AISpec, CostSpec, EntrySpec, ProfitSpec, RiskSpec, Target, TradePlan,
    stop_price, target_price,
)
from vnedge.strategy.base_strategy import SignalIntent


def signal_intent_to_plan(
    sig: SignalIntent,
    ref_price: float,
    cost_model: CostModel,
    *,
    decision_tf: str,
    time_stop_bars: int,
    funding_bps: float = 0.0,
    source: str = "",
) -> TradePlan | None:
    """Re-express a SignalIntent as a cost-priced TradePlan (None if targetless)."""
    if ref_price <= 0 or sig.take_profit_price is None:
        return None
    stop_bps = abs(ref_price - sig.stop_price) / ref_price * 1e4
    if stop_bps <= 0:
        return None
    levels = sig.take_profit_levels or (sig.take_profit_price,)
    n = len(levels)
    targets = tuple(
        Target(bps=abs(lvl - ref_price) / ref_price * 1e4, size_pct=100.0 / n)
        for lvl in levels
    )
    costs = CostSpec(
        fee_entry_bps=cost_model.fee_bps(),
        fee_exit_bps=cost_model.fee_bps(),
        slip_entry_bps=cost_model.config.default_slip_entry_bps,
        slip_exit_bps=cost_model.config.default_slip_exit_bps,
        funding_bps=funding_bps,
    )
    # first-order estimate: if the nearest target is reached, net of full cost
    tp1 = min(t.bps for t in targets)
    expected_net = tp1 - (costs.round_trip_bps + cost_model.config.safety_buffer_bps)
    return TradePlan(
        side=sig.side, decision_tf=decision_tf,
        entry=EntrySpec(type="next_open"),
        risk=RiskSpec(stop_bps=stop_bps),
        profit=ProfitSpec(targets=targets, time_stop_bars=time_stop_bars),
        costs=costs,
        ai=AISpec(expected_net_bps=expected_net),
        source=source, reason=sig.reason,
    )


def plan_to_signal_intent(plan: TradePlan, entry_price: float) -> SignalIntent:
    """Convert a plan back to the SignalIntent the risk gateway consumes."""
    stop = stop_price(entry_price, plan.risk.stop_bps, plan.side)
    tps = tuple(target_price(entry_price, t.bps, plan.side) for t in plan.profit.targets)
    farthest = tps[-1] if plan.side == "long" else tps[-1]
    # farthest target = full-close level; keep the ladder if multiple
    return SignalIntent(
        side=plan.side,
        stop_price=max(stop, 1e-9),
        take_profit_price=max(farthest, 1e-9),
        take_profit_levels=tps if len(tps) > 1 else None,
        reason=plan.reason,
    )
