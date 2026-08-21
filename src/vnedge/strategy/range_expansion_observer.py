"""Causal 1h range-expansion continuation observer (research only).

This lane complements, rather than modifies, squeeze acceptance.  It looks for
the first close through a prior 12-hour range with meaningful candle body and
volume participation.  The 2026-08-19 move is already-seen data, so this is an
exploratory measurement registration; it cannot be promoted on that window.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

import pandas as pd

from vnedge.plan.cost_model import CostModel
from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent


@dataclass(frozen=True, slots=True)
class RangeExpansionParams:
    range_bars: int = 12
    volume_lookback: int = 24
    volume_mult: float = 1.2
    body_min_bps: float = 20.0
    break_buffer_bps: float = 5.0
    atr_period: int = 14
    atr_stop_mult: float = 1.5
    min_bars_between_signals: int = 12
    projected_reward_r: float = 2.0
    round_trip_cost_bps: float = CostModel.for_profile("swing").round_trip_bps()
    min_projected_net_bps: float = 20.0

    def __post_init__(self) -> None:
        if self.range_bars < 2 or self.volume_lookback < 2 or self.atr_period < 2:
            raise ValueError("lookback settings are invalid")
        if self.volume_mult <= 0 or self.body_min_bps <= 0:
            raise ValueError("participation settings are invalid")
        if self.break_buffer_bps < 0 or self.atr_stop_mult <= 0:
            raise ValueError("break/stop settings are invalid")
        if self.min_bars_between_signals < 1 or self.projected_reward_r <= 0:
            raise ValueError("spacing/reward settings are invalid")


PARAMS: Final = RangeExpansionParams()
STRATEGY_SPEC = MappingProxyType({
    "strategy_id": "range_expansion_observer_v1",
    "eligibility": "RESEARCH_ONLY",
    "capital_eligible": False,
    "tradeable": False,
    "timeframe": "1h",
    "params": PARAMS,
    "purpose": "first accepted 1h break from a prior 12h range",
})


class RangeExpansionObserver(BaseStrategy):
    strategy_id = "range_expansion_observer_v1"
    eligibility = "RESEARCH_ONLY"
    timeframe = "1h"
    params = PARAMS
    warmup_bars = max(PARAMS.volume_lookback, PARAMS.atr_period, PARAMS.range_bars) + 1

    def __init__(self, funding: pd.DataFrame | None = None) -> None:
        self.funding = funding

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        required = {"open", "high", "low", "close", "volume"}
        missing = required.difference(candles.columns)
        if missing:
            raise ValueError(f"range expansion missing columns: {sorted(missing)}")
        p = self.params
        out = candles.copy()
        open_ = pd.to_numeric(out["open"], errors="coerce")
        high = pd.to_numeric(out["high"], errors="coerce")
        low = pd.to_numeric(out["low"], errors="coerce")
        close = pd.to_numeric(out["close"], errors="coerce")
        volume = pd.to_numeric(out["volume"], errors="coerce")
        prior_high = high.shift(1).rolling(p.range_bars, min_periods=p.range_bars).max()
        prior_low = low.shift(1).rolling(p.range_bars, min_periods=p.range_bars).min()
        vol_base = volume.shift(1).rolling(
            p.volume_lookback, min_periods=p.volume_lookback
        ).median()
        body_bps = (close - open_).abs().div(open_).mul(10_000)
        prev_close = close.shift(1)
        true_range = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
        ).max(axis=1)
        atr = true_range.shift(1).rolling(p.atr_period, min_periods=p.atr_period).mean()
        long_raw = (
            close.gt(prior_high.mul(1 + p.break_buffer_bps / 10_000))
            & body_bps.ge(p.body_min_bps)
            & volume.ge(vol_base.mul(p.volume_mult))
        )
        short_raw = (
            close.lt(prior_low.mul(1 - p.break_buffer_bps / 10_000))
            & body_bps.ge(p.body_min_bps)
            & volume.ge(vol_base.mul(p.volume_mult))
        )
        fire_long = [False] * len(out)
        fire_short = [False] * len(out)
        last_fire = -(10**9)
        for index, (is_long, is_short) in enumerate(
            zip(long_raw.fillna(False), short_raw.fillna(False), strict=True)
        ):
            if index - last_fire < p.min_bars_between_signals:
                continue
            if is_long:
                fire_long[index] = True
                last_fire = index
            elif is_short:
                fire_short[index] = True
                last_fire = index
        out["rex_prior_high"] = prior_high
        out["rex_prior_low"] = prior_low
        out["rex_volume_base"] = vol_base
        out["rex_body_bps"] = body_bps
        out["rex_atr"] = atr
        out["rex_fire_long"] = pd.Series(fire_long, index=out.index).astype(float)
        out["rex_fire_short"] = pd.Series(fire_short, index=out.index).astype(float)
        return out

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index < self.warmup_bars or index >= len(df):
            return None
        row = df.iloc[index]
        is_long = float(row["rex_fire_long"]) > 0
        is_short = float(row["rex_fire_short"]) > 0
        if not (is_long or is_short):
            return None
        close = float(row["close"])
        atr = float(row["rex_atr"])
        if not math.isfinite(close) or not math.isfinite(atr) or close <= 0 or atr <= 0:
            return None
        risk = self.params.atr_stop_mult * atr
        projected_net = (
            risk / close * 10_000 * self.params.projected_reward_r
            - self.params.round_trip_cost_bps
        )
        if projected_net < self.params.min_projected_net_bps:
            return None
        side: Literal["long", "short"] = "long" if is_long else "short"
        stop = close - risk if side == "long" else close + risk
        return SignalIntent(
            side=side,
            stop_price=stop,
            take_profit_price=None,
            reason=(
                f"range_expansion side={side} body={float(row['rex_body_bps']):.1f}bps "
                f"projected_net={projected_net:.1f}bps 12h_break virtual_only"
            ),
        )
