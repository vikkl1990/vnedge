"""AI archetype candidate: RSI(2) pullback in trend (Connors style).

Archetype for the FMZ corpus's oscillator family (901 of 5,807 posts). Causal
and stateless: long when the close sits above the slow SMA (trend up) and the
2-period RSI closes below the oversold floor — a sharp pullback inside an
uptrend. Stop ``stop_atr`` ATRs below; target one risk unit up (the mechanism
mean-reverts fast, so the archetype books small reversions). Mirror short.
Research candidate only — same gauntlet as every strategy.
"""

from __future__ import annotations

import math

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr

_REQUIRED = ("rsi2", "sma_slow", "atr")


class FmzRsi2PullbackAI(BaseStrategy):
    strategy_id = "fmz_rsi2_pullback_v1"

    def __init__(
        self,
        rsi_window: int = 2,
        oversold: float = 10.0,
        overbought: float = 90.0,
        sma_slow_window: int = 200,
        stop_atr: float = 2.0,
        take_profit_r: float = 1.0,
        atr_window: int = 14,
    ) -> None:
        self.rsi_window = rsi_window
        self.oversold = oversold
        self.overbought = overbought
        self.sma_slow_window = sma_slow_window
        self.stop_atr = stop_atr
        self.take_profit_r = take_profit_r
        self.atr_window = atr_window
        self.warmup_bars = max(sma_slow_window, atr_window, rsi_window) + 2

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        delta = df["close"].diff()
        gain = delta.clip(lower=0.0).ewm(alpha=1.0 / self.rsi_window, adjust=False).mean()
        loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / self.rsi_window, adjust=False).mean()
        rs = gain / loss.where(loss > 0)
        df["rsi2"] = 100.0 - 100.0 / (1.0 + rs)
        df["sma_slow"] = df["close"].rolling(self.sma_slow_window).mean()
        df["atr"] = atr(df, self.atr_window)
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(math.isnan(float(row[col])) for col in _REQUIRED):
            return None
        close = float(row["close"])
        bar_atr = float(row["atr"])
        rsi = float(row["rsi2"])
        sma_slow = float(row["sma_slow"])
        if bar_atr <= 0:
            return None

        if close > sma_slow and rsi < self.oversold:
            stop = close - self.stop_atr * bar_atr
            risk = close - stop
            return SignalIntent(
                side="long", stop_price=stop,
                take_profit_price=close + self.take_profit_r * risk,
                reason=f"RSI(2)={rsi:.1f} pullback above slow SMA at {close:.2f}",
            )
        if close < sma_slow and rsi > self.overbought:
            stop = close + self.stop_atr * bar_atr
            risk = stop - close
            return SignalIntent(
                side="short", stop_price=stop,
                take_profit_price=close - self.take_profit_r * risk,
                reason=f"RSI(2)={rsi:.1f} rally below slow SMA at {close:.2f}",
            )
        return None
