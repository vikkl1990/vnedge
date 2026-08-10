"""htf_structure_break_v1 — mechanical higher-timeframe structure break.

Frozen pre-registration: research/prereg/htf_structure_break_v1_20260810.md.
Continuation only: a 1H break of the last confirmed swing IN THE DIRECTION of a
4H EMA bias, targeting the nearest confirmed opposing swing beyond entry, but
only when that target is >= 5x modelled round-trip cost. Purely price
structure — no order-flow, no funding, no ML.

Causality: swing pivots are non-repainting (known only ``pivot_len`` bars after
they print, via ``liquidity_pools._pivots``); the 4H bias is shifted to the 4H
*close* time before the backward as-of merge, so a 1H bar never sees a 4H bar
that has not closed yet.
"""
from __future__ import annotations

import bisect

import pandas as pd

from vnedge.strategy.base_strategy import BaseStrategy, SignalIntent
from vnedge.strategy.indicators import atr
from vnedge.strategy.liquidity_pools import _pivots


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


class HtfStructureBreak(BaseStrategy):
    strategy_id = "htf_structure_break_v1"

    def __init__(
        self,
        bias_4h: pd.DataFrame,
        *,
        pivot_len: int = 5,
        ema_fast: int = 20,
        ema_slow: int = 50,
        atr_window: int = 14,
        stop_buf_atr: float = 0.10,
        cost_bps: float = 14.0,
        target_cost_mult: float = 5.0,
    ) -> None:
        if bias_4h is None or bias_4h.empty:
            raise ValueError("htf_structure_break requires a 4H bias frame")
        self.bias_4h = bias_4h
        self.R = pivot_len
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.atr_window = atr_window
        self.stop_buf_atr = stop_buf_atr
        # 5x cost as a price fraction (14 bps -> 70 bps minimum target distance)
        self.min_target_frac = target_cost_mult * cost_bps / 10_000.0
        # 4H EMA50 needs ~50 4H bars = ~200 1H bars, plus pivots + ATR warmup
        self.warmup_bars = max(250, ema_slow * 4 + atr_window + pivot_len + 2)
        self._sh: list[tuple[int, float]] = []
        self._sl: list[tuple[int, float]] = []
        self._sh_conf: list[int] = []
        self._sl_conf: list[int] = []

    def _bias_series(self) -> pd.DataFrame:
        b = self.bias_4h
        ef = _ema(b["close"], self.ema_fast).to_numpy()
        es = _ema(b["close"], self.ema_slow).to_numpy()
        bias = [1 if f > s else (-1 if f < s else 0) for f, s in zip(ef, es)]
        # shift to the 4H CLOSE time so a 1H bar only sees a CLOSED 4H bar
        return pd.DataFrame({
            "timestamp": b["timestamp"] + pd.Timedelta("4h"),
            "bias": bias,
        })

    def prepare(self, candles: pd.DataFrame) -> pd.DataFrame:
        df = candles.copy().reset_index(drop=True)
        bias = self._bias_series()
        # the +4h shift can bump the datetime resolution (ms→us); merge_asof
        # requires identical key dtypes
        bias["timestamp"] = bias["timestamp"].astype(df["timestamp"].dtype)
        merged = pd.merge_asof(df, bias, on="timestamp", direction="backward")
        df["bias"] = merged["bias"].fillna(0).astype(int).to_numpy()
        df["atr"] = atr(df, self.atr_window)
        # non-repainting confirmed pivots: (confirm_bar = pivot+R, level_price)
        self._sh = [(p + self.R, float(df["high"].iat[p])) for p in _pivots(df, "high", self.R, self.R, True)]
        self._sl = [(p + self.R, float(df["low"].iat[p])) for p in _pivots(df, "low", self.R, self.R, False)]
        self._sh_conf = [c for c, _ in self._sh]
        self._sl_conf = [c for c, _ in self._sl]
        return df

    def signal(self, df: pd.DataFrame, index: int) -> SignalIntent | None:
        if index < self.warmup_bars:
            return None
        row = df.iloc[index]
        bias = int(row["bias"])
        a = float(row["atr"])
        close = float(row["close"])
        if bias == 0 or a != a or a <= 0:
            return None
        hi = bisect.bisect_right(self._sh_conf, index)  # swing highs confirmed by now
        lo = bisect.bisect_right(self._sl_conf, index)
        if hi == 0 or lo == 0:
            return None
        buf = self.stop_buf_atr * a

        if bias == 1 and close > self._sh[hi - 1][1]:
            # break of the last confirmed swing high, 4H bias up → long continuation
            above = [self._sh[k][1] for k in range(hi) if self._sh[k][1] > close]
            if not above:
                return None
            target = min(above)  # nearest confirmed swing high ABOVE entry
            if (target - close) / close < self.min_target_frac:
                return None
            stop = self._sh[hi - 1][1] - buf  # beyond the broken swing
            if stop <= 0 or stop >= close:
                return None
            return SignalIntent("long", stop_price=stop, take_profit_price=target,
                                reason="BOS up · 4H bias up")

        if bias == -1 and close < self._sl[lo - 1][1]:
            below = [self._sl[k][1] for k in range(lo) if self._sl[k][1] < close]
            if not below:
                return None
            target = max(below)  # nearest confirmed swing low BELOW entry
            if (close - target) / close < self.min_target_frac:
                return None
            stop = self._sl[lo - 1][1] + buf
            if stop <= close:
                return None
            return SignalIntent("short", stop_price=stop, take_profit_price=target,
                                reason="BOS down · 4H bias down")
        return None
