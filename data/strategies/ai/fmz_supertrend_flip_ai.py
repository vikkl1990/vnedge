"""AI archetype candidate: Supertrend flip trend following.

Archetype for the FMZ corpus's supertrend/trailing family (234 of 5,807
posts). The Supertrend line is a recursive ATR trailing band; the recursion
runs FORWARD over rows <= i inside ``prepare`` (a causal left-to-right pass),
so truncation invariance holds. Entry on the flip bar: long when the trend
flips up, stop at the flip bar's Supertrend line. No fixed target — the
archetype rides until stop or the gauntlet's holding cap. Mirror short.
Research candidate only — same gauntlet as every strategy.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr

_REQUIRED = ("st_line", "st_dir", "st_dir_prev", "atr")


class FmzSupertrendFlipAI(BaseStrategy):
    strategy_id = "fmz_supertrend_flip_v1"

    def __init__(
        self,
        atr_window: int = 10,
        mult: float = 3.0,
        min_stop_atr: float = 0.25,
    ) -> None:
        self.atr_window = atr_window
        self.mult = mult
        self.min_stop_atr = min_stop_atr
        self.warmup_bars = atr_window + 3

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        df["atr"] = atr(df, self.atr_window)
        hl2 = (df["high"] + df["low"]) / 2.0
        upper_basic = (hl2 + self.mult * df["atr"]).to_numpy(dtype=float)
        lower_basic = (hl2 - self.mult * df["atr"]).to_numpy(dtype=float)
        closes = df["close"].to_numpy(dtype=float)
        n = len(df)
        upper = np.full(n, np.nan)
        lower = np.full(n, np.nan)
        direction = np.full(n, np.nan)
        line = np.full(n, np.nan)
        for i in range(n):
            if math.isnan(upper_basic[i]) or math.isnan(lower_basic[i]):
                continue
            if i == 0 or math.isnan(upper[i - 1]):
                upper[i] = upper_basic[i]
                lower[i] = lower_basic[i]
                direction[i] = 1.0
                line[i] = lower[i]
                continue
            upper[i] = (
                upper_basic[i]
                if upper_basic[i] < upper[i - 1] or closes[i - 1] > upper[i - 1]
                else upper[i - 1]
            )
            lower[i] = (
                lower_basic[i]
                if lower_basic[i] > lower[i - 1] or closes[i - 1] < lower[i - 1]
                else lower[i - 1]
            )
            if direction[i - 1] > 0:
                direction[i] = -1.0 if closes[i] < lower[i] else 1.0
            else:
                direction[i] = 1.0 if closes[i] > upper[i] else -1.0
            line[i] = lower[i] if direction[i] > 0 else upper[i]
        df["st_line"] = line
        df["st_dir"] = direction
        df["st_dir_prev"] = df["st_dir"].shift(1)
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(math.isnan(float(row[col])) for col in _REQUIRED):
            return None
        close = float(row["close"])
        bar_atr = float(row["atr"])
        if bar_atr <= 0:
            return None
        direction = float(row["st_dir"])
        prev_direction = float(row["st_dir_prev"])
        line = float(row["st_line"])

        if direction > 0 and prev_direction < 0:
            stop = min(line, close - self.min_stop_atr * bar_atr)
            if close - stop > 0:
                return SignalIntent(
                    side="long", stop_price=stop, take_profit_price=None,
                    reason=f"supertrend flipped up at {close:.2f}",
                )
        if direction < 0 and prev_direction > 0:
            stop = max(line, close + self.min_stop_atr * bar_atr)
            if stop - close > 0:
                return SignalIntent(
                    side="short", stop_price=stop, take_profit_price=None,
                    reason=f"supertrend flipped down at {close:.2f}",
                )
        return None
