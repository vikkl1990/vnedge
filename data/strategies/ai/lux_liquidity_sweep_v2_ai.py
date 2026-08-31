"""AI candidate v2: qualified liquidity sweep reversal.

Pre-registered improvement over ai_lux_liquidity_sweep_v1, whose 180d verdict
was REJECT with positive GROSS edge eaten by costs (PF 0.83 net, +379 gross /
-58 net bps over the last 30d). Hypothesis: the raw sweep fires on noise pokes
of fresh rolling extremes; real stop-hunts sweep CONFIRMED, AGED swing pivots
with absorption volume, and revert further when faded WITH the higher trend.
Four qualifications, all causal and stateless:

1. Level = last CONFIRMED swing pivot (left/right bars), not a rolling min.
2. Level age >= min_level_age bars: liquidity needs time to accumulate.
3. Sweep bar volume z-score above threshold: absorption, not drift.
4. Trend filter: fade sell-side sweeps only above the slow SMA (mirror short).
Target = the opposing confirmed pivot (structure-based) bounded to
[min_rr, max_rr] R; skip trades whose structure target pays < min_rr R.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr

_REQUIRED = ("pivot_low_level", "pivot_low_age", "pivot_high_level",
             "pivot_high_age", "vol_z", "sma_slow", "atr")


class LuxLiquiditySweepV2AI(BaseStrategy):
    strategy_id = "lux_liquidity_sweep_v2"

    def __init__(
        self,
        pivot_wing: int = 5,
        min_level_age: int = 12,
        min_penetration_atr: float = 0.10,
        min_vol_z: float = 0.5,
        sma_slow_window: int = 200,
        stop_atr_pad: float = 0.20,
        min_rr: float = 1.2,
        max_rr: float = 4.0,
        vol_window: int = 48,
        atr_window: int = 14,
    ) -> None:
        self.pivot_wing = pivot_wing
        self.min_level_age = min_level_age
        self.min_penetration_atr = min_penetration_atr
        self.min_vol_z = min_vol_z
        self.sma_slow_window = sma_slow_window
        self.stop_atr_pad = stop_atr_pad
        self.min_rr = min_rr
        self.max_rr = max_rr
        self.vol_window = vol_window
        self.atr_window = atr_window
        self.warmup_bars = max(sma_slow_window, vol_window, 2 * pivot_wing + 1) + 2

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        w = self.pivot_wing
        span = 2 * w + 1
        # A confirmed pivot low at bar c needs w bars each side; it becomes
        # KNOWN only at c + w. Rolling windows ending at i identify the center
        # bar i - w as a pivot; shifting nothing forward keeps this causal:
        # at bar i we may use pivots confirmed at or before i.
        center_low = df["low"].shift(w)
        is_pivot_low = center_low.eq(df["low"].rolling(span).min())
        center_high = df["high"].shift(w)
        is_pivot_high = center_high.eq(df["high"].rolling(span).max())

        confirm_bar = pd.Series(np.arange(len(df)), index=df.index, dtype=float)
        df["pivot_low_level"] = center_low.where(is_pivot_low).ffill()
        df["pivot_low_confirmed"] = confirm_bar.where(is_pivot_low.fillna(False)).ffill()
        df["pivot_low_age"] = confirm_bar - df["pivot_low_confirmed"]
        df["pivot_high_level"] = center_high.where(is_pivot_high).ffill()
        df["pivot_high_confirmed"] = confirm_bar.where(is_pivot_high.fillna(False)).ffill()
        df["pivot_high_age"] = confirm_bar - df["pivot_high_confirmed"]

        vol_mean = df["volume"].rolling(self.vol_window).mean()
        vol_std = df["volume"].rolling(self.vol_window).std()
        df["vol_z"] = (df["volume"] - vol_mean) / vol_std.where(vol_std > 0)
        df["sma_slow"] = df["close"].rolling(self.sma_slow_window).mean()
        df["atr"] = atr(df, self.atr_window)
        return df

    def _bounded_target(self, close: float, risk: float, structure: float,
                        *, side: str) -> float | None:
        reward = (structure - close) if side == "long" else (close - structure)
        rr = reward / risk if risk > 0 else 0.0
        if rr < self.min_rr:
            return None
        rr = min(rr, self.max_rr)
        return close + rr * risk if side == "long" else close - rr * risk

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(
            (col not in row) or pd.isna(row[col]) or (
                isinstance(row[col], float) and math.isnan(float(row[col]))
            )
            for col in _REQUIRED
        ):
            return None
        close = float(row["close"])
        low = float(row["low"])
        high = float(row["high"])
        bar_atr = float(row["atr"])
        if bar_atr <= 0:
            return None
        if float(row["vol_z"]) < self.min_vol_z:
            return None
        sma_slow = float(row["sma_slow"])

        level_low = float(row["pivot_low_level"])
        level_high = float(row["pivot_high_level"])
        swept_down = (
            float(row["pivot_low_age"]) >= self.min_level_age
            and low < level_low
            and close > level_low
            and (level_low - low) >= self.min_penetration_atr * bar_atr
            and close > sma_slow
        )
        swept_up = (
            float(row["pivot_high_age"]) >= self.min_level_age
            and high > level_high
            and close < level_high
            and (high - level_high) >= self.min_penetration_atr * bar_atr
            and close < sma_slow
        )

        if swept_down:
            stop = low - self.stop_atr_pad * bar_atr
            risk = close - stop
            target = self._bounded_target(close, risk, level_high, side="long")
            if risk > 0 and target is not None:
                return SignalIntent(
                    side="long", stop_price=stop, take_profit_price=target,
                    reason=f"qualified sell-side sweep reclaimed at {close:.2f}",
                )
        if swept_up:
            stop = high + self.stop_atr_pad * bar_atr
            risk = stop - close
            target = self._bounded_target(close, risk, level_low, side="short")
            if risk > 0 and target is not None:
                return SignalIntent(
                    side="short", stop_price=stop, take_profit_price=target,
                    reason=f"qualified buy-side sweep rejected at {close:.2f}",
                )
        return None
