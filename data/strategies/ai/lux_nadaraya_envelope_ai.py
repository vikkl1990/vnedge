"""AI-ported candidate: Nadaraya-Watson envelope reversion (causal endpoint form).

LuxAlgo's on-chart Nadaraya-Watson Envelope is famously REPAINTING in its
default form: the kernel regression smooths both directions, so historical
bands move when new bars arrive. This port is the endpoint (non-repainting)
variant the authors themselves recommend for signals: at each bar the gaussian
kernel weights only the trailing ``window`` closes, and the band width is the
same-kernel weighted mean absolute deviation times ``mult``. Long when the
close re-enters from below the lower band; short mirrored at the upper band.
ATR stops beyond the reversion extreme.

Research candidate only — same gauntlet as every strategy. The causality
analyzer is the point here: the popular repainting form cannot pass it.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr

_REQUIRED = ("nw_mean", "nw_lower", "nw_upper", "nw_lower_prev", "nw_upper_prev", "close_prev", "atr")


class LuxNadarayaEnvelopeAI(BaseStrategy):
    strategy_id = "lux_nadaraya_envelope_v1"

    def __init__(
        self,
        window: int = 100,
        bandwidth: float = 8.0,
        mult: float = 3.0,
        stop_atr_pad: float = 0.75,
        take_profit_r: float = 1.5,
        atr_window: int = 14,
    ) -> None:
        self.window = window
        self.bandwidth = bandwidth
        self.mult = mult
        self.stop_atr_pad = stop_atr_pad
        self.take_profit_r = take_profit_r
        self.atr_window = atr_window
        self.warmup_bars = window + 2

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        # Gaussian kernel weights over the trailing window; weight 1 on the
        # newest bar decaying backward. Precomputed once — the same weights
        # apply at every bar, which is what makes the endpoint form causal.
        offsets = np.arange(self.window, dtype=float)
        weights = np.exp(-(offsets ** 2) / (2.0 * self.bandwidth ** 2))
        weights = weights[::-1]  # oldest..newest to align with rolling windows
        weight_sum = float(weights.sum())

        def _kernel_mean(values: np.ndarray) -> float:
            return float(np.dot(values, weights) / weight_sum)

        df["nw_mean"] = df["close"].rolling(self.window).apply(_kernel_mean, raw=True)

        def _kernel_mae(values: np.ndarray) -> float:
            mean = np.dot(values, weights) / weight_sum
            return float(np.dot(np.abs(values - mean), weights) / weight_sum)

        mae = df["close"].rolling(self.window).apply(_kernel_mae, raw=True)
        df["nw_upper"] = df["nw_mean"] + self.mult * mae
        df["nw_lower"] = df["nw_mean"] - self.mult * mae
        df["nw_upper_prev"] = df["nw_upper"].shift(1)
        df["nw_lower_prev"] = df["nw_lower"].shift(1)
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
        if bar_atr <= 0:
            return None

        crossed_up_from_below = (
            float(row["close_prev"]) < float(row["nw_lower_prev"])
            and close > float(row["nw_lower"])
        )
        crossed_down_from_above = (
            float(row["close_prev"]) > float(row["nw_upper_prev"])
            and close < float(row["nw_upper"])
        )

        if crossed_up_from_below:
            stop = low - self.stop_atr_pad * bar_atr
            risk = close - stop
            if risk > 0:
                return SignalIntent(
                    side="long", stop_price=stop,
                    take_profit_price=close + self.take_profit_r * risk,
                    reason=f"close re-entered NW envelope from below at {close:.2f}",
                )
        if crossed_down_from_above:
            stop = high + self.stop_atr_pad * bar_atr
            risk = stop - close
            if risk > 0:
                return SignalIntent(
                    side="short", stop_price=stop,
                    take_profit_price=close - self.take_profit_r * risk,
                    reason=f"close re-entered NW envelope from above at {close:.2f}",
                )
        return None
