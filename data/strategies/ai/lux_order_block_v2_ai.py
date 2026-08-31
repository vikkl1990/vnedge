"""AI candidate v2: displacement-qualified order block mitigation.

Pre-registered improvement over ai_lux_order_block_v1 (REJECT: PF 0.79,
under-sampled, net negative on 180d). Hypothesis: an order block is only worth
trading when the structure break that created it shows DISPLACEMENT — an
impulsive break bar — and the block bar itself carries above-average volume
(the "institutional footprint" the mechanism claims to capture), taken only
with the slow trend. Three qualifications, all causal and stateless:

1. Displacement: break bar range >= displacement_atr x ATR and the close
   clears the swing extreme by >= clearance_atr x ATR.
2. Block volume: the block bar's volume >= its rolling volume mean.
3. Trend: long blocks only above the slow SMA; shorts only below.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr

_REQUIRED = ("atr", "sma_slow")


def _qualified_block_features(
    df: pd.DataFrame, breaks: pd.Series, *, bullish: bool, swing_length: int
) -> pd.DataFrame:
    out = pd.DataFrame(index=df.index)
    zone_top = pd.Series(np.nan, index=df.index)
    zone_bottom = pd.Series(np.nan, index=df.index)
    break_positions = np.flatnonzero(breaks.to_numpy(dtype=bool))
    lows = df["low"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    opens = df["open"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    vol_ok = (df["volume"] >= df["volume"].rolling(48).mean()).to_numpy(dtype=bool)
    for position in break_positions:
        start = max(0, position - swing_length)
        if start >= position:
            continue
        if bullish:
            ob = start + int(np.argmin(lows[start:position]))
            if not vol_ok[ob]:
                continue
            zone_top.iloc[position] = max(opens[ob], closes[ob])
            zone_bottom.iloc[position] = lows[ob]
        else:
            ob = start + int(np.argmax(highs[start:position]))
            if not vol_ok[ob]:
                continue
            zone_top.iloc[position] = highs[ob]
            zone_bottom.iloc[position] = min(opens[ob], closes[ob])
    formed = zone_top.notna()
    block_id = pd.Series(
        np.where(formed.to_numpy(dtype=bool), np.arange(len(df)), np.nan),
        index=df.index,
    ).ffill()
    out["block_id"] = block_id
    out["zone_top"] = zone_top.ffill()
    out["zone_bottom"] = zone_bottom.ffill()

    def _prior_cummin(series: pd.Series) -> pd.Series:
        return series.shift(1).cummin()

    def _prior_cummax(series: pd.Series) -> pd.Series:
        return series.shift(1).cummax()

    out["prior_min_low"] = (
        df["low"].groupby(block_id).apply(_prior_cummin).reset_index(level=0, drop=True)
    )
    out["prior_max_high"] = (
        df["high"].groupby(block_id).apply(_prior_cummax).reset_index(level=0, drop=True)
    )
    out["bar_in_block"] = block_id.groupby(block_id).cumcount()
    return out


class LuxOrderBlockV2AI(BaseStrategy):
    strategy_id = "lux_order_block_v2"

    def __init__(
        self,
        swing_length: int = 10,
        displacement_atr: float = 1.2,
        clearance_atr: float = 0.25,
        sma_slow_window: int = 200,
        stop_atr_pad: float = 0.25,
        take_profit_r: float = 2.0,
        atr_window: int = 14,
        max_block_age: int = 96,
    ) -> None:
        self.swing_length = swing_length
        self.displacement_atr = displacement_atr
        self.clearance_atr = clearance_atr
        self.sma_slow_window = sma_slow_window
        self.stop_atr_pad = stop_atr_pad
        self.take_profit_r = take_profit_r
        self.atr_window = atr_window
        self.max_block_age = max_block_age
        self.warmup_bars = max(sma_slow_window, swing_length * 2, atr_window) + 2

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        n = self.swing_length
        df["atr"] = atr(df, self.atr_window)
        swing_high_prev = df["high"].rolling(n).max().shift(1)
        swing_low_prev = df["low"].rolling(n).min().shift(1)
        bar_range = df["high"] - df["low"]
        displaced = bar_range >= self.displacement_atr * df["atr"]
        break_up = (
            (df["close"] > swing_high_prev + self.clearance_atr * df["atr"]) & displaced
        )
        break_down = (
            (df["close"] < swing_low_prev - self.clearance_atr * df["atr"]) & displaced
        )
        bull = _qualified_block_features(
            df, break_up.fillna(False), bullish=True, swing_length=n
        )
        bear = _qualified_block_features(
            df, break_down.fillna(False), bullish=False, swing_length=n
        )
        for col in ("block_id", "zone_top", "zone_bottom", "prior_min_low", "bar_in_block"):
            df[f"bull_{col}"] = bull[col]
        for col in ("block_id", "zone_top", "zone_bottom", "prior_max_high", "bar_in_block"):
            df[f"bear_{col}"] = bear[col]
        df["sma_slow"] = df["close"].rolling(self.sma_slow_window).mean()
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(math.isnan(float(row[col])) for col in _REQUIRED):
            return None
        close = float(row["close"])
        low = float(row["low"])
        high = float(row["high"])
        bar_atr = float(row["atr"])
        sma_slow = float(row["sma_slow"])
        if bar_atr <= 0:
            return None

        if (
            close > sma_slow
            and not pd.isna(row["bull_block_id"])
            and not math.isnan(float(row["bull_zone_top"]))
        ):
            top = float(row["bull_zone_top"])
            bottom = float(row["bull_zone_bottom"])
            age = float(row["bull_bar_in_block"])
            prior_min = (
                float(row["bull_prior_min_low"])
                if not pd.isna(row["bull_prior_min_low"]) else math.inf
            )
            if (
                0 < age <= self.max_block_age
                and prior_min > top and prior_min > bottom
                and low >= bottom and low <= top and close > bottom
            ):
                stop = bottom - self.stop_atr_pad * bar_atr
                risk = close - stop
                if risk > 0:
                    return SignalIntent(
                        side="long", stop_price=stop,
                        take_profit_price=close + self.take_profit_r * risk,
                        reason=f"displaced bullish OB mitigation at {close:.2f}",
                    )
        if (
            close < sma_slow
            and not pd.isna(row["bear_block_id"])
            and not math.isnan(float(row["bear_zone_top"]))
        ):
            top = float(row["bear_zone_top"])
            bottom = float(row["bear_zone_bottom"])
            age = float(row["bear_bar_in_block"])
            prior_max = (
                float(row["bear_prior_max_high"])
                if not pd.isna(row["bear_prior_max_high"]) else -math.inf
            )
            if (
                0 < age <= self.max_block_age
                and prior_max < bottom and prior_max < top
                and high <= top and high >= bottom and close < top
            ):
                stop = top + self.stop_atr_pad * bar_atr
                risk = stop - close
                if risk > 0:
                    return SignalIntent(
                        side="short", stop_price=stop,
                        take_profit_price=close - self.take_profit_r * risk,
                        reason=f"displaced bearish OB mitigation at {close:.2f}",
                    )
        return None
