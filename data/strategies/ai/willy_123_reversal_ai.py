"""AI reconstruction: 1-2-3 reversal with confluence (after Trader Assistant Pro).

The original (WillyAlgoTrader) is invite-only — source unavailable — so this
is a faithful reconstruction of the MECHANISM ITS OWN PAGE SPECIFIES: classic
1-2-3 reversal (point 1 swing extreme, point 2 reaction, point 3 higher-low /
lower-high retest), confirmed-entry on the break of point 2, stop beyond
point 3, measured-move target, and a five-factor confluence gate standing in
for the advertised star rating: trend alignment, retracement quality, leg
size, time symmetry, and break-bar volume. Causal and stateless: pivots are
confirmed ``wing`` bars late, sequences derive from ffilled confirmed events,
first-break state from group-cumulative extremes.

Research candidate only — same gauntlet as every strategy. The vendor's
preset tables are self-described as "in-sample results without fees" from a
50-million-combination grid; this port exists to score the mechanism under
fees, out-of-sample, with deflation-aware trial accounting.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr

_REQUIRED = ("atr", "sma_slow", "vol_ma")


def _prior_cummax(series: pd.Series) -> pd.Series:
    return series.shift(1).cummax()


def _prior_cummin(series: pd.Series) -> pd.Series:
    return series.shift(1).cummin()


def _grouped(values: pd.Series, group_id: pd.Series, fn) -> pd.Series:
    grouped = values.groupby(group_id).apply(fn)
    if isinstance(grouped.index, pd.MultiIndex):
        grouped = grouped.reset_index(level=0, drop=True)
    return grouped.reindex(values.index)


class Willy123ReversalAI(BaseStrategy):
    strategy_id = "willy_123_reversal_v1"

    def __init__(
        self,
        wing: int = 5,
        retrace_min: float = 0.35,
        retrace_max: float = 0.85,
        retrace_sweet_lo: float = 0.5,
        retrace_sweet_hi: float = 0.786,
        min_leg_atr: float = 2.0,
        symmetry_lo: float = 0.3,
        symmetry_hi: float = 1.5,
        min_stars: int = 3,
        stop_atr_pad: float = 0.25,
        sma_slow_window: int = 200,
        vol_window: int = 48,
        atr_window: int = 14,
    ) -> None:
        self.wing = wing
        self.retrace_min = retrace_min
        self.retrace_max = retrace_max
        self.retrace_sweet_lo = retrace_sweet_lo
        self.retrace_sweet_hi = retrace_sweet_hi
        self.min_leg_atr = min_leg_atr
        self.symmetry_lo = symmetry_lo
        self.symmetry_hi = symmetry_hi
        self.min_stars = min_stars
        self.stop_atr_pad = stop_atr_pad
        self.sma_slow_window = sma_slow_window
        self.vol_window = vol_window
        self.atr_window = atr_window
        self.warmup_bars = max(sma_slow_window, vol_window, 2 * wing + 1, atr_window) + 2

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy()
        wing = self.wing
        span = 2 * wing + 1
        positions = pd.Series(np.arange(len(df), dtype=float), index=df.index)

        ph_val = df["high"].shift(wing)
        ph_conf = ph_val.eq(df["high"].rolling(span).max()).fillna(False)
        pl_val = df["low"].shift(wing)
        pl_conf = pl_val.eq(df["low"].rolling(span).min()).fillna(False)

        # Last confirmed pivot high / low, with center positions.
        df["ph"] = ph_val.where(ph_conf).ffill()
        df["ph_pos"] = (positions - wing).where(ph_conf).ffill()
        df["pl"] = pl_val.where(pl_conf).ffill()
        df["pl_pos"] = (positions - wing).where(pl_conf).ffill()
        # The pivot low BEFORE the current one (point 1 for bullish patterns):
        # at each confirmation event, capture the value the ffill held before.
        df["pl_prev"] = df["pl"].shift(1).where(pl_conf).ffill()
        df["pl_prev_pos"] = df["pl_pos"].shift(1).where(pl_conf).ffill()
        df["ph_prev"] = df["ph"].shift(1).where(ph_conf).ffill()
        df["ph_prev_pos"] = df["ph_pos"].shift(1).where(ph_conf).ffill()

        # First-break state: one entry per pattern, keyed by the most recent
        # point-3 confirmation (pivot low id for bullish, pivot high for bearish).
        bull_id = positions.where(pl_conf).ffill()
        bear_id = positions.where(ph_conf).ffill()
        df["bull_prior_close_max"] = _grouped(df["close"], bull_id, _prior_cummax)
        df["bear_prior_close_min"] = _grouped(df["close"], bear_id, _prior_cummin)

        df["sma_slow"] = df["close"].rolling(self.sma_slow_window).mean()
        df["vol_ma"] = df["volume"].rolling(self.vol_window).mean()
        df["atr"] = atr(df, self.atr_window)
        return df

    def _stars(self, *, trend_ok: bool, retrace: float, leg_atr: float,
               symmetry: float, volume_ok: bool) -> int:
        stars = 0
        if trend_ok:
            stars += 1
        if self.retrace_sweet_lo <= retrace <= self.retrace_sweet_hi:
            stars += 1
        if leg_atr >= self.min_leg_atr:
            stars += 1
        if self.symmetry_lo <= symmetry <= self.symmetry_hi:
            stars += 1
        if volume_ok:
            stars += 1
        return stars

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        row = df.iloc[index]
        if any(math.isnan(float(row[col])) for col in _REQUIRED):
            return None
        close = float(row["close"])
        bar_atr = float(row["atr"])
        if bar_atr <= 0:
            return None
        volume_ok = float(row["volume"]) >= float(row["vol_ma"])
        sma_slow = float(row["sma_slow"])

        # ---- bullish 1-2-3: P1 low, P2 high, P3 higher low; break of P2 ----
        needed = ("pl", "pl_pos", "ph", "ph_pos", "pl_prev", "pl_prev_pos")
        if all(not pd.isna(row[c]) for c in needed):
            p3, p3_pos = float(row["pl"]), float(row["pl_pos"])
            p2, p2_pos = float(row["ph"]), float(row["ph_pos"])
            p1, p1_pos = float(row["pl_prev"]), float(row["pl_prev_pos"])
            ordered = p1_pos < p2_pos < p3_pos
            leg = p2 - p1
            if ordered and leg > 0 and p3 > p1:
                retrace = (p2 - p3) / leg
                prior_max = (
                    float(row["bull_prior_close_max"])
                    if not pd.isna(row["bull_prior_close_max"]) else -math.inf
                )
                if (
                    self.retrace_min <= retrace <= self.retrace_max
                    and close > p2 and prior_max <= p2
                ):
                    symmetry = (p3_pos - p2_pos) / max(1.0, p2_pos - p1_pos)
                    stars = self._stars(
                        trend_ok=close > sma_slow, retrace=retrace,
                        leg_atr=leg / bar_atr, symmetry=symmetry,
                        volume_ok=volume_ok,
                    )
                    if stars >= self.min_stars:
                        stop = p3 - self.stop_atr_pad * bar_atr
                        if close - stop > 0:
                            return SignalIntent(
                                side="long", stop_price=stop,
                                take_profit_price=close + leg,  # measured move
                                reason=f"1-2-3 bullish break of {p2:.2f} ({stars}*)",
                            )
        # ---- bearish mirror: P1 high, P2 low, P3 lower high; break of P2 ----
        needed = ("ph", "ph_pos", "pl", "pl_pos", "ph_prev", "ph_prev_pos")
        if all(not pd.isna(row[c]) for c in needed):
            p3, p3_pos = float(row["ph"]), float(row["ph_pos"])
            p2, p2_pos = float(row["pl"]), float(row["pl_pos"])
            p1, p1_pos = float(row["ph_prev"]), float(row["ph_prev_pos"])
            ordered = p1_pos < p2_pos < p3_pos
            leg = p1 - p2
            if ordered and leg > 0 and p3 < p1:
                retrace = (p3 - p2) / leg
                prior_min = (
                    float(row["bear_prior_close_min"])
                    if not pd.isna(row["bear_prior_close_min"]) else math.inf
                )
                if (
                    self.retrace_min <= retrace <= self.retrace_max
                    and close < p2 and prior_min >= p2
                ):
                    symmetry = (p3_pos - p2_pos) / max(1.0, p2_pos - p1_pos)
                    stars = self._stars(
                        trend_ok=close < sma_slow, retrace=retrace,
                        leg_atr=leg / bar_atr, symmetry=symmetry,
                        volume_ok=volume_ok,
                    )
                    if stars >= self.min_stars:
                        stop = p3 + self.stop_atr_pad * bar_atr
                        if stop - close > 0:
                            return SignalIntent(
                                side="short", stop_price=stop,
                                take_profit_price=close - leg,
                                reason=f"1-2-3 bearish break of {p2:.2f} ({stars}*)",
                            )
        return None
