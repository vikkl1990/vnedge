"""AI archetype candidate: Donchian/turtle channel breakout.

Archetype for the FMZ corpus's breakout/channel family (881 of 5,807 posts).
Causal and stateless: long when the close breaks the ``entry_window`` high of
the bars ENDING at i-1; initial stop at the ``exit_window`` low known at i-1
(the turtle N-bar exit expressed as a protective stop). Mirror for shorts.
Research candidate only — same gauntlet as every strategy.
"""

from __future__ import annotations

import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr

_REQUIRED = ("entry_high_prev", "entry_low_prev", "exit_low_prev", "exit_high_prev", "atr")


class FmzDonchianBreakoutAI(BaseStrategy):
    strategy_id = "fmz_donchian_breakout_v1"

    def __init__(
        self,
        entry_window: int = 20,
        exit_window: int = 10,
        min_stop_atr: float = 0.5,
        atr_window: int = 14,
    ) -> None:
        self.entry_window = entry_window
        self.exit_window = exit_window
        self.min_stop_atr = min_stop_atr
        self.atr_window = atr_window
        self.warmup_bars = max(entry_window, atr_window) + 2

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        df["entry_high_prev"] = df["high"].rolling(self.entry_window).max().shift(1)
        df["entry_low_prev"] = df["low"].rolling(self.entry_window).min().shift(1)
        df["exit_low_prev"] = df["low"].rolling(self.exit_window).min().shift(1)
        df["exit_high_prev"] = df["high"].rolling(self.exit_window).max().shift(1)
        df["atr"] = atr(df, self.atr_window)
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(math.isnan(float(row[col])) for col in _REQUIRED):
            return None
        close = float(row["close"])
        bar_atr = float(row["atr"])
        if bar_atr <= 0:
            return None

        if close > float(row["entry_high_prev"]):
            stop = min(float(row["exit_low_prev"]), close - self.min_stop_atr * bar_atr)
            if close - stop > 0:
                return SignalIntent(
                    side="long", stop_price=stop, take_profit_price=None,
                    reason=f"{self.entry_window}-bar Donchian breakout up at {close:.2f}",
                )
        if close < float(row["entry_low_prev"]):
            stop = max(float(row["exit_high_prev"]), close + self.min_stop_atr * bar_atr)
            if stop - close > 0:
                return SignalIntent(
                    side="short", stop_price=stop, take_profit_price=None,
                    reason=f"{self.entry_window}-bar Donchian breakout down at {close:.2f}",
                )
        return None
