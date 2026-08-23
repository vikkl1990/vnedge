"""Corrected 15-minute BoS trigger (shadow only).

V3 preserves the V2 structure, session, volume, and economics contract.  It
applies cooldown only after the direction-specific projected-edge gate has
passed, so an uneconomic raw break cannot suppress the next valid break.
V2 remains available for exact historical replay.
"""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType
from typing import Final

import pandas as pd

from vnedge.strategy.base_strategy import SignalIntent
from vnedge.strategy.scanner_spacing import apply_final_eligibility_spacing
from vnedge.strategy.structure_bos_15m_trigger_v2 import (
    StructureBos15mParams,
    StructureBos15mTriggerV2,
)

PARAMS: Final = StructureBos15mParams()
STRATEGY_SPEC = MappingProxyType(
    {
        "strategy_id": "structure_bos_15m_trigger_v3",
        "eligibility": "RESEARCH_ONLY",
        "capital_eligible": False,
        "tradeable": False,
        "timeframe": "15m",
        "params": PARAMS,
        "context": "V2 causal structure with final-eligibility signal spacing",
    }
)


class StructureBos15mTriggerV3(StructureBos15mTriggerV2):
    strategy_id = "structure_bos_15m_trigger_v3"
    params = PARAMS

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out = super().prepare(candles)
        p = self.params
        ready = out["bos15_structure_ready"].fillna(False).astype(bool)
        trend = out["bos15_structure_trend"].astype(str)
        htf_trend = out["bos15_htf_structure_trend"].astype(str)
        bias = out["bos15_dual_avwap_bias"].astype(str)
        common = (
            ready
            & out["bos15_quality_ok"].eq(1)
            & out["bos15_session_ok"].eq(1)
            & out["bos15_volume_ok"].eq(1)
        )
        long_eligible = (
            common
            & trend.eq("up")
            & htf_trend.eq("up")
            & ~bias.eq("strong_short")
            & out["bos15_break_long"].eq(1)
            & out["bos15_projected_net_long_bps"].ge(p.min_projected_net_bps)
        )
        short_eligible = (
            common
            & trend.eq("down")
            & htf_trend.eq("down")
            & ~bias.eq("strong_long")
            & out["bos15_break_short"].eq(1)
            & out["bos15_projected_net_short_bps"].ge(p.min_projected_net_bps)
        )
        fire_long, fire_short, spacing_ok = apply_final_eligibility_spacing(
            long_eligible,
            short_eligible,
            min_bars_between_signals=p.min_bars_between_signals,
        )
        out["bos15_v3_final_eligible_long"] = long_eligible.astype(float)
        out["bos15_v3_final_eligible_short"] = short_eligible.astype(float)
        out["bos15_spacing_ok"] = spacing_ok
        out["bos15_fire_long"] = fire_long
        out["bos15_fire_short"] = fire_short
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        intent = super().signal(df, index)
        if intent is None:
            return None
        return replace(
            intent,
            reason=intent.reason.replace(
                "structure_bos_15m_trigger_v2", "structure_bos_15m_trigger_v3"
            ),
        )


__all__ = [
    "PARAMS",
    "STRATEGY_SPEC",
    "StructureBos15mParams",
    "StructureBos15mTriggerV3",
]
