"""AI-ported candidate: Smart Money Breakout (after AlgoAlpha's Pine v6 script).

Faithful, causal, stateless port of the signal path: a swing high confirmed
``wing`` bars after it forms (pivot high over a 2*wing+1 window) becomes the
structure level; the FIRST close above that level after confirmation is the
bullish break (the script's 'Candle Close' BOS mode). Target and stop are
structure-scaled exactly as the script computes them: one third of the
structure span's high-low range as the unit, TP = level + range/3, SL =
level - range/3 (RR=1). Mirror for lows. The Pine original's on-chart
win-rate table is NOT ported: it fails to reset its TP trackers on stop-outs
and therefore overstates wins; this port lets the gauntlet do the scoring.
Research candidate only — same gauntlet as every strategy.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr

_REQUIRED = ("atr",)


def _swing_features(df: pd.DataFrame, *, wing: int, high_side: bool) -> pd.DataFrame:
    """Level/center/first-break state for confirmed swings, rows <= i only."""
    out = pd.DataFrame(index=df.index)
    span = 2 * wing + 1
    if high_side:
        center_val = df["high"].shift(wing)
        confirmed = center_val.eq(df["high"].rolling(span).max())
    else:
        center_val = df["low"].shift(wing)
        confirmed = center_val.eq(df["low"].rolling(span).min())
    confirmed = confirmed.fillna(False)
    positions = pd.Series(np.arange(len(df), dtype=float), index=df.index)
    swing_id = positions.where(confirmed).ffill()
    out["swing_id"] = swing_id
    out["level"] = center_val.where(confirmed).ffill()
    out["center_pos"] = (positions - wing).where(confirmed).ffill()

    def _prior_cummax(series: pd.Series) -> pd.Series:
        return series.shift(1).cummax()

    def _prior_cummin(series: pd.Series) -> pd.Series:
        return series.shift(1).cummin()

    # First-break detection within the swing's lifetime: the close extreme
    # BEFORE bar i, so bar i is the break only if no earlier bar broke it.
    if high_side:
        out["prior_close_extreme"] = (
            df["close"].groupby(swing_id).apply(_prior_cummax).reset_index(level=0, drop=True)
        )
    else:
        out["prior_close_extreme"] = (
            df["close"].groupby(swing_id).apply(_prior_cummin).reset_index(level=0, drop=True)
        )
    return out


class AlgoalphaSmcBreakoutAI(BaseStrategy):
    strategy_id = "algoalpha_smc_breakout_v1"

    def __init__(
        self,
        wing: int = 25,
        rr: float = 1.0,
        min_dist_atr: float = 0.25,
        atr_window: int = 14,
    ) -> None:
        self.wing = wing
        self.rr = rr
        self.min_dist_atr = min_dist_atr
        self.atr_window = atr_window
        self.warmup_bars = 2 * wing + max(atr_window, 2) + 2

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        hi = _swing_features(df, wing=self.wing, high_side=True)
        lo = _swing_features(df, wing=self.wing, high_side=False)
        for col in ("swing_id", "level", "center_pos", "prior_close_extreme"):
            df[f"sh_{col}"] = hi[col]
            df[f"sl_{col}"] = lo[col]
        df["atr"] = atr(df, self.atr_window)
        return df

    def _structure_dist(
        self, df: pd.DataFrame, index: int, center_pos: float,
        bar_high: float, bar_low: float,
    ) -> float:
        """The script's target unit: (highest-lowest over the structure span)/3.

        The slice covers rows strictly BEFORE ``index``; the current bar's
        extremes arrive as scalars, so no subscript ever reaches past bar i.
        """
        start = max(0, int(center_pos))
        window = df.iloc[start:index]
        top = bar_high if window.empty else max(float(window["high"].max()), bar_high)
        bottom = bar_low if window.empty else min(float(window["low"].min()), bar_low)
        return (top - bottom) / 3.0

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(math.isnan(float(row[col])) for col in _REQUIRED):
            return None
        close = float(row["close"])
        bar_atr = float(row["atr"])
        if bar_atr <= 0:
            return None

        if not pd.isna(row["sh_level"]) and not pd.isna(row["sh_center_pos"]):
            level = float(row["sh_level"])
            prior = (
                float(row["sh_prior_close_extreme"])
                if not pd.isna(row["sh_prior_close_extreme"]) else -math.inf
            )
            if close > level and prior <= level:
                dist = self._structure_dist(df, index, float(row["sh_center_pos"]), float(row["high"]), float(row["low"]))
                if dist >= self.min_dist_atr * bar_atr:
                    stop = level - dist / self.rr
                    if close - stop > 0:
                        return SignalIntent(
                            side="long", stop_price=stop,
                            take_profit_price=level + dist,
                            reason=f"BOS above {level:.2f} at {close:.2f}",
                        )
        if not pd.isna(row["sl_level"]) and not pd.isna(row["sl_center_pos"]):
            level = float(row["sl_level"])
            prior = (
                float(row["sl_prior_close_extreme"])
                if not pd.isna(row["sl_prior_close_extreme"]) else math.inf
            )
            if close < level and prior >= level:
                dist = self._structure_dist(df, index, float(row["sl_center_pos"]), float(row["high"]), float(row["low"]))
                if dist >= self.min_dist_atr * bar_atr:
                    stop = level + dist / self.rr
                    if stop - close > 0:
                        return SignalIntent(
                            side="short", stop_price=stop,
                            take_profit_price=level - dist,
                            reason=f"BOS below {level:.2f} at {close:.2f}",
                        )
        return None
