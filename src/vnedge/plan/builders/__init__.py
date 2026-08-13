"""PlanBuilders — locked, cost-aware rewrites of edges onto the TradePlan contract.

Each builder turns a pre-registered hypothesis into ``TradePlan | None`` and MUST
call ``plan_gate`` before returning. Builders place no orders; a sealed research
runner resolves their plans. Reviving a dead scanner means a NEW locked builder
here + a prereg + a one-shot sealed evaluation — never turning the old file back on.
"""
from vnedge.plan.builders.funding_extreme_fade_short_v2 import (
    FundingExtremeFadeShortV2Builder, FundingExtremeFadeShortV2Params,
)

__all__ = [
    "FundingExtremeFadeShortV2Builder", "FundingExtremeFadeShortV2Params",
]
