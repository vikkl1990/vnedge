"""Corrected 15-minute range-expansion observer (shadow only).

V4 preserves the frozen V3 setup and economics.  Its sole behavioral change
is the spacing contract: a raw breakout that later fails projected net edge
does not consume the cooldown and cannot suppress a later eligible setup.
V3 remains available for exact historical replay.
"""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType
from typing import Final

import pandas as pd

from vnedge.strategy.base_strategy import SignalIntent
from vnedge.strategy.range_expansion_observer_v3 import (
    RangeExpansionObserverV3,
    RangeExpansionV3Params,
)
from vnedge.strategy.scanner_spacing import apply_final_eligibility_spacing

PARAMS: Final = RangeExpansionV3Params()
STRATEGY_SPEC = MappingProxyType(
    {
        "strategy_id": "range_expansion_observer_v4",
        "eligibility": "RESEARCH_ONLY",
        "capital_eligible": False,
        "tradeable": False,
        "timeframe": "15m",
        "params": PARAMS,
        "purpose": "V3 expansion setup with final-eligibility signal spacing",
    }
)


class RangeExpansionObserverV4(RangeExpansionObserverV3):
    strategy_id = "range_expansion_observer_v4"
    params = PARAMS

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        out = super().prepare(candles)
        p = self.params
        final_common = (
            out["rex3_quality_ok"].eq(1)
            & out["rex3_session_ok"].eq(1)
            & out["rex3_expansion_ok"].eq(1)
            & out["rex3_volume_ok"].eq(1)
            & out["rex3_body_bps"].ge(p.body_min_bps)
            & out["rex3_projected_net_bps"].ge(p.min_projected_net_bps)
        )
        long_eligible = final_common & out["rex3_break_long"].eq(1)
        short_eligible = final_common & out["rex3_break_short"].eq(1)
        fire_long, fire_short, spacing_ok = apply_final_eligibility_spacing(
            long_eligible,
            short_eligible,
            min_bars_between_signals=p.min_bars_between_signals,
        )
        out["rex4_final_eligible_long"] = long_eligible.astype(float)
        out["rex4_final_eligible_short"] = short_eligible.astype(float)
        out["rex3_spacing_ok"] = spacing_ok
        out["rex3_fire_long"] = fire_long
        out["rex3_fire_short"] = fire_short
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        intent = super().signal(df, index)
        if intent is None:
            return None
        return replace(
            intent,
            reason=intent.reason.replace("range_expansion_v3", "range_expansion_v4"),
        )


__all__ = [
    "PARAMS",
    "STRATEGY_SPEC",
    "RangeExpansionObserverV4",
    "RangeExpansionV3Params",
]
