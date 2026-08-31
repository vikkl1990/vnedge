"""AI-ported candidate: fair value gap retrace (after LuxAlgo's Fair Value Gap).

Mechanism, causal and stateless: a bullish FVG forms when low[i] > high[i-2]
with the gap at least ``min_gap_atr`` ATRs wide; the candidate buys the FIRST
later bar that retraces into the freshest unviolated gap, stop under the gap
bottom. Mirror image for bearish gaps. Gap lifetime is bounded. First-touch
and violation state derive from group-cumulative operations keyed by the gap's
formation bar, so every feature at bar i uses rows <= i only.

Research candidate only — same gauntlet as every strategy.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr

_REQUIRED = ("atr",)


def _gap_features(df: pd.DataFrame, formed: pd.Series, top: pd.Series,
                  bottom: pd.Series) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    gap_id = pd.Series(
        np.where(formed.to_numpy(dtype=bool), np.arange(len(df)), np.nan),
        index=df.index,
    ).ffill()
    out["gap_id"] = gap_id
    out["gap_top"] = top.where(formed).ffill()
    out["gap_bottom"] = bottom.where(formed).ffill()

    def _prior_cummin(series: pd.Series) -> pd.Series:
        return series.shift(1).cummin()

    def _prior_cummax(series: pd.Series) -> pd.Series:
        return series.shift(1).cummax()

    out["prior_min_low"] = (
        df["low"].groupby(gap_id).apply(_prior_cummin).reset_index(level=0, drop=True)
    )
    out["prior_max_high"] = (
        df["high"].groupby(gap_id).apply(_prior_cummax).reset_index(level=0, drop=True)
    )
    out["bar_in_gap"] = gap_id.groupby(gap_id).cumcount()
    return out


class LuxFairValueGapAI(BaseStrategy):
    strategy_id = "lux_fair_value_gap_v1"

    def __init__(
        self,
        min_gap_atr: float = 0.25,
        stop_atr_pad: float = 0.20,
        take_profit_r: float = 2.0,
        atr_window: int = 14,
        max_gap_age: int = 72,
    ) -> None:
        self.min_gap_atr = min_gap_atr
        self.stop_atr_pad = stop_atr_pad
        self.take_profit_r = take_profit_r
        self.atr_window = atr_window
        self.max_gap_age = max_gap_age
        self.warmup_bars = atr_window + 3

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        df["atr"] = atr(df, self.atr_window)
        high_2 = df["high"].shift(2)
        low_2 = df["low"].shift(2)
        bull_formed = (df["low"] > high_2) & ((df["low"] - high_2) >= self.min_gap_atr * df["atr"])
        bear_formed = (df["high"] < low_2) & ((low_2 - df["high"]) >= self.min_gap_atr * df["atr"])
        bull = _gap_features(df, bull_formed.fillna(False), df["low"], high_2)
        bear = _gap_features(df, bear_formed.fillna(False), low_2, df["high"])
        for col in ("gap_id", "gap_top", "gap_bottom", "prior_min_low", "bar_in_gap"):
            df[f"bull_{col}"] = bull[col]
        for col in ("gap_id", "gap_top", "gap_bottom", "prior_max_high", "bar_in_gap"):
            df[f"bear_{col}"] = bear[col]
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

        if not pd.isna(row["bull_gap_id"]) and not math.isnan(float(row["bull_gap_top"])):
            top = float(row["bull_gap_top"])
            bottom = float(row["bull_gap_bottom"])
            age = float(row["bull_bar_in_gap"])
            prior_min = (
                float(row["bull_prior_min_low"])
                if not pd.isna(row["bull_prior_min_low"]) else math.inf
            )
            if (
                0 < age <= self.max_gap_age
                and prior_min > top      # first retrace into the gap
                and low >= bottom        # gap not blown through this bar
                and low <= top and close > bottom
            ):
                stop = bottom - self.stop_atr_pad * bar_atr
                risk = close - stop
                if risk > 0:
                    return SignalIntent(
                        side="long", stop_price=stop,
                        take_profit_price=close + self.take_profit_r * risk,
                        reason=f"bullish FVG retrace at {close:.2f}",
                    )
        if not pd.isna(row["bear_gap_id"]) and not math.isnan(float(row["bear_gap_top"])):
            top = float(row["bear_gap_top"])
            bottom = float(row["bear_gap_bottom"])
            age = float(row["bear_bar_in_gap"])
            prior_max = (
                float(row["bear_prior_max_high"])
                if not pd.isna(row["bear_prior_max_high"]) else -math.inf
            )
            if (
                0 < age <= self.max_gap_age
                and prior_max < bottom
                and high <= top
                and high >= bottom and close < top
            ):
                stop = top + self.stop_atr_pad * bar_atr
                risk = stop - close
                if risk > 0:
                    return SignalIntent(
                        side="short", stop_price=stop,
                        take_profit_price=close - self.take_profit_r * risk,
                        reason=f"bearish FVG retrace at {close:.2f}",
                    )
        return None
