"""AI-authored candidate: engulfing exhaustion reversal.

Sandbox-compliant: whitelisted imports only, every feature at bar i is built
from rows <= i (backward shifts), NaN marks warmup, and every intent carries a
stop. Loading this file registers nothing — it is a research candidate that
must clear the causality analyzer, walk-forward gates, a pre-registered
untouched-data judgment, and human approval like any other strategy.

Hypothesis (unproven, pre-registered here): after a sustained one-sided leg
(``streak`` consecutive lower closes), a wide-range bullish engulfing bar --
body engulfs the prior bar's body, close in the top of its range, range at
least one ATR -- marks short-term seller exhaustion; enter long with an ATR
stop under the reversal bar's low and an R-multiple target. Mirror image for
shorts after consecutive higher closes. The mechanism is the classic
exhaustion/absorption reversal, distinct from the registered squeeze,
range-expansion, structure-break, and pullback families.
"""

from __future__ import annotations

import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr

_REQUIRED = (
    "open_prev", "close_prev", "down_streak_prev", "up_streak_prev", "atr",
)


class ExhaustionEngulfingAI(BaseStrategy):
    strategy_id = "exhaustion_engulfing_v1"

    def __init__(
        self,
        streak: int = 3,
        stop_atr_pad: float = 0.25,
        take_profit_r: float = 2.0,
        min_range_atr: float = 1.0,
        close_location: float = 0.6,
        atr_window: int = 14,
    ) -> None:
        if streak < 2:
            raise ValueError("streak must cover at least two prior closes")
        if not 0.0 < close_location < 1.0:
            raise ValueError("close_location must be a fraction of the bar range")
        self.streak = streak
        self.stop_atr_pad = stop_atr_pad
        self.take_profit_r = take_profit_r
        self.min_range_atr = min_range_atr
        self.close_location = close_location
        self.atr_window = atr_window
        # ATR needs its window, the streak needs `streak` prior closes plus
        # the one-bar shift that keeps the streak strictly before bar i.
        self.warmup_bars = max(atr_window, streak) + 2

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        df["open_prev"] = df["open"].shift(1)
        df["close_prev"] = df["close"].shift(1)
        # A down (up) close compares each close with the close one bar back.
        # The streak is summed over the `streak` bars ENDING AT i-1 -- shifted
        # once so the engulfing bar i is never part of its own precondition.
        down = (df["close"] < df["close"].shift(1)).astype(float)
        up = (df["close"] > df["close"].shift(1)).astype(float)
        df["down_streak_prev"] = down.shift(1).rolling(self.streak).sum()
        df["up_streak_prev"] = up.shift(1).rolling(self.streak).sum()
        df["atr"] = atr(df, self.atr_window)
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(math.isnan(float(row[col])) for col in _REQUIRED):
            return None  # indicator warmup

        bar_open = float(row["open"])
        bar_close = float(row["close"])
        bar_high = float(row["high"])
        bar_low = float(row["low"])
        open_prev = float(row["open_prev"])
        close_prev = float(row["close_prev"])
        bar_atr = float(row["atr"])
        bar_range = bar_high - bar_low
        if bar_atr <= 0 or bar_range <= 0:
            return None
        if bar_range < self.min_range_atr * bar_atr:
            return None  # narrow bar: no exhaustion evidence

        down_leg = float(row["down_streak_prev"]) >= float(self.streak)
        up_leg = float(row["up_streak_prev"]) >= float(self.streak)

        bull_engulf = (
            down_leg
            and close_prev < open_prev  # prior bar continued the down leg
            and bar_close > bar_open
            and bar_open <= close_prev
            and bar_close >= open_prev  # body engulfs the prior body
            and (bar_close - bar_low) / bar_range >= self.close_location
        )
        bear_engulf = (
            up_leg
            and close_prev > open_prev  # prior bar continued the up leg
            and bar_close < bar_open
            and bar_open >= close_prev
            and bar_close <= open_prev
            and (bar_high - bar_close) / bar_range >= self.close_location
        )

        if bull_engulf:
            stop = bar_low - self.stop_atr_pad * bar_atr
            risk = bar_close - stop
            if risk <= 0:
                return None
            return SignalIntent(
                side="long",
                stop_price=stop,
                take_profit_price=bar_close + self.take_profit_r * risk,
                reason=(
                    f"bullish engulfing after {self.streak} lower closes"
                    f" at {bar_close:.2f}"
                ),
            )
        if bear_engulf:
            stop = bar_high + self.stop_atr_pad * bar_atr
            risk = stop - bar_close
            if risk <= 0:
                return None
            return SignalIntent(
                side="short",
                stop_price=stop,
                take_profit_price=bar_close - self.take_profit_r * risk,
                reason=(
                    f"bearish engulfing after {self.streak} higher closes"
                    f" at {bar_close:.2f}"
                ),
            )
        return None
