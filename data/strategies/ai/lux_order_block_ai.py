"""AI-ported candidate: Order Block mitigation (after LuxAlgo's Order Block Detector).

Mechanism, made causal and STATELESS for the sandbox: a swing-high break
(structure shift up) defines a bullish order block at the lowest-low bar of
the preceding swing window; the entry is the FIRST later bar that trades back
into the block while its far edge is still unviolated. Mirror image for
bearish blocks. Every feature at bar i derives from rows <= i through
rolling/shift/ffill/group-cumulative operations, so truncation invariance
holds without instance state.

Research candidate only — must clear causality, walk-forward, untouched-data
judgment, and human approval. Fidelity note: the on-chart original tracks
multiple concurrent blocks with volume filters; this port keeps the freshest
block per side, which is the tradeable core of the mechanism.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr

_REQUIRED = ("atr",)


def _block_features(
    df: pd.DataFrame, breaks: pd.Series, *, bullish: bool, swing_length: int
) -> pd.DataFrame:
    """Zone columns for the freshest block implied by each break event."""
    out = pd.DataFrame(index=df.index)
    zone_top = pd.Series(np.nan, index=df.index)
    zone_bottom = pd.Series(np.nan, index=df.index)
    break_positions = np.flatnonzero(breaks.to_numpy(dtype=bool))
    lows = df["low"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    opens = df["open"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    for position in break_positions:
        start = max(0, position - swing_length)
        if start >= position:
            continue
        if bullish:
            ob = start + int(np.argmin(lows[start:position]))
            zone_top.iloc[position] = max(opens[ob], closes[ob])
            zone_bottom.iloc[position] = lows[ob]
        else:
            ob = start + int(np.argmax(highs[start:position]))
            zone_top.iloc[position] = highs[ob]
            zone_bottom.iloc[position] = min(opens[ob], closes[ob])
    # The freshest block is carried forward; its identity is the break bar.
    block_id = pd.Series(
        np.where(breaks.to_numpy(dtype=bool), np.arange(len(df)), np.nan),
        index=df.index,
    ).ffill()
    out["block_id"] = block_id
    out["zone_top"] = zone_top.ffill()
    out["zone_bottom"] = zone_bottom.ffill()
    # State BEFORE bar i within the block's lifetime (shift inside the group).
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


class LuxOrderBlockAI(BaseStrategy):
    strategy_id = "lux_order_block_v1"

    def __init__(
        self,
        swing_length: int = 10,
        stop_atr_pad: float = 0.25,
        take_profit_r: float = 2.0,
        atr_window: int = 14,
        max_block_age: int = 96,
    ) -> None:
        self.swing_length = swing_length
        self.stop_atr_pad = stop_atr_pad
        self.take_profit_r = take_profit_r
        self.atr_window = atr_window
        self.max_block_age = max_block_age
        self.warmup_bars = max(swing_length * 2, atr_window) + 2

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        n = self.swing_length
        swing_high_prev = df["high"].rolling(n).max().shift(1)
        swing_low_prev = df["low"].rolling(n).min().shift(1)
        break_up = df["close"] > swing_high_prev
        break_down = df["close"] < swing_low_prev
        bull = _block_features(df, break_up.fillna(False), bullish=True, swing_length=n)
        bear = _block_features(df, break_down.fillna(False), bullish=False, swing_length=n)
        for col in ("block_id", "zone_top", "zone_bottom", "prior_min_low", "bar_in_block"):
            df[f"bull_{col}"] = bull[col]
        for col in ("block_id", "zone_top", "zone_bottom", "prior_max_high", "bar_in_block"):
            df[f"bear_{col}"] = bear[col]
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

        bull_id = float(row["bull_block_id"]) if not pd.isna(row["bull_block_id"]) else None
        if bull_id is not None and not math.isnan(float(row["bull_zone_top"])):
            top = float(row["bull_zone_top"])
            bottom = float(row["bull_zone_bottom"])
            age = float(row["bull_bar_in_block"])
            prior_min = float(row["bull_prior_min_low"]) if not pd.isna(row["bull_prior_min_low"]) else math.inf
            first_touch = prior_min > top  # no earlier bar reached the zone
            unviolated = prior_min > bottom and low >= bottom
            if (
                0 < age <= self.max_block_age
                and first_touch and unviolated
                and low <= top and close > bottom
            ):
                stop = bottom - self.stop_atr_pad * bar_atr
                risk = close - stop
                if risk > 0:
                    return SignalIntent(
                        side="long", stop_price=stop,
                        take_profit_price=close + self.take_profit_r * risk,
                        reason=f"bullish OB mitigation at {close:.2f}",
                    )
        bear_id = float(row["bear_block_id"]) if not pd.isna(row["bear_block_id"]) else None
        if bear_id is not None and not math.isnan(float(row["bear_zone_top"])):
            top = float(row["bear_zone_top"])
            bottom = float(row["bear_zone_bottom"])
            age = float(row["bear_bar_in_block"])
            prior_max = float(row["bear_prior_max_high"]) if not pd.isna(row["bear_prior_max_high"]) else -math.inf
            first_touch = prior_max < bottom
            unviolated = prior_max < top and high <= top
            if (
                0 < age <= self.max_block_age
                and first_touch and unviolated
                and high >= bottom and close < top
            ):
                stop = top + self.stop_atr_pad * bar_atr
                risk = stop - close
                if risk > 0:
                    return SignalIntent(
                        side="short", stop_price=stop,
                        take_profit_price=close - self.take_profit_r * risk,
                        reason=f"bearish OB mitigation at {close:.2f}",
                    )
        return None
