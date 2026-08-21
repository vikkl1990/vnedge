"""Tick-accepted squeeze expansion observer (v3, research/shadow only).

The v2 bar-close observer proved that compression contained useful gross
movement, but its one-fire-per-episode state machine burned the setup after a
failed probe and entered from an old level after the confirming candle closed.
V3 deliberately separates *measurement* (closed-bar compression/levels) from
*acceptance* (current executable quotes).  The runtime acceptance engine is in
``vnedge.runtime.expansion_acceptance``.

This registration is frozen.  It cannot place orders and is not capital
eligible.  Changing any default requires a new strategy id and new evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

import pandas as pd

from vnedge.plan.cost_model import CostModel
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.squeeze_expansion_breakout import SqueezeExpansionBreakout


@dataclass(frozen=True, slots=True)
class SqueezeExpansionV3Params:
    """Pre-registered lifecycle and quote-acceptance settings."""

    arm_grace_bars: int = 3
    acceptance_hold_seconds: float = 5.0
    min_acceptance_samples: int = 3
    max_quote_lag_seconds: float = 2.0
    max_quote_future_skew_seconds: float = 1.0
    max_chase_bps: float = 20.0
    break_buffer_bps: float = 2.0
    max_probes_per_side: int = 3
    max_fires_per_side: int = 2
    max_fires_per_day: int = 4
    cooldown_loss_bars: int = 2
    cooldown_win_bars: int = 4
    atr_stop_mult: float = 1.7
    round_trip_cost_bps: float = CostModel.for_profile("delta_scalp").round_trip_bps()
    breakeven_buffer_bps: float = 1.0

    def __post_init__(self) -> None:
        if self.arm_grace_bars < 1:
            raise ValueError("arm_grace_bars must be positive")
        if self.acceptance_hold_seconds <= 0 or self.min_acceptance_samples < 2:
            raise ValueError("acceptance settings are invalid")
        if self.max_quote_lag_seconds <= 0 or self.max_quote_future_skew_seconds < 0:
            raise ValueError("quote clock settings are invalid")
        if self.max_chase_bps <= 0 or self.break_buffer_bps < 0:
            raise ValueError("break/chase settings are invalid")
        if self.max_probes_per_side < 1 or self.max_fires_per_side < 1:
            raise ValueError("probe/fire limits must be positive")
        if self.max_fires_per_day < 1:
            raise ValueError("daily fire limit must be positive")
        if self.cooldown_loss_bars < 0 or self.cooldown_win_bars < 0:
            raise ValueError("cooldowns cannot be negative")
        if self.atr_stop_mult <= 0 or self.round_trip_cost_bps < 0:
            raise ValueError("risk/cost settings are invalid")


PARAMS: Final = SqueezeExpansionV3Params()

STRATEGY_SPEC = MappingProxyType(
    {
        "strategy_id": "squeeze_expansion_breakout_v3",
        "eligibility": "RESEARCH_ONLY",
        "capital_eligible": False,
        "tradeable": False,
        "timeframe": "5m",
        "params": PARAMS,
        "purpose": "closed-bar squeeze arms with quote-held breakout acceptance",
    }
)


class SqueezeExpansionBreakoutV3(BaseStrategy):
    """Feature provider for the v3 runtime acceptance state machine.

    ``signal`` is intentionally silent.  Allowing the generic bar-close path
    to emit too would recreate two conflicting entry semantics.
    """

    strategy_id = "squeeze_expansion_breakout_v3"
    eligibility = "RESEARCH_ONLY"
    timeframe = "5m"
    params = PARAMS
    warmup_bars = SqueezeExpansionBreakout.warmup_bars

    def __init__(self, funding: pd.DataFrame | None = None) -> None:
        self.funding = funding
        self._features = SqueezeExpansionBreakout(funding)

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        return self._features.prepare(candles)

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        return None
