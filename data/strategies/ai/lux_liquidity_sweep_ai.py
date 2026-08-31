"""AI-ported candidate: liquidity sweep reversal (after LuxAlgo's Liquidity Sweeps).

Mechanism, causal and stateless: price wicks THROUGH a prior pivot low (the
resting sell-side liquidity) but closes back above it — a stop hunt — and the
candidate fades the sweep long with the stop under the sweep extreme. Mirror
image above prior pivot highs. The pivot level is the rolling extreme of the
window ENDING at the previous bar, so the sweeping bar never defines its own
level. A minimum wick penetration in ATR units filters micro-pokes.

Research candidate only — same gauntlet as every strategy. Distinct from the
registered liquidity_sweep_reversal_15m_v1: this port follows LuxAlgo's
single-bar sweep definition; the registered scanner requires a swing-anchored
level with structure context.
"""

from __future__ import annotations

import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr

_REQUIRED = ("pivot_low_prev", "pivot_high_prev", "atr")


class LuxLiquiditySweepAI(BaseStrategy):
    strategy_id = "lux_liquidity_sweep_v1"

    def __init__(
        self,
        pivot_length: int = 14,
        min_penetration_atr: float = 0.10,
        stop_atr_pad: float = 0.20,
        take_profit_r: float = 2.0,
        atr_window: int = 14,
    ) -> None:
        self.pivot_length = pivot_length
        self.min_penetration_atr = min_penetration_atr
        self.stop_atr_pad = stop_atr_pad
        self.take_profit_r = take_profit_r
        self.atr_window = atr_window
        self.warmup_bars = max(pivot_length, atr_window) + 2

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        df["pivot_low_prev"] = df["low"].rolling(self.pivot_length).min().shift(1)
        df["pivot_high_prev"] = df["high"].rolling(self.pivot_length).max().shift(1)
        df["atr"] = atr(df, self.atr_window)
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(math.isnan(float(row[col])) for col in _REQUIRED):
            return None
        close = float(row["close"])
        low = float(row["low"])
        high = float(row["high"])
        bar_atr = float(row["atr"])
        if bar_atr <= 0:
            return None
        level_low = float(row["pivot_low_prev"])
        level_high = float(row["pivot_high_prev"])

        swept_down = (
            low < level_low
            and close > level_low
            and (level_low - low) >= self.min_penetration_atr * bar_atr
        )
        swept_up = (
            high > level_high
            and close < level_high
            and (high - level_high) >= self.min_penetration_atr * bar_atr
        )

        if swept_down:
            stop = low - self.stop_atr_pad * bar_atr
            risk = close - stop
            if risk > 0:
                return SignalIntent(
                    side="long", stop_price=stop,
                    take_profit_price=close + self.take_profit_r * risk,
                    reason=f"sell-side liquidity sweep reclaimed at {close:.2f}",
                )
        if swept_up:
            stop = high + self.stop_atr_pad * bar_atr
            risk = stop - close
            if risk > 0:
                return SignalIntent(
                    side="short", stop_price=stop,
                    take_profit_price=close - self.take_profit_r * risk,
                    reason=f"buy-side liquidity sweep rejected at {close:.2f}",
                )
        return None
