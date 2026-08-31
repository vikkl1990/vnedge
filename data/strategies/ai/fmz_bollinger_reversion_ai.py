"""AI archetype candidate: Bollinger band mean reversion.

Archetype for the FMZ corpus's Bollinger family (279 of 5,807 posts). Causal
and stateless: long when the close crosses back above the lower band after
closing below it (re-entry, not the initial pierce — the same discipline as
the NW envelope port); target the middle band, stop ``stop_atr`` ATRs under
the reversion low. Mirror short at the upper band.
Research candidate only — same gauntlet as every strategy.
"""

from __future__ import annotations

import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr

_REQUIRED = ("bb_lower", "bb_upper", "bb_mid", "bb_lower_prev", "bb_upper_prev", "close_prev", "atr")


class FmzBollingerReversionAI(BaseStrategy):
    strategy_id = "fmz_bollinger_reversion_v1"

    def __init__(
        self,
        window: int = 20,
        mult: float = 2.0,
        stop_atr: float = 1.5,
        min_rr: float = 0.8,
        atr_window: int = 14,
    ) -> None:
        self.window = window
        self.mult = mult
        self.stop_atr = stop_atr
        self.min_rr = min_rr
        self.atr_window = atr_window
        self.warmup_bars = max(window, atr_window) + 2

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        mid = df["close"].rolling(self.window).mean()
        std = df["close"].rolling(self.window).std()
        df["bb_mid"] = mid
        df["bb_upper"] = mid + self.mult * std
        df["bb_lower"] = mid - self.mult * std
        df["bb_upper_prev"] = df["bb_upper"].shift(1)
        df["bb_lower_prev"] = df["bb_lower"].shift(1)
        df["close_prev"] = df["close"].shift(1)
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
        mid = float(row["bb_mid"])
        if bar_atr <= 0:
            return None

        re_entered_up = (
            float(row["close_prev"]) < float(row["bb_lower_prev"])
            and close > float(row["bb_lower"])
        )
        re_entered_down = (
            float(row["close_prev"]) > float(row["bb_upper_prev"])
            and close < float(row["bb_upper"])
        )

        if re_entered_up and mid > close:
            stop = low - self.stop_atr * bar_atr
            risk = close - stop
            if risk > 0 and (mid - close) / risk >= self.min_rr:
                return SignalIntent(
                    side="long", stop_price=stop, take_profit_price=mid,
                    reason=f"lower-band reversion toward mid at {close:.2f}",
                )
        if re_entered_down and mid < close:
            stop = high + self.stop_atr * bar_atr
            risk = stop - close
            if risk > 0 and (close - mid) / risk >= self.min_rr:
                return SignalIntent(
                    side="short", stop_price=stop, take_profit_price=mid,
                    reason=f"upper-band reversion toward mid at {close:.2f}",
                )
        return None
